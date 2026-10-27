"""Path rules for cleanup.

Config can turn built-in targets off. It cannot add a path. Every delete root
is checked again, lexically, before anything is removed. Symlinks and Windows
reparse points (junctions) are never used as delete roots and are never followed.
"""

from __future__ import annotations

import re
from pathlib import PurePath

KNOWN_CLEANUP_IDS: tuple[str, ...] = (
    "user_temp",
    "windows_temp",
    "chrome_cache",
    "edge_cache",
    "firefox_cache",
    "thumbnail_cache",
    "recycle_bin",
)

PROTECTED_COMPONENTS: frozenset[str] = frozenset(
    {
        "documents",
        "desktop",
        "downloads",
        "pictures",
        "videos",
        "music",
        "favorites",
        "contacts",
        "links",
        "saved games",
        "searches",
        "3d objects",
        "documents and settings",
    }
)

# Shared system temps. %TEMP% on Windows is per-user; these are not.
SYSTEM_TEMP_PATHS: frozenset[str] = frozenset(
    {"/tmp", "/var/tmp", "/private/tmp", "/dev/shm"}
)

BROWSER_CACHE_LEAVES: frozenset[str] = frozenset({"cache", "code cache", "gpucache"})
CHROME_PREFIX: tuple[str, ...] = ("google", "chrome", "user data")
EDGE_PREFIX: tuple[str, ...] = ("microsoft", "edge", "user data")
FIREFOX_PREFIX: tuple[str, ...] = ("mozilla", "firefox", "profiles")
THUMBNAIL_PREFIX: tuple[str, ...] = ("microsoft", "windows", "explorer")
THUMBNAIL_NAME = re.compile(r"^thumbcache_.*\.db$", re.IGNORECASE)
RECYCLE_ROOT = re.compile(r"^[A-Za-z]:[\\/]\$Recycle\.Bin$")


class SafetyError(Exception):
    """A path or target must not be deleted."""


def ensure_known_cleanup_id(target_id: str) -> None:
    if target_id not in KNOWN_CLEANUP_IDS:
        known = ", ".join(KNOWN_CLEANUP_IDS)
        raise SafetyError(
            f"unknown cleanup target {target_id!r}. "
            "Headroom only cleans its built-in allowlist and cannot take paths from config. "
            f"Known targets: {known}"
        )


def protected_component(parts: tuple[str, ...] | list[str]) -> str | None:
    for part in parts:
        if _is_protected(part):
            return part
    return None


def _is_protected(part: str) -> bool:
    lowered = part.lower()
    if lowered in PROTECTED_COMPONENTS:
        return True
    # "OneDrive - Contoso" and similar library folders.
    return lowered.startswith("onedrive")


def validate_user_temp(path: PurePath) -> None:
    """Accept a per-user temp directory and reject document folders and roots.

    The last component must be ``Temp`` or ``tmp``. A drive root, a profile
    directory, and any path that contains Documents, Desktop, Downloads, or
    the other protected names is rejected. ``/tmp`` is rejected because it is
    shared, not a Windows per-user temp.
    """
    parts = path.parts
    if len(parts) < 2:
        raise SafetyError(f"temp path is too broad: {path}")
    hit = protected_component(parts)
    if hit is not None:
        raise SafetyError(f"temp path includes protected folder '{hit}': {path}")
    if parts[-1].lower() not in {"temp", "tmp"}:
        raise SafetyError(f"user temp must end with Temp or tmp: {path}")
    normalized = path.as_posix().rstrip("/").lower()
    if normalized in SYSTEM_TEMP_PATHS:
        raise SafetyError(f"refusing to clean the shared system temp directory: {path}")


def validate_windows_temp(path: PurePath, system_root: PurePath) -> None:
    """Accept only ``%SystemRoot%\\Temp``."""
    if system_root.name.lower() != "windows":
        raise SafetyError(
            f"SystemRoot must be the Windows directory, got {system_root}"
        )
    hit = protected_component(path.parts) or protected_component(system_root.parts)
    if hit is not None:
        raise SafetyError(f"Windows temp path includes protected folder '{hit}': {path}")
    expected = (*system_root.parts, "Temp")
    actual = path.parts
    if len(actual) != len(expected) or any(
        left.lower() != right.lower() for left, right in zip(actual, expected)
    ):
        raise SafetyError(f"Windows temp must be exactly %SystemRoot%\\Temp, got {path}")


