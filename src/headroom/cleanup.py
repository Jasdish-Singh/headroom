"""Known-safe cleanup.

The only targets are user temp, Windows temp, Chrome/Edge/Firefox caches,
Explorer thumbnail caches, and the Recycle Bin. Discovery drops anything that
fails the safety checks. ``apply`` checks every path again before the first
delete, removes nothing outside those roots, and does not follow links.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from headroom.config import ConfigError
from headroom.safety import (
    KNOWN_CLEANUP_IDS,
    SafetyError,
    THUMBNAIL_NAME,
    assert_contained,
    assert_not_link,
    ensure_known_cleanup_id,
    validate_chrome_cache,
    validate_edge_cache,
    validate_firefox_cache,
    validate_recycle_root,
    validate_thumbnail,
    validate_user_temp,
    validate_windows_temp,
)
from headroom.walk import classify_path, list_child_names, measure_tree, plan_deletions

DESCRIPTIONS: dict[str, str] = {
    "user_temp": "User temporary files (%TEMP%)",
    "windows_temp": "Windows temporary files (%SystemRoot%\\Temp)",
    "chrome_cache": "Chrome cache (Cache, Code Cache, GPUCache)",
    "edge_cache": "Edge cache (Cache, Code Cache, GPUCache)",
    "firefox_cache": "Firefox cache (profile cache2 folders)",
    "thumbnail_cache": "Explorer thumbnail cache (thumbcache_*.db)",
    "recycle_bin": "Recycle Bin",
}

_BROWSER_LEAVES = ("Cache", "Code Cache", "GPUCache")


@dataclass(frozen=True)
class DiscoverContext:
    env: Mapping[str, str]
    home: Path

    def local_appdata(self) -> Path | None:
        raw = self.env.get("LOCALAPPDATA")
        if raw:
            return Path(raw)
        if os.name == "nt":
            return self.home / "AppData" / "Local"
        return None

    def system_root(self) -> Path:
        raw = self.env.get("SystemRoot") or self.env.get("SYSTEMROOT")
        if raw:
            return Path(raw)
        if os.name == "nt":
            return Path(r"C:\Windows")
        return Path("C:/Windows")


@dataclass(frozen=True)
class CleanupGroup:
    id: str
    description: str
    kind: str
    paths: tuple[Path, ...] = ()
    blocked: bool = False
    note: str = ""


@dataclass(frozen=True)
class PathMeasure:
    path: Path
    bytes: int
    files: int
    links: int
    errors: int


@dataclass(frozen=True)
class GroupPreview:
    id: str
    description: str
    blocked: bool
    note: str
    paths: tuple[PathMeasure, ...]
    total_bytes: int
    files: int
    links: int
    errors: int
    supported: bool = True


@dataclass(frozen=True)
class Preview:
    groups: tuple[GroupPreview, ...]

    @property
    def total_bytes(self) -> int:
        return sum(group.total_bytes for group in self.groups if not group.blocked)

    @property
    def total_files(self) -> int:
        return sum(group.files for group in self.groups if not group.blocked)

    @property
    def total_links(self) -> int:
        return sum(group.links for group in self.groups if not group.blocked)

    @property
    def has_work(self) -> bool:
        return any(
            (not group.blocked) and group.supported and (group.files > 0 or group.links > 0)
            for group in self.groups
        )


@dataclass
class DeleteStats:
    removed_files: int = 0
    removed_dirs: int = 0
    removed_links: int = 0
    freed_bytes: int = 0
    skipped: int = 0
    recycle_emptied: bool = False


@dataclass(frozen=True)
class RecycleMeasure:
    bytes: int
    files: int
    supported: bool
    note: str


class UnavailableRecycleBin:
    def __init__(self, note: str) -> None:
        self.note = note

    def measure(self) -> RecycleMeasure:
        return RecycleMeasure(0, 0, False, self.note)

    def empty(self) -> None:
        raise SafetyError(self.note)


class WindowsRecycleBin:
    def measure(self) -> RecycleMeasure:
        roots = windows_recycle_roots()
        if not roots:
            return RecycleMeasure(0, 0, True, "not present")
        total_bytes = 0
        total_files = 0
        errors = 0
        for root in roots:
            try:
                validate_recycle_root(root)
                assert_not_link(root)
            except SafetyError:
                continue
            measured = measure_tree(root)
            total_bytes += measured.bytes
            total_files += measured.files
            errors += measured.errors
        note = f"{errors} unreadable entries" if errors else ""
        return RecycleMeasure(total_bytes, total_files, True, note)

    def empty(self) -> None:
        empty_windows_recycle_bin()


def default_recycle_backend() -> UnavailableRecycleBin | WindowsRecycleBin:
    if os.name != "nt":
        return UnavailableRecycleBin("Recycle Bin is available on Windows only")
    return WindowsRecycleBin()


def discover_groups(
    enabled: tuple[str, ...] | list[str],
    env: Mapping[str, str],
    home: Path | None = None,
) -> tuple[list[CleanupGroup], DiscoverContext]:
    if len(set(enabled)) != len(tuple(enabled)):
        raise ConfigError("cleanup targets are listed more than once")
    unknown = [item for item in enabled if item not in KNOWN_CLEANUP_IDS]
    if unknown:
        known = ", ".join(KNOWN_CLEANUP_IDS)
        raise ConfigError(
            "unknown cleanup target "
            + ", ".join(unknown)
            + ". Config can disable built-in targets, not add paths. "
            f"Known targets: {known}"
        )
    context = DiscoverContext(env=env, home=home or Path.home())
    finders = {
        "user_temp": _discover_user_temp,
        "windows_temp": _discover_windows_temp,
        "chrome_cache": _discover_chrome,
        "edge_cache": _discover_edge,
        "firefox_cache": _discover_firefox,
        "thumbnail_cache": _discover_thumbnails,
        "recycle_bin": _discover_recycle,
    }
    groups = [finders[target_id](context) for target_id in enabled]
    return groups, context


def build_preview(groups: list[CleanupGroup], recycle_bin: object, context: DiscoverContext) -> Preview:
    rows: list[GroupPreview] = []
    for group in groups:
        try:
            ensure_known_cleanup_id(group.id)
        except SafetyError as exc:
            rows.append(_blocked(group, str(exc)))
            continue
        if group.blocked:
            rows.append(_blocked(group, group.note))
            continue
        if group.kind == "recycle":
            rows.append(_preview_recycle(group, recycle_bin))
            continue
        safe_paths: list[Path] = []
        notes: list[str] = []
        for path in group.paths:
            try:
                validate_group_path(group.id, path, context)
            except SafetyError as exc:
                notes.append(str(exc))
                continue
            safe_paths.append(path)
        if notes and not safe_paths:
            rows.append(_blocked(group, "; ".join(notes)))
            continue
        measured = tuple(_measure_path(group.kind, path) for path in safe_paths)
        note = group.note
        if notes:
            extra = "; ".join(notes)
            note = f"{note}; {extra}" if note else extra
        if not measured and not note:
            note = "not present"
        rows.append(
            GroupPreview(
                id=group.id,
                description=group.description,
                blocked=False,
                note=note,
                paths=measured,
                total_bytes=sum(item.bytes for item in measured),
                files=sum(item.files for item in measured),
                links=sum(item.links for item in measured),
                errors=sum(item.errors for item in measured),
                supported=True,
            )
        )
    return Preview(groups=tuple(rows))


def apply(
    groups: list[CleanupGroup],
    *,
    logger: logging.Logger,
    recycle_bin: object,
    context: DiscoverContext,
) -> DeleteStats:
    """Delete allowlisted contents. Raises ``SafetyError`` before any delete if a path is illegal."""
    preflight(groups, context)
    stats = DeleteStats()
    for group in groups:
        if group.blocked:
            logger.warning("skipped target %s: %s", group.id, group.note)
            continue
        if group.kind == "recycle":
            _empty_recycle(recycle_bin, stats, logger)
            continue
        if group.kind == "files":
            for path in group.paths:
                _delete_file_target(group.id, path, stats, logger)
            continue
        if group.kind == "directories":
            for path in group.paths:
                _delete_directory_target(group.id, path, stats, logger)
            continue
        raise SafetyError(f"unknown cleanup kind {group.kind!r}")
    logger.info(
        "cleanup-summary removed_files=%s removed_links=%s freed_bytes=%s skipped=%s recycle_emptied=%s",
        stats.removed_files,
        stats.removed_links,
        stats.freed_bytes,
        stats.skipped,
        stats.recycle_emptied,
    )
    return stats


def preflight(groups: list[CleanupGroup], context: DiscoverContext) -> None:
    for group in groups:
        ensure_known_cleanup_id(group.id)
        if group.blocked:
            continue
        if group.kind == "recycle":
            continue
        if group.kind not in {"directories", "files"}:
            raise SafetyError(f"unknown cleanup kind {group.kind!r}")
        for path in group.paths:
            validate_group_path(group.id, path, context)
            kind = classify_path(path)
            if group.kind == "directories" and kind not in {"dir", "missing"}:
                raise SafetyError(f"{path} is not a directory")
            if group.kind == "files" and kind not in {"file", "missing"}:
                raise SafetyError(f"{path} is not a thumbnail cache file")


def validate_group_path(group_id: str, path: Path, context: DiscoverContext) -> None:
    ensure_known_cleanup_id(group_id)
    if group_id == "user_temp":
        validate_user_temp(path)
        if classify_path(path) != "missing":
            assert_not_link(path)
        return
    if group_id == "windows_temp":
        validate_windows_temp(path, context.system_root())
        if classify_path(path) != "missing":
            assert_not_link(path)
        return
    local = context.local_appdata()
    if local is None:
        raise SafetyError("LOCALAPPDATA is not set")
    if classify_path(path) != "missing":
        assert_not_link(path)
    if group_id == "chrome_cache":
        validate_chrome_cache(path, local)
        return
    if group_id == "edge_cache":
        validate_edge_cache(path, local)
        return
    if group_id == "firefox_cache":
        validate_firefox_cache(path, local)
        return
    if group_id == "thumbnail_cache":
        validate_thumbnail(path, local)
        return
    raise SafetyError(f"target {group_id} does not delete filesystem paths")


def unlink_file(path: Path) -> None:
    path.unlink()


def remove_empty_dir(path: Path) -> None:
    path.rmdir()


def remove_link(path: Path) -> None:
    """Remove a symlink or junction, never the target's contents."""
    try:
        path.unlink()
        return
    except IsADirectoryError:
        pass
    except OSError:
        if classify_path(path) != "link":
            raise
    if classify_path(path) != "link":
        raise SafetyError(f"refusing to rmdir a path that is no longer a link: {path}")
    os.rmdir(path)


