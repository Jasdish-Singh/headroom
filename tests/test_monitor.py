from __future__ import annotations

from types import SimpleNamespace

import psutil
import pytest

from headroom import probe
from headroom.alerts import AlertState, disk_pressure, evaluate
from headroom.config import Config
from headroom.notify import (
    LogNotifier,
    WindowsToastNotifier,
    build_notifiers,
    send_toast,
)
from headroom.probe import DiskStatus, commit_from_memory_status, parse_proc_meminfo
from headroom.processes import top_processes
from headroom.watch import run_watch_loop
from tests.conftest import GB, make_sample


def test_ram_alert_waits_for_the_sustained_window() -> None:
    state = AlertState()
    config = Config()
    assert evaluate(make_sample(ram_percent=90), state, config, now=1_000) == []
    assert state.ram_high_since == 1_000
    assert evaluate(make_sample(ram_percent=91), state, config, now=1_119) == []
    alerts = evaluate(make_sample(ram_percent=92), state, config, now=1_120)
    assert len(alerts) == 1
    assert alerts[0].code == "ram_sustained"
    assert alerts[0].level == "warning"
    assert "85.0%" in alerts[0].message
    assert "2 minutes" in alerts[0].message
    assert evaluate(make_sample(ram_percent=99), state, config, now=2_000) == []
    assert evaluate(make_sample(ram_percent=50), state, config, now=2_010) == []
    assert state.ram_high_since is None
    assert evaluate(make_sample(ram_percent=90), state, config, now=3_000) == []
    again = evaluate(make_sample(ram_percent=90), state, config, now=3_120)
    assert len(again) == 1


def test_ram_equal_to_the_line_does_not_alert() -> None:
    state = AlertState()
    alerts = evaluate(make_sample(ram_percent=85), state, Config(), now=0)
    assert alerts == []
    assert state.ram_high_since is None


def test_zero_second_window_alerts_immediately() -> None:
    state = AlertState()
    config = Config(ram_sustained_seconds=0)
    alerts = evaluate(make_sample(ram_percent=90), state, config, now=5)
    assert len(alerts) == 1


def test_disk_alerts_only_when_pressure_gets_worse() -> None:
    state = AlertState()
    config = Config()

    def sample(free: int, mount: str = "C:\\", key: str = "C:"):
        disk = DiskStatus(mount, key, 477 * GB, 477 * GB - free, free, key)
        return make_sample(disks=(disk,))

    assert evaluate(sample(40 * GB), state, config, now=0) == []
    warn = evaluate(sample(20 * GB), state, config, now=1)
    assert [alert.code for alert in warn] == ["disk_low"]
    assert evaluate(sample(20 * GB), state, config, now=2) == []
    critical = evaluate(sample(10 * GB), state, config, now=3)
    assert [alert.code for alert in critical] == ["disk_critical"]
    assert critical[0].level == "critical"
    assert evaluate(sample(10 * GB), state, config, now=4) == []
    assert evaluate(sample(20 * GB), state, config, now=5) == []
    assert evaluate(sample(40 * GB), state, config, now=6) == []
    again = evaluate(sample(10 * GB), state, config, now=7)
    assert [alert.code for alert in again] == ["disk_critical"]


def test_disk_lines_are_strict_and_unwatched_drives_are_ignored() -> None:
    config = Config()
    assert disk_pressure(30 * GB, config) == "ok"
    assert disk_pressure(30 * GB - 1, config) == "warn"
    assert disk_pressure(15 * GB, config) == "warn"
    assert disk_pressure(15 * GB - 1, config) == "critical"
    state = AlertState()
    disks = (
        DiskStatus("C:\\", "C:", 100, 10, 40 * GB, "C:"),
        DiskStatus("D:\\", "D:", 100, 90, 1 * GB, "D:"),
    )
    assert evaluate(make_sample(disks=disks), state, config, now=0) == []


