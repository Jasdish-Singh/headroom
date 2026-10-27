"""Turn toss rules into a review list. Nothing moves unless the user types MOVE.

The destination is the Recycle Bin when Windows accepts the file, and an
archive folder otherwise. This is not the cache-cleanup allowlist.
"""

from __future__ import annotations

import ctypes
import os
import shutil
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from headroom.analyze import Analysis, FileRecord, importance, load_analysis
from headroom.explain import explain_record
from headroom.formatutil import format_bytes
from headroom.plans import load_plans
from headroom.rules import Candidate, Rule, decide, load_merged_rules, state_dir, winning_rule
from headroom.walk import classify_path

MOVE_PROMPT = "Type MOVE to move the selected files: "
_FO_DELETE = 3
_FOF_ALLOWUNDO = 0x40
_FOF_NOCONFIRMATION = 0x10
_FOF_SILENT = 0x4
_FOF_NOERRORUI = 0x400


class ReviewError(Exception):
    """The review list was missing or a selected path is not safe to move."""


@dataclass
class Suggestion:
    record: FileRecord
    source: str
    rule_text: str


@dataclass
class ReviewGroup:
    category: str
    label: str
    items: list[Suggestion] = field(default_factory=list)

    @property
    def total_bytes(self) -> int:
        return sum(item.record.size for item in self.items)


@dataclass
class MoveStats:
    archived: int = 0
    recycled: int = 0
    archived_bytes: int = 0
    skipped: int = 0
    archive: Path | None = None


def suggestions_for(
    analysis: Analysis,
    rules: list[Rule],
    *,
    now: float,
    today: date,
) -> list[Suggestion]:
    found: list[Suggestion] = []
    for record in analysis.records:
        item = _candidate(record)
        rule = winning_rule(rules, item, now=now, today=today)
        if rule is None or rule.action != "toss":
            continue
        if decide(rules, item, now=now, today=today) != "toss":
            continue
        found.append(Suggestion(record=record, source=rule.source, rule_text=rule.text))
    found.sort(key=lambda item: (-item.record.size, item.record.path))
    return found


def collect_suggestions(
    directory: Path,
    *,
    now: float,
    today: date,
) -> tuple[list[Suggestion], list[Path]]:
    """Rule suggestions plus duplicate and download plans. Read only."""
    items: list[Suggestion] = []
    roots: list[Path] = []
    seen: set[str] = set()
    analysis = load_analysis(directory)
    if analysis is not None:
        items.extend(suggestions_for(analysis, load_merged_rules(directory), now=now, today=today))
        for root in analysis.roots:
            if root not in seen:
                seen.add(root)
                roots.append(Path(root))
    plan_items, plan_roots = load_plans(directory)
    for plan in plan_items:
        record = FileRecord(
            path=plan.path,
            size=plan.size,
            accessed=plan.accessed,
            category=plan.category,
            importance=importance(plan.category, plan.accessed, now),
            is_dir=plan.is_dir,
            base_category=plan.category,
        )
        items.append(Suggestion(record=record, source=plan.source, rule_text=plan.reason))
    for root in plan_roots:
        if root not in seen:
            seen.add(root)
            roots.append(Path(root))
    return items, roots


def group_suggestions(items: list[Suggestion]) -> list[ReviewGroup]:
    buckets: dict[tuple[str, str], ReviewGroup] = {}
    for item in items:
        path = Path(item.record.path)
        label = str(path if item.record.is_dir else path.parent)
        key = (item.record.category, label)
        group = buckets.get(key)
        if group is None:
            group = ReviewGroup(category=item.record.category, label=label)
            buckets[key] = group
        group.items.append(item)
    groups = list(buckets.values())
    groups.sort(key=lambda group: (-group.total_bytes, group.label))
    return groups


def format_review(groups: list[ReviewGroup], *, archive: Path) -> str:
    lines = [
        "Headroom review",
        "Dry run. Nothing will be moved.",
        "Toss suggestions are not deleted. Approving a group moves those files",
        "to the Recycle Bin when Windows allows it, otherwise into:",
        f"  {archive}",
        "Cache cleanup is separate and still uses the allowlist and Type YES.",
        "",
    ]
    if not groups:
        lines.append("No toss suggestions for the current profiles and your rules.")
        return "\n".join(lines) + "\n"
    for index, group in enumerate(groups, start=1):
        lines.append(
            f"[{index}] {group.category}  {format_bytes(group.total_bytes)}  "
            f"{len(group.items)} items  {group.label}"
        )
        for item in group.items[:8]:
            when = datetime.fromtimestamp(item.record.accessed).strftime("%Y-%m-%d")
            lines.append(
                f"    {format_bytes(item.record.size):>10}  {item.record.importance:3d}  "
                f"opened {when}  {item.record.path}"
            )
            lines.append(f"    rule ({item.source}): {item.rule_text}")
            lines.append(f"    why: {explain_record(item.record, item.rule_text)}")
        hidden = len(group.items) - 8
        if hidden > 0:
            lines.append(f"    ... {hidden} more")
        lines.append("")
    lines.append("Run headroom review --apply to choose groups. Nothing moves until you type MOVE.")
    return "\n".join(lines) + "\n"


