"""Group old downloads and installers that match an installed program.

Matches are suggestions for review. Nothing here is moved.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from headroom.formatutil import format_bytes
from headroom.walk import classify_path, list_child_names

STALE_DAYS = 30
_INSTALLER_EXT = {".exe", ".msi", ".iso", ".dmg", ".pkg"}
_ARCHIVE_EXT = {".zip", ".7z", ".rar", ".tar", ".gz", ".bz2", ".xz"}
_DOC_EXT = {".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx", ".txt", ".rtf"}
_MEDIA_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".mp4", ".mov", ".mkv", ".mp3", ".wav"}
_STOP = {
    "setup", "installer", "install", "update", "latest", "win64", "win32", "x64", "x86",
    "amd64", "windows", "the", "for", "and", "exe", "msi",
}
_VERSION_RE = re.compile(r"\d+(?:\.\d+)*")


@dataclass
class DownloadItem:
    path: str
    size: int
    accessed: float
    kind: str
    age_days: int
    bucket: str
    installed_match: str | None = None

    @property
    def suggest(self) -> bool:
        return self.installed_match is not None or self.age_days >= STALE_DAYS


def classify_downloads(
    root: Path,
    installed: list[str],
    *,
    now: float,
) -> tuple[list[DownloadItem], int]:
    items: list[DownloadItem] = []
    skipped = 0
    stack = [root]
    if classify_path(root) == "link":
        return [], 1
    if classify_path(root) != "dir":
        return [], 0
    names_index = [(name, _tokens(name)) for name in installed]
    while stack:
        current = stack.pop()
        try:
            children = list_child_names(current)
        except OSError:
            continue
        if children is None:
            skipped += 1
            continue
        for name in children:
            child = current / name
            kind = classify_path(child)
            if kind == "link":
                skipped += 1
                continue
            if kind == "dir":
                stack.append(child)
                continue
            if kind != "file":
                continue
            item = _item(child, names_index, now=now)
            if item is not None:
                items.append(item)
    items.sort(key=lambda item: (-item.size, item.path))
    return items, skipped


def installed_program_names(reader=None) -> list[str]:
    if reader is not None:
        return [str(name) for name in reader() if str(name).strip()]
    if os.name != "nt":
        return []
    return _windows_uninstall_names()


def format_downloads(items: list[DownloadItem], *, skipped: int) -> str:
    lines = [
        "Headroom downloads",
        "Dry run. Nothing will be moved.",
        "Installers that match an installed program, and downloads last opened 30 days ago or earlier,",
        "are suggestions. Confirm them in review with MOVE.",
        "",
    ]
    suggested = [item for item in items if item.suggest]
    if not suggested:
        lines.append("Nothing in this folder is old enough or already installed.")
    else:
        groups: dict[tuple[str, str], list[DownloadItem]] = {}
        for item in suggested:
            groups.setdefault((item.kind, item.bucket), []).append(item)
        for (kind, bucket), members in sorted(groups.items()):
            total = sum(item.size for item in members)
            lines.append(f"{kind}  {bucket}  {len(members)} files  {format_bytes(total)}")
            for item in members[:12]:
                match = f"  matches {item.installed_match}" if item.installed_match else ""
                lines.append(f"    {item.age_days:4d}d  {item.path}{match}")
            hidden = len(members) - 12
            if hidden > 0:
                lines.append(f"    ... {hidden} more")
            lines.append("")
    if skipped:
        lines.append(f"Skipped links: {skipped}.")
    lines.append("Run headroom downloads --apply to move the suggestions. Nothing moves until you type MOVE.")
    return "\n".join(lines) + "\n"


def default_downloads(home: Path | None = None) -> Path:
    home_dir = Path.home() if home is None else home
    return home_dir / "Downloads"


def _item(path: Path, installed: list[tuple[str, set[str]]], *, now: float) -> DownloadItem | None:
    try:
        stat_result = path.lstat()
    except OSError:
        return None
    accessed = float(stat_result.st_atime or stat_result.st_mtime or now)
    age_days = max(0, int((now - accessed) / 86400))
    kind = _kind(path.suffix)
    match = _match_installed(path.name, installed) if kind == "installer" else None
    return DownloadItem(
        path=str(path),
        size=int(stat_result.st_size),
        accessed=accessed,
        kind=kind,
        age_days=age_days,
        bucket=_bucket(age_days),
        installed_match=match,
    )


def _kind(suffix: str) -> str:
    ext = suffix.casefold()
    if ext in _INSTALLER_EXT:
        return "installer"
    if ext in _ARCHIVE_EXT:
        return "archive"
    if ext in _DOC_EXT:
        return "document"
    if ext in _MEDIA_EXT:
        return "media"
    return "other"


def _bucket(age_days: int) -> str:
    if age_days >= 180:
        return "180d+"
    if age_days >= 90:
        return "90d+"
    if age_days >= 30:
        return "30d+"
    return "recent"


def _tokens(text: str) -> set[str]:
    """Split names on punctuation, camel case, and glued words like ChromeSetup."""
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    spaced = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", spaced)
    cleaned = _VERSION_RE.sub(" ", spaced.casefold())
    found: set[str] = set()
    for piece in re.split(r"[^a-z0-9]+", cleaned):
        if not piece:
            continue
        for stop in sorted(_STOP, key=len, reverse=True):
            if piece.endswith(stop) and len(piece) > len(stop):
                piece = piece[: -len(stop)]
                break
        if len(piece) >= 3 and piece not in _STOP:
            found.add(piece)
    return found


def _match_installed(filename: str, installed: list[tuple[str, set[str]]]) -> str | None:
    tokens = _tokens(Path(filename).stem)
    if not tokens:
        return None
    for name, program_tokens in installed:
        if not program_tokens:
            continue
        if tokens & program_tokens:
            return name
    return None


def _windows_uninstall_names() -> list[str]:
    try:
        import winreg
    except ImportError:
        return []
    names: list[str] = []
    roots = (
        (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
    )
    for hive, key_path in roots:
        try:
            with winreg.OpenKey(hive, key_path) as key:
                count = winreg.QueryInfoKey(key)[0]
                for index in range(count):
                    try:
                        sub_name = winreg.EnumKey(key, index)
                        with winreg.OpenKey(key, sub_name) as sub:
                            display, _typ = winreg.QueryValueEx(sub, "DisplayName")
                    except OSError:
                        continue
                    if display and str(display) not in names:
                        names.append(str(display))
        except OSError:
            continue
    return names
