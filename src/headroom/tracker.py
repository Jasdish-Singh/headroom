"""Record samples and run a budgeted folder walk. This module never deletes files."""

from __future__ import annotations

import logging
import os
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path

from headroom.alerts import Alert
from headroom.config import Config
from headroom.formatutil import format_bytes, format_duration
from headroom.history import HistoryStore, Leak, find_leaks
from headroom.probe import Sample
from headroom.processes import ProcessSample, sample_processes
from headroom.walk import classify_path, list_child_names

TRACKED_PROCESSES = 8
PASS_INTERVAL_SECONDS = 6 * 3600


def tracking_roots(config: Config, home: Path | None = None) -> list[Path]:
    """Folders to snapshot. An empty config means the user profile, read-only."""
    home_dir = Path.home() if home is None else home
    if not config.tracking_paths:
        return [home_dir]
    roots: list[Path] = []
    for raw in config.tracking_paths:
        expanded = os.path.expandvars(os.path.expanduser(raw))
        roots.append(Path(expanded))
    return roots


def fast_drop_amount(
    previous_free: int,
    free: int,
    delta_t: float,
    config: Config,
) -> int | None:
    """Bytes lost, or None when the drop is too small or the gap is too long."""
    if delta_t <= 0:
        return None
    if delta_t > config.fast_drop_window_seconds or delta_t > config.fast_drop_max_gap_seconds:
        return None
    lost = previous_free - free
    if lost >= config.fast_drop_bytes:
        return lost
    return None


class DeepScanner:
    """Breadth-first size snapshot. One tick visits a few directories, then stops.

    Sizes roll up to the top-level folder under each root. Files that sit
    directly in a root roll up to that root. Links are not opened.
    """

    def __init__(
        self,
        roots: list[Path],
        *,
        large_file_bytes: int,
        top_folders: int,
        pass_interval: float = PASS_INTERVAL_SECONDS,
    ) -> None:
        self.roots = list(roots)
        self.large_file_bytes = large_file_bytes
        self.top_folders = max(1, top_folders)
        self.pass_interval = pass_interval
        self.queue: deque[tuple[Path, str | None]] = deque()
        self.buckets: dict[str, int] = {}
        self.large: dict[str, int] = {}
        self.started = False
        self.waiting = False
        self.next_pass_at = 0.0

    def step(
        self,
        *,
        max_dirs: int,
        max_seconds: float,
        now: float,
        store: HistoryStore,
        clock: Callable[[], float] = time.monotonic,
    ) -> bool:
        """Visit up to ``max_dirs`` directories or until ``max_seconds`` elapses.

        Returns True when a snapshot was written.
        """
        if self.waiting and now < self.next_pass_at:
            return False
        if not self.started:
            self._begin(now)
            if not self.started:
                return False
        deadline = clock() + max(0.0, max_seconds)
        visited = 0
        while self.queue and visited < max_dirs and clock() < deadline:
            path, bucket = self.queue.popleft()
            self._visit(path, bucket)
            visited += 1
        if self.started and not self.queue:
            ranked = sorted(self.buckets.items(), key=lambda item: (-item[1], item[0]))
            store.record_folders(now, dict(ranked[: self.top_folders]))
            store.record_large_files(now, dict(self.large))
            self.started = False
            self.waiting = True
            self.next_pass_at = now + self.pass_interval
            self.buckets.clear()
            self.large.clear()
            return True
        return False

    def _begin(self, now: float) -> None:
        self.queue.clear()
        self.buckets = {}
        self.large = {}
        for root in self.roots:
            try:
                kind = classify_path(root)
            except OSError:
                continue
            if kind == "dir":
                self.queue.append((root, None))
        if not self.queue:
            self.started = False
            self.waiting = True
            self.next_pass_at = now + self.pass_interval
            return
        self.started = True
        self.waiting = False

    def _visit(self, path: Path, bucket: str | None) -> None:
        try:
            names = list_child_names(path)
        except OSError:
            return
        if names is None:
            return
        for name in names:
            child = path / name
            try:
                kind = classify_path(child)
            except OSError:
                continue
            if kind == "link":
                continue
            if kind == "file":
                try:
                    size = child.lstat().st_size
                except OSError:
                    continue
                key = str(path) if bucket is None else bucket
                self.buckets[key] = self.buckets.get(key, 0) + size
                if size >= self.large_file_bytes:
                    self.large[str(child)] = size
            elif kind == "dir":
                child_bucket = str(child) if bucket is None else bucket
                self.queue.append((child, child_bucket))


