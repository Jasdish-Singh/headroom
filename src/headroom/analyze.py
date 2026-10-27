"""Read-only categorization of files the user might care about.

The scan never deletes, moves, or writes inside the folders it reads. It stores
its report under Headroom's own state directory.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath

from headroom.formatutil import format_bytes
from headroom.rules import CATEGORIES, state_dir
from headroom.walk import classify_path, hash_file, list_child_names, measure_tree

STALE_DAYS = 180
RECENT_DAYS = 14
MAX_RECORDS = 8000
_HASH_LIMIT = 8 * 1024 * 1024

CATEGORY_LABELS = {
    "school": "School and documents",
    "code": "Code projects",
    "games": "Games",
    "media": "Media",
    "downloads": "Downloads",
    "installers": "Installers",
    "duplicates": "Duplicates",
    "caches": "Caches",
    "old": "Old or unopened",
}

_IMPORTANCE = {
    "school": 80,
    "code": 85,
    "games": 75,
    "media": 55,
    "downloads": 35,
    "installers": 20,
    "duplicates": 15,
    "caches": 10,
    "old": 25,
}
_CACHE_DIRS = {
    "node_modules",
    "__pycache__",
    ".next",
    "bower_components",
    "shadercache",
    "gpucache",
    "dxcache",
    "appcache",
    "htmlcache",
}
_GAME_PARTS = {
    "steamapps",
    "epic games",
    "gog games",
    "gog galaxy",
    "riot games",
    "battlenet",
}
_PROJECT_FILES = {
    ".git",
    "package.json",
    "pyproject.toml",
    "cargo.toml",
    "go.mod",
    "pom.xml",
    "composer.json",
    "setup.py",
}
_CODE_EXT = {
    ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".rs", ".go",
    ".java", ".c", ".h", ".hpp", ".cpp", ".cs", ".rb", ".php", ".swift", ".kt",
    ".scala",
}
_MEDIA_EXT = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".mp4", ".mov", ".mkv",
    ".avi", ".mp3", ".wav", ".flac", ".m4a",
}
_DOC_EXT = {
    ".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx", ".txt", ".rtf",
    ".odt", ".odp", ".ods",
}
_INSTALLER_EXT = {".msi", ".iso", ".dmg", ".pkg"}
_SCHOOL_HINTS = (
    "syllabus",
    "homework",
    "assignment",
    "lecture",
    "essay",
    "course",
    "semester",
)
_FOLDER_NAMES = (
    "Documents",
    "Desktop",
    "Downloads",
    "Pictures",
    "Videos",
    "Music",
    "Saved Games",
    "Games",
    "source",
    "projects",
    "dev",
    "code",
)


@dataclass
class FileRecord:
    path: str
    size: int
    accessed: float
    category: str
    importance: int
    is_dir: bool = False
    stale: bool = False
    duplicate_of: str | None = None
    base_category: str = ""

    def to_json(self) -> dict[str, object]:
        return {
            "path": self.path,
            "size": self.size,
            "accessed": self.accessed,
            "category": self.category,
            "importance": self.importance,
            "is_dir": self.is_dir,
            "stale": self.stale,
            "duplicate_of": self.duplicate_of,
            "base_category": self.base_category or self.category,
        }

    @classmethod
    def from_json(cls, data: dict[str, object]) -> FileRecord:
        base = str(data.get("base_category") or data["category"])
        return cls(
            path=str(data["path"]),
            size=int(data["size"]),
            accessed=float(data["accessed"]),
            category=str(data["category"]),
            importance=int(data["importance"]),
            is_dir=bool(data.get("is_dir", False)),
            stale=bool(data.get("stale", False)),
            duplicate_of=None if data.get("duplicate_of") is None else str(data["duplicate_of"]),
            base_category=base,
        )


@dataclass
class CategoryTotal:
    count: int = 0
    bytes: int = 0


@dataclass
class Analysis:
    scanned_at: float
    roots: tuple[str, ...]
    totals: dict[str, CategoryTotal] = field(default_factory=dict)
    records: tuple[FileRecord, ...] = ()
    errors: int = 0
    skipped_links: int = 0
    capped: bool = False
    stale_count: int = 0
    stale_bytes: int = 0

    def to_json(self) -> dict[str, object]:
        return {
            "scanned_at": self.scanned_at,
            "roots": list(self.roots),
            "totals": {
                key: {"count": total.count, "bytes": total.bytes}
                for key, total in self.totals.items()
            },
            "records": [record.to_json() for record in self.records],
            "errors": self.errors,
            "skipped_links": self.skipped_links,
            "capped": self.capped,
            "stale_count": self.stale_count,
            "stale_bytes": self.stale_bytes,
        }

    @classmethod
    def from_json(cls, data: dict[str, object]) -> Analysis:
        totals: dict[str, CategoryTotal] = {}
        raw_totals = data.get("totals") or {}
        if isinstance(raw_totals, dict):
            for key, value in raw_totals.items():
                if isinstance(value, dict):
                    totals[str(key)] = CategoryTotal(
                        count=int(value.get("count", 0)),
                        bytes=int(value.get("bytes", 0)),
                    )
        raw_records = data.get("records") or []
        records = tuple(
            FileRecord.from_json(item)
            for item in raw_records
            if isinstance(item, dict)
        )
        roots = data.get("roots") or []
        return cls(
            scanned_at=float(data.get("scanned_at", 0)),
            roots=tuple(str(item) for item in roots) if isinstance(roots, list) else (),
            totals=totals,
            records=records,
            errors=int(data.get("errors", 0)),
            skipped_links=int(data.get("skipped_links", 0)),
            capped=bool(data.get("capped", False)),
            stale_count=int(data.get("stale_count", 0)),
            stale_bytes=int(data.get("stale_bytes", 0)),
        )


def importance(category: str, accessed: float, now: float) -> int:
    """0 to 100. Recent files score higher. Old files score lower."""
    score = _IMPORTANCE.get(category, _IMPORTANCE["old"])
    age_days = (now - accessed) / 86400
    if age_days <= RECENT_DAYS:
        score += 10
    elif age_days >= STALE_DAYS:
        score -= 15
    return max(0, min(100, score))


def classify_entry(path: str, *, is_dir: bool, inside_project: bool) -> str:
    """Pick one category id from the path. The caller marks duplicates later."""
    posix = path.replace("\\", "/")
    parts = [part.casefold() for part in PurePosixPath(posix).parts]
    name = parts[-1] if parts else ""
    ext = "" if is_dir else PurePosixPath(name).suffix.casefold()
    joined = "/".join(parts)
    if any(part in _CACHE_DIRS for part in parts):
        return "caches"
    if "saved games" in joined or any(part in _GAME_PARTS for part in parts):
        return "games"
    if ext in _INSTALLER_EXT or (
        ext == ".exe" and any(token in name for token in ("setup", "install"))
    ):
        return "installers"
    if inside_project or ext in _CODE_EXT:
        return "code"
    if ext in _MEDIA_EXT:
        return "media"
    if any(hint in joined for hint in _SCHOOL_HINTS):
        return "school"
    if "downloads" in parts:
        return "downloads"
    if ext in _DOC_EXT or any(part in {"documents", "desktop"} for part in parts):
        return "school"
    return "old"


def default_roots(home: Path | None = None, env: dict[str, str] | None = None) -> list[Path]:
    """User folders that usually hold personal files, plus a few known caches."""
    home_dir = Path.home() if home is None else home
    environ = os.environ if env is None else env
    roots = [home_dir / name for name in _FOLDER_NAMES if (home_dir / name).is_dir()]
    local = environ.get("LOCALAPPDATA")
    if local:
        base = Path(local)
        for relative in (
            "NVIDIA/DXCache",
            "D3DSCache",
            "Steam/appcache",
            "Steam/htmlcache",
            "NVIDIA Corporation/NV_Cache",
        ):
            candidate = base / relative
            if candidate.is_dir():
                roots.append(candidate)
    dot_cache = home_dir / ".cache"
    if dot_cache.is_dir():
        roots.append(dot_cache)
    if not roots:
        roots.append(home_dir)
    return roots


def analysis_path(directory: Path | None = None) -> Path:
    return (state_dir() if directory is None else directory) / "analysis.json"


def save_analysis(analysis: Analysis, directory: Path | None = None) -> Path:
    path = analysis_path(directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(analysis.to_json()), encoding="utf-8")
    return path


def load_analysis(directory: Path | None = None) -> Analysis | None:
    path = analysis_path(directory)
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return None
    return Analysis.from_json(data)


def scan(
    roots: list[Path],
    *,
    now: float,
    skip_under: Path | None = None,
    max_records: int = MAX_RECORDS,
) -> Analysis:
    """Walk ``roots`` without following links. The tree is not modified."""
    totals = {key: CategoryTotal() for key in CATEGORIES}
    seen: list[FileRecord] = []
    errors = 0
    skipped_links = 0
    stale_count = 0
    stale_bytes = 0
    skip = skip_under
    for root in roots:
        kind = classify_path(root)
        if kind == "link":
            skipped_links += 1
            continue
        if kind == "file":
            _note_file(root, inside_project=False, now=now, totals=totals, seen=seen)
            continue
        if kind != "dir":
            errors += 1
            continue
        stack: list[tuple[Path, bool]] = [(root, False)]
        while stack:
            current, inherited = stack.pop()
            if skip is not None and _is_under(current, skip):
                continue
            try:
                names = list_child_names(current)
            except OSError:
                errors += 1
                continue
            if names is None:
                skipped_links += 1
                continue
            project = inherited or any(_project_marker(name) for name in names)
            for name in names:
                child = current / name
                if skip is not None and _is_under(child, skip):
                    continue
                child_kind = classify_path(child)
                if child_kind == "link":
                    skipped_links += 1
                    continue
                if child_kind == "dir" and name.casefold() in _CACHE_DIRS:
                    added = _note_bundle(child, now=now, totals=totals, seen=seen)
                    if not added:
                        errors += 1
                    continue
                if child_kind == "dir":
                    stack.append((child, project))
                    continue
                if child_kind == "file":
                    if not _note_file(child, inside_project=project, now=now, totals=totals, seen=seen):
                        errors += 1
                    continue
                if child_kind == "error":
                    errors += 1
    for record in seen:
        if record.stale:
            stale_count += 1
            stale_bytes += record.size
    capped = len(seen) > max_records
    if capped:
        seen.sort(key=lambda item: (-item.size, item.path))
        seen = seen[:max_records]
    _mark_duplicates(seen, now)
    seen.sort(key=lambda item: (-item.size, item.path))
    return Analysis(
        scanned_at=now,
        roots=tuple(str(root) for root in roots),
        totals=totals,
        records=tuple(seen),
        errors=errors,
        skipped_links=skipped_links,
        capped=capped,
        stale_count=stale_count,
        stale_bytes=stale_bytes,
    )


def format_analysis(analysis: Analysis) -> str:
    when = datetime.fromtimestamp(analysis.scanned_at).strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        f"Headroom analysis  {when}",
        "Read-only. Nothing was moved or deleted.",
        "",
        f"{'CATEGORY':<24} {'FILES':>8} {'SIZE':>12}",
    ]
    for key in CATEGORIES:
        total = analysis.totals.get(key) or CategoryTotal()
        if total.count == 0:
            continue
        lines.append(
            f"{CATEGORY_LABELS[key]:<24} {total.count:8d} {format_bytes(total.bytes):>12}"
        )
    if not any(total.count for total in analysis.totals.values()):
        lines.append("No files were read.")
    lines.append("")
    lines.append(
        f"Old or unopened (last opened about {STALE_DAYS} days ago or more): "
        f"{analysis.stale_count} files, {format_bytes(analysis.stale_bytes)}."
    )
    if analysis.skipped_links:
        lines.append(f"Skipped links: {analysis.skipped_links}. Their targets were not read.")
    if analysis.errors:
        lines.append(f"Unreadable entries: {analysis.errors}.")
    if analysis.capped:
        lines.append(
            f"The review list keeps the largest {len(analysis.records)} items. Totals include the rest."
        )
    notable = [record for record in analysis.records if record.duplicate_of or record.importance >= 70]
    if notable:
        lines.append("")
        lines.append("Higher-importance items and duplicate copies:")
        for record in notable[:12]:
            extra = ""
            if record.duplicate_of:
                extra = f"  duplicate of {record.duplicate_of}"
            lines.append(
                f"  {format_bytes(record.size):>10}  {record.importance:3d}  "
                f"{CATEGORY_LABELS.get(record.category, record.category):<22}  {record.path}{extra}"
            )
    lines.append("")
    lines.append("Suggestions stay suggestions until you review them.")
    return "\n".join(lines) + "\n"


def _note_file(path: Path, *, inside_project: bool, now: float, totals: dict, seen: list) -> bool:
    try:
        stat_result = path.lstat()
        size = int(stat_result.st_size)
        accessed = float(stat_result.st_atime or stat_result.st_mtime or now)
    except OSError:
        return False
    _append(str(path), size, accessed, str(path), inside_project, False, now, totals, seen)
    return True


def _note_bundle(path: Path, *, now: float, totals: dict, seen: list) -> bool:
    measured = measure_tree(path)
    try:
        stat_result = path.lstat()
        accessed = float(stat_result.st_atime or stat_result.st_mtime or now)
    except OSError:
        accessed = now
    _append(str(path), measured.bytes, accessed, str(path), False, True, now, totals, seen)
    return True


def _append(
    text: str,
    size: int,
    accessed: float,
    display: str,
    inside_project: bool,
    is_dir: bool,
    now: float,
    totals: dict[str, CategoryTotal],
    seen: list[FileRecord],
) -> None:
    category = classify_entry(display, is_dir=is_dir, inside_project=inside_project)
    age_days = (now - accessed) / 86400
    stale = age_days >= STALE_DAYS
    record = FileRecord(
        path=display,
        size=max(0, size),
        accessed=accessed,
        category=category,
        importance=importance(category, accessed, now),
        is_dir=is_dir,
        stale=stale,
        base_category=category,
    )
    bucket = totals.setdefault(category, CategoryTotal())
    bucket.count += 1
    bucket.bytes += record.size
    seen.append(record)


def _mark_duplicates(records: list[FileRecord], now: float) -> None:
    by_size: dict[int, list[FileRecord]] = {}
    for record in records:
        if record.is_dir or record.size <= 0:
            continue
        by_size.setdefault(record.size, []).append(record)
    for size, group in by_size.items():
        if len(group) < 2:
            continue
        group.sort(key=lambda item: item.path)
        for same in _same_content(group, size):
            original = same[0].path
            for record in same[1:]:
                record.duplicate_of = original
                record.base_category = record.base_category or record.category
                record.category = "duplicates"
                record.importance = importance("duplicates", record.accessed, now)


def _same_content(group: list[FileRecord], size: int) -> list[list[FileRecord]]:
    """Split same-size records into sets with equal bytes.

    The prefix hash is only a filter. A file longer than the prefix counts as a
    duplicate after its whole content matches.
    """
    buckets: dict[str, list[FileRecord]] = {}
    for record in group:
        digest = hash_file(Path(record.path), _HASH_LIMIT)
        if digest is not None:
            buckets.setdefault(digest, []).append(record)
    matches: list[list[FileRecord]] = []
    for bucket in buckets.values():
        if len(bucket) < 2:
            continue
        if size <= _HASH_LIMIT:
            matches.append(bucket)
            continue
        full: dict[str, list[FileRecord]] = {}
        for record in bucket:
            digest = hash_file(Path(record.path))
            if digest is not None:
                full.setdefault(digest, []).append(record)
        matches.extend(same for same in full.values() if len(same) > 1)
    return matches


def _project_marker(name: str) -> bool:
    folded = name.casefold()
    return folded in _PROJECT_FILES or folded.endswith(".sln")


def _is_under(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True
