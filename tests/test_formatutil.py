from __future__ import annotations

from headroom.formatutil import format_bytes, format_duration, format_percent


def test_format_bytes_boundaries() -> None:
    assert format_bytes(0) == "0 B"
    assert format_bytes(1023) == "1023 B"
    assert format_bytes(1024) == "1.0 KB"
    assert format_bytes(1024**2) == "1.0 MB"
    assert format_bytes(30 * 1024**3) == "30.0 GB"
    assert format_bytes(-2048) == "-2.0 KB"


def test_format_duration_and_percent() -> None:
    assert format_duration(0) == "0 seconds"
    assert format_duration(1) == "1 second"
    assert format_duration(90) == "90 seconds"
    assert format_duration(60) == "1 minute"
    assert format_duration(120) == "2 minutes"
    assert format_percent(85) == "85.0%"
