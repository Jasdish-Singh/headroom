"""Duplicate finder: size, then a partial hash, then a full hash.

The keeper is the copy in the better folder, or the newer copy when the folders
tie. The other copies are suggestions. This module does not move files.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from headroom.walk import classify_path, list_child_names

PARTIAL_BYTES = 64 * 1024


@dataclass
class DupeFile:
    path: str
    size: int
    accessed: float


@dataclass
class DupeGroup:
    digest: str
    size: int
    keeper: DupeFile
    copies: tuple[DupeFile, ...]

    @property
    def extras(self) -> tuple[DupeFile, ...]:
        return tuple(item for item in self.copies if item.path != self.keeper.path)


def find_duplicates(roots: list[Path]) -> tuple[list[DupeGroup], int]:
    """Return duplicate groups and how many links were skipped."""
    files: list[DupeFile] = []
    skipped = 0
    for root in roots:
        found, links = _collect(root)
        files.extend(found)
        skipped += links
    by_size: dict[int, list[DupeFile]] = {}
    for item in files:
        if item.size <= 0:
            continue
        by_size.setdefault(item.size, []).append(item)
    groups: list[DupeGroup] = []
    for size, members in by_size.items():
        if len(members) < 2:
            continue
        partials: dict[str, list[DupeFile]] = {}
        for item in members:
            digest = _hash_file(Path(item.path), PARTIAL_BYTES)
            if digest is None:
                continue
            partials.setdefault(digest, []).append(item)
        for subset in partials.values():
            if len(subset) < 2:
                continue
            full: dict[str, list[DupeFile]] = {}
            for item in subset:
                digest = _hash_file(Path(item.path), None)
                if digest is None:
                    continue
                full.setdefault(digest, []).append(item)
            for digest, copies in full.items():
                if len(copies) < 2:
                    continue
                keeper = pick_keeper(copies)
                groups.append(DupeGroup(digest=digest, size=size, keeper=keeper, copies=tuple(copies)))
    groups.sort(key=lambda group: (-group.size * len(group.extras), group.keeper.path))
    return groups, skipped


def pick_keeper(copies: list[DupeFile]) -> DupeFile:
    """Prefer the better folder. When those tie, keep the newest copy."""
    return sorted(
        copies,
        key=lambda item: (-location_score(item.path), -item.accessed, item.path.casefold()),
    )[0]


def location_score(path: str) -> int:
    """Score the nearest labeled folder. A cache inside Documents still loses."""
    parts = [part.casefold() for part in path.replace("\\", "/").split("/") if part]
    cache = {"node_modules", "__pycache__", "temp", "tmp", "cache", ".cache"}
    weak = cache | {"downloads"}
    strong = {"documents", "desktop", "pictures", "saved games", "music", "videos"}
    for part in reversed(parts):
        if part in cache:
            return 0
        if part in weak:
            return 20
        if part in strong:
            return 80
    return 40


def format_dupes(groups: list[DupeGroup], *, skipped: int) -> str:
    lines = [
        "Headroom duplicates",
        "Dry run. Nothing will be moved.",
        "Each group keeps one copy. The others can go through review after you type MOVE.",
        "",
    ]
    if not groups:
        lines.append("No duplicate files.")
    for index, group in enumerate(groups, start=1):
        lines.append(
            f"[{index}] {group.size} bytes x {len(group.copies)}  keep {group.keeper.path}"
        )
        for item in group.copies:
            mark = "keep" if item.path == group.keeper.path else "toss"
            lines.append(f"    {mark:4}  {item.path}")
        lines.append("")
    if skipped:
        lines.append(f"Skipped links: {skipped}. Their targets were not hashed.")
    lines.append("Run headroom dupes --apply to move the extra copies. Nothing moves until you type MOVE.")
    return "\n".join(lines) + "\n"


def _collect(root: Path) -> tuple[list[DupeFile], int]:
    files: list[DupeFile] = []
    skipped = 0
    kind = classify_path(root)
    if kind == "link":
        return [], 1
    if kind == "file":
        item = _file(root)
        return ([item] if item is not None else []), 0
    if kind != "dir":
        return [], 0
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            names = list_child_names(current)
        except OSError:
            continue
        if names is None:
            skipped += 1
            continue
        for name in names:
            child = current / name
            child_kind = classify_path(child)
            if child_kind == "link":
                skipped += 1
                continue
            if child_kind == "dir":
                stack.append(child)
                continue
            if child_kind == "file":
                item = _file(child)
                if item is not None:
                    files.append(item)
    return files, skipped


def _file(path: Path) -> DupeFile | None:
    try:
        stat_result = path.lstat()
    except OSError:
        return None
    return DupeFile(str(path), int(stat_result.st_size), float(stat_result.st_atime or stat_result.st_mtime or 0))


def _hash_file(path: Path, limit: int | None) -> str | None:
    flags = os.O_RDONLY
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if nofollow:
        flags |= nofollow
    fd = None
    try:
        fd = os.open(path, flags)
        hasher = hashlib.sha256()
        remaining = limit
        while True:
            chunk_size = 65536 if remaining is None else min(65536, remaining)
            if chunk_size <= 0:
                break
            chunk = os.read(fd, chunk_size)
            if not chunk:
                break
            hasher.update(chunk)
            if remaining is not None:
                remaining -= len(chunk)
        return hasher.hexdigest()
    except OSError:
        return None
    finally:
        if fd is not None:
            os.close(fd)
