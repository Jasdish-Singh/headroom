"""CPU and RAM for this process, and for the watch process named in the lock file."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import psutil

from headroom.instance import read_lock_pid

RSS_BUDGET_BYTES = 40 * 1024 * 1024
CPU_BUDGET_PERCENT = 1.0
PET_RSS_BUDGET_BYTES = 30 * 1024 * 1024
PET_CPU_BUDGET_PERCENT = 2.0


@dataclass(frozen=True)
class SelfUsage:
    pid: int
    rss_bytes: int
    cpu_percent: float


def read_self(interval: float | None = 0.2) -> SelfUsage:
    proc = psutil.Process()
    cpu = float(proc.cpu_percent(interval=interval))
    rss = int(proc.memory_info().rss)
    return SelfUsage(pid=proc.pid, rss_bytes=rss, cpu_percent=cpu)


def read_agent(lock_path: Path, *, interval: float | None = 0.2) -> SelfUsage | None:
    """RSS and CPU of the locked pid when that process is still Headroom."""
    pid = read_lock_pid(lock_path)
    if pid is None or pid == os_pid():
        return None
    try:
        proc = psutil.Process(pid)
        if not proc.is_running():
            return None
        command = " ".join(proc.cmdline()).lower()
        if "headroom" not in command:
            return None
        cpu = float(proc.cpu_percent(interval=interval))
        rss = int(proc.memory_info().rss)
    except (psutil.Error, OSError, ValueError):
        return None
    return SelfUsage(pid=pid, rss_bytes=rss, cpu_percent=cpu)


def read_pet(directory: Path, *, interval: float | None = 0.2) -> SelfUsage | None:
    """RSS and CPU of Roomy when ``pet.port`` names a live Headroom process."""
    path = directory / "pet.port"
    if not path.is_file():
        return None
    try:
        parts = path.read_text(encoding="utf-8").split()
        pid = int(parts[1] if len(parts) > 1 else parts[0])
    except (OSError, ValueError, IndexError):
        return None
    if pid == os_pid():
        return None
    try:
        proc = psutil.Process(pid)
        if not proc.is_running():
            return None
        command = " ".join(proc.cmdline()).lower()
        if "headroom" not in command:
            return None
        cpu = float(proc.cpu_percent(interval=interval))
        rss = int(proc.memory_info().rss)
    except (psutil.Error, OSError, ValueError):
        return None
    return SelfUsage(pid=pid, rss_bytes=rss, cpu_percent=cpu)


def os_pid() -> int:
    return psutil.Process().pid