def test_missing_watched_drive_does_not_alert() -> None:
    state = AlertState()
    disks = (DiskStatus("/data", "/data", 100, 50, 1 * GB, "/data"),)
    assert evaluate(make_sample(disks=disks), state, Config(), now=0) == []


def test_commit_math_and_proc_meminfo() -> None:
    assert commit_from_memory_status(100, 25) == (75, 100)
    assert commit_from_memory_status(10, 40) == (0, 10)
    assert commit_from_memory_status(-5, -1) == (0, 0)
    parsed = parse_proc_meminfo("CommitLimit:     2048 kB\nCommitted_AS:   512 kB\nMemTotal: 1 kB\n")
    assert parsed == (512 * 1024, 2048 * 1024)
    assert parse_proc_meminfo("MemTotal: 1 kB\n") is None


def test_probe_maps_mocked_psutil(monkeypatch: pytest.MonkeyPatch) -> None:
    memory = SimpleNamespace(total=16 * GB, available=4 * GB, used=12 * GB, percent=75.0)
    swap = SimpleNamespace(total=8 * GB, used=2 * GB, percent=25.0, free=6 * GB)
    parts = [
        SimpleNamespace(mountpoint="D:\\", device="D:"),
        SimpleNamespace(mountpoint="C:\\", device="C:"),
    ]

    def disk_usage(path: str):
        if path.startswith("D"):
            raise PermissionError("no media")
        return SimpleNamespace(total=477 * GB, free=42 * GB, used=(477 - 42) * GB)

    monkeypatch.setattr(probe.psutil, "virtual_memory", lambda: memory)
    monkeypatch.setattr(probe.psutil, "swap_memory", lambda: swap)
    monkeypatch.setattr(probe.psutil, "disk_partitions", lambda all=False: parts)
    monkeypatch.setattr(probe.psutil, "disk_usage", disk_usage)
    monkeypatch.setattr(probe, "read_commit_counters", lambda: (20 * GB, 32 * GB, False))
    current = probe.sample()
    assert current.memory.ram_percent == 75.0
    assert current.memory.ram_available == 4 * GB
    assert current.memory.commit_estimated is False
    assert current.memory.commit_percent == pytest.approx(20 / 32 * 100)
    assert len(current.disks) == 1
    assert current.disks[0].key == "C:"
    assert current.disks[0].free == 42 * GB


def test_probe_estimates_commit_when_counters_are_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    memory = SimpleNamespace(total=16 * GB, available=4 * GB, used=12 * GB, percent=75.0)
    swap = SimpleNamespace(total=8 * GB, used=2 * GB, percent=25.0, free=6 * GB)
    monkeypatch.setattr(probe.psutil, "virtual_memory", lambda: memory)
    monkeypatch.setattr(probe.psutil, "swap_memory", lambda: swap)
    monkeypatch.setattr(probe.psutil, "disk_partitions", lambda all=False: [])
    monkeypatch.setattr(probe, "read_commit_counters", lambda: (0, 0, True))
    current = probe.sample()
    assert current.memory.commit_estimated is True
    assert current.memory.commit_used == 14 * GB
    assert current.memory.commit_total == 24 * GB


def test_live_sample_and_processes_do_not_crash() -> None:
    current = probe.sample()
    assert current.memory.ram_total > 0
    assert 0 <= current.memory.ram_percent <= 100
    rows = top_processes(5)
    assert len(rows) <= 5


def test_top_processes_sorts_and_skips_access_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    class Process:
        def __init__(self, pid: int, name: str, rss: int) -> None:
            self.info = {
                "pid": pid,
                "name": name,
                "memory_info": SimpleNamespace(rss=rss),
            }

    class Denied:
        @property
        def info(self):
            raise psutil.AccessDenied(pid=9)

    monkeypatch.setattr(
        "headroom.processes.psutil.process_iter",
        lambda attrs=None: [
            Process(2, "small", 100),
            Denied(),
            Process(1, "big\nname", 5000),
            Process(3, "mid", 1000),
        ],
    )
    rows = top_processes(2)
    assert [(row.pid, row.name, row.rss_bytes) for row in rows] == [
        (1, "big name", 5000),
        (3, "mid", 1000),
    ]