def parse_group_selection(text: str, count: int) -> list[int]:
    """Return 0-based indexes. An empty answer selects nothing."""
    raw = text.strip()
    if not raw:
        return []
    if raw.casefold() == "all":
        return list(range(count))
    indexes: list[int] = []
    for part in raw.split(","):
        piece = part.strip()
        if not piece:
            continue
        try:
            number = int(piece)
        except ValueError as exc:
            raise ReviewError("group numbers look like 1,2 or all") from exc
        if number < 1 or number > count:
            raise ReviewError(f"there is no group {number}")
        if number - 1 not in indexes:
            indexes.append(number - 1)
    return indexes


def confirm_move(answer: str) -> bool:
    return answer.strip() == "MOVE"


def archive_directory(directory: Path | None = None) -> Path:
    return (state_dir() if directory is None else directory) / "archive"


def move_records(
    records: list[FileRecord],
    roots: list[Path],
    archive: Path,
    *,
    recycle,
    state: Path | None = None,
) -> MoveStats:
    """Move approved records. Illegal paths abort before the first move."""
    state_path = state_dir() if state is None else state
    planned: list[tuple[FileRecord, Path]] = []
    skipped = 0
    for record in records:
        path = Path(record.path)
        verdict = _check_path(path, roots, state_path)
        if verdict == "illegal":
            raise ReviewError(f"refusing to move {path}")
        if verdict == "missing":
            skipped += 1
            continue
        if not record.is_dir:
            try:
                current = path.lstat().st_size
            except OSError:
                skipped += 1
                continue
            if current != record.size:
                skipped += 1
                continue
        planned.append((record, path))
    planned = _drop_children(planned)
    stats = MoveStats(skipped=skipped, archive=archive)
    for record, path in planned:
        try:
            if recycle is not None and recycle(path):
                stats.recycled += 1
                continue
            destination = _destination(path, roots, archive)
            shutil.move(os.fspath(path), os.fspath(destination))
        except OSError:
            stats.skipped += 1
            continue
        stats.archived += 1
        stats.archived_bytes += record.size
    return stats


def format_move_stats(stats: MoveStats) -> str:
    lines = []
    if stats.recycled:
        lines.append(f"Moved {stats.recycled} items to the Recycle Bin.")
    if stats.archived:
        lines.append(
            f"Moved {stats.archived} items ({format_bytes(stats.archived_bytes)}) to {stats.archive}."
        )
    if not stats.recycled and not stats.archived:
        lines.append("Nothing was moved.")
    if stats.skipped:
        lines.append(f"Skipped {stats.skipped} items that were missing, changed, or locked.")
    return "\n".join(lines) + "\n"


def recycle_file(path: Path) -> bool:
    """Move one file to the Windows Recycle Bin. Returns False everywhere else."""
    if os.name != "nt":
        return False
    try:
        return _recycle_windows(path)
    except OSError:
        return False


def _candidate(record: FileRecord) -> Candidate:
    return Candidate(
        path=record.path,
        size=record.size,
        accessed=record.accessed,
        category=record.category,
        is_dir=record.is_dir,
    )


def _check_path(path: Path, roots: list[Path], state: Path) -> str:
    if any(part == ".." for part in path.parts):
        return "illegal"
    if path.parent == path or path in roots:
        return "illegal"
    if _is_under(path, state):
        return "illegal"
    if not any(_is_under(path, root) for root in roots):
        return "illegal"
    kind = classify_path(path)
    if kind == "missing":
        return "missing"
    if kind == "link" or kind not in {"file", "dir"}:
        return "illegal"
    return "ok"


def _drop_children(planned: list[tuple[FileRecord, Path]]) -> list[tuple[FileRecord, Path]]:
    paths = [path for _record, path in planned]
    kept: list[tuple[FileRecord, Path]] = []
    for record, path in planned:
        if any(path != other and _is_under(path, other) for other in paths):
            continue
        kept.append((record, path))
    kept.sort(key=lambda item: len(item[1].parts), reverse=True)
    return kept


def _destination(path: Path, roots: list[Path], archive: Path) -> Path:
    containers = [root for root in roots if _is_under(path, root)]
    root = max(containers, key=lambda item: len(item.parts))
    relative = path.relative_to(root)
    destination = archive / relative
    if destination.exists():
        destination = destination.with_name(destination.name + ".headroom")
    destination.parent.mkdir(parents=True, exist_ok=True)
    return destination


def _is_under(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _recycle_windows(path: Path) -> bool:
    buffer = ctypes.create_unicode_buffer(str(path) + "\0")
    operation = _SHFILEOPSTRUCTW()
    operation.hwnd = None
    operation.wFunc = _FO_DELETE
    operation.pFrom = ctypes.cast(buffer, ctypes.c_wchar_p)
    operation.pTo = None
    operation.fFlags = _FOF_ALLOWUNDO | _FOF_NOCONFIRMATION | _FOF_SILENT | _FOF_NOERRORUI
    operation.fAnyOperationsAborted = 0
    operation.hNameMappings = None
    operation.lpszProgressTitle = None
    result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(operation))
    return result == 0 and not operation.fAnyOperationsAborted


class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = (
        ("hwnd", ctypes.c_void_p),
        ("wFunc", ctypes.c_uint),
        ("pFrom", ctypes.c_wchar_p),
        ("pTo", ctypes.c_wchar_p),
        ("fFlags", ctypes.c_ushort),
        ("fAnyOperationsAborted", ctypes.c_int),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", ctypes.c_wchar_p),
    )