def windows_recycle_roots() -> list[Path]:
    import psutil

    roots: list[Path] = []
    try:
        partitions = psutil.disk_partitions(all=False)
    except (OSError, SystemError):
        return roots
    for part in partitions:
        mount = part.mountpoint
        if len(mount) < 2 or mount[1] != ":":
            continue
        candidate = Path(mount[:2] + "\\$Recycle.Bin")
        try:
            validate_recycle_root(candidate)
        except SafetyError:
            continue
        if candidate.is_dir() and classify_path(candidate) == "dir":
            roots.append(candidate)
    return roots


def empty_windows_recycle_bin() -> None:
    if os.name != "nt":
        raise SafetyError("Recycle Bin can only be emptied on Windows")
    import ctypes

    shell32 = ctypes.windll.shell32
    shell32.SHEmptyRecycleBinW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint]
    shell32.SHEmptyRecycleBinW.restype = ctypes.c_long
    no_confirmation = 0x00000001
    no_progress = 0x00000002
    no_sound = 0x00000004
    result = shell32.SHEmptyRecycleBinW(None, None, no_confirmation | no_progress | no_sound)
    if result < 0:
        raise OSError(f"SHEmptyRecycleBinW failed with HRESULT {result:#x}")


def _discover_user_temp(context: DiscoverContext) -> CleanupGroup:
    raw = context.env.get("TEMP") or context.env.get("TMP")
    if not raw:
        return _group("user_temp", "directories", note="TEMP is not set")
    path = Path(raw)
    try:
        validate_user_temp(path)
    except SafetyError as exc:
        return _group("user_temp", "directories", blocked=True, note=str(exc))
    return _existing_dir_group("user_temp", path)


