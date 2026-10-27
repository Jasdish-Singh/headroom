"""Always-on scheduling, history, and startup registration. Nothing here deletes."""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from headroom.autostart import (
    AutostartError,
    CommandResult,
    build_task_xml,
    app_command,
    install,
    logon_commands,
    resolve_pythonw,
    startup_vbs_path,
    uninstall,
    vbs_source,
    watch_command,
)
from headroom.cli import main, parse_window
from headroom.config import Config, ConfigError, config_from_mapping
from headroom.history import (
    HistoryStore,
    ProcessPoint,
    build_report,
    disk_trend,
    find_leaks,
    folder_diff,
)
from headroom.instance import InstanceLock, read_lock_pid
from headroom.probe import DiskStatus
from headroom.render import format_report, format_self
from headroom.schedule import choose_interval, detect_resume, lower_priority, on_battery
from headroom.selfcheck import SelfUsage
from headroom.service import Session, supervise
from headroom.tracker import DeepScanner, Tracker, fast_drop_amount
from headroom.watch import run_watch_loop
from tests.conftest import GB, make_sample

MB = 1024**2


def test_choose_interval_pressure_beats_battery_and_idle() -> None:
    config = Config()
    assert (
        choose_interval(config, on_battery=False, idle_seconds=0, under_pressure=False)
        == 30
    )
    assert (
        choose_interval(config, on_battery=True, idle_seconds=0, under_pressure=False)
        == 120
    )
    assert (
        choose_interval(config, on_battery=False, idle_seconds=300, under_pressure=False)
        == 90
    )
    assert (
        choose_interval(config, on_battery=False, idle_seconds=None, under_pressure=False)
        == 30
    )
    assert (
        choose_interval(config, on_battery=True, idle_seconds=10_000, under_pressure=True)
        == 10
    )
    wide = Config(interval_seconds=200)
    assert choose_interval(wide, on_battery=True, idle_seconds=0, under_pressure=False) == 200


def test_interval_fields_are_independent() -> None:
    loaded = config_from_mapping({"monitor": {"interval_seconds": 200}})
    assert loaded.interval_seconds == 200
    assert loaded.idle_interval_seconds == 90
    with pytest.raises(ConfigError, match="battery_interval_seconds"):
        config_from_mapping({"monitor": {"battery_interval_seconds": 1}})


def test_tracking_paths_load_and_cleanup_paths_still_fail() -> None:
    loaded = config_from_mapping(
        {"tracking": {"paths": ["D:/Photos"], "large_file_mb": 100, "history_max_mb": 8}}
    )
    assert loaded.tracking_paths == ("D:/Photos",)
    assert loaded.large_file_bytes == 100 * MB
    assert loaded.history_max_bytes == 8 * MB
    assert loaded.enabled_cleanup == Config().enabled_cleanup
    with pytest.raises(ConfigError, match="paths"):
        config_from_mapping({"cleanup": {"paths": ["D:/Photos"]}})
    with pytest.raises(ConfigError, match="tracking"):
        config_from_mapping({"tracking": {"delete": True}})


def test_on_battery_treats_unknown_as_plugged_in() -> None:
    assert on_battery(lambda: None) is False
    assert on_battery(lambda: (_ for _ in ()).throw(RuntimeError("no sensor"))) is False

    class Battery:
        power_plugged = False

    assert on_battery(lambda: Battery()) is True
    Battery.power_plugged = True
    assert on_battery(lambda: Battery()) is False


def test_detect_resume_needs_a_wall_clock_jump() -> None:
    assert detect_resume(0, 0, 30, 30) is False
    assert detect_resume(0, 0, 10_000, 30) is True
    assert detect_resume(100, 100, 90, 110) is False


