"""Committed history is visible to a second connection, and empty charts say so."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from headroom.config import Config
from headroom.dashboard import (
    bind_dashboard,
    collecting_message,
    existing_dashboard_url,
    first_chart_minutes,
    new_token,
    remember_dashboard,
    serve_dashboard,
)
from headroom.history import HistoryStore
from headroom.instance import InstanceLock, default_lock_path
from headroom.pet import publish_watch_sample
from headroom.rules import state_dir
from headroom.tracker import Tracker
from headroom.watch import run_watch_loop
from tests.conftest import make_sample
from tests.test_v4 import _request


class _Quiet:
    def notify(self, alert) -> None:
        del alert


def test_first_chart_minutes_rounds_up() -> None:
    assert first_chart_minutes(30) == 1
    assert first_chart_minutes(60) == 1
    assert first_chart_minutes(61) == 2
    assert first_chart_minutes(120) == 2
    assert collecting_message(1) == "Collecting data, first chart in 1 minute."
    assert collecting_message(2) == "Collecting data, first chart in 2 minutes."


def test_watch_loop_commits_rows_a_separate_connection_can_see(tmp_path: Path) -> None:
    database = tmp_path / "history.db"
    store = HistoryStore(database)
    stamps = iter((1_700_000_000.0, 1_700_000_030.0))
    events: list[str] = []

    def probe():
        events.append("sample")
        return make_sample(ram_percent=41, captured_at=next(stamps))

    def sleeper(seconds: float) -> None:
        events.append(f"sleep:{seconds}")

    tracker = Tracker(store, Config(), process_reader=lambda: [], scanner=None)
    try:
        run_watch_loop(
            Config(interval_seconds=30),
            probe=probe,
            notifier=_Quiet(),
            sleep=sleeper,
            iterations=2,
            tracker=tracker,
            logger=logging.getLogger("headroom.test.commit"),
        )
        assert events == ["sample", "sleep:30.0", "sample"]
        other = sqlite3.connect(database)
        try:
            assert other.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
            assert other.execute("SELECT COUNT(*) FROM samples").fetchone()[0] >= 2
            assert other.execute("SELECT COUNT(*) FROM disks").fetchone()[0] >= 2
        finally:
            other.close()
    finally:
        store.close()

    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(database) + suffix)
        sidecar.unlink(missing_ok=True)
    fresh = sqlite3.connect(database)
    try:
        assert fresh.execute("SELECT COUNT(*) FROM samples").fetchone()[0] >= 2
        assert fresh.execute("SELECT COUNT(*) FROM disks").fetchone()[0] >= 2
    finally:
        fresh.close()


def test_record_sample_from_another_thread_is_visible(tmp_path: Path) -> None:
    database = tmp_path / "history.db"
    store = HistoryStore(database)
    try:
        worker = threading.Thread(
            target=lambda: store.record_sample(make_sample(ram_percent=18, captured_at=1_700_000_100)),
            name="headroom-watch",
        )
        worker.start()
        worker.join(timeout=5)
        assert not worker.is_alive()
        other = sqlite3.connect(database)
        try:
            row = other.execute("SELECT ram_percent FROM samples").fetchone()
        finally:
            other.close()
        assert row is not None
        assert row[0] == 18
    finally:
        store.close()


def test_empty_history_shows_the_live_snapshot_and_a_wait(tmp_path: Path) -> None:
    publish_watch_sample(make_sample(ram_percent=42, captured_at=time.time()), None, tmp_path)
    token = new_token()
    server = bind_dashboard(
        "127.0.0.1",
        0,
        token,
        directory=tmp_path,
        history=tmp_path / "missing.db",
        interval_seconds=30,
        live_probe=lambda: (_ for _ in ()).throw(AssertionError("probe should not run")),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = int(server.server_address[1])
    try:
        status, page = _request(port, "GET", f"/?token={token}")
        assert status == 200
        text = page.decode("utf-8")
        assert "Collecting data, first chart in 1 minute." in text
        assert "Type MOVE" in text
        assert 'id="move"' in text
        assert "Move checked files" in text
        assert 'id="live"' in text
        assert 'id="ram-note"' in text
        assert 'id="disk-note"' in text
        assert 'id="review"' in text
        status, body = _request(port, "GET", f"/api/history?token={token}")
        assert status == 200
        payload = json.loads(body)
        assert payload["ram"] == []
        assert payload["disks"] == {}
        assert payload["collecting"] is True
        assert payload["message"] == "Collecting data, first chart in 1 minute."
        assert payload["live"]["source"] == "watch"
        assert payload["live"]["ram_percent"] == 42
        status, report = _request(port, "GET", f"/api/report?token={token}")
        assert status == 200
        report_text = report.decode("utf-8")
        assert "Collecting data, first chart in 1 minute." in report_text
        assert "Live RAM 42.0%" in report_text
        assert "the watch snapshot" in report_text
        status, review = _request(port, "GET", f"/api/review?token={token}")
        assert status == 200
        assert json.loads(review)["items"] == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_empty_history_falls_back_to_a_direct_sample(tmp_path: Path) -> None:
    token = "direct"
    server = bind_dashboard(
        "127.0.0.1",
        0,
        token,
        directory=tmp_path,
        history=tmp_path / "missing.db",
        interval_seconds=120,
        live_probe=lambda: make_sample(ram_percent=33, captured_at=time.time()),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = int(server.server_address[1])
    try:
        status, page = _request(port, "GET", f"/?token={token}")
        assert status == 200
        assert b"Collecting data, first chart in 2 minutes." in page
        status, body = _request(port, "GET", f"/api/history?token={token}")
        payload = json.loads(body)
        assert payload["live"]["source"] == "probe"
        assert payload["live"]["ram_percent"] == 33
        assert payload["message"] == "Collecting data, first chart in 2 minutes."
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_widget_toggle_starts_stops_and_persists(tmp_path: Path) -> None:
    started: list[tuple[Path, bool]] = []
    stopped: list[Path] = []

    def start(folder: Path, lite: bool) -> None:
        started.append((folder, lite))

    def stop(folder: Path) -> None:
        stopped.append(folder)

    token = new_token()
    server = bind_dashboard(
        "127.0.0.1",
        0,
        token,
        directory=tmp_path,
        history=tmp_path / "missing.db",
        pet_start=start,
        pet_stop=stop,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = int(server.server_address[1])
    try:
        status, body = _request(port, "GET", f"/api/widget?token={token}")
        assert status == 200
        assert json.loads(body) == {"on": False, "running": False}

        status, body = _request(
            port,
            "POST",
            f"/api/widget?token={token}",
            body=json.dumps({"enabled": True}).encode(),
            headers={"Content-Type": "application/json", "X-Headroom-Token": token},
        )
        assert status == 200
        assert json.loads(body)["on"] is True
        assert started == [(tmp_path, False)]
        assert stopped == []
        assert (tmp_path / "widget.enabled").read_text(encoding="utf-8").strip() == "1"

        status, body = _request(
            port,
            "POST",
            f"/api/widget?token={token}",
            body=json.dumps({"enabled": False}).encode(),
            headers={"Content-Type": "application/json", "X-Headroom-Token": token},
        )
        assert status == 200
        assert json.loads(body)["on"] is False
        assert stopped == [tmp_path]
        assert (tmp_path / "widget.enabled").read_text(encoding="utf-8").strip() == "0"

        status, _body = _request(
            port,
            "POST",
            f"/api/widget?token={token}",
            body=json.dumps({"enabled": "maybe"}).encode(),
            headers={"Content-Type": "application/json", "X-Headroom-Token": token},
        )
        assert status == 400
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_second_dashboard_opens_the_existing_url(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    import socket

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    port = int(sock.getsockname()[1])
    url = f"http://127.0.0.1:{port}/?token=already"
    remember_dashboard(tmp_path, url)
    opened: list[str] = []
    try:
        serve_dashboard(
            "127.0.0.1",
            port,
            "fresh-token",
            opener=opened.append,
            directory=tmp_path,
        )
    finally:
        sock.close()
    assert opened == [url]
    printed = capsys.readouterr().out
    assert "Dashboard is already running." in printed
    assert url in printed


def test_stale_dashboard_url_is_forgotten(tmp_path: Path) -> None:
    import socket

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    remember_dashboard(tmp_path, f"http://127.0.0.1:{port}/?token=gone")
    assert existing_dashboard_url(tmp_path) is None
    assert not (tmp_path / "dashboard.url").exists()


def test_watch_once_respects_the_single_instance_lock(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from headroom.cli import main

    monkeypatch.setattr("headroom.cli.lower_priority", lambda: None)
    lock = InstanceLock(default_lock_path())
    assert lock.acquire()
    try:
        assert main(["--no-log", "watch", "--once", "--no-tray"]) == 0
    finally:
        lock.release()
    assert "already running" in capsys.readouterr().out


def test_second_pet_refuses_when_the_lock_is_held(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from headroom.cli import main

    monkeypatch.setattr(
        "headroom.cli.run_pet",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("pet started")),
    )
    lock = InstanceLock(state_dir() / "pet.lock")
    assert lock.acquire()
    try:
        assert main(["--no-log", "pet"]) == 0
    finally:
        lock.release()
    assert "already running" in capsys.readouterr().out
