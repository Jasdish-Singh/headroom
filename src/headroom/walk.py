"""Directory walks that do not follow symlinks or Windows reparse points.

Junctions are not symlinks. ``os.scandir(path)`` on Windows can list the
junction target, so directories are opened with ``O_NOFOLLOW`` when the
platform allows it. If an entry cannot be classified, it is not descended.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

FILE_ATTRIBUTE_REPARSE_POINT = 0x400


@dataclass(frozen=True)
class Measure:
    bytes: int = 0
    files: int = 0
    dirs: int = 0
    links: int = 0
    errors: int = 0

    def add(self, other: Measure) -> Measure:
        return Measure(
            self.bytes + other.bytes,
            self.files + other.files,
            self.dirs + other.dirs,
            self.links + other.links,
            self.errors + other.errors,
        )


def classify_from_stat(
    *,
    is_symlink: bool,
    stat_result: object | None,
    stat_error: bool,
) -> str:
    """Return ``link``, ``dir``, ``file``, ``other``, or ``error``.

    A directory with the reparse-point attribute is a junction or mount point
    and is reported as ``link`` so callers do not descend.
    """
    if is_symlink:
        return "link"
    if stat_error or stat_result is None:
        return "error"
    attributes = getattr(stat_result, "st_file_attributes", 0) or 0
    if attributes & FILE_ATTRIBUTE_REPARSE_POINT:
        return "link"
    mode = getattr(stat_result, "st_mode", 0)
    if stat.S_ISDIR(mode):
        return "dir"
    if stat.S_ISREG(mode):
        return "file"
    return "other"


def classify_path(path: Path) -> str:
    try:
        if path.is_symlink():
            return "link"
        stat_result = path.lstat()
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "error"
    return classify_from_stat(is_symlink=False, stat_result=stat_result, stat_error=False)


def list_child_names(path: Path) -> list[str] | None:
    """Return child names of a real directory.

    Returns None when ``path`` is a symlink or reparse point. Never opens the
    target of a link. Raises ``OSError`` when a real directory cannot be read.
    """
    kind = classify_path(path)
    if kind == "link":
        return None
    if kind != "dir":
        raise NotADirectoryError(str(path))
    try:
        return _list_via_fd(path)
    except OSError:
        if classify_path(path) != "dir":
            return None
        # Opening a directory by file descriptor can fail on Windows.
        # Scandir is used only after a fresh "this is not a reparse point" check.
        if os.name == "nt":
            return _list_via_scandir(path)
        raise


def measure_tree(root: Path) -> Measure:
    """Sum regular files under ``root`` without following links.

    The size of a link target is not included. The root directory itself is
    not counted as a file.
    """
    kind = classify_path(root)
    if kind == "missing":
        return Measure()
    if kind == "link":
        return Measure(links=1)
    if kind == "file":
        try:
            return Measure(bytes=root.lstat().st_size, files=1)
        except OSError:
            return Measure(errors=1)
    if kind == "other":
        return Measure(files=1)
    if kind != "dir":
        return Measure(errors=1)

    total = Measure()
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            names = list_child_names(current)
        except OSError:
            total = total.add(Measure(errors=1))
            continue
        if names is None:
            if current != root:
                total = total.add(Measure(links=1))
            else:
                return Measure(links=1)
            continue
        for name in names:
            child = current / name
            child_kind = classify_path(child)
            if child_kind == "link":
                total = total.add(Measure(links=1))
            elif child_kind == "file":
                try:
                    size = child.lstat().st_size
                except OSError:
                    total = total.add(Measure(errors=1))
                else:
                    total = total.add(Measure(bytes=size, files=1))
            elif child_kind == "other":
                total = total.add(Measure(files=1))
            elif child_kind == "dir":
                total = total.add(Measure(dirs=1))
                stack.append(child)
            elif child_kind == "error":
                total = total.add(Measure(errors=1))
    return total


def plan_deletions(root: Path) -> list[tuple[str, Path]]:
    """Post-order delete plan for the contents of ``root``.

    The root itself is never included. Link entries are the link paths, not
    their targets. Kinds are ``file``, ``link``, ``dir``, and ``error``.
    """
    plan: list[tuple[str, Path]] = []
    stack: list[tuple[Path, bool]] = [(root, False)]
    while stack:
        current, visited = stack.pop()
        if visited:
            if current != root:
                plan.append(("dir", current))
            continue
        kind = classify_path(current)
        if current != root and kind in {"file", "other"}:
            plan.append(("file", current))
            continue
        if current != root and kind == "link":
            plan.append(("link", current))
            continue
        if current != root and kind == "error":
            plan.append(("error", current))
            continue
        if current != root and kind == "missing":
            continue
        if kind != "dir":
            if current == root:
                plan.append(("error", current))
            continue
        try:
            names = list_child_names(current)
        except OSError:
            plan.append(("error", current))
            continue
        if names is None:
            if current != root:
                plan.append(("link", current))
            else:
                plan.append(("error", current))
            continue
        stack.append((current, True))
        for name in reversed(names):
            stack.append((current / name, False))
    return plan


def _list_via_fd(path: Path) -> list[str]:
    flags = os.O_RDONLY
    directory_flag = getattr(os, "O_DIRECTORY", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if directory_flag:
        flags |= directory_flag
    if nofollow:
        flags |= nofollow
    directory_fd = os.open(os.fspath(path), flags)
    try:
        names: list[str] = []
        with os.scandir(directory_fd) as iterator:
            for entry in iterator:
                if entry.name not in {".", ".."}:
                    names.append(entry.name)
        return names
    finally:
        os.close(directory_fd)


def _list_via_scandir(path: Path) -> list[str]:
    if classify_path(path) != "dir":
        return []
    names: list[str] = []
    with os.scandir(path) as iterator:
        for entry in iterator:
            if entry.name not in {".", ".."}:
                names.append(entry.name)
    return names
