"""Byte and duration formatting. Sizes are powers of 1024, labeled GB."""

from __future__ import annotations


def format_bytes(num: int | float) -> str:
    """Format a byte count using 1024-based units."""
    value = float(num)
    sign = "-" if value < 0 else ""
    value = abs(value)
    units = ("B", "KB", "MB", "GB", "TB", "PB")
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{sign}{int(value)} B"
            return f"{sign}{value:.1f} {unit}"
        value /= 1024
    return f"{sign}{value:.1f} PB"


def format_percent(value: float) -> str:
    return f"{value:.1f}%"


def format_duration(seconds: int) -> str:
    if seconds >= 60 and seconds % 60 == 0:
        minutes = seconds // 60
        unit = "minute" if minutes == 1 else "minutes"
        return f"{minutes} {unit}"
    unit = "second" if seconds == 1 else "seconds"
    return f"{seconds} {unit}"