def _discover_windows_temp(context: DiscoverContext) -> CleanupGroup:
    system_root = context.system_root()
    path = system_root / "Temp"
    try:
        validate_windows_temp(path, system_root)
    except SafetyError as exc:
        return _group("windows_temp", "directories", blocked=True, note=str(exc))
    return _existing_dir_group("windows_temp", path)


def _discover_chrome(context: DiscoverContext) -> CleanupGroup:
    return _discover_browser(
        context,
        "chrome_cache",
        ("Google", "Chrome", "User Data"),
        _BROWSER_LEAVES,
        validate_chrome_cache,
    )


def _discover_edge(context: DiscoverContext) -> CleanupGroup:
    return _discover_browser(
        context,
        "edge_cache",
        ("Microsoft", "Edge", "User Data"),
        _BROWSER_LEAVES,
        validate_edge_cache,
    )


def _discover_firefox(context: DiscoverContext) -> CleanupGroup:
    return _discover_browser(
        context,
        "firefox_cache",
        ("Mozilla", "Firefox", "Profiles"),
        ("cache2",),
        validate_firefox_cache,
    )


def _discover_browser(
    context: DiscoverContext,
    target_id: str,
    relative: tuple[str, ...],
    leaves: tuple[str, ...],
    validate,
) -> CleanupGroup:
    local = context.local_appdata()
    if local is None:
        return _group(target_id, "directories", note="LOCALAPPDATA is not set")
    if not local.exists():
        return _group(target_id, "directories", note="not present")
    base = local.joinpath(*relative)
    if not base.exists():
        return _group(target_id, "directories", note="not present")
    if classify_path(base) == "link":
        return _group(
            target_id,
            "directories",
            blocked=True,
            note=f"skipped because {base} is a link",
        )
    try:
        profiles = list_child_names(base)
    except OSError as exc:
        return _group(target_id, "directories", note=f"cannot read profiles: {exc}")
    if profiles is None:
        return _group(target_id, "directories", blocked=True, note=f"skipped because {base} is a link")
    found: list[Path] = []
    problems: list[str] = []
    for profile in profiles:
        if profile in {".", ".."}:
            continue
        for leaf in leaves:
            candidate = base / profile / leaf
            if not candidate.exists():
                continue
            try:
                validate(candidate, local)
                assert_not_link(candidate)
            except SafetyError as exc:
                problems.append(str(exc))
                continue
            if classify_path(candidate) != "dir":
                continue
            found.append(candidate)
    if problems and not found:
        return _group(target_id, "directories", blocked=True, note="; ".join(problems))
    if not found:
        return _group(target_id, "directories", note="not present")
    return _group(target_id, "directories", paths=tuple(found), note="; ".join(problems))


