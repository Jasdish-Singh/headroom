"""Top processes by resident memory. Access denied is skipped, not fatal."""

from __future__ import annotations

from dataclasses import dataclass

import psutil


@dataclass(frozen=True)
class ProcessMem:
    pid: int
    name: str
    rss_bytes: int


@dataclass(frozen=True)
class ProcessSample:
    pid: int
    name: str
    rss_bytes: int
    create_time: float


def top_processes(limit: int) -> list[ProcessMem]:
    if limit < 1:
        raise ValueError("limit must be at least 1")
    rows: list[ProcessMem] = []
    for proc in psutil.process_iter(["pid", "name", "memory_info"]):
        try:
            info = proc.info
            if not isinstance(info, dict):
                continue
            memory = info.get("memory_info")
            if memory is None:
                continue
            rss = int(memory.rss)
            name = info.get("name") or "?"
            name = " ".join(str(name).split()) or "?"
            pid = int(info["pid"])
        except (psutil.Error, KeyError, AttributeError, TypeError, ValueError):
            continue
        rows.append(ProcessMem(pid=pid, name=name, rss_bytes=max(0, rss)))
    rows.sort(key=lambda row: (-row.rss_bytes, row.name.lower(), row.pid))
    return rows[:limit]


def sample_processes(limit: int) -> list[ProcessSample]:
    """Top processes by RSS, including create time so a reused PID is not one life."""
    if limit < 1:
        raise ValueError("limit must be at least 1")
    rows: list[ProcessSample] = []
    for proc in psutil.process_iter(["pid", "name", "memory_info", "create_time"]):
        try:
            info = proc.info
            if not isinstance(info, dict):
                continue
            memory = info.get("memory_info")
            if memory is None:
                continue
            rss = int(memory.rss)
            name = info.get("name") or "?"
            name = " ".join(str(name).split()) or "?"
            pid = int(info["pid"])
            created = float(info.get("create_time") or 0.0)
        except (psutil.Error, KeyError, AttributeError, TypeError, ValueError):
            continue
        rows.append(
            ProcessSample(
                pid=pid,
                name=name,
                rss_bytes=max(0, rss),
                create_time=created,
            )
        )
    rows.sort(key=lambda row: (-row.rss_bytes, row.name.lower(), row.pid))
    return rows[:limit]
