"""RAM, commit charge, page file, and per-drive free space."""

from __future__ import annotations

import ctypes
import os
from dataclasses import dataclass
from pathlib import Path

import psutil

from headroom.config import normalize_drive


@dataclass(frozen=True)
class MemoryStatus:
    ram_total: int
    ram_available: int
    ram_used: int
    ram_percent: float
    commit_total: int
    commit_used: int
    commit_percent: float
    commit_estimated: bool
    pagefile_total: int
    pagefile_used: int
    pagefile_percent: float


@dataclass(frozen=True)
class DiskStatus:
    mount: str
    device: str
    total: int
    used: int
    free: int
    key: str


@dataclass(frozen=True)
class Sample:
    memory: MemoryStatus
    disks: tuple[DiskStatus, ...]
    captured_at: float


class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = (
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    )


def commit_from_memory_status(total_page_file: int, avail_page_file: int) -> tuple[int, int]:
    """Return ``(commit used, commit limit)`` from GlobalMemoryStatusEx counters."""
    total = max(0, int(total_page_file))
    available = max(0, int(avail_page_file))
    used = total - available
    if used < 0:
        used = 0
    return used, total


def parse_proc_meminfo(text: str) -> tuple[int, int] | None:
    """Parse Linux ``/proc/meminfo`` commit fields. Values are returned in bytes."""
    values: dict[str, int] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, rest = line.split(":", 1)
        bits = rest.split()
        if not bits or key not in {"CommitLimit", "Committed_AS"}:
            continue
        try:
            values[key] = int(bits[0]) * 1024
        except ValueError:
            return None
    if "CommitLimit" not in values or "Committed_AS" not in values:
        return None
    return values["Committed_AS"], values["CommitLimit"]


def read_commit_counters() -> tuple[int, int, bool]:
    """Return ``(used, total, estimated)``.

    On Windows the counters come from ``GlobalMemoryStatusEx``. On Linux they
    come from ``/proc/meminfo``. ``estimated`` is true when those sources are
    unavailable and the caller should add RAM and page-file usage instead.
    """
    if os.name == "nt":
        windows = _windows_commit()
        if windows is not None:
            return windows[0], windows[1], False
        return 0, 0, True
    text = _read_meminfo_text()
    if text is None:
        return 0, 0, True
    parsed = parse_proc_meminfo(text)
    if parsed is None:
        return 0, 0, True
    return parsed[0], parsed[1], False


def sample() -> Sample:
    """Read one snapshot. Safe to call from the watch loop."""
    import time

    memory = psutil.virtual_memory()
    try:
        swap = psutil.swap_memory()
        page_total = int(swap.total)
        page_used = int(swap.used)
        page_percent = float(swap.percent) if page_total else 0.0
    except (RuntimeError, OSError):
        # The page-file counters can be unavailable (for example on some CI runners).
        page_total = page_used = 0
        page_percent = 0.0
    ram_total = int(memory.total)
    ram_available = int(memory.available)
    ram_used = int(getattr(memory, "used", ram_total - ram_available))
    ram_percent = float(memory.percent)
    commit_used, commit_total, estimated = read_commit_counters()
    if estimated:
        commit_used = max(0, ram_used) + max(0, page_used)
        commit_total = max(0, ram_total) + max(0, page_total)
    disks: list[DiskStatus] = []
    for part in psutil.disk_partitions(all=False):
        mount = part.mountpoint
        try:
            usage = psutil.disk_usage(mount)
        except (OSError, PermissionError, SystemError):
            continue
        total = int(usage.total)
        free = int(usage.free)
        used = int(usage.used)
        disks.append(
            DiskStatus(
                mount=mount,
                device=str(getattr(part, "device", "") or ""),
                total=total,
                used=used,
                free=free,
                key=normalize_drive(mount),
            )
        )
    disks.sort(key=lambda item: item.mount.lower())
    return Sample(
        memory=MemoryStatus(
            ram_total=ram_total,
            ram_available=ram_available,
            ram_used=ram_used,
            ram_percent=ram_percent,
            commit_total=commit_total,
            commit_used=commit_used,
            commit_percent=_percent(commit_used, commit_total),
            commit_estimated=estimated,
            pagefile_total=page_total,
            pagefile_used=page_used,
            pagefile_percent=page_percent,
        ),
        disks=tuple(disks),
        captured_at=time.time(),
    )


def _percent(used: int, total: int) -> float:
    if total <= 0:
        return 0.0
    return used / total * 100


def _read_meminfo_text() -> str | None:
    try:
        return Path("/proc/meminfo").read_text(encoding="utf-8")
    except OSError:
        return None


def _windows_commit() -> tuple[int, int] | None:
    if os.name != "nt":
        return None
    status = MEMORYSTATUSEX()
    status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    try:
        ok = ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
    except (AttributeError, OSError):
        return None
    if not ok:
        return None
    return commit_from_memory_status(status.ullTotalPageFile, status.ullAvailPageFile)
