"""Entry-point plugins for scanners, profiles, and report sections.

Groups:
  headroom.scanners  callable(root) -> list of {path, size, note}
  headroom.profiles  callable() -> rule text. The entry point name is the profile name.
  headroom.reports   callable(context) -> text appended to a report
  headroom.skins     callable() -> {name, sheet} for a Roomy sprite sheet

A plugin that fails to load is skipped. Built-in profile names are never replaced.
Built-in pet skins (roomy, midnight, tower) are never replaced.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from importlib.metadata import entry_points
from pathlib import Path
from typing import Callable

SCANNERS = "headroom.scanners"
PROFILES = "headroom.profiles"
REPORTS = "headroom.reports"
SKINS = "headroom.skins"


@dataclass(frozen=True)
class Plugin:
    name: str
    group: str
    fn: Callable


def iter_plugins(group: str, entries=None) -> list[Plugin]:
    found: list[Plugin] = []
    for entry in _entries(group, entries):
        name = str(getattr(entry, "name", "") or "").strip()
        if not name:
            continue
        try:
            fn = entry.load() if hasattr(entry, "load") else entry
        except Exception:
            continue
        if not callable(fn):
            continue
        found.append(Plugin(name=name, group=group, fn=fn))
    return found


def profile_plugins(entries=None) -> list[Plugin]:
    builtin = {"student", "developer", "gamer"}
    plugins = []
    for plugin in iter_plugins(PROFILES, entries):
        if plugin.name.casefold() in builtin:
            continue
        plugins.append(plugin)
    return plugins


def profile_text(name: str, entries=None) -> str | None:
    key = name.strip().casefold()
    for plugin in profile_plugins(entries):
        if plugin.name.casefold() != key:
            continue
        try:
            text = plugin.fn()
        except Exception:
            return None
        if isinstance(text, str) and text.strip():
            return text
    return None


def scanner_records(roots: list[Path], *, now: float, entries=None) -> list:
    from headroom.analyze import FileRecord, importance

    records: list[FileRecord] = []
    seen: set[str] = set()
    for plugin in iter_plugins(SCANNERS, entries):
        for root in roots:
            try:
                hits = plugin.fn(root)
            except Exception:
                continue
            if not isinstance(hits, list):
                continue
            for hit in hits:
                if not isinstance(hit, dict) or "path" not in hit:
                    continue
                path = str(hit["path"])
                if path in seen:
                    continue
                seen.add(path)
                size = int(hit.get("size") or 0)
                records.append(
                    FileRecord(
                        path=path,
                        size=max(0, size),
                        accessed=float(hit.get("accessed") or now),
                        category="old",
                        importance=importance("old", float(hit.get("accessed") or now), now),
                        is_dir=bool(hit.get("is_dir", False)),
                        base_category="old",
                    )
                )
    return records


def report_sections(context: dict[str, object], entries=None) -> str:
    parts: list[str] = []
    for plugin in iter_plugins(REPORTS, entries):
        try:
            text = plugin.fn(context)
        except Exception:
            continue
        if isinstance(text, str) and text.strip():
            parts.append(text.strip())
    if not parts:
        return ""
    return "\n\n".join(parts) + "\n"


def _entries(group: str, entries):
    if entries is not None:
        return list(entries)
    try:
        discovered = entry_points()
    except Exception:
        return []
    select = getattr(discovered, "select", None)
    if callable(select):
        return list(select(group=group))
    grouped = discovered.get(group, []) if hasattr(discovered, "get") else []
    return list(grouped)


def safe_walk_files(root: Path):
    """Yield files under ``root`` without following links. Shared by the example plugin."""
    if not root.exists() or root.is_symlink():
        return
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        kept = []
        for name in dirnames:
            child = Path(dirpath) / name
            if not child.is_symlink():
                kept.append(name)
        dirnames[:] = kept
        for name in filenames:
            path = Path(dirpath) / name
            if path.is_symlink():
                continue
            yield path