def _discover_thumbnails(context: DiscoverContext) -> CleanupGroup:
    local = context.local_appdata()
    if local is None:
        return _group("thumbnail_cache", "files", note="LOCALAPPDATA is not set")
    explorer = local / "Microsoft" / "Windows" / "Explorer"
    if not explorer.exists():
        return _group("thumbnail_cache", "files", note="not present")
    if classify_path(explorer) != "dir":
        return _group(
            "thumbnail_cache",
            "files",
            blocked=True,
            note=f"skipped because {explorer} is not a real directory",
        )
    try:
        names = list_child_names(explorer)
    except OSError as exc:
        return _group("thumbnail_cache", "files", note=f"cannot read thumbnail cache: {exc}")
    if names is None:
        return _group(
            "thumbnail_cache",
            "files",
            blocked=True,
            note=f"skipped because {explorer} is a link",
        )
    found: list[Path] = []
    problems: list[str] = []
    for name in names:
        if not THUMBNAIL_NAME.match(name):
            continue
        candidate = explorer / name
        try:
            validate_thumbnail(candidate, local)
            assert_not_link(candidate)
        except SafetyError as exc:
            problems.append(str(exc))
            continue
        if classify_path(candidate) != "file":
            continue
        found.append(candidate)
    if problems and not found:
        return _group("thumbnail_cache", "files", blocked=True, note="; ".join(problems))
    if not found:
        return _group("thumbnail_cache", "files", note="not present")
    return _group("thumbnail_cache", "files", paths=tuple(found), note="; ".join(problems))


def _discover_recycle(context: DiscoverContext) -> CleanupGroup:
    del context
    return _group("recycle_bin", "recycle")


def _existing_dir_group(target_id: str, path: Path) -> CleanupGroup:
    if not path.exists():
        return _group(target_id, "directories", note="not present")
    try:
        assert_not_link(path)
    except SafetyError as exc:
        return _group(target_id, "directories", blocked=True, note=str(exc))
    if classify_path(path) != "dir":
        return _group(target_id, "directories", blocked=True, note=f"{path} is not a directory")
    return _group(target_id, "directories", paths=(path,))


def _group(
    target_id: str,
    kind: str,
    *,
    paths: tuple[Path, ...] = (),
    blocked: bool = False,
    note: str = "",
) -> CleanupGroup:
    return CleanupGroup(
        id=target_id,
        description=DESCRIPTIONS[target_id],
        kind=kind,
        paths=paths,
        blocked=blocked,
        note=note,
    )


def _blocked(group: CleanupGroup, note: str) -> GroupPreview:
    return GroupPreview(
        id=group.id,
        description=group.description,
        blocked=True,
        note=note,
        paths=(),
        total_bytes=0,
        files=0,
        links=0,
        errors=0,
        supported=False,
    )


