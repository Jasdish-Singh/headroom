"""Dashboard, explanations, weekly summary, dupes, startup, downloads, plugins, MCP, and the exe."""

from __future__ import annotations

import http.client
import importlib.util
import inspect
import json
import os
import re
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlencode

import pytest

import headroom
from headroom.analyze import FileRecord
from headroom.cli import main
from headroom.config import Config
from headroom.dashboard import DashboardError, bind_dashboard, new_token
from headroom.downloads import classify_downloads
from headroom.dupes import PARTIAL_BYTES, find_duplicates, location_score
from headroom.explain import ExplainError, OllamaClient, _RejectRedirect, explain_items, explain_record
from headroom.firstrun import maybe_offer_first_run
from headroom.history import HistoryStore
from headroom.plans import PlanItem, load_plans, replace_plan
from headroom.plugins import PROFILES, Plugin, iter_plugins, profile_plugins, report_sections, scanner_records
from headroom.probe import DiskStatus
from headroom.processes import ProcessSample
from headroom.rules import RuleError, load_profile, load_profile_text, state_dir, write_active_profiles
from headroom.startup import (
    FileCatalog,
    MemoryCatalog,
    append_backup,
    entries_from_run_values,
    entries_from_schtasks_csv,
    estimate_impact,
    load_backup,
    pop_run_value,
    put_run_value,
    remove_backup,
)
from headroom.watch import run_watch_loop
from headroom.weekly import WEEK_SECONDS, build_weekly_report, maybe_weekly, weekly_due
from tests.conftest import GB, make_sample

ROOT = Path(__file__).resolve().parents[1]
MB = 1024 * 1024


def _record(path: str, category: str, accessed: float, *, size: int = 128) -> FileRecord:
    return FileRecord(path=path, size=size, accessed=accessed, category=category, importance=1)


class _Body:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self) -> _Body:
        return self

    def __exit__(self, *args: object) -> bool:
        return False


def _request(
    port: int,
    method: str,
    path: str,
    *,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    connection.request(method, path, body=body, headers=headers or {})
    response = connection.getresponse()
    data = response.read()
    status = response.status
    connection.close()
    return status, data


def _strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for item in value.values() for text in _strings(item)]
    if isinstance(value, list):
        return [text for item in value for text in _strings(item)]
    return []


