"""Offer a read-only analysis the first time someone uses an interactive command.

``headroom watch`` does not ask. A login task would otherwise stop on a prompt.
The offer is recorded so it is asked once.
"""

from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path

from headroom.analyze import (
    default_roots,
    format_analysis,
    save_analysis,
    scan,
)
from headroom.review import format_review, group_suggestions, suggestions_for
from headroom.rules import (
    PROFILE_NAMES,
    RuleError,
    load_merged_rules,
    state_dir,
    write_active_profiles,
)

_OFFER_COMMANDS = {"status", "top", "scan", "clean", "history", "changes", "report", "self"}


def maybe_offer_first_run(
    command: str,
    *,
    isatty=None,
    input_fn=None,
    state: Path | None = None,
    home: Path | None = None,
    now: float | None = None,
    today: date | None = None,
) -> None:
    if command not in _OFFER_COMMANDS:
        return
    tty = (lambda: sys.stdin.isatty() and sys.stdout.isatty()) if isatty is None else isatty
    if not tty():
        return
    directory = state_dir() if state is None else state
    marker = directory / "first-run.offered"
    if marker.exists():
        return
    read = input if input_fn is None else input_fn
    print(
        "First run. headroom analyze only reads files. It does not delete or move anything.",
        flush=True,
    )
    try:
        answer = read("Run a read-only analysis now? [y/N] ")
    except EOFError:
        answer = ""
    directory.mkdir(parents=True, exist_ok=True)
    marker.write_text("offered\n", encoding="utf-8")
    if answer.strip().casefold() not in {"y", "yes"}:
        print("Skipped. You can run headroom analyze later.", flush=True)
        return
    print("Starter profiles: " + ", ".join(PROFILE_NAMES) + ".", flush=True)
    try:
        chosen = read("Use which profiles? (comma-separated, or none) ")
    except EOFError:
        chosen = ""
    try:
        names = parse_profile_list(chosen)
    except RuleError as exc:
        print(f"headroom: {exc}", file=sys.stderr)
        names = []
    write_active_profiles(names, directory)
    moment = datetime.now().timestamp() if now is None else now
    clock = date.today() if today is None else today
    roots = default_roots(Path.home() if home is None else home)
    analysis = scan(roots, now=moment, skip_under=directory)
    save_analysis(analysis, directory)
    sys.stdout.write(format_analysis(analysis))
    rules = load_merged_rules(directory)
    groups = group_suggestions(suggestions_for(analysis, rules, now=moment, today=clock))
    sys.stdout.write(format_review(groups, archive=directory / "archive"))


def parse_profile_list(text: str) -> list[str]:
    raw = text.strip()
    if not raw or raw.casefold() == "none":
        return []
    names: list[str] = []
    for part in raw.replace(",", " ").split():
        key = part.casefold()
        if key not in PROFILE_NAMES:
            known = ", ".join(PROFILE_NAMES)
            raise RuleError(f"unknown profile {part!r}. Known profiles: {known}")
        if key not in names:
            names.append(key)
    return names
