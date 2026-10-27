"""Weekly health summary. The watch loop asks for it; a short history does not toast."""

from __future__ import annotations

import logging
from pathlib import Path

from headroom.alerts import Alert
from headroom.config import Config
from headroom.explain import explain_record
from headroom.formatutil import format_bytes
from headroom.history import HistoryStore, find_leaks
from headroom.review import collect_suggestions
from headroom.rules import state_dir

WEEK_SECONDS = 7 * 86400


def weekly_due(store: HistoryStore, *, now: float, marker: Path) -> bool:
    if marker.is_file():
        try:
            last = float(marker.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            last = 0.0
        if now - last < WEEK_SECONDS:
            return False
    rows = store.samples_between(0, now + 1)
    if len(rows) < 2:
        return False
    oldest = float(rows[0]["ts"])
    return now - oldest >= WEEK_SECONDS


def build_weekly_report(store: HistoryStore, config: Config, *, now: float, directory: Path | None = None) -> str:
    lines = ["Headroom weekly", ""]
    lines.append("Space")
    space_lines = _space_lines(store, now)
    lines.extend(space_lines or ["  Not enough disk history to compare a week."])
    lines.append("")
    lines.append("Folders that grew")
    from headroom.history import build_changes

    changes = build_changes(store, now=now, window_seconds=WEEK_SECONDS)
    if changes.folder_changes:
        for change in changes.folder_changes[:8]:
            lines.append(f"  +{format_bytes(change.delta)}  {change.path}")
    else:
        lines.append(f"  {changes.note or 'No tracked folder grew.'}")
    lines.append("")
    lines.append("Leak suspects")
    points = store.processes_between(now - config.leak_window_seconds, now + 1)
    leaks = find_leaks(
        points,
        now=now,
        window_seconds=config.leak_window_seconds,
        growth_bytes=config.leak_growth_bytes,
    )
    if leaks:
        for leak in leaks[:5]:
            lines.append(f"  {leak.name} grew {format_bytes(leak.growth)} and is using {format_bytes(leak.end_rss)}")
    else:
        lines.append("  No process grew enough to look like a leak.")
    lines.append("")
    lines.append("Suggestions")
    try:
        items, _roots = collect_suggestions(state_dir() if directory is None else directory, now=now, today=_today(now))
    except Exception:
        items = []
    if not items:
        lines.append("  No toss suggestions yet. Run headroom analyze when you want a review list.")
    else:
        for item in items[:5]:
            reason = explain_record(item.record, item.rule_text, now=now)
            lines.append(f"  {item.record.path}")
            lines.append(f"    {reason}")
        if len(items) > 5:
            lines.append(f"  {len(items) - 5} more are in headroom review.")
    lines.append("")
    lines.append("Nothing was moved or deleted.")
    return "\n".join(lines) + "\n"


def maybe_weekly(
    store: HistoryStore,
    config: Config,
    notifier,
    *,
    now: float,
    directory: Path | None = None,
    logger: logging.Logger | None = None,
) -> str | None:
    """Toast and save a report at most once a week. Returns the text when one is sent."""
    folder = state_dir() if directory is None else directory
    marker = folder / "weekly.last"
    if not weekly_due(store, now=now, marker=marker):
        return None
    text = build_weekly_report(store, config, now=now, directory=folder)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "weekly.txt").write_text(text, encoding="utf-8")
    marker.write_text(f"{now}\n", encoding="utf-8")
    summary = _toast_body(text)
    alert = Alert(
        level="warning",
        code="weekly",
        title="Headroom weekly",
        message=summary,
    )
    try:
        getattr(notifier, "notify")(alert)
    except Exception:
        logging.getLogger("headroom").warning("weekly toast failed", exc_info=True)
    if logger is not None:
        logger.info("weekly summary written to %s", folder / "weekly.txt")
    return text


def _space_lines(store: HistoryStore, now: float) -> list[str]:
    lines: list[str] = []
    for drive in store.drives():
        points = store.disk_points(drive)
        if len(points) < 2:
            continue
        latest_ts, latest_free = points[-1]
        target = now - WEEK_SECONDS
        earlier_ts, earlier_free = points[0]
        for ts, free in points:
            if ts <= target:
                earlier_ts, earlier_free = ts, free
        delta = latest_free - earlier_free
        if delta > 0:
            verb = "gained"
        elif delta < 0:
            verb = "lost"
        else:
            verb = "held"
        lines.append(
            f"  {drive} {verb} {format_bytes(abs(delta))} "
            f"({format_bytes(earlier_free)} -> {format_bytes(latest_free)})"
        )
        del latest_ts, earlier_ts
    return lines


def _toast_body(text: str) -> str:
    body = " ".join(line.strip() for line in text.splitlines() if line.strip())
    if len(body) > 320:
        return body[:317] + "..."
    return body


def _today(now: float):
    from datetime import datetime

    return datetime.fromtimestamp(now).date()