def test_dashboard_requires_loopback_token_and_move(tmp_path: Path) -> None:
    with pytest.raises(DashboardError):
        bind_dashboard("0.0.0.0", 0, "token")

    root = tmp_path / "docs"
    root.mkdir()
    extra = root / "extra.txt"
    extra.write_bytes(b"move-me")
    keeper = root / "keep.txt"
    keeper.write_bytes(b"stay")
    directory = state_dir()
    replace_plan(
        "dupes",
        [root],
        [
            PlanItem(
                path=str(extra),
                size=extra.stat().st_size,
                accessed=extra.stat().st_atime,
                category="duplicates",
                reason=f"duplicate of {keeper}",
                source="dupes",
            )
        ],
        directory,
    )
    token = new_token()
    server = bind_dashboard(
        "127.0.0.1",
        0,
        token,
        directory=directory,
        recycle=lambda _path: False,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = int(server.server_address[1])
    try:
        status, _body = _request(port, "GET", "/", headers={"Host": "evil.example"})
        assert status == 403
        status, _body = _request(port, "GET", "/")
        assert status == 403
        status, page = _request(port, "GET", f"/?token={token}")
        assert status == 200
        assert b"Type MOVE" in page
        status, _body = _request(port, "GET", "/", headers={"Host": "127.0.0.1", "X-Headroom-Token": token})
        assert status == 200
        status, review = _request(port, "GET", f"/api/review?token={token}")
        assert status == 200
        assert str(extra) in _strings(json.loads(review))

        refused = urlencode([("token", token), ("confirm", "no"), ("path", str(extra))]).encode()
        status, body = _request(
            port,
            "POST",
            f"/move?token={token}",
            body=refused,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert status == 400
        assert b"Nothing was moved" in body
        assert extra.read_bytes() == b"move-me"

        missing_form = urlencode([("confirm", "MOVE"), ("path", str(extra))]).encode()
        status, _body = _request(
            port,
            "POST",
            f"/move?token={token}",
            body=missing_form,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert status == 403
        assert extra.is_file()

        stranger = urlencode([("token", token), ("confirm", "MOVE"), ("path", str(keeper))]).encode()
        status, body = _request(
            port,
            "POST",
            f"/move?token={token}",
            body=stranger,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert status == 400
        assert b"not on the review list" in body
        assert keeper.read_bytes() == b"stay"
        assert extra.read_bytes() == b"move-me"

        approved = urlencode([("token", token), ("confirm", "MOVE"), ("path", str(extra))]).encode()
        status, body = _request(
            port,
            "POST",
            "/move",
            body=approved,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "X-Headroom-Token": token,
            },
        )
        assert status == 200
        assert b"Moved" in body
        assert not extra.exists()
        archived = directory / "archive" / extra.name
        assert archived.read_bytes() == b"move-me"
        assert keeper.read_bytes() == b"stay"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_explanations_stay_on_loopback_and_fall_back(tmp_path: Path) -> None:
    opened: list[object] = []

    def refuse(request, timeout):  # type: ignore[no-untyped-def]
        del timeout
        opened.append(request)
        raise AssertionError("opener should not run")

    with pytest.raises(ExplainError):
        OllamaClient("http://example.com:11434", opener=refuse)
    assert opened == []
    assert OllamaClient("http://127.0.0.1:11434", opener=lambda *_args: None).base_url.endswith("11434")
    with pytest.raises(ExplainError):
        _RejectRedirect().redirect_request(None, None, 302, "Found", {}, "https://example.com/x")

    secret = tmp_path / "notes.pdf"
    secret.write_bytes(b"SECRET-BYTES-NOT-SENT")
    seen: list[bytes] = []

    def opener(request, timeout):  # type: ignore[no-untyped-def]
        del timeout
        seen.append(request.data or b"")
        if request.full_url.endswith("/api/tags"):
            return _Body(json.dumps({"models": [{"name": "tiny"}]}).encode())
        return _Body(json.dumps({"response": "It looks idle."}).encode())

    now = 1_700_000_000.0
    record = _record(str(secret), "downloads", now - 40 * 86400, size=secret.stat().st_size)
    client = OllamaClient(opener=opener)
    assert client.explain(record, "toss older-than: 30d", now=now) == "It looks idle."
    assert b"SECRET-BYTES-NOT-SENT" not in b"".join(seen)

    def down(_request, timeout=None):  # type: ignore[no-untyped-def]
        del timeout
        raise OSError("down")

    offline = OllamaClient(opener=down)
    assert offline.explain(record, "toss older-than: 30d", now=now) is None
    assert "40 days" in explain_record(record, "toss older-than: 30d", now=now)

    semester = explain_record(
        _record("homework/essay.docx", "school", now - 400 * 86400),
        "toss path: */essays/* semester: past",
        now=now,
    )
    assert "semester" in semester.casefold()
    cache = explain_record(
        _record("/proj/node_modules/pkg", "caches", now, size=4096),
        "toss",
        now=now,
    )
    assert "rebuild" in cache.casefold()
    duplicate = explain_record(_record("copy.bin", "duplicates", now), "duplicate of other.bin", now=now)
    assert "byte for byte" in duplicate
    installer = explain_record(
        _record("ChromeSetup.exe", "installers", now - 2 * 86400),
        "installer already run; matches Google Chrome",
        now=now,
    )
    assert "already installed" in installer.casefold()

    class Item:
        def __init__(self, record: FileRecord, rule_text: str) -> None:
            self.record = record
            self.rule_text = rule_text

    fallback = explain_items([Item(record, "toss older-than: 30d")], now=now, client=OllamaClient(opener=down))
    assert "40 days" in fallback[0]
    modeled = explain_items([Item(record, "toss older-than: 30d")], now=now, client=OllamaClient(opener=opener))
    assert modeled == ["It looks idle."]


def test_weekly_summary_needs_a_week_and_does_not_run_for_bounded_watch(tmp_path: Path) -> None:
    now = 1_800_000_000.0
    earlier = now - 8 * 86400
    store = HistoryStore(tmp_path / "history.sqlite")
    full = 477 * GB
    store.record_sample(
        make_sample(
            captured_at=earlier,
            disks=(DiskStatus(mount="C:\\", device="C:", total=full, used=full - 40 * GB, free=40 * GB, key="C:"),),
        )
    )
    store.record_sample(
        make_sample(
            captured_at=now,
            disks=(DiskStatus(mount="C:\\", device="C:", total=full, used=full - 30 * GB, free=30 * GB, key="C:"),),
        )
    )
    growing = tmp_path / "GrowingNotes"
    store.record_folders(earlier, {str(growing): 1000})
    store.record_folders(now, {str(growing): 9000})
    store.record_processes(now - 1000, [ProcessSample(pid=7, name="LeakSuspect", rss_bytes=100 * MB, create_time=10.0)])
    store.record_processes(now - 10, [ProcessSample(pid=7, name="LeakSuspect", rss_bytes=600 * MB, create_time=10.0)])

    text = build_weekly_report(store, Config(), now=now, directory=tmp_path / "empty-state")
    assert "lost" in text
    assert "GrowingNotes" in text
    assert "LeakSuspect" in text
    assert "Nothing was moved or deleted." in text

    class Notifier:
        def __init__(self) -> None:
            self.codes: list[str] = []

        def notify(self, alert) -> None:  # type: ignore[no-untyped-def]
            self.codes.append(alert.code)

    folder = tmp_path / "weekly"
    notifier = Notifier()
    sent = maybe_weekly(store, Config(), notifier, now=now, directory=folder)
    assert sent is not None
    assert "lost" in (folder / "weekly.txt").read_text(encoding="utf-8")
    assert float((folder / "weekly.last").read_text(encoding="utf-8")) == now
    assert notifier.codes == ["weekly"]
    assert maybe_weekly(store, Config(), notifier, now=now + 3600, directory=folder) is None
    assert notifier.codes == ["weekly"]

    short = HistoryStore(tmp_path / "short.sqlite")
    short.record_sample(make_sample(captured_at=now - 86400))
    short.record_sample(make_sample(captured_at=now))
    short_dir = tmp_path / "short-state"
    assert weekly_due(short, now=now, marker=short_dir / "weekly.last") is False
    assert maybe_weekly(short, Config(), notifier, now=now, directory=short_dir) is None
    assert not (short_dir / "weekly.txt").exists()
    empty = HistoryStore(tmp_path / "one.sqlite")
    assert not weekly_due(empty, now=now, marker=tmp_path / "missing.last")
    empty.close()

    class Dummy:
        def __init__(self, history: HistoryStore) -> None:
            self.store = history

        def observe(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            return []

    sleeps: list[float] = []
    watch_notes = Notifier()
    run_watch_loop(
        Config(),
        probe=lambda: make_sample(),
        notifier=watch_notes,
        sleep=sleeps.append,
        iterations=3,
        tracker=Dummy(store),
        wall_clock=lambda: now,
    )
    assert sleeps == [30, 30]
    assert "weekly" not in watch_notes.codes
    assert not (state_dir() / "weekly.last").exists()
    assert WEEK_SECONDS == 7 * 86400
    store.close()
    short.close()


def test_duplicates_keep_the_better_copy_and_skip_links(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "user"
    documents = root / "Documents"
    downloads = root / "Downloads"
    documents.mkdir(parents=True)
    downloads.mkdir()
    content = b"same-bytes-here"
    older = documents / "photo.bin"
    newer = downloads / "photo.bin"
    older.write_bytes(content)
    newer.write_bytes(content)
    old_time = time.time() - 10 * 86400
    os.utime(older, (old_time, old_time))
    groups, _skipped = find_duplicates([root])
    assert len(groups) == 1
    assert groups[0].keeper.path == str(older)
    assert [item.path for item in groups[0].extras] == [str(newer)]
    assert location_score(str(older)) > location_score(str(newer))
    assert location_score(str(documents / "proj" / "node_modules" / "index.js")) == 0

    monkeypatch.setattr("headroom.dupes.PARTIAL_BYTES", 4)
    assert PARTIAL_BYTES == 64 * 1024
    prefix = tmp_path / "prefix"
    prefix.mkdir()
    left = prefix / "left.bin"
    right = prefix / "right.bin"
    same_a = prefix / "same-a.bin"
    same_b = prefix / "same-b.bin"
    left.write_bytes(b"AAAAXXXX")
    right.write_bytes(b"AAAAYYYY")
    same_a.write_bytes(b"AAAASAME")
    same_b.write_bytes(b"AAAASAME")
    partial_groups, _skipped = find_duplicates([prefix])
    grouped = {tuple(sorted(item.path for item in group.copies)) for group in partial_groups}
    assert tuple(sorted((str(same_a), str(same_b)))) in grouped
    copies = {item.path for group in partial_groups for item in group.copies}
    assert str(left) not in copies
    assert str(right) not in copies

    linked = tmp_path / "linked"
    linked.mkdir()
    real = linked / "real.bin"
    real.write_bytes(content)
    twin = linked / "twin.bin"
    twin.write_bytes(content)
    link = linked / "alias.bin"
    try:
        os.symlink(real, link)
    except (OSError, NotImplementedError):
        return
    before = real.read_bytes()
    link_groups, skipped = find_duplicates([linked])
    assert skipped >= 1
    assert real.read_bytes() == before
    assert link.is_symlink()
    names = {Path(item.path).name for group in link_groups for item in group.copies}
    assert "alias.bin" not in names
    assert {"real.bin", "twin.bin"} <= names


def test_dupes_cli_is_a_dry_run_until_move(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "user"
    documents = root / "Documents"
    downloads = root / "Downloads"
    documents.mkdir(parents=True)
    downloads.mkdir()
    content = b"duplicate-body"
    keeper = documents / "notes.bin"
    extra = downloads / "notes.bin"
    keeper.write_bytes(content)
    extra.write_bytes(content)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)

    assert main(["--no-log", "dupes", "--root", str(root)]) == 0
    dry = capsys.readouterr().out
    assert "Dry run" in dry
    assert extra.is_file() and keeper.is_file()
    planned, _roots = load_plans(state_dir())
    assert [item.path for item in planned] == [str(extra)]

    assert main(["--no-log", "review"]) == 0
    review = capsys.readouterr().out
    assert str(extra) in review
    assert "type MOVE" in review

    assert main(["--no-log", "dupes", "--root", str(root), "--apply"]) == 2
    refused = capsys.readouterr()
    assert "Refusing to move" in refused.err
    assert extra.read_bytes() == content

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt="": "MOVE")
    monkeypatch.setattr("headroom.cli.recycle_file", lambda _path: False)
    assert main(["--no-log", "dupes", "--root", str(root), "--apply"]) == 0
    moved = capsys.readouterr().out
    assert "Moved" in moved
    assert not extra.exists()
    assert keeper.read_bytes() == content
    archived = list((state_dir() / "archive").rglob("notes.bin"))
    assert archived and archived[0].read_bytes() == content


def test_startup_audit_is_reversible(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    impact, _ram = estimate_impact("Discord", r"C:\Users\me\AppData\Discord.exe")
    assert impact == "high"
    rows = entries_from_run_values("HKCU", r"Software\Microsoft\Windows\CurrentVersion\Run", [("OneDrive", "OneDrive.exe")])
    assert rows[0].id == "run:HKCU:OneDrive"
    csv_text = (
        '"TaskName","Status","Schedule Type","Task To Run"\n'
        '"\\Discord","Ready","At logon","discord.exe"\n'
        '"\\Backup","Ready","Daily","backup.exe"\n'
    )
    tasks = entries_from_schtasks_csv(csv_text)
    assert [entry.name for entry in tasks] == ["\\Discord"]

    folder = tmp_path / "Startup"
    backup_dir = tmp_path / "startup-disabled"
    folder.mkdir()
    shortcut = folder / "app.lnk"
    shortcut.write_text("shortcut", encoding="utf-8")
    files = FileCatalog(folder, backup_dir, label="user")
    file_entry = files.list_entries()[0]
    disabled_file = files.disable(file_entry.id)
    assert not shortcut.exists()
    assert (backup_dir / "app.lnk").is_file()
    files.restore(disabled_file)
    assert shortcut.read_text(encoding="utf-8") == "shortcut"
    assert not (backup_dir / "app.lnk").exists()

    values = {"OneDrive": "OneDrive.exe"}
    assert pop_run_value(values, "OneDrive") == "OneDrive.exe"
    assert "OneDrive" not in values
    put_run_value(values, "OneDrive", "OneDrive.exe")
    assert values["OneDrive"] == "OneDrive.exe"

    entry = rows[0]
    catalog = MemoryCatalog([entry])
    disabled = catalog.disable(entry.id)
    append_backup(disabled, tmp_path)
    loaded = load_backup(tmp_path)
    assert loaded[0].id == entry.id
    catalog.restore(loaded[0])
    assert entry.id in catalog.entries
    remove_backup({entry.id}, tmp_path)
    assert load_backup(tmp_path) == []

    live = MemoryCatalog(entries_from_run_values("HKCU", "Run", [("OneDrive", "OneDrive.exe")]))
    monkeypatch.setattr("headroom.cli.default_catalog", lambda: live)
    monkeypatch.setattr("headroom.startup.default_catalog", lambda: live)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert main(["--no-log", "startup", "disable", "run:HKCU:OneDrive"]) == 2
    assert "Refusing to disable" in capsys.readouterr().err
    assert "run:HKCU:OneDrive" in live.entries
    assert not (state_dir() / "startup-backup.json").exists()

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt="": "disable")
    assert main(["--no-log", "startup", "disable", "run:HKCU:OneDrive"]) == 0
    assert "Nothing changed" in capsys.readouterr().out
    assert "run:HKCU:OneDrive" in live.entries

    monkeypatch.setattr("builtins.input", lambda _prompt="": "DISABLE")
    assert main(["--no-log", "startup", "disable", "run:HKCU:OneDrive"]) == 0
    assert "run:HKCU:OneDrive" in live.disabled
    assert (state_dir() / "startup-backup.json").is_file()
    monkeypatch.setattr("builtins.input", lambda _prompt="": "RESTORE")
    assert main(["--no-log", "startup", "restore", "run:HKCU:OneDrive"]) == 0
    assert "run:HKCU:OneDrive" in live.entries
    assert "Restored" in capsys.readouterr().out


def test_downloads_group_old_files_and_installed_setups(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    now = time.time()
    setup = downloads / "ChromeSetup.exe"
    old_pdf = downloads / "notes.pdf"
    fresh = downloads / "fresh.pdf"
    setup.write_bytes(b"installer")
    old_pdf.write_bytes(b"pdf")
    fresh.write_bytes(b"new")
    os.utime(setup, (now - 86400, now - 86400))
    os.utime(old_pdf, (now - 100 * 86400, now - 100 * 86400))
    os.utime(fresh, (now - 86400, now - 86400))
    target = downloads / "fresh.pdf"
    link = downloads / "linked.pdf"
    linked = False
    try:
        os.symlink(target, link)
        linked = True
    except (OSError, NotImplementedError):
        linked = False

    items, skipped = classify_downloads(downloads, ["Google Chrome"], now=now + 5)
    by_name = {Path(item.path).name: item for item in items}
    assert by_name["ChromeSetup.exe"].installed_match == "Google Chrome"
    assert by_name["ChromeSetup.exe"].suggest is True
    assert by_name["ChromeSetup.exe"].bucket == "recent"
    assert by_name["notes.pdf"].kind == "document"
    assert by_name["notes.pdf"].bucket == "90d+"
    assert by_name["notes.pdf"].suggest is True
    assert by_name["fresh.pdf"].suggest is False
    if linked:
        assert skipped >= 1
        assert "linked.pdf" not in by_name
        assert link.is_symlink()
    assert target.read_bytes() == b"new"

    very_old = downloads / "ancient.pdf"
    very_old.write_bytes(b"old")
    os.utime(very_old, (now - 200 * 86400, now - 200 * 86400))
    aged, _skipped = classify_downloads(downloads, ["Google Chrome"], now=now + 5)
    ancient = next(item for item in aged if item.path.endswith("ancient.pdf"))
    assert ancient.bucket == "180d+"
    assert ancient.suggest is True

    monkeypatch.setattr("headroom.cli.installed_program_names", lambda: ["Google Chrome"])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert main(["--no-log", "downloads", "--root", str(downloads)]) == 0
    listed = capsys.readouterr().out
    assert "ChromeSetup.exe" in listed
    assert "Dry run" in listed
    assert main(["--no-log", "review"]) == 0
    review = capsys.readouterr().out
    assert "ChromeSetup.exe" in review
    assert "notes.pdf" in review
    assert main(["--no-log", "downloads", "--root", str(downloads), "--apply"]) == 2
    assert "Refusing to move" in capsys.readouterr().err
    assert setup.read_bytes() == b"installer"
    assert old_pdf.read_bytes() == b"pdf"


def _load_example():
    path = ROOT / "examples" / "plugin" / "headroom_demo_plugin.py"
    spec = importlib.util.spec_from_file_location("headroom_demo_plugin", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_plugins_scan_profiles_and_do_not_replace_builtins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    demo = _load_example()
    tree = tmp_path / "tree"
    tree.mkdir()
    scratch = tree / "a.scratch"
    scratch.write_text("x", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.scratch"
    secret.write_text("no", encoding="utf-8")
    real = tree / "real.txt"
    real.write_text("keep", encoding="utf-8")
    try:
        os.symlink(outside, tree / "linked", target_is_directory=True)
        os.symlink(real, tree / "b.scratch")
    except (OSError, NotImplementedError):
        pass
    hits = demo.scan(tree)
    paths = [hit["path"] for hit in hits]
    assert any(path.endswith("a.scratch") for path in paths)
    assert not any("secret.scratch" in path or path.endswith("real.txt") for path in paths)
    assert demo.profile().startswith("toss name: *.scratch")
    assert "scratch" in demo.report_section({}).casefold()

    class Boom:
        name = "boom"

        def load(self):
            raise RuntimeError("nope")

    class Good:
        name = "scratch"

        def load(self):
            return demo.profile

    loaded = iter_plugins(PROFILES, [Boom(), Good()])
    assert [plugin.name for plugin in loaded] == ["scratch"]

    class StudentEntry:
        name = "student"

        def load(self):
            return lambda: "toss name: *.stolen\n"

    assert profile_plugins([StudentEntry()]) == []
    assert "stolen" not in load_profile_text("student")
    assert "semester: past" in load_profile("student")[0].text or any(
        "semester: past" in rule.text for rule in load_profile("student")
    )

    plugin = Plugin(name="scratch", group=PROFILES, fn=demo.profile)
    monkeypatch.setattr("headroom.plugins.profile_plugins", lambda entries=None: [plugin])
    monkeypatch.setattr("headroom.plugins.profile_text", lambda name, entries=None: demo.profile() if name == "scratch" else None)
    assert load_profile("scratch")
    write_active_profiles(["scratch"], directory=tmp_path)
    with pytest.raises(RuleError):
        write_active_profiles(["nope"], directory=tmp_path)
    assert (tmp_path / "profiles.active").read_text(encoding="utf-8").strip() == "scratch"

    class ScanEntry:
        name = "scratch"

        def load(self):
            return demo.scan

    records = scanner_records([tree], now=time.time(), entries=[ScanEntry()])
    assert any(record.path.endswith("a.scratch") for record in records)

    class ReportEntry:
        name = "scratch"

        def load(self):
            return demo.report_section

    assert "Demo plugin" in report_sections({}, entries=[ReportEntry()])


def test_mcp_tools_are_read_only_or_dry_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from headroom.mcp_tools import TOOLS, tool_analyze, tool_clean, tool_dupes, tool_move, tool_startup_disable

    for fn in (tool_clean, tool_move, tool_startup_disable, tool_dupes):
        source = inspect.getsource(fn)
        assert "move_records" not in source
        assert "os.remove" not in source
        assert ".disable(" not in source
        assert "apply(" not in source

    kept = Path(os.environ["TEMP"]) / "headroom-must-stay.txt"
    kept.write_text("stay", encoding="utf-8")
    assert "Dry run" in tool_clean()
    assert kept.read_text(encoding="utf-8") == "stay"

    twin_root = tmp_path / "twins"
    twin_root.mkdir()
    (twin_root / "one.bin").write_bytes(b"dup")
    (twin_root / "two.bin").write_bytes(b"dup")
    dupes = tool_dupes(str(twin_root))
    assert "Dry run" in dupes
    assert not (state_dir() / "plans.json").exists()
    assert (twin_root / "one.bin").read_bytes() == b"dup"

    root = tmp_path / "docs"
    root.mkdir()
    extra = root / "extra.txt"
    extra.write_bytes(b"still-here")
    replace_plan(
        "dupes",
        [root],
        [
            PlanItem(
                path=str(extra),
                size=extra.stat().st_size,
                accessed=extra.stat().st_atime,
                category="duplicates",
                reason="duplicate of keeper",
                source="dupes",
            )
        ],
    )
    moved = tool_move()
    assert "Dry run" in moved
    assert extra.read_bytes() == b"still-here"
    assert not (state_dir() / "archive").exists()

    folder = tmp_path / "scanme"
    folder.mkdir()
    (folder / "a.txt").write_text("hi", encoding="utf-8")
    analyzed = tool_analyze(str(folder))
    assert "Read-only" in analyzed
    assert not (state_dir() / "analysis.json").exists()

    entry = entries_from_run_values("HKCU", "Run", [("OneDrive", "OneDrive.exe")])[0]
    catalog = MemoryCatalog([entry])
    monkeypatch.setattr("headroom.startup.default_catalog", lambda: catalog)
    described = tool_startup_disable(entry.id)
    assert "Dry run" in described
    assert "Nothing was disabled" in described
    assert entry.id in catalog.entries
    assert not (state_dir() / "startup-backup.json").exists()

    names = [name for name, _description, _fn in TOOLS]
    assert names == [
        "status",
        "top",
        "scan",
        "analyze",
        "report",
        "changes",
        "dupes",
        "startup_list",
        "review_list",
        "clean",
        "move",
        "startup_disable",
    ]

    import asyncio

    from headroom.mcp_server import build_server

    server = build_server()
    listed = asyncio.run(server.list_tools())
    assert {tool.name for tool in listed} == set(names)
    prompts = asyncio.run(server.list_prompts())
    assert {prompt.name for prompt in prompts} == {"health_check"}
    rendered = asyncio.run(server.get_prompt("health_check"))
    message = rendered.messages[0]
    body = message["content"]["text"] if isinstance(message, dict) else message.content.text
    from headroom.mcp_prompts import HEALTH_CHECK_PROMPT

    assert body == HEALTH_CHECK_PROMPT
    starter = (ROOT / "docs" / "ai-starter-prompt.md").read_text(encoding="utf-8")
    assert starter == HEALTH_CHECK_PROMPT
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "### Quick start with AI" in readme
    assert "Paste this into Claude, ChatGPT, or Cursor after connecting the MCP server." in readme
    assert HEALTH_CHECK_PROMPT.strip("\n") in readme


def test_mcp_command_explains_a_missing_sdk(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delitem(sys.modules, "headroom.mcp_server", raising=False)
    monkeypatch.setitem(sys.modules, "mcp.server.mcpserver", None)
    assert main(["--no-log", "mcp"]) == 2
    assert "pip install --upgrade headroom-pc" in capsys.readouterr().err


def test_packaging_and_readme_document_the_exe_and_mcp() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "claude_desktop_config.json" in readme
    assert ".cursor/mcp.json" in readme
    assert '"args": ["mcp"]' in readme
    assert "py -3.12 -m PyInstaller --noconfirm packaging\\headroom.spec" in (
        ROOT / "docs" / "develop.md"
    ).read_text(encoding="utf-8")
    assert "headroom.exe" in readme
    spec = (ROOT / "packaging" / "headroom.spec").read_text(encoding="utf-8")
    assert 'name="headroom"' in spec
    assert "parent.parent" not in spec
    iss = (ROOT / "packaging" / "headroom.iss").read_text(encoding="utf-8")
    assert "headroom.exe" in iss
    winget = (ROOT / "packaging" / "winget" / "JasdishSingh.Headroom.installer.yaml").read_text(encoding="utf-8")
    version = re.search(r"^PackageVersion: (\S+)$", winget, re.MULTILINE).group(1)
    assert version == headroom.__version__
    assert f"/releases/download/v{version}/headroom-{version}-windows-x64.exe" in winget
    sha = re.search(r"^\s*InstallerSha256: ([0-9A-F]{64})$", winget, re.MULTILINE).group(1)
    assert set(sha) != {"0"}
    workflow = (ROOT / ".github" / "workflows" / "test.yml").read_text(encoding="utf-8")
    assert "upload-artifact" in workflow
    assert "dist/headroom.exe" in workflow
    docs = (ROOT / "docs" / "plugins.md").read_text(encoding="utf-8")
    assert "headroom.scanners" in docs


def test_spec_datas_include_every_package_file() -> None:
    spec_path = ROOT / "packaging" / "headroom.spec"
    captured: dict[str, object] = {}

    class Analysis:
        def __init__(self, scripts, **kwargs) -> None:
            captured["scripts"] = scripts
            captured["datas"] = kwargs["datas"]
            self.pure = []
            self.scripts = []
            self.binaries = []
            self.datas = kwargs["datas"]

    namespace = {
        "SPECPATH": str(spec_path.parent),
        "Analysis": Analysis,
        "PYZ": lambda *args, **kwargs: None,
        "EXE": lambda *args, **kwargs: None,
        "__name__": "headroom_spec",
    }
    exec(compile(spec_path.read_text(encoding="utf-8"), str(spec_path), "exec"), namespace)
    scripts = captured["scripts"]
    assert isinstance(scripts, list)
    assert Path(scripts[0]).resolve() == (ROOT / "src" / "headroom" / "__main__.py").resolve()
    datas = captured["datas"]
    assert isinstance(datas, list)
    listed = {Path(source).resolve() for source, _dest in datas}
    package = ROOT / "src" / "headroom"
    expected = [
        path.resolve()
        for path in package.rglob("*")
        if path.is_file() and path.suffix != ".py" and "__pycache__" not in path.parts
    ]
    assert expected
    missing = [str(path.relative_to(package)) for path in expected if path not in listed]
    assert missing == []
    by_name = {Path(source).name: dest for source, dest in datas}
    assert by_name["roomy.png"] == "headroom/pets"
    assert by_name["midnight.png"] == "headroom/pets"
    assert by_name["tower.png"] == "headroom/pets"
    assert by_name["student.rules"] == "headroom/profiles"
    assert by_name["py.typed"] == "headroom"


def test_new_commands_do_not_offer_first_run(tmp_path: Path) -> None:
    for command in ("dashboard", "dupes", "startup", "downloads", "mcp"):
        maybe_offer_first_run(
            command,
            isatty=lambda: True,
            input_fn=lambda _prompt="": (_ for _ in ()).throw(AssertionError("prompted")),
            state=tmp_path / command,
        )
        assert not (tmp_path / command / "first-run.offered").exists()


def test_version_is_public() -> None:
    import headroom

    assert headroom.__version__ == "0.1.0"
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'version = "0.1.0"' in text
    assert "mcp>=2.0,<3" in text