class Tracker:
    """Write one cheap sample, and occasionally a small piece of a folder walk."""

    def __init__(
        self,
        store: HistoryStore,
        config: Config,
        *,
        logger: logging.Logger | None = None,
        process_reader: Callable[[], list[ProcessSample]] | None = None,
        scanner: DeepScanner | None = None,
    ) -> None:
        self.store = store
        self.config = config
        self.logger = logger or logging.getLogger("headroom")
        self.process_reader = process_reader or (lambda: sample_processes(TRACKED_PROCESSES))
        self.scanner = scanner
        self._ticks = 0
        self._last_disk: dict[str, tuple[float, int]] = {}
        self._drop_alerted: dict[str, tuple[float, int]] = {}
        self._leak_open: set[tuple[str, float]] = set()

    def observe(
        self,
        sample: Sample,
        *,
        resumed: bool,
        deep: bool,
        idle_seconds: float | None,
        slow_cadence: bool,
    ) -> list[Alert]:
        alerts: list[Alert] = []
        try:
            self.store.record_sample(sample)
        except Exception:
            self.logger.exception("could not record sample")
            return alerts
        alerts.extend(self._disk_alerts(sample, resumed=resumed))
        every = self.config.process_every if self.config.process_every > 0 else 1
        if self._ticks % every == 0:
            alerts.extend(self._process_alerts(sample.captured_at))
        self._ticks += 1
        if deep and self._should_scan(idle_seconds, slow_cadence):
            self._scan(sample.captured_at)
        try:
            self.store.trim(self.config.history_max_bytes)
        except Exception:
            self.logger.exception("could not trim history")
        return alerts

    def _should_scan(self, idle_seconds: float | None, slow_cadence: bool) -> bool:
        if idle_seconds is None:
            return slow_cadence
        return idle_seconds >= self.config.idle_after_seconds

    def _disk_alerts(self, sample: Sample, *, resumed: bool) -> list[Alert]:
        alerts: list[Alert] = []
        watched = set(self.config.drives)
        seen: set[str] = set()
        for disk in sample.disks:
            seen.add(disk.key)
            previous = self._last_disk.get(disk.key)
            self._last_disk[disk.key] = (sample.captured_at, disk.free)
            if disk.key not in watched or previous is None or resumed:
                continue
            prev_ts, prev_free = previous
            lost = fast_drop_amount(
                prev_free,
                disk.free,
                sample.captured_at - prev_ts,
                self.config,
            )
            alerted = self._drop_alerted.get(disk.key)
            if alerted is not None:
                alert_ts, alert_free = alerted
                recovered = disk.free > alert_free
                aged = sample.captured_at - alert_ts >= self.config.fast_drop_window_seconds
                if not recovered and not aged:
                    continue
                self._drop_alerted.pop(disk.key, None)
            if lost is None:
                continue
            self._drop_alerted[disk.key] = (sample.captured_at, disk.free)
            alerts.append(
                Alert(
                    level="warning",
                    code="disk_fast_drop",
                    title="Headroom: disk space dropped",
                    message=(
                        f"{disk.key} lost {format_bytes(lost)} in "
                        f"{format_duration(max(1, int(sample.captured_at - prev_ts)))} "
                        f"({format_bytes(disk.free)} free)."
                    ),
                )
            )
        for drive in list(self._last_disk):
            if drive not in seen:
                self._last_disk.pop(drive, None)
        return alerts

    def _process_alerts(self, now: float) -> list[Alert]:
        try:
            rows = self.process_reader()
            self.store.record_processes(now, rows)
            points = self.store.processes_between(now - self.config.leak_window_seconds, now + 1)
            leaks = find_leaks(
                points,
                now=now,
                window_seconds=self.config.leak_window_seconds,
                growth_bytes=self.config.leak_growth_bytes,
            )
        except Exception:
            self.logger.exception("could not record processes")
            return []
        current: set[tuple[str, float]] = set()
        alerts: list[Alert] = []
        for leak in leaks:
            key = (leak.name, leak.create_time)
            current.add(key)
            if key in self._leak_open:
                continue
            self._leak_open.add(key)
            alerts.append(_leak_alert(leak))
        self._leak_open &= current
        return alerts

    def _scan(self, now: float) -> None:
        if self.scanner is None:
            return
        try:
            finished = self.scanner.step(
                max_dirs=self.config.deep_scan_dirs_per_tick,
                max_seconds=self.config.deep_scan_budget_seconds,
                now=now,
                store=self.store,
            )
        except Exception:
            self.logger.exception("folder snapshot failed")
            return
        if finished:
            self.logger.info("folder snapshot written")


def _leak_alert(leak: Leak) -> Alert:
    return Alert(
        level="warning",
        code="process_leak",
        title="Headroom: memory grew",
        message=(
            f"{leak.name} (pid {leak.pid}) grew {format_bytes(leak.growth)} "
            f"over {format_duration(max(1, int(leak.span_seconds)))} "
            f"and is still using {format_bytes(leak.end_rss)}."
        ),
    )
