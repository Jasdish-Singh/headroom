"""Cheap decisions for the monitor loop: interval, battery, idle, resume, priority."""

from __future__ import annotations

import ctypes
import os

import psutil

from headroom.config import Config
from headroom.probe import Sample

_priority_lowered = False
_BELOW_NORMAL_PRIORITY_CLASS = 0x00004000


def choose_interval(
    config: Config,
    *,
    on_battery: bool,
    idle_seconds: float | None,
    under_pressure: bool,
) -> int:
    """Pick the next sleep. Pressure wins over battery and idle."""
    if under_pressure:
        return config.pressure_interval_seconds
    if on_battery:
        return max(config.interval_seconds, config.battery_interval_seconds)
    if idle_seconds is not None and idle_seconds >= config.idle_after_seconds:
        return max(config.interval_seconds, config.idle_interval_seconds)
    return config.interval_seconds


def under_pressure(sample: Sample, config: Config) -> bool:
    """True when RAM is over the line or a watched drive is under the warning line."""
    if sample.memory.ram_percent > config.ram_percent:
        return True
    watched = set(config.drives)
    for disk in sample.disks:
        if disk.key in watched and disk.free < config.disk_warn_free_bytes:
            return True
    return False


def on_battery(reader=None) -> bool:
    """True only when a battery is present and unplugged. Unknown means plugged in."""
    read = psutil.sensors_battery if reader is None else reader
    try:
        battery = read()
    except Exception:
        return False
    if battery is None:
        return False
    plugged = getattr(battery, "power_plugged", None)
    if plugged is None:
        return False
    return not bool(plugged)


def idle_seconds() -> float | None:
    """Seconds since the last keyboard or mouse input. None when the OS will not say."""
    if os.name != "nt":
        return None
    try:
        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = (("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint))

        info = LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(info)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return None
        tick = ctypes.windll.kernel32.GetTickCount()
        elapsed_ms = (int(tick) - int(info.dwTime)) & 0xFFFFFFFF
        return elapsed_ms / 1000.0
    except Exception:
        return None


def detect_resume(
    previous_wall: float,
    previous_mono: float,
    wall: float,
    mono: float,
    slack: float = 15.0,
) -> bool:
    """True when wall time jumped ahead of the monotonic clock (sleep or hibernate)."""
    return (wall - previous_wall) > (mono - previous_mono) + slack


def lower_priority() -> None:
    """Drop this process to below-normal priority. Safe to call more than once."""
    global _priority_lowered
    if _priority_lowered:
        return
    _priority_lowered = True
    try:
        if os.name == "nt":
            kernel = ctypes.windll.kernel32
            handle = kernel.GetCurrentProcess()
            kernel.SetPriorityClass(handle, _BELOW_NORMAL_PRIORITY_CLASS)
        else:
            os.nice(10)
    except Exception:
        return