def _preview_recycle(group: CleanupGroup, recycle_bin: object) -> GroupPreview:
    measure = recycle_bin.measure()  # type: ignore[attr-defined]
    return GroupPreview(
        id=group.id,
        description=group.description,
        blocked=False,
        note=measure.note,
        paths=(),
        total_bytes=measure.bytes if measure.supported else 0,
        files=measure.files if measure.supported else 0,
        links=0,
        errors=0,
        supported=bool(measure.supported),
    )


def _measure_path(kind: str, path: Path) -> PathMeasure:
    if kind == "files":
        current = classify_path(path)
        if current == "link":
            return PathMeasure(path, 0, 0, 1, 0)
        if current == "file":
            try:
                return PathMeasure(path, path.lstat().st_size, 1, 0, 0)
            except OSError:
                return PathMeasure(path, 0, 0, 0, 1)
        if current == "missing":
            return PathMeasure(path, 0, 0, 0, 0)
        return PathMeasure(path, 0, 0, 0, 1)
    if classify_path(path) == "missing":
        return PathMeasure(path, 0, 0, 0, 0)
    measured = measure_tree(path)
    return PathMeasure(path, measured.bytes, measured.files, measured.links, measured.errors)


def _delete_directory_target(
    group_id: str,
    root: Path,
    stats: DeleteStats,
    logger: logging.Logger,
) -> None:
    if classify_path(root) == "missing":
        return
    for kind, path in plan_deletions(root):
        if path == root:
            stats.skipped += 1
            logger.warning("skipped target=%s path=%s error=cannot read directory", group_id, path)
            continue
        _delete_planned(group_id, root, kind, path, stats, logger)


def _delete_planned(
    group_id: str,
    root: Path,
    kind: str,
    path: Path,
    stats: DeleteStats,
    logger: logging.Logger,
) -> None:
    del kind  # Reclassify at delete time so a swapped-in link is not followed.
    assert_contained(root, path)
    current = classify_path(path)
    try:
        if current == "missing":
            return
        if current == "link":
            remove_link(path)
            stats.removed_links += 1
            logger.info("removed-link target=%s path=%s", group_id, path)
            return
        if current == "dir":
            try:
                remove_empty_dir(path)
            except OSError:
                logger.debug("left directory in place: %s", path)
                return
            stats.removed_dirs += 1
            logger.info("removed-dir target=%s path=%s", group_id, path)
            return
        if current in {"file", "other"}:
            size = _file_size(path)
            unlink_file(path)
            stats.removed_files += 1
            stats.freed_bytes += size
            logger.info("removed target=%s bytes=%s path=%s", group_id, size, path)
            return
        stats.skipped += 1
        logger.warning("skipped target=%s path=%s error=cannot inspect", group_id, path)
    except FileNotFoundError:
        return
    except SafetyError:
        raise
    except OSError as exc:
        stats.skipped += 1
        logger.warning("skipped target=%s path=%s error=%s", group_id, path, exc)


def _delete_file_target(
    group_id: str,
    path: Path,
    stats: DeleteStats,
    logger: logging.Logger,
) -> None:
    current = classify_path(path)
    if current == "missing":
        return
    if current == "link":
        raise SafetyError(f"refusing to clean through a link: {path}")
    if current != "file":
        stats.skipped += 1
        logger.warning("skipped target=%s path=%s error=not a file", group_id, path)
        return
    try:
        size = _file_size(path)
        unlink_file(path)
    except FileNotFoundError:
        return
    except OSError as exc:
        stats.skipped += 1
        logger.warning("skipped target=%s path=%s error=%s", group_id, path, exc)
        return
    stats.removed_files += 1
    stats.freed_bytes += size
    logger.info("removed target=%s bytes=%s path=%s", group_id, size, path)


def _empty_recycle(recycle_bin: object, stats: DeleteStats, logger: logging.Logger) -> None:
    measure = recycle_bin.measure()  # type: ignore[attr-defined]
    if not measure.supported:
        logger.info("recycle bin skipped: %s", measure.note)
        return
    if measure.files == 0 and measure.bytes == 0:
        logger.info("recycle bin already empty")
        return
    try:
        recycle_bin.empty()  # type: ignore[attr-defined]
    except SafetyError:
        raise
    except OSError as exc:
        stats.skipped += 1
        logger.warning("skipped target=recycle_bin error=%s", exc)
        return
    stats.recycle_emptied = True
    stats.freed_bytes += measure.bytes
    stats.removed_files += measure.files
    logger.info(
        "removed target=recycle_bin bytes=%s files=%s path=Recycle Bin",
        measure.bytes,
        measure.files,
    )


def _file_size(path: Path) -> int:
    try:
        return max(0, int(path.lstat().st_size))
    except OSError:
        return 0
