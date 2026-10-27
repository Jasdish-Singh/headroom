"""Read-only and dry-run tools exposed by ``headroom mcp``.

Action tools describe a plan. They do not call cleanup, move files, or disable
startup entries. Confirm those in Headroom itself.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

_DRY = "Dry run. Nothing was deleted or moved. Confirm it in Headroom on this PC."


def tool_status() -> str:
    """Read RAM, commit, page file, and free space. Does not change anything."""
    from headroom.config import load_config
    from headroom.probe import sample
    from headroom.render import format_status

    current = sample()
    config, path = load_config(None)
    return format_status(current, config, path)


def tool_top(count: int = 10) -> str:
    """List the processes using the most RAM. Does not stop any process."""
    from headroom.probe import sample
    from headroom.processes import top_processes
    from headroom.render import format_processes

    limit = count if count > 0 else 10
    current = sample()
    return format_processes(top_processes(limit), current.memory.ram_total)


def tool_scan(path: str) -> str:
    """List the largest entries under a folder. Links are not followed."""
    from headroom.render import format_scan
    from headroom.scan import scan_path

    root = Path(path)
    if not root.exists():
        return f"headroom: folder not found: {root}\n"
    return format_scan(scan_path(root, 20))


def tool_analyze(path: str) -> str:
    """Categorize files under a folder. Does not save, move, or delete them."""
    from headroom.analyze import format_analysis, scan

    root = Path(path)
    if not root.exists():
        return f"headroom: folder not found: {root}\n"
    analysis = scan([root], now=time.time())
    return format_analysis(analysis) + "Read-only. Nothing was saved, moved, or deleted.\n"


def tool_report() -> str:
    """Show the latest history report. Does not record a new sample."""
    from headroom.config import Config
    from headroom.history import HistoryStore, build_report, default_history_path
    from headroom.render import format_report

    path = default_history_path()
    if not path.is_file():
        return "No history yet. Leave headroom watch running to record samples.\n"
    store = HistoryStore(path)
    try:
        return format_report(build_report(store, Config(), now=time.time()))
    finally:
        store.close()


def tool_changes(since: str = "7d") -> str:
    """Show folders that grew and new large files. Does not delete anything."""
    from headroom.cli import parse_window
    from headroom.history import HistoryStore, build_changes, default_history_path
    from headroom.render import format_changes

    path = default_history_path()
    if not path.is_file():
        return "No history yet.\n"
    store = HistoryStore(path)
    try:
        report = build_changes(store, now=time.time(), window_seconds=parse_window(since))
    finally:
        store.close()
    return format_changes(report)


def tool_dupes(path: str) -> str:
    """Group duplicate files. Does not move the extra copies."""
    from headroom.dupes import find_duplicates, format_dupes

    root = Path(path)
    if not root.exists():
        return f"headroom: folder not found: {root}\n"
    groups, skipped = find_duplicates([root])
    return format_dupes(groups, skipped=skipped) + _DRY + "\nRun headroom dupes --apply locally and type MOVE.\n"


def tool_startup_list() -> str:
    """List startup entries and estimated RAM. Does not disable any."""
    from headroom.startup import default_catalog, format_startup

    return format_startup(default_catalog().list_entries())


def tool_review_list() -> str:
    """Show toss suggestions. Does not move them."""
    from datetime import date

    from headroom.review import collect_suggestions, format_review, group_suggestions
    from headroom.rules import state_dir

    directory = state_dir()
    items, _roots = collect_suggestions(directory, now=time.time(), today=date.today())
    return format_review(group_suggestions(items), archive=directory / "archive")


def tool_clean() -> str:
    """Preview allowlisted cache cleanup. Does not delete those files."""
    from headroom.cleanup import build_preview, default_recycle_backend, discover_groups
    from headroom.config import load_config
    from headroom.render import format_preview

    config, _path = load_config(None)
    groups, context = discover_groups(config.enabled_cleanup, os.environ)
    preview = build_preview(groups, default_recycle_backend(), context)
    text = format_preview(preview, dry_run=True)
    return text + _DRY + "\nType YES in headroom clean on this PC if you want the allowlisted caches removed.\n"


def tool_move() -> str:
    """Describe the review list that headroom review --apply would offer. Moves nothing."""
    text = tool_review_list()
    return text + _DRY + "\nType MOVE in headroom review --apply, headroom dupes --apply, or the local dashboard.\n"


def tool_startup_disable(entry_id: str) -> str:
    """Describe one startup entry. Does not disable it or write a backup."""
    from headroom.startup import default_catalog

    catalog = default_catalog()
    match = [entry for entry in catalog.list_entries() if entry.id == entry_id]
    if not match:
        return (
            f"No startup entry {entry_id}. Nothing was disabled.\n"
            "Run headroom startup locally to see ids.\n"
        )
    entry = match[0]
    return (
        f"Dry run. Would disable {entry.name} ({entry.id}) from {entry.location}.\n"
        f"Command: {entry.command}\n"
        "Nothing was disabled. On this PC run headroom startup disable "
        f"{entry.id} and type DISABLE. Restore later with headroom startup restore.\n"
    )


TOOLS: list[tuple[str, str, object]] = [
    ("status", "Read RAM, commit, and free disk space. Does not change anything.", tool_status),
    ("top", "List the processes using the most RAM. Does not stop any process.", tool_top),
    ("scan", "List the largest entries under a folder. Links are not followed.", tool_scan),
    ("analyze", "Categorize files under a folder. Does not save, move, or delete them.", tool_analyze),
    ("report", "Show the latest history report.", tool_report),
    ("changes", "Show folders that grew and new large files.", tool_changes),
    ("dupes", "Group duplicate files without moving them.", tool_dupes),
    ("startup_list", "List startup entries and estimated RAM. Does not disable any.", tool_startup_list),
    ("review_list", "Show toss suggestions. Does not move them.", tool_review_list),
    ("clean", "Preview allowlisted cache cleanup. Does not delete those files.", tool_clean),
    ("move", "Describe files headroom review could move. Moves nothing.", tool_move),
    ("startup_disable", "Describe one startup entry. Does not disable it.", tool_startup_disable),
]
