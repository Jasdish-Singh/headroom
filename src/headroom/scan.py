"""Largest immediate children of a folder. Links are not followed."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from headroom.safety import SafetyError
from headroom.walk import classify_path, list_child_names, measure_tree


@dataclass(frozen=True)
class ScanEntry:
    name: str
    size: int
    kind: str


@dataclass(frozen=True)
class ScanResult:
    root: Path
    entries: tuple[ScanEntry, ...]
    total_bytes: int
    skipped_links: int
    errors: int
    omitted: int


def scan_path(path: Path, top: int) -> ScanResult:
    if top < 1:
        raise ValueError("top must be at least 1")
    if not path.exists():
        raise FileNotFoundError(path)
    if classify_path(path) == "link":
        raise SafetyError(f"refusing to scan through a link: {path}")
    if classify_path(path) == "file":
        size = path.lstat().st_size
        entry = ScanEntry(path.name, size, "file")
        return ScanResult(path, (entry,), size, 0, 0, 0)
    if classify_path(path) != "dir":
        raise NotADirectoryError(path)

    names = list_child_names(path)
    if names is None:
        raise SafetyError(f"refusing to scan through a link: {path}")

    rows: list[ScanEntry] = []
    total = 0
    links = 0
    errors = 0
    for name in names:
        child = path / name
        kind = classify_path(child)
        if kind == "link":
            links += 1
            continue
        if kind in {"file", "other"}:
            try:
                size = child.lstat().st_size if kind == "file" else 0
            except OSError:
                errors += 1
                continue
            total += size
            rows.append(ScanEntry(name, size, "file"))
        elif kind == "dir":
            measured = measure_tree(child)
            total += measured.bytes
            links += measured.links
            errors += measured.errors
            rows.append(ScanEntry(name, measured.bytes, "dir"))
        elif kind == "error":
            errors += 1
    rows.sort(key=lambda row: (-row.size, row.name.lower()))
    omitted = max(0, len(rows) - top)
    return ScanResult(
        root=path,
        entries=tuple(rows[:top]),
        total_bytes=total,
        skipped_links=links,
        errors=errors,
        omitted=omitted,
    )