def validate_chrome_cache(path: PurePath, local_appdata: PurePath) -> None:
    _validate_browser_cache(path, local_appdata, CHROME_PREFIX, BROWSER_CACHE_LEAVES)


def validate_edge_cache(path: PurePath, local_appdata: PurePath) -> None:
    _validate_browser_cache(path, local_appdata, EDGE_PREFIX, BROWSER_CACHE_LEAVES)


def validate_firefox_cache(path: PurePath, local_appdata: PurePath) -> None:
    _validate_browser_cache(path, local_appdata, FIREFOX_PREFIX, frozenset({"cache2"}))


def validate_thumbnail(path: PurePath, local_appdata: PurePath) -> None:
    relative = _relative_parts(path, local_appdata)
    if len(relative) != 4 or tuple(part.lower() for part in relative[:3]) != THUMBNAIL_PREFIX:
        raise SafetyError(
            "thumbnail cache must be a file in Microsoft\\Windows\\Explorer: "
            f"{path}"
        )
    if not THUMBNAIL_NAME.match(relative[-1]):
        raise SafetyError(f"refusing non-thumbnail file: {path}")


def validate_recycle_root(path: PurePath) -> None:
    text = str(path)
    if not RECYCLE_ROOT.fullmatch(text) or path.name.lower() != "$recycle.bin":
        raise SafetyError(f"not a Recycle Bin root: {path}")
    if len(path.parts) != 2:
        raise SafetyError(f"not a Recycle Bin root: {path}")


def assert_contained(root: PurePath, child: PurePath) -> None:
    """Require ``child`` to be strictly inside ``root`` by path parts.

    This does not resolve symlinks. Callers must not follow links and then
    ask this function about the target.
    """
    if any(part == ".." for part in child.parts):
        raise SafetyError(f"path contains ..: {child}")
    root_parts = tuple(part.lower() for part in root.parts)
    child_parts = tuple(part.lower() for part in child.parts)
    if child_parts == root_parts:
        raise SafetyError(f"refusing to remove the cleanup root itself: {root}")
    if len(child_parts) <= len(root_parts) or child_parts[: len(root_parts)] != root_parts:
        raise SafetyError(f"path is outside the cleanup root: {child}")


def assert_not_link(path: PurePath) -> None:
    """Refuse cleanup roots that are symlinks or reparse points.

    ``path`` must be a concrete ``pathlib.Path`` on the machine being cleaned.
    The annotation is ``PurePath`` so lexical tests can share the module, but
    link checks need a real filesystem.
    """
    from pathlib import Path

    from headroom.walk import classify_path

    if not isinstance(path, Path):
        raise SafetyError(f"cannot check links for a non-filesystem path: {path}")
    kind = classify_path(path)
    if kind == "link":
        raise SafetyError(f"refusing to clean through a link: {path}")
    if kind == "error":
        raise SafetyError(f"cannot inspect path safely: {path}")


def _validate_browser_cache(
    path: PurePath,
    local_appdata: PurePath,
    prefix: tuple[str, ...],
    leaves: frozenset[str],
) -> None:
    relative = _relative_parts(path, local_appdata)
    lowered = tuple(part.lower() for part in relative)
    if len(relative) != len(prefix) + 2 or lowered[: len(prefix)] != prefix:
        raise SafetyError(f"browser cache is outside the expected profile cache: {path}")
    profile = relative[len(prefix)]
    if profile in {".", ".."} or _is_protected(profile):
        raise SafetyError(f"refusing browser profile path: {path}")
    if lowered[-1] not in leaves:
        raise SafetyError(f"refusing to delete outside cache directories: {path}")


def _relative_parts(path: PurePath, parent: PurePath) -> tuple[str, ...]:
    parent_parts = tuple(part.lower() for part in parent.parts)
    path_parts = tuple(part.lower() for part in path.parts)
    if len(path_parts) <= len(parent_parts) or path_parts[: len(parent_parts)] != parent_parts:
        raise SafetyError(f"path is not under the expected parent: {path}")
    return path.parts[len(parent.parts) :]
