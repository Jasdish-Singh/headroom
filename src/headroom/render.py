"""Text reports for status, process lists, scans, and cleanup previews."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from headroom.cleanup import DeleteStats, Preview
from headroom.config import Config
from headroom.formatutil import format_bytes, format_percent
from headroom.history import ChangeReport, DiskTrend, Report
from headroom.probe import Sample
from headroom.processes import ProcessMem
from headroom.scan import ScanResult
from headroom.selfcheck import (
    CPU_BUDGET_PERCENT,
    PET_CPU_BUDGET_PERCENT,
    PET_RSS_BUDGET_BYTES,
    RSS_BUDGET_BYTES,
    SelfUsage,
)


def format_status(
    sample: Sample,
    config: Config,
    config_path: Path | None,
) -> str:
    memory = sample.memory
    when = datetime.fromtimestamp(sample.captured_at).strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        f"Headroom status  {when}",
        "",
        "Memory",
        (
            f"  RAM         {format_percent(memory.ram_percent):>7}   "
            f"{format_bytes(memory.ram_used)} used / {format_bytes(memory.ram_total)}"
            f"    {format_bytes(memory.ram_available)} available"
        ),
    ]
    if memory.commit_total <= 0:
        lines.append("  Commit      unavailable")
    else:
        estimated = "  (estimated)" if memory.commit_estimated else ""
        lines.append(
            f"  Commit      {format_percent(memory.commit_percent):>7}   "
            f"{format_bytes(memory.commit_used)} used / {format_bytes(memory.commit_total)}"
            f"{estimated}"
        )
    if memory.pagefile_total <= 0:
        lines.append("  Page file   none")
    else:
        lines.append(
            f"  Page file   {format_percent(memory.pagefile_percent):>7}   "
            f"{format_bytes(memory.pagefile_used)} used / {format_bytes(memory.pagefile_total)}"
        )
    lines.extend(["", "Disks", "  Mount                         Free        Size     Watch"])
    if not sample.disks:
        lines.append("  No drives reported.")
    watched = set(config.drives)
    present = {disk.key for disk in sample.disks}
    for disk in sample.disks:
        if disk.key in watched:
            if disk.free < config.disk_critical_free_bytes:
                watch = "critical"
            elif disk.free < config.disk_warn_free_bytes:
                watch = "warn"
            else:
                watch = "ok"
        else:
            watch = "not watched"
        lines.append(
            f"  {disk.mount:<28}  {format_bytes(disk.free):>10}  {format_bytes(disk.total):>10}  {watch}"
        )
    missing = [drive for drive in config.drives if drive not in present]
    for drive in missing:
        lines.append(f"  {drive:<28}  not present")
    lines.append("")
    if memory.ram_percent > config.ram_percent:
        lines.append(
            f"RAM is above {format_percent(config.ram_percent)} "
            f"(now {format_percent(memory.ram_percent)}). "
            "A warning is sent after it stays there for "
            f"{config.ram_sustained_seconds} seconds."
        )
    else:
        lines.append(f"RAM is at or under the {format_percent(config.ram_percent)} line.")
    lines.append(
        "Watched free-space lines: warn under "
        f"{format_bytes(config.disk_warn_free_bytes)}, critical under "
        f"{format_bytes(config.disk_critical_free_bytes)}."
    )
    lines.append(f"Config: {config_path if config_path is not None else 'defaults'}")
    return "\n".join(lines) + "\n"


def status_is_attention(sample: Sample, config: Config) -> bool:
    """True when RAM is over the line or a watched drive is low right now."""
    if sample.memory.ram_percent > config.ram_percent:
        return True
    watched = set(config.drives)
    for disk in sample.disks:
        if disk.key in watched and disk.free < config.disk_warn_free_bytes:
            return True
    return False


def format_processes(rows: list[ProcessMem], ram_total: int | None) -> str:
    if not rows:
        return "No processes reported.\n"
    lines = ["PID      RAM          SHARE  NAME"]
    for row in rows:
        share = (row.rss_bytes / ram_total * 100) if ram_total else 0.0
        lines.append(
            f"{row.pid:<7d}  {format_bytes(row.rss_bytes):>10}  {share:5.1f}%  {row.name}"
        )
    return "\n".join(lines) + "\n"


def format_scan(result: ScanResult) -> str:
    lines = [
        f"Scan {result.root}",
        "Links and junctions are not followed.",
        "",
    ]
    if not result.entries:
        lines.append("(no files or folders)")
    else:
        for entry in result.entries:
            kind = "dir" if entry.kind == "dir" else "file"
            lines.append(f"{format_bytes(entry.size):>12}  {entry.name}  ({kind})")
    lines.append("")
    lines.append(f"Total {format_bytes(result.total_bytes)}")
    if result.omitted:
        lines.append(f"{result.omitted} smaller entries not shown")
    if result.skipped_links:
        lines.append(f"Skipped links: {result.skipped_links}")
    if result.errors:
        lines.append(f"Unreadable entries: {result.errors}")
    return "\n".join(lines) + "\n"


def format_preview(preview: Preview, *, dry_run: bool) -> str:
    lines = ["Headroom cleanup preview"]
    if dry_run:
        lines.append("Dry run. Nothing will be deleted.")
    else:
        lines.append("Nothing is deleted until you type YES.")
    lines.append("Only the built-in allowlist is shown. User documents are not included.")
    lines.append("")
    for group in preview.groups:
        lines.append(group.id)
        lines.append(f"  {group.description}")
        if group.blocked:
            lines.append(f"  SKIPPED  {group.note}")
            lines.append("")
            continue
        if group.id == "recycle_bin" and not group.supported:
            lines.append(f"  {group.note or 'not available'}")
            lines.append("")
            continue
        if not group.paths and group.id == "recycle_bin":
            lines.append(f"  {format_bytes(group.total_bytes):>10}  {group.files:6d} files")
            if group.note:
                lines.append(f"  {group.note}")
            lines.append("")
            continue
        if not group.paths:
            lines.append(f"  {group.note or 'not present'}")
            lines.append("")
            continue
        shown = group.paths[:8]
        for item in shown:
            lines.append(
                f"  {format_bytes(item.bytes):>10}  {item.files:6d} files  {item.path}"
            )
        hidden = len(group.paths) - len(shown)
        if hidden:
            lines.append(f"  ... and {hidden} more paths")
        if group.links:
            lines.append(
                f"  links that would be removed without following them: {group.links}"
            )
        if group.errors:
            lines.append(f"  unreadable entries skipped: {group.errors}")
        if group.note:
            lines.append(f"  note: {group.note}")
        lines.append("")
    lines.append(
        f"Reclaimable: {format_bytes(preview.total_bytes)} in {preview.total_files} files."
    )
    if preview.total_links:
        lines.append(
            f"{preview.total_links} links would be removed without following them."
        )
    return "\n".join(lines).rstrip() + "\n"


def format_stats(stats: DeleteStats) -> str:
    lines = [
        f"Removed {_noun(stats.removed_files, 'file')} ({format_bytes(stats.freed_bytes)})."
    ]
    if stats.removed_links:
        lines.append(f"Removed {_noun(stats.removed_links, 'link')} without following them.")
    if stats.removed_dirs:
        lines.append(f"Removed {_noun(stats.removed_dirs, 'directory', 'directories')}.")
    lines.append(
        "Skipped "
        + _noun(stats.skipped, "locked or unreadable entry", "locked or unreadable entries")
        + "."
    )
    if stats.recycle_emptied:
        lines.append("Emptied the Recycle Bin.")
    return "\n".join(lines) + "\n"


def _noun(count: int, singular: str, plural: str | None = None) -> str:
    word = singular if count == 1 else (plural or f"{singular}s")
    return f"{count} {word}"


def format_history(
    rows: list[tuple[float, float, float, tuple[tuple[str, int], ...]]],
    *,
    path: Path,
    window_seconds: float,
    total: int,
) -> str:
    lines = [
        f"Headroom history  {path}",
        f"Window: last {_window_label(window_seconds)}. {total} samples.",
    ]
    if not rows:
        lines.append("No samples in this window.")
        return "\n".join(lines) + "\n"
    shown = rows[-20:]
    if total > len(shown):
        lines.append(f"Showing the latest {len(shown)}.")
    lines.append("")
    lines.append(f"{'TIME':<20} {'RAM':>7} {'COMMIT':>8}  FREE")
    for ts, ram, commit, disks in shown:
        when = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
        free = "  ".join(f"{drive} {format_bytes(amount)}" for drive, amount in disks) or "-"
        lines.append(f"{when:<20} {format_percent(ram):>7} {format_percent(commit):>8}  {free}")
    return "\n".join(lines) + "\n"


def format_changes(report: ChangeReport) -> str:
    lines = [f"Headroom changes  since {_window_label(report.window_seconds)}"]
    if report.note:
        lines.append(report.note)
    else:
        lines.append("Folders that grew between the snapshots at the ends of this window:")
    if report.folder_changes:
        lines.append("")
        for change in report.folder_changes[:15]:
            if change.before <= 0:
                lines.append(f"  {change.path}")
                lines.append(f"    new in the top folders, now {format_bytes(change.after)}")
            else:
                lines.append(f"  {change.path}")
                lines.append(
                    f"    +{format_bytes(change.delta)}   "
                    f"{format_bytes(change.before)} -> {format_bytes(change.after)}"
                )
    elif report.note is None:
        lines.append("No tracked folder grew.")
    if report.new_files:
        lines.append("")
        lines.append("New files over the large-file line:")
        for item in report.new_files[:15]:
            when = datetime.fromtimestamp(item.first_seen).strftime("%Y-%m-%d %H:%M")
            lines.append(f"  {format_bytes(item.bytes):>10}  {item.path}  (first seen {when})")
    else:
        lines.append("")
        lines.append("No new large files in this window.")
    return "\n".join(lines) + "\n"


def format_report(report: Report) -> str:
    lines = [
        "Headroom report",
        "",
        (
            f"Database: {report.path}  ({format_bytes(report.db_bytes)}, "
            f"cap {format_bytes(report.cap_bytes)})"
        ),
        f"Samples: {report.sample_count}",
    ]
    if report.newest_ts is None or report.ram_percent is None:
        lines.append("No samples yet. Leave `headroom watch` running to record them.")
        return "\n".join(lines) + "\n"
    when = datetime.fromtimestamp(report.newest_ts).strftime("%Y-%m-%d %H:%M:%S")
    lines.append(f"Latest sample: {when}")
    lines.append(
        f"RAM {format_percent(report.ram_percent)}   "
        f"{format_bytes(report.ram_used or 0)} / {format_bytes(report.ram_total or 0)}"
    )
    lines.append(
        f"Commit {format_percent(report.commit_percent or 0)}   "
        f"{format_bytes(report.commit_used or 0)} / {format_bytes(report.commit_total or 0)}"
    )
    if (report.page_total or 0) > 0:
        lines.append(
            f"Page file {format_bytes(report.page_used or 0)} / {format_bytes(report.page_total or 0)}"
        )
    lines.append("")
    lines.append("Disk")
    if not report.disk_free and not report.trends:
        lines.append("  No drive history.")
    free_by_drive = {drive: (free, total) for drive, free, total in report.disk_free}
    seen: set[str] = set()
    for trend in report.trends:
        seen.add(trend.drive)
        current = free_by_drive.get(trend.drive)
        if current is None:
            lines.append(f"  {trend.drive}  not in the latest sample. {_trend_phrase(trend)}")
        else:
            free, total = current
            lines.append(
                f"  {trend.drive}  {format_bytes(free)} free / {format_bytes(total)}. "
                f"{_trend_phrase(trend)}"
            )
    lines.append("")
    if report.leaks:
        lines.append("Likely leaks")
        for leak in report.leaks[:8]:
            lines.append(
                f"  {leak.name} (pid {leak.pid}) grew {format_bytes(leak.growth)} "
                f"and is using {format_bytes(leak.end_rss)}"
            )
    else:
        lines.append("No likely memory leaks in the configured window.")
    lines.append("")
    lines.append("Since yesterday")
    if report.changes.note:
        lines.append(f"  {report.changes.note}")
    elif not report.changes.folder_changes:
        lines.append("  No tracked folder grew.")
    else:
        for change in report.changes.folder_changes[:8]:
            lines.append(f"  +{format_bytes(change.delta)}  {change.path}")
    if report.changes.new_files:
        lines.append(f"  New large files: {len(report.changes.new_files)}")
    return "\n".join(lines) + "\n"


def format_self(current: SelfUsage, agent: SelfUsage | None, pet: SelfUsage | None = None) -> str:
    lines = ["Headroom resource use", ""]
    if agent is not None and agent.pid != current.pid:
        lines.append("Watch")
        lines.extend(_usage_lines(agent))
        lines.append(_budget_line(agent))
        lines.append("")
        lines.append("This command")
        lines.extend(_usage_lines(current))
    else:
        lines.append("This process")
        lines.extend(_usage_lines(current))
        lines.append(_budget_line(current))
        if agent is None:
            lines.append("No other Headroom process is holding the single-instance lock.")
    lines.append(
        f"Target: under {format_bytes(RSS_BUDGET_BYTES)} RSS and under "
        f"{CPU_BUDGET_PERCENT:.0f}% CPU on average."
    )
    lines.append("")
    lines.append("Roomy")
    if pet is not None:
        lines.extend(_usage_lines(pet))
        lines.append(
            _budget_line(
                pet,
                rss_budget=PET_RSS_BUDGET_BYTES,
                cpu_budget=PET_CPU_BUDGET_PERCENT,
                rss_label="30 MB",
                cpu_label="2%",
            )
        )
    else:
        lines.append("  Roomy is not running.")
    lines.append(
        f"Pet target: under {format_bytes(PET_RSS_BUDGET_BYTES)} extra RSS and under "
        f"{PET_CPU_BUDGET_PERCENT:.0f}% CPU on average."
    )
    return "\n".join(lines) + "\n"


def _usage_lines(usage: SelfUsage) -> list[str]:
    return [
        f"  pid {usage.pid}",
        f"  RSS {format_bytes(usage.rss_bytes)}",
        f"  CPU {usage.cpu_percent:.1f}% (short sample, not a long average)",
    ]


def _budget_line(
    usage: SelfUsage,
    *,
    rss_budget: int = RSS_BUDGET_BYTES,
    cpu_budget: float = CPU_BUDGET_PERCENT,
    rss_label: str = "40 MB",
    cpu_label: str = "1%",
) -> str:
    rss = f"under {rss_label}" if usage.rss_bytes < rss_budget else f"at or above {rss_label}"
    cpu = f"under {cpu_label}" if usage.cpu_percent < cpu_budget else f"at or above {cpu_label}"
    return f"  This sample is {rss} and {cpu}."


def _trend_phrase(trend: DiskTrend) -> str:
    if not trend.enough:
        return "Not enough history yet (need about 6 hours)."
    rate = trend.bytes_per_day
    if rate <= -(1024 * 1024):
        losing = format_bytes(abs(rate))
        if trend.days_until_full is None:
            return f"Losing ~{losing}/day."
        days = trend.days_until_full
        if days >= 10:
            when = f"~{days:.0f} days"
        elif days >= 1:
            when = f"~{days:.1f} days"
        else:
            when = f"~{days * 24:.0f} hours"
        return f"Losing ~{losing}/day, full in {when}."
    if rate >= 1024 * 1024:
        return f"Gaining ~{format_bytes(rate)}/day."
    return "Holding steady."


def _window_label(seconds: float) -> str:
    if seconds == 86400:
        return "1 day"
    if seconds == 7 * 86400:
        return "7 days"
    if seconds > 0 and seconds % 86400 == 0:
        days = int(seconds // 86400)
        return f"{days} day" if days == 1 else f"{days} days"
    if seconds > 0 and seconds % 3600 == 0:
        hours = int(seconds // 3600)
        return f"{hours} hour" if hours == 1 else f"{hours} hours"
    return f"{int(seconds)} seconds"


def watch_line(sample: Sample, config: Config) -> str:
    memory = sample.memory
    parts = [
        f"RAM {format_percent(memory.ram_percent)}",
        f"commit {format_percent(memory.commit_percent)}",
    ]
    for drive in config.drives:
        match = next((disk for disk in sample.disks if disk.key == drive), None)
        if match is None:
            parts.append(f"{drive} not present")
        else:
            parts.append(f"{drive} {format_bytes(match.free)} free")
    return "  ".join(parts)
