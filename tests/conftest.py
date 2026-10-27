"""Shared fixtures. Tests never read the developer machine's real temp or home."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from headroom.probe import DiskStatus, MemoryStatus, Sample

GB = 1024**3


@pytest.fixture(autouse=True)
def isolate_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    home = tmp_path / "home"
    local = tmp_path / "local"
    roaming = tmp_path / "roaming"
    temp = tmp_path / "Temp"
    for directory in (home, local, roaming, temp):
        directory.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setenv("APPDATA", str(roaming))
    monkeypatch.setenv("TEMP", str(temp))
    monkeypatch.setenv("TMP", str(temp))
    monkeypatch.delenv("SystemRoot", raising=False)
    monkeypatch.delenv("SYSTEMROOT", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.delenv("HEADROOM_DB", raising=False)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    return home


def make_sample(
    *,
    ram_percent: float = 50.0,
    ram_total: int = 16 * GB,
    commit_percent: float = 40.0,
    commit_total: int = 24 * GB,
    commit_estimated: bool = False,
    page_percent: float = 10.0,
    page_total: int = 8 * GB,
    disks: tuple[DiskStatus, ...] | None = None,
    captured_at: float = 1_700_000_000.0,
) -> Sample:
    ram_used = int(ram_total * ram_percent / 100)
    if disks is None:
        disks = (
            DiskStatus(
                mount="C:\\",
                device="C:",
                total=477 * GB,
                used=(477 - 42) * GB,
                free=42 * GB,
                key="C:",
            ),
        )
    memory = MemoryStatus(
        ram_total=ram_total,
        ram_available=ram_total - ram_used,
        ram_used=ram_used,
        ram_percent=ram_percent,
        commit_total=commit_total,
        commit_used=int(commit_total * commit_percent / 100),
        commit_percent=commit_percent,
        commit_estimated=commit_estimated,
        pagefile_total=page_total,
        pagefile_used=int(page_total * page_percent / 100),
        pagefile_percent=page_percent,
    )
    return Sample(memory=memory, disks=tuple(disks), captured_at=captured_at)


def memory_logger() -> tuple[logging.Logger, list[str]]:
    logger = logging.getLogger(f"headroom.test.{id(object())}")
    logger.handlers.clear()
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    messages: list[str] = []

    class _Handler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            messages.append(record.getMessage())

    logger.addHandler(_Handler())
    return logger, messages