def test_toast_keeps_the_message_out_of_the_command() -> None:
    calls: list[tuple[list[str], dict[str, str]]] = []

    def runner(command, env) -> None:
        calls.append((list(command), dict(env)))

    body = "UNIQUE_BODY_9f3a <tag>"
    send_toast("Headroom: RAM is high", body, runner=runner)
    command, env = calls[0]
    assert "UNIQUE_BODY_9f3a" not in " ".join(command)
    assert env["HEADROOM_TOAST_BODY"] == body
    assert command[0] == "powershell.exe"
    notifier = WindowsToastNotifier(enabled=False)
    assert notifier.enabled is False
    notifier.notify(evaluate(make_sample(ram_percent=99), AlertState(), Config(ram_sustained_seconds=0), 0)[0])


def test_log_notifier_and_disabled_toast(monkeypatch: pytest.MonkeyPatch) -> None:
    messages: list[tuple[str, str]] = []

    class Logger:
        def warning(self, message, *args) -> None:
            messages.append(("warning", message % args))

        def critical(self, message, *args) -> None:
            messages.append(("critical", message % args))

    alert = evaluate(
        make_sample(ram_percent=99),
        AlertState(),
        Config(ram_sustained_seconds=0),
        0,
    )[0]
    LogNotifier(Logger()).notify(alert)  # type: ignore[arg-type]
    assert messages[0][0] == "warning"
    assert "RAM is high" in messages[0][1]
    fanout = build_notifiers(Config(toast=False), Logger())  # type: ignore[arg-type]
    assert len(fanout.notifiers) == 1


def test_watch_loop_alerts_once_and_does_not_sleep_after_the_last_sample() -> None:
    samples = [
        make_sample(ram_percent=90),
        make_sample(ram_percent=91),
        make_sample(ram_percent=92),
    ]
    cursor = {"index": 0}

    def read():
        sample = samples[cursor["index"]]
        cursor["index"] += 1
        return sample

    times = iter([0, 60, 120])
    notes: list[str] = []

    class Notifier:
        def notify(self, alert) -> None:
            notes.append(alert.code)

    def fail_sleep(_seconds: float) -> None:
        raise AssertionError("sleep should not run when iterations already finished")

    # The loop sleeps between samples, not after the final one.
    sleeps: list[float] = []

    def record_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) > 2:
            raise AssertionError("too many sleeps")

    lines: list[str] = []
    run_watch_loop(
        Config(),
        probe=read,
        notifier=Notifier(),
        sleep=record_sleep,
        clock=lambda: next(times),
        iterations=3,
        on_line=lines.append,
    )
    assert notes == ["ram_sustained"]
    assert len(lines) == 3
    assert lines[0].startswith("RAM 90.0%")
    assert sleeps == [30, 30]

    def boom(_seconds: float) -> None:
        raise AssertionError("should not sleep")

    run_watch_loop(
        Config(),
        probe=lambda: make_sample(),
        notifier=Notifier(),
        sleep=boom,
        iterations=1,
        on_line=lambda _line: None,
    )


def test_watch_loop_survives_a_failed_sample() -> None:
    def explode():
        raise RuntimeError("psutil failed")

    run_watch_loop(
        Config(),
        probe=explode,
        notifier=SimpleNamespace(notify=lambda alert: None),
        sleep=lambda _seconds: None,
        iterations=1,
        on_line=lambda _line: None,
        logger=_quiet_logger(),
    )


def _quiet_logger():
    import logging

    logger = logging.getLogger("headroom.test.quiet")
    logger.handlers.clear()
    logger.addHandler(logging.NullHandler())
    logger.propagate = False
    return logger
