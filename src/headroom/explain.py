"""Plain-English reasons a review item looks unimportant.

Ollama is optional and local. A URL that is not loopback is refused before any
connection is opened. When the local model is down, a rule-based sentence is used.
File contents are never sent, even to localhost.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

from headroom.formatutil import format_bytes

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"


class ExplainError(Exception):
    """The explanation request is not allowed."""


def assert_loopback(url: str) -> str:
    """Return the URL when it points at this machine. Otherwise raise."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold().strip("[]")
    if parsed.scheme not in {"http", "https"} or host not in LOOPBACK_HOSTS:
        raise ExplainError(f"refusing to contact a non-local model at {url}")
    return url


def explain_record(record, rule_text: str, *, now: float | None = None) -> str:
    """One or two sentences from the rule, age, and category. No network."""
    moment = time.time() if now is None else now
    age_days = max(0, int((moment - float(record.accessed)) / 86400))
    name = str(record.path).replace("\\", "/").rstrip("/").split("/")[-1]
    text = rule_text.casefold()
    path = str(record.path).casefold()
    opened = "today" if age_days <= 0 else f"{age_days} days ago"
    if "semester: past" in text or "semester" in text and "past" in text:
        return (
            f"{name} sits in a folder whose semester label is already over. "
            "Course shells and current-semester work are not on this list."
        )
    if "node_modules" in path or "__pycache__" in path or ".next" in path or record.category == "caches":
        return (
            f"{name} is a cache or dependency folder ({format_bytes(record.size)}). "
            "The tool that created it can rebuild it."
        )
    if record.category == "duplicates" or "duplicate" in text:
        return (
            f"{name} matches another file byte for byte. "
            "The copy in the better folder, or the newer one, is the one to keep."
        )
    if "already" in text and "install" in text:
        return (
            f"{name} looks like a setup file for a program that is already installed. "
            f"It was last opened {opened}."
        )
    if record.category == "installers" or any(token in text for token in (".iso", ".msi", "setup", "installer")):
        return (
            f"{name} is an installer, not the installed program. "
            f"It was last opened {opened} and takes {format_bytes(record.size)}."
        )
    if "older-than" in text or age_days >= 30:
        return (
            f"{name} was last opened {opened}, so the matching toss rule treats it as idle. "
            f"It is {format_bytes(record.size)} of {record.category}."
        )
    return (
        f"{name} matched a toss rule ({rule_text}). "
        f"It is {format_bytes(record.size)} and was last opened {opened}."
    )


class OllamaClient:
    """Talk only to a loopback Ollama. ``opener`` is for tests."""

    def __init__(
        self,
        base_url: str = DEFAULT_OLLAMA_URL,
        *,
        timeout: float = 1.5,
        opener=None,
    ) -> None:
        self.base_url = assert_loopback(base_url.rstrip("/"))
        self.timeout = timeout
        self.opener = opener or _loopback_open
        self._up: bool | None = None
        self._model: str | None = None

    def available(self) -> bool:
        if self._up is not None:
            return self._up
        try:
            payload = self._get("/api/tags")
        except (OSError, ExplainError, ValueError, json.JSONDecodeError):
            self._up = False
            return False
        models = payload.get("models") if isinstance(payload, dict) else None
        if not isinstance(models, list) or not models:
            self._up = False
            return False
        first = models[0]
        name = first.get("name") if isinstance(first, dict) else None
        if not name:
            self._up = False
            return False
        self._model = str(name)
        self._up = True
        return True

    def explain(self, record, rule_text: str, *, now: float | None = None) -> str | None:
        if not self.available() or not self._model:
            return None
        moment = time.time() if now is None else now
        age_days = max(0, int((moment - float(record.accessed)) / 86400))
        prompt = (
            "In two short sentences, say why this file might be unimportant to keep. "
            "Do not tell the user to permanently delete it. "
            f"Name: {record.path}. Size bytes: {record.size}. "
            f"Category: {record.category}. Last opened days ago: {age_days}. "
            f"Rule: {rule_text}."
        )
        try:
            payload = self._post(
                "/api/generate",
                {"model": self._model, "prompt": prompt, "stream": False},
            )
        except (OSError, ExplainError, ValueError, json.JSONDecodeError):
            return None
        text = payload.get("response") if isinstance(payload, dict) else None
        if not isinstance(text, str) or not text.strip():
            return None
        return " ".join(text.split())

    def _get(self, path: str) -> object:
        url = assert_loopback(self.base_url + path)
        request = urllib.request.Request(url, method="GET")
        with self.opener(request, timeout=self.timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    def _post(self, path: str, body: dict[str, object]) -> object:
        url = assert_loopback(self.base_url + path)
        data = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with self.opener(request, timeout=self.timeout) as response:
            return json.loads(response.read().decode("utf-8"))


class _RejectRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        assert_loopback(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _loopback_open(request, timeout):  # type: ignore[no-untyped-def]
    assert_loopback(request.full_url)
    opener = urllib.request.build_opener(_RejectRedirect)
    try:
        return opener.open(request, timeout=timeout)
    except urllib.error.URLError as exc:
        raise OSError(str(exc)) from exc


def explain_items(items, *, now: float | None = None, client: OllamaClient | None = None) -> list[str]:
    """Prefer the local model when it answers. Otherwise use the rule text."""
    local = client
    use_model = False
    if local is not None:
        try:
            use_model = local.available()
        except ExplainError:
            use_model = False
    reasons: list[str] = []
    for item in items:
        record = item.record
        rule_text = item.rule_text
        text = None
        if use_model and local is not None:
            try:
                text = local.explain(record, rule_text, now=now)
            except ExplainError:
                text = None
        if not text:
            text = explain_record(record, rule_text, now=now)
        reasons.append(text)
    return reasons
