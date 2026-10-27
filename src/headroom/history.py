"""Small rotating SQLite history of RAM, commit, disks, folders, and large files.

Each write is a short transaction that is committed before the call returns.
The file stays in WAL mode so another process can read those rows immediately.
A passive checkpoint then copies them into the main database when it can.
Trim drops the oldest rows when the file (plus its WAL) is over the cap.
It does not VACUUM on the sample path.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from headroom.config import Config
from headroom.logsetup import default_log_path
from headroom.probe import Sample

MIN_TREND_SPAN_SECONDS = 6 * 3600

_SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    ts REAL PRIMARY KEY,
    ram_percent REAL NOT NULL,
    ram_used INTEGER NOT NULL,
    ram_total INTEGER NOT NULL,
    commit_percent REAL NOT NULL,
    commit_used INTEGER NOT NULL,
    commit_total INTEGER NOT NULL,
    page_used INTEGER NOT NULL,
    page_total INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS disks (
    ts REAL NOT NULL,
    drive TEXT NOT NULL,
    free INTEGER NOT NULL,
    total INTEGER NOT NULL,
    PRIMARY KEY (ts, drive)
);
CREATE TABLE IF NOT EXISTS processes (
    ts REAL NOT NULL,
    pid INTEGER NOT NULL,
    name TEXT NOT NULL,
    rss INTEGER NOT NULL,
    create_time REAL NOT NULL,
    PRIMARY KEY (ts, pid, name)
);
CREATE TABLE IF NOT EXISTS folders (
    path TEXT NOT NULL,
    ts REAL NOT NULL,
    bytes INTEGER NOT NULL,
    PRIMARY KEY (path, ts)
);
CREATE TABLE IF NOT EXISTS large_files (
    path TEXT PRIMARY KEY,
    bytes INTEGER NOT NULL,
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL
);
"""


def default_history_path(env: Mapping[str, str] | None = None, home: Path | None = None) -> Path:
    environ = os.environ if env is None else env
    override = environ.get("HEADROOM_DB")
    if override:
        return Path(override)
    return default_log_path(env, home).parent / "history.db"


@dataclass(frozen=True)
class ProcessPoint:
    ts: float
    pid: int
    name: str
    rss: int
    create_time: float


@dataclass(frozen=True)
class DiskTrend:
    drive: str
    bytes_per_day: float
    days_until_full: float | None
    span_seconds: float
    samples: int
    latest_free: int
    enough: bool


@dataclass(frozen=True)
class FolderChange:
    path: str
    before: int
    after: int

    @property
    def delta(self) -> int:
        return self.after - self.before


@dataclass(frozen=True)
class Leak:
    name: str
    pid: int
    create_time: float
    start_rss: int
    end_rss: int
    growth: int
    span_seconds: float


@dataclass(frozen=True)
class LargeFile:
    path: str
    bytes: int
    first_seen: float


@dataclass(frozen=True)
class ChangeReport:
    window_seconds: float
    folder_changes: tuple[FolderChange, ...]
    earlier_ts: float | None
    latest_ts: float | None
    new_files: tuple[LargeFile, ...]
    note: str | None


@dataclass(frozen=True)
class Report:
    path: Path
    db_bytes: int
    cap_bytes: int
    sample_count: int
    newest_ts: float | None
    ram_percent: float | None
    ram_used: int | None
    ram_total: int | None
    commit_percent: float | None
    commit_used: int | None
    commit_total: int | None
    page_used: int | None
    page_total: int | None
    disk_free: tuple[tuple[str, int, int], ...]
    trends: tuple[DiskTrend, ...]
    leaks: tuple[Leak, ...]
    changes: ChangeReport


class HistoryStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fresh = (not self.path.exists()) or self.path.stat().st_size == 0
        # Autocommit stops the driver from leaving a deferred transaction open.
        # The tray runs the watch loop on a different thread than the one that
        # opened this connection, so the connection is shared under a lock.
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            self.path,
            isolation_level=None,
            check_same_thread=False,
            timeout=5.0,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout = 5000")
        if fresh:
            self._conn.execute("PRAGMA auto_vacuum = INCREMENTAL")
        mode = self._conn.execute("PRAGMA journal_mode = WAL").fetchone()
        if mode is None or str(mode[0]).lower() != "wal":
            mode = self._conn.execute("PRAGMA journal_mode = WAL").fetchone()
        if mode is None or str(mode[0]).lower() != "wal":
            raise sqlite3.OperationalError("history database did not switch to WAL mode")
        self._conn.execute("PRAGMA synchronous = NORMAL")
        self._conn.executescript(_SCHEMA)
        self._checkpoint()

    def close(self) -> None:
        with self._lock:
            try:
                self._checkpoint()
            except sqlite3.Error:
                pass
            self._conn.close()

    def record_sample(self, sample: Sample) -> None:
        memory = sample.memory
        ts = float(sample.captured_at)
        with self._lock:
            self._begin()
            try:
                self._conn.execute(
                    """
                    INSERT OR REPLACE INTO samples (
                        ts, ram_percent, ram_used, ram_total,
                        commit_percent, commit_used, commit_total,
                        page_used, page_total
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        ts,
                        memory.ram_percent,
                        memory.ram_used,
                        memory.ram_total,
                        memory.commit_percent,
                        memory.commit_used,
                        memory.commit_total,
                        memory.pagefile_used,
                        memory.pagefile_total,
                    ),
                )
                for disk in sample.disks:
                    self._conn.execute(
                        "INSERT OR REPLACE INTO disks (ts, drive, free, total) VALUES (?, ?, ?, ?)",
                        (ts, disk.key, int(disk.free), int(disk.total)),
                    )
                self._commit()
            except Exception:
                self._rollback()
                raise

    def _begin(self) -> None:
        self._conn.execute("BEGIN IMMEDIATE")

    def _commit(self) -> None:
        self._conn.execute("COMMIT")
        self._checkpoint()

    def _rollback(self) -> None:
        try:
            self._conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass

    def _checkpoint(self) -> None:
        """Copy committed frames into the main file when no reader is blocking."""
        try:
            self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        except sqlite3.Error:
            pass

    def record_processes(self, ts: float, rows: list) -> None:
        with self._lock:
            self._begin()
            try:
                for row in rows:
                    self._conn.execute(
                        """
                        INSERT OR REPLACE INTO processes (ts, pid, name, rss, create_time)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (float(ts), int(row.pid), str(row.name), int(row.rss_bytes), float(row.create_time)),
                    )
                self._commit()
            except Exception:
                self._rollback()
                raise

    def record_folders(self, ts: float, sizes: Mapping[str, int]) -> None:
        with self._lock:
            self._begin()
            try:
                for path, size in sizes.items():
                    self._conn.execute(
                        "INSERT OR REPLACE INTO folders (path, ts, bytes) VALUES (?, ?, ?)",
                        (str(path), float(ts), int(size)),
                    )
                self._commit()
            except Exception:
                self._rollback()
                raise

    def record_large_files(self, ts: float, files: Mapping[str, int]) -> None:
        with self._lock:
            self._begin()
            try:
                for path, size in files.items():
                    existing = self._conn.execute(
                        "SELECT first_seen FROM large_files WHERE path = ?",
                        (str(path),),
                    ).fetchone()
                    if existing is None:
                        self._conn.execute(
                            """
                            INSERT INTO large_files (path, bytes, first_seen, last_seen)
                            VALUES (?, ?, ?, ?)
                            """,
                            (str(path), int(size), float(ts), float(ts)),
                        )
                    else:
                        self._conn.execute(
                            "UPDATE large_files SET bytes = ?, last_seen = ? WHERE path = ?",
                            (int(size), float(ts), str(path)),
                        )
                self._commit()
            except Exception:
                self._rollback()
                raise

    def byte_size(self) -> int:
        total = 0
        for candidate in (self.path, Path(str(self.path) + "-wal"), Path(str(self.path) + "-shm")):
            try:
                total += candidate.stat().st_size
            except OSError:
                continue
        return total

    def trim(self, max_bytes: int) -> int:
        """Drop the oldest quarter of each table until the file is under the cap."""
        if max_bytes < 0 or self.byte_size() <= max_bytes:
            return 0
        with self._lock:
            removed = 0
            for _ in range(8):
                if self.byte_size() <= max_bytes:
                    break
                step = 0
                for table, column in (
                    ("samples", "ts"),
                    ("disks", "ts"),
                    ("processes", "ts"),
                    ("folders", "ts"),
                    ("large_files", "last_seen"),
                ):
                    step += self._drop_oldest(table, column)
                removed += step
                if step == 0:
                    break
            try:
                self._conn.execute("PRAGMA incremental_vacuum")
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.DatabaseError:
                pass
            return removed

    def count_samples(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) FROM samples").fetchone()
        return int(row[0] if row is not None else 0)

    def count_rows(self, table: str) -> int:
        if table not in {"samples", "disks", "processes", "folders", "large_files"}:
            raise ValueError(table)
        with self._lock:
            row = self._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        return int(row[0] if row is not None else 0)

    def samples_between(self, start: float, end: float) -> list[sqlite3.Row]:
        with self._lock:
            return list(
                self._conn.execute(
                    "SELECT * FROM samples WHERE ts >= ? AND ts <= ? ORDER BY ts ASC",
                    (float(start), float(end)),
                )
            )

    def disks_at(self, ts: float) -> list[tuple[str, int, int]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT drive, free, total FROM disks WHERE ts = ? ORDER BY drive ASC",
                (float(ts),),
            ).fetchall()
        return [(str(row[0]), int(row[1]), int(row[2])) for row in rows]

    def disk_points(self, drive: str) -> list[tuple[float, int]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts, free FROM disks WHERE drive = ? ORDER BY ts ASC",
                (drive,),
            ).fetchall()
        return [(float(row[0]), int(row[1])) for row in rows]

    def drives(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute("SELECT DISTINCT drive FROM disks ORDER BY drive ASC").fetchall()
        return [str(row[0]) for row in rows]

    def processes_between(self, start: float, end: float) -> list[ProcessPoint]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT ts, pid, name, rss, create_time
                FROM processes
                WHERE ts >= ? AND ts <= ?
                ORDER BY ts ASC
                """,
                (float(start), float(end)),
            ).fetchall()
        return [
            ProcessPoint(float(row[0]), int(row[1]), str(row[2]), int(row[3]), float(row[4]))
            for row in rows
        ]

    def latest_folder_ts(self, at_or_before: float | None = None) -> float | None:
        with self._lock:
            if at_or_before is None:
                row = self._conn.execute("SELECT MAX(ts) FROM folders").fetchone()
            else:
                row = self._conn.execute(
                    "SELECT MAX(ts) FROM folders WHERE ts <= ?",
                    (float(at_or_before),),
                ).fetchone()
        if row is None or row[0] is None:
            return None
        return float(row[0])

    def folders_at(self, ts: float) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT path, bytes FROM folders WHERE ts = ?",
                (float(ts),),
            ).fetchall()
        return {str(row[0]): int(row[1]) for row in rows}

    def large_files_since(self, ts: float) -> list[LargeFile]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT path, bytes, first_seen
                FROM large_files
                WHERE first_seen >= ?
                ORDER BY bytes DESC, path ASC
                """,
                (float(ts),),
            ).fetchall()
        return [LargeFile(str(row[0]), int(row[1]), float(row[2])) for row in rows]

    def _drop_oldest(self, table: str, column: str) -> int:
        row = self._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        count = int(row[0] if row is not None else 0)
        if count == 0:
            return 0
        drop = max(1, count // 4)
        cursor = self._conn.execute(
            f"""
            DELETE FROM {table}
            WHERE rowid IN (
                SELECT rowid FROM {table} ORDER BY {column} ASC, rowid ASC LIMIT ?
            )
            """,
            (drop,),
        )
        return int(cursor.rowcount or 0)


def linear_slope(points: list[tuple[float, float]]) -> float:
    """Bytes per second. ``points`` are ``(timestamp, free_bytes)``."""
    count = len(points)
    if count < 2:
        return 0.0
    mean_x = sum(point[0] for point in points) / count
    mean_y = sum(point[1] for point in points) / count
    numerator = sum((point[0] - mean_x) * (point[1] - mean_y) for point in points)
    denominator = sum((point[0] - mean_x) ** 2 for point in points)
    if denominator == 0:
        return 0.0
    return numerator / denominator


def disk_trend(
    drive: str,
    points: list[tuple[float, int]],
    *,
    min_span: float = MIN_TREND_SPAN_SECONDS,
) -> DiskTrend:
    latest = points[-1][1] if points else 0
    if len(points) < 2:
        return DiskTrend(drive, 0.0, None, 0.0, len(points), latest, False)
    span = points[-1][0] - points[0][0]
    if span < min_span:
        return DiskTrend(drive, 0.0, None, span, len(points), latest, False)
    per_second = linear_slope([(point[0], float(point[1])) for point in points])
    per_day = per_second * 86400
    days: float | None = None
    if per_day < 0 and latest > 0:
        days = latest / -per_day
    return DiskTrend(drive, per_day, days, span, len(points), latest, True)


def folder_diff(before: Mapping[str, int], after: Mapping[str, int]) -> list[FolderChange]:
    changes: list[FolderChange] = []
    for path, size in after.items():
        previous = int(before.get(path, 0))
        current = int(size)
        if current > previous:
            changes.append(FolderChange(path, previous, current))
    changes.sort(key=lambda item: (-item.delta, item.path))
    return changes


def find_leaks(
    points: list[ProcessPoint],
    *,
    now: float,
    window_seconds: float,
    growth_bytes: int,
) -> list[Leak]:
    """Flag identities whose RSS rose by ``growth_bytes`` and is still near the peak."""
    groups: dict[tuple[str, float], list[ProcessPoint]] = {}
    start = now - window_seconds
    for point in points:
        if point.ts < start or point.ts > now:
            continue
        key = (point.name, round(point.create_time, 2))
        groups.setdefault(key, []).append(point)
    leaks: list[Leak] = []
    for (name, created), rows in groups.items():
        rows.sort(key=lambda item: item.ts)
        if len(rows) < 2 or rows[-1].ts <= rows[0].ts:
            continue
        start_rss = rows[0].rss
        end_rss = rows[-1].rss
        peak = max(row.rss for row in rows)
        growth = end_rss - start_rss
        if growth >= growth_bytes and peak > 0 and end_rss >= peak * 0.9:
            leaks.append(
                Leak(
                    name=name,
                    pid=rows[-1].pid,
                    create_time=created,
                    start_rss=start_rss,
                    end_rss=end_rss,
                    growth=growth,
                    span_seconds=rows[-1].ts - rows[0].ts,
                )
            )
    leaks.sort(key=lambda item: (-item.growth, item.name, item.pid))
    return leaks


def build_changes(store: HistoryStore, *, now: float, window_seconds: float) -> ChangeReport:
    latest = store.latest_folder_ts()
    cutoff = now - window_seconds
    earlier = store.latest_folder_ts(cutoff)
    files = tuple(store.large_files_since(cutoff))
    if latest is None:
        return ChangeReport(window_seconds, (), None, None, files, "No folder snapshots yet.")
    if earlier is None:
        return ChangeReport(
            window_seconds,
            (),
            None,
            latest,
            files,
            "No folder snapshot from the start of this window yet.",
        )
    if earlier == latest:
        return ChangeReport(
            window_seconds,
            (),
            earlier,
            latest,
            files,
            "Only one folder snapshot so far.",
        )
    changes = tuple(folder_diff(store.folders_at(earlier), store.folders_at(latest)))
    return ChangeReport(window_seconds, changes, earlier, latest, files, None)


def build_report(store: HistoryStore, config: Config, *, now: float) -> Report:
    count = store.count_samples()
    newest = store.samples_between(0, now + 1)
    row = newest[-1] if newest else None
    disk_free: tuple[tuple[str, int, int], ...] = ()
    trends_list: list[DiskTrend] = []
    if row is not None:
        disk_free = tuple(store.disks_at(float(row["ts"])))
    drives = list(dict.fromkeys([*config.drives, *store.drives()]))
    for drive in drives:
        trends_list.append(disk_trend(drive, store.disk_points(drive)))
    points = store.processes_between(now - config.leak_window_seconds, now + 1)
    leaks = tuple(
        find_leaks(
            points,
            now=now,
            window_seconds=config.leak_window_seconds,
            growth_bytes=config.leak_growth_bytes,
        )
    )
    changes = build_changes(store, now=now, window_seconds=86400)
    return Report(
        path=store.path,
        db_bytes=store.byte_size(),
        cap_bytes=config.history_max_bytes,
        sample_count=count,
        newest_ts=None if row is None else float(row["ts"]),
        ram_percent=None if row is None else float(row["ram_percent"]),
        ram_used=None if row is None else int(row["ram_used"]),
        ram_total=None if row is None else int(row["ram_total"]),
        commit_percent=None if row is None else float(row["commit_percent"]),
        commit_used=None if row is None else int(row["commit_used"]),
        commit_total=None if row is None else int(row["commit_total"]),
        page_used=None if row is None else int(row["page_used"]),
        page_total=None if row is None else int(row["page_total"]),
        disk_free=disk_free,
        trends=tuple(trends_list),
        leaks=leaks,
        changes=changes,
    )