def test_lower_priority_runs_once(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    monkeypatch.setattr("headroom.schedule.os.name", "posix")
    monkeypatch.setattr(
        "headroom.schedule.os.nice",
        lambda amount: calls.append(amount),
        raising=False,
    )
    monkeypatch.setattr("headroom.schedule._priority_lowered", False)
    lower_priority()
    lower_priority()
    assert calls == [10]


def test_instance_lock_rejects_a_second_holder(tmp_path: Path) -> None:
    path = tmp_path / "headroom.lock"
    first = InstanceLock(path)
    second = InstanceLock(path)
    assert first.acquire() is True
    assert second.acquire() is False
    first.release()
    assert second.acquire() is True
    second.release()
    assert path.read_text(encoding="utf-8").strip().isdigit()


def test_lock_holder_pid_stays_readable(tmp_path: Path) -> None:
    path = tmp_path / "headroom.lock"
    lock = InstanceLock(path)
    assert lock.acquire()
    try:
        assert read_lock_pid(path) == os.getpid()
    finally:
        lock.release()
    assert read_lock_pid(path) == os.getpid()
    path.write_bytes(b"4242\n".ljust(32, b" "))
    assert read_lock_pid(path) == 4242
    assert read_lock_pid(tmp_path / "missing.lock") is None


def test_session_stop_is_idempotent() -> None:
    session = Session()
    calls: list[str] = []
    session.add_callback(lambda: calls.append("stop"))
    session.request_stop()
    session.request_stop()
    assert session.stop.is_set()
    assert calls == ["stop"]


def test_supervise_restarts_after_a_crash_and_stops_after_a_clean_return() -> None:
    logger = logging.getLogger("headroom.test.supervise")
    logger.handlers.clear()
    logger.addHandler(logging.NullHandler())
    logger.propagate = False
    calls = {"n": 0}

    def crash_then_stop() -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        stop.set()

    stop = __import__("threading").Event()
    supervise(crash_then_stop, stop, logger, sleep=lambda _seconds: None)
    assert calls["n"] == 2

    calls["n"] = 0

    def clean() -> None:
        calls["n"] += 1

    supervise(clean, __import__("threading").Event(), logger, sleep=lambda _seconds: None)
    assert calls["n"] == 1


def test_fast_drop_ignores_long_gaps_and_small_changes() -> None:
    config = Config()
    assert fast_drop_amount(40 * GB, 37 * GB, 60, config) == 3 * GB
    assert fast_drop_amount(40 * GB, 39 * GB, 60, config) is None
    assert fast_drop_amount(40 * GB, 10 * GB, 601, config) is None
    assert fast_drop_amount(40 * GB, 10 * GB, 0, config) is None
    assert fast_drop_amount(40 * GB, 10 * GB, 10_000, config) is None


def test_disk_trend_estimates_days_until_full() -> None:
    points = [(0.0, 40 * GB), (5 * 86400.0, 30 * GB)]
    trend = disk_trend("C:", points)
    assert trend.enough is True
    assert trend.bytes_per_day == pytest.approx(-2 * GB)
    assert trend.days_until_full == pytest.approx(15)
    short = disk_trend("C:", [(0.0, 40 * GB), (60.0, 39 * GB)])
    assert short.enough is False
    store = _store_with_trend(points)
    try:
        text = format_report(build_report(store, Config(), now=points[-1][0]))
    finally:
        store.close()
    assert "C:" in text
    assert "2.0 GB/day" in text
    assert "15 days" in text


def test_folder_diff_and_leak_grouping() -> None:
    changes = folder_diff({"a": 10, "b": 50}, {"a": 15, "b": 40, "c": 5})
    assert [(item.path, item.delta) for item in changes] == [("a", 5), ("c", 5)]
    leaks = find_leaks(
        [
            ProcessPoint(0, 4, "chrome", 100 * MB, 10.0),
            ProcessPoint(3600, 4, "chrome", 600 * MB, 10.0),
            ProcessPoint(0, 8, "editor", 100 * MB, 3.0),
            ProcessPoint(3600, 9, "editor", 120 * MB, 3.0),
            ProcessPoint(0, 2, "spike", 100 * MB, 1.0),
            ProcessPoint(10, 2, "spike", 900 * MB, 1.0),
            ProcessPoint(3600, 2, "spike", 150 * MB, 1.0),
        ],
        now=3600,
        window_seconds=6 * 3600,
        growth_bytes=400 * MB,
    )
    assert [leak.name for leak in leaks] == ["chrome"]
    assert leaks[0].growth == 500 * MB


def test_history_trim_drops_old_rows(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path / "history.db")
    try:
        for index in range(40):
            store.record_sample(make_sample(captured_at=1_700_000_000 + index))
        assert store.count_samples() == 40
        removed = store.trim(1)
        assert removed > 0
        assert store.count_samples() < 40
    finally:
        store.close()


def test_deep_scan_is_incremental_and_does_not_follow_or_delete(tmp_path: Path) -> None:
    root = tmp_path / "profile"
    (root / "docs" / "nested").mkdir(parents=True)
    (root / "docs" / "a.txt").write_bytes(b"a" * 100)
    (root / "docs" / "nested" / "b.txt").write_bytes(b"b" * 50)
    (root / "big.bin").write_bytes(b"x" * 600)
    outside = tmp_path / "outside"
    outside.mkdir()
    huge = outside / "huge.bin"
    huge.write_bytes(b"z" * 5000)
    try:
        (root / "link.bin").symlink_to(huge)
        (root / "linked-dir").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink not permitted")
    store = HistoryStore(tmp_path / "history.db")
    scanner = DeepScanner([root], large_file_bytes=500, top_folders=10, pass_interval=10_000)
    try:
        partial = scanner.step(max_dirs=1, max_seconds=5, now=1000, store=store)
        assert partial is False
        assert scanner.started is True
        finished = False
        for _ in range(20):
            finished = scanner.step(max_dirs=100, max_seconds=5, now=1000, store=store)
            if finished:
                break
        assert finished is True
        folders = store.folders_at(store.latest_folder_ts() or 0)
        assert folders[str(root / "docs")] == 150
        assert folders[str(root)] == 600
        assert all("outside" not in path for path in folders)
        files = store.large_files_since(0)
        assert any(item.path.endswith("big.bin") for item in files)
        assert all("huge.bin" not in item.path for item in files)
        assert huge.read_bytes() == b"z" * 5000
        again = scanner.step(max_dirs=100, max_seconds=5, now=1000, store=store)
        assert again is False
    finally:
        store.close()


def test_tracker_notifies_once_for_a_fast_drop_and_a_leak(tmp_path: Path) -> None:
    config = Config(process_every=1, leak_growth_bytes=400 * MB, leak_window_seconds=6 * 3600)
    store = HistoryStore(tmp_path / "history.db")
    samples = [
        _process(4, "chrome", 100 * MB, 10.0),
        _process(4, "chrome", 600 * MB, 10.0),
        _process(4, "chrome", 610 * MB, 10.0),
        _process(4, "chrome", 120 * MB, 10.0),
        _process(4, "chrome", 700 * MB, 10.0),
    ]
    cursor = {"index": 0}

    def reader():
        row = samples[min(cursor["index"], len(samples) - 1)]
        cursor["index"] += 1
        return [row]

    tracker = Tracker(store, config, process_reader=reader, scanner=None)
    logger = logging.getLogger("headroom.test.tracker")
    logger.handlers.clear()
    logger.addHandler(logging.NullHandler())
    tracker.logger = logger
    try:
        first = tracker.observe(
            make_sample(captured_at=1_000),
            resumed=False,
            deep=False,
            idle_seconds=0,
            slow_cadence=True,
        )
        assert first == []
        dropped = make_sample(
            captured_at=1_060,
            disks=(DiskStatus("C:\\", "C:", 477 * GB, 0, 39 * GB, "C:"),),
        )
        second = tracker.observe(
            dropped, resumed=False, deep=False, idle_seconds=0, slow_cadence=True
        )
        assert [alert.code for alert in second] == ["disk_fast_drop", "process_leak"]
        third = tracker.observe(
            make_sample(
                captured_at=1_120,
                disks=(DiskStatus("C:\\", "C:", 477 * GB, 0, 38 * GB, "C:"),),
            ),
            resumed=False,
            deep=False,
            idle_seconds=0,
            slow_cadence=True,
        )
        assert third == []
        recovered = tracker.observe(
            make_sample(captured_at=5_000),
            resumed=False,
            deep=False,
            idle_seconds=0,
            slow_cadence=True,
        )
        assert recovered == []
        again = tracker.observe(
            make_sample(
                captured_at=5_060,
                disks=(DiskStatus("C:\\", "C:", 477 * GB, 0, 39 * GB, "C:"),),
            ),
            resumed=False,
            deep=False,
            idle_seconds=0,
            slow_cadence=True,
        )
        assert [alert.code for alert in again] == ["disk_fast_drop", "process_leak"]
        skipped = tracker.observe(
            make_sample(
                captured_at=20_000,
                disks=(DiskStatus("C:\\", "C:", 477 * GB, 0, 10 * GB, "C:"),),
            ),
            resumed=True,
            deep=False,
            idle_seconds=0,
            slow_cadence=True,
        )
        assert [alert.code for alert in skipped] == []
    finally:
        store.close()


def test_adaptive_watch_sleeps_faster_under_pressure_and_resets_after_resume() -> None:
    sleeps: list[float] = []
    notes: list[str] = []

    class Notifier:
        def notify(self, alert) -> None:
            notes.append(alert.code)

    run_watch_loop(
        Config(),
        probe=lambda: make_sample(ram_percent=90),
        notifier=Notifier(),
        sleep=sleeps.append,
        clock=lambda: 0,
        iterations=3,
        on_line=lambda _line: None,
        adaptive=True,
        on_battery_fn=lambda: False,
        idle_fn=lambda: 0,
        wall_clock=lambda: 0,
        mono_clock=lambda: 0,
    )
    assert sleeps == [10, 10]

    times = iter([0, 120])
    walls = iter([0, 10_000])
    monos = iter([0, 30])
    notes.clear()
    run_watch_loop(
        Config(),
        probe=lambda: make_sample(ram_percent=90),
        notifier=Notifier(),
        sleep=lambda _seconds: None,
        clock=lambda: next(times),
        iterations=2,
        on_line=lambda _line: None,
        wall_clock=lambda: next(walls),
        mono_clock=lambda: next(monos),
    )
    assert notes == []


def test_task_xml_and_startup_fallback(tmp_path: Path) -> None:
    command = [r"C:\Program Files\Python\pythonw.exe", "-m", "headroom", "watch"]
    xml = build_task_xml(command)
    for snippet in (
        "LogonTrigger",
        "RestartOnFailure",
        "PT1M",
        "<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>",
        "<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>",
        "<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>",
        "<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>",
        "<RunLevel>LeastPrivilege</RunLevel>",
        "InteractiveToken",
        "pythonw.exe",
        "-m headroom watch",
    ):
        assert snippet in xml
    assert "&" not in xml or "&amp;" in xml or "&quot;" in xml
    vbs = vbs_source(command)
    assert 'CreateObject("Wscript.Shell").Run' in vbs
    assert ", 0, False" in vbs
    assert '""C:\\Program Files\\Python\\pythonw.exe""' in vbs
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> CommandResult:
        calls.append(argv)
        if argv[1] == "/Create":
            return CommandResult(1, stderr="denied")
        return CommandResult(0)

    spawned: list[list[str]] = []
    env = {"APPDATA": str(tmp_path / "roaming"), "LOCALAPPDATA": str(tmp_path / "local")}
    message = install(
        env=env,
        home=tmp_path / "home",
        runner=runner,
        spawn=spawned.append,
        command=command,
        platform="nt",
    )
    assert "Startup" in message
    vbs_path = startup_vbs_path(env, tmp_path / "home")
    assert vbs_path.is_file()
    assert spawned == [command]
    removed = uninstall(env=env, home=tmp_path / "home", runner=runner, platform="nt")
    assert "Startup" in removed
    assert not vbs_path.exists()
    with pytest.raises(AutostartError, match="Nothing was changed"):
        install(platform="posix", runner=runner)
    assert not (tmp_path / "posix-wrote").exists()


def test_pythonw_prefers_a_sibling(tmp_path: Path) -> None:
    exe = tmp_path / "python.exe"
    windowless = tmp_path / "pythonw.exe"
    exe.write_text("", encoding="utf-8")
    windowless.write_text("", encoding="utf-8")
    assert resolve_pythonw(exe) == windowless
    assert resolve_pythonw(windowless) == windowless
    assert watch_command(exe)[1:] == ["-m", "headroom", "watch"]


def test_frozen_logon_launcher_starts_watch_and_optional_pet(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    exe = r"C:\Program Files\Headroom\headroom.exe"
    monkeypatch.setattr("headroom.autostart.sys.frozen", True, raising=False)
    monkeypatch.setattr("headroom.autostart.sys.executable", exe)
    assert app_command("watch") == [exe, "watch"]
    assert logon_commands(include_pet=False) == [[exe, "watch"]]
    both = logon_commands(include_pet=True)
    assert both == [[exe, "watch"], [exe, "pet"]]
    xml = build_task_xml(both[0], commands=both)
    assert xml.count("<Exec>") == 2
    assert "headroom.exe" in xml
    assert ">watch<" in xml
    assert ">pet<" in xml
    assert "-m headroom" not in xml
    script = vbs_source(both[0], commands=both)
    assert script.count("Wscript.Shell") == 2
    assert '""C:\\Program Files\\Headroom\\headroom.exe"" watch' in script
    assert '""C:\\Program Files\\Headroom\\headroom.exe"" pet' in script

    calls: list[list[str]] = []

    def runner(argv: list[str]) -> CommandResult:
        calls.append(argv)
        if argv[1] == "/Create":
            return CommandResult(1, stderr="denied")
        return CommandResult(0)

    spawned: list[list[str]] = []
    env = {"APPDATA": str(tmp_path / "roaming"), "LOCALAPPDATA": str(tmp_path / "local")}
    message = install(env=env, home=tmp_path / "home", runner=runner, spawn=spawned.append, include_pet=True, platform="nt")
    assert "Startup" in message
    text = startup_vbs_path(env, tmp_path / "home").read_text(encoding="utf-8")
    assert "watch" in text
    assert "pet" in text
    assert "-m" not in text
    assert spawned == both
    from headroom.logsetup import default_log_path

    saved = (default_log_path(env, tmp_path / "home").parent / "Headroom.task.xml").read_text(encoding="utf-16")
    assert saved.count("<Exec>") == 2


@pytest.mark.skipif(os.name == "nt", reason="faking a non-Windows os.name breaks pathlib on Windows")
def test_history_commands_and_refusing_install_off_windows(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    import time

    database = tmp_path / "history.db"
    monkeypatch.setenv("HEADROOM_DB", str(database))
    assert main(["--no-log", "history"]) == 0
    assert "No history yet" in capsys.readouterr().out
    now = time.time()
    store = HistoryStore(database)
    store.record_sample(make_sample(ram_percent=12, captured_at=now - 3600))
    store.record_folders(now - 8 * 86400, {r"C:\Users\you\Videos": 1 * GB})
    store.record_folders(now - 60, {r"C:\Users\you\Videos": 3 * GB})
    store.record_large_files(now - 3600, {r"C:\Users\you\Videos\clip.mp4": 800 * MB})
    store.close()
    assert main(["--no-log", "history", "--since", "week"]) == 0
    assert "12.0%" in capsys.readouterr().out
    assert main(["--no-log", "changes", "--since", "7d"]) == 0
    changes = capsys.readouterr().out
    assert "Videos" in changes
    assert "2.0 GB" in changes
    assert "clip.mp4" in changes
    assert main(["--no-log", "report"]) == 0
    assert "Headroom report" in capsys.readouterr().out
    assert parse_window("yesterday") == 86400
    monkeypatch.setattr("headroom.autostart.os.name", "posix")
    assert main(["--no-log", "install"]) == 2
    assert "Nothing was changed" in capsys.readouterr().err
    assert main(["--no-log", "uninstall"]) == 2


def test_second_watch_exits_when_the_lock_is_held(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from headroom.instance import default_lock_path

    monkeypatch.setattr("headroom.cli.lower_priority", lambda: None)
    lock = InstanceLock(default_lock_path())
    assert lock.acquire()
    try:
        assert main(["--no-log", "watch", "--no-tray"]) == 0
    finally:
        lock.release()
    assert "already running" in capsys.readouterr().out


def test_self_command_prints_the_budget(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--no-log", "self"]) == 0
    text = capsys.readouterr().out
    assert "40" in text
    assert "1%" in text
    rendered = format_self(
        SelfUsage(pid=1, rss_bytes=20 * MB, cpu_percent=0.2),
        SelfUsage(pid=2, rss_bytes=30 * MB, cpu_percent=0.4),
    )
    assert "Watch" in rendered
    assert "within" not in rendered or "under 40 MB" in rendered


def test_tracker_source_does_not_delete() -> None:
    source = Path(__import__("headroom.tracker", fromlist=["tracker"]).__file__).read_text(
        encoding="utf-8"
    )
    assert "cleanup" not in source
    assert "unlink" not in source
    assert "os.remove" not in source


def _process(pid: int, name: str, rss: int, created: float):
    from headroom.processes import ProcessSample

    return ProcessSample(pid=pid, name=name, rss_bytes=rss, create_time=created)


def _store_with_trend(points: list[tuple[float, int]]) -> HistoryStore:
    import tempfile

    directory = tempfile.mkdtemp()
    store = HistoryStore(Path(directory) / "history.db")
    for ts, free in points:
        sample = make_sample(
            captured_at=ts,
            disks=(DiskStatus("C:\\", "C:", 477 * GB, 477 * GB - free, free, "C:"),),
        )
        store.record_sample(sample)
    return store
