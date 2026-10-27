from __future__ import annotations

from pathlib import Path

import pytest

from headroom.cli import confirm_interactive, main
from headroom.config import Config
from headroom.probe import DiskStatus
from headroom.render import format_preview, format_status, status_is_attention
from headroom.cleanup import GroupPreview, Preview
from tests.conftest import GB, make_sample


def test_global_flags_work_before_or_after_the_subcommand(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("headroom.probe.sample", lambda: make_sample())
    assert main(["status", "--no-log"]) == 0
    assert "Headroom status" in capsys.readouterr().out
    assert main(["--no-log", "status"]) == 0
    log = tmp_path / "home" / ".local" / "share" / "headroom" / "headroom.log"
    assert not log.exists()


def test_version_and_usage() -> None:
    with pytest.raises(SystemExit) as version:
        main(["--version"])
    assert version.value.code == 0
    with pytest.raises(SystemExit) as usage:
        main([])
    assert usage.value.code == 2


def test_status_exit_codes(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr("headroom.probe.sample", lambda: make_sample())
    assert main(["--no-log", "status"]) == 0
    out = capsys.readouterr().out
    assert "Headroom status" in out
    assert "50.0%" in out
    assert "42.0 GB" in out
    assert "Config: defaults" in out

    low = make_sample(
        ram_percent=91,
        disks=(DiskStatus("C:\\", "C:", 477 * GB, 467 * GB, 10 * GB, "C:"),),
    )
    monkeypatch.setattr("headroom.probe.sample", lambda: low)
    assert main(["--no-log", "status"]) == 1
    attention = capsys.readouterr().out
    assert "critical" in attention
    assert "RAM is above" in attention
    assert status_is_attention(low, Config()) is True
    assert status_is_attention(make_sample(), Config()) is False


def test_status_text_mentions_commit_and_pagefile() -> None:
    text = format_status(make_sample(), Config(), None)
    assert "Commit" in text
    assert "Page file" in text
    assert "C:\\" in text


def test_top_uses_mocked_processes(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    from headroom.processes import ProcessMem

    monkeypatch.setattr(
        "headroom.processes.top_processes",
        lambda limit: [ProcessMem(10, "chrome", 1024**3), ProcessMem(11, "code", 1024**2)],
    )
    monkeypatch.setattr("headroom.probe.sample", lambda: make_sample())
    assert main(["--no-log", "top", "-n", "2"]) == 0
    out = capsys.readouterr().out
    assert "chrome" in out
    assert "code" in out


def test_scan_command(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path / "photos"
    folder = root / "raw"
    folder.mkdir(parents=True)
    (folder / "a.bin").write_bytes(b"x" * 2048)
    (root / "note.txt").write_text("hi", encoding="utf-8")
    assert main(["--no-log", "scan", str(root), "--top", "5"]) == 0
    out = capsys.readouterr().out
    assert "raw" in out
    assert "not followed" in out
    assert main(["--no-log", "scan", str(tmp_path / "missing")]) == 2


def test_clean_dry_run_keeps_files(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    temp = tmp_path / "Temp"
    # The autouse fixture already created Temp and pointed TEMP at it.
    target = temp / "session.tmp"
    target.write_text("keep-me", encoding="utf-8")
    assert main(["--no-log", "clean", "--dry-run", "--only", "user_temp"]) == 0
    out = capsys.readouterr().out
    assert "Dry run" in out
    assert "Nothing will be deleted" in out
    assert "session.tmp" in out or "user_temp" in out
    assert target.read_text(encoding="utf-8") == "keep-me"


def test_clean_refuses_without_a_tty(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    target = tmp_path / "Temp" / "session.tmp"
    target.write_text("keep-me", encoding="utf-8")
    assert main(["--no-log", "clean", "--only", "user_temp"]) == 0
    captured = capsys.readouterr()
    assert "Nothing deleted." in captured.out
    assert "interactive" in captured.err
    assert target.read_text(encoding="utf-8") == "keep-me"


def test_clean_deletes_after_explicit_yes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    temp = tmp_path / "Temp"
    target = temp / "session.tmp"
    target.write_text("bye", encoding="utf-8")
    thumb_dir = tmp_path / "local" / "Microsoft" / "Windows" / "Explorer"
    thumb_dir.mkdir(parents=True)
    thumb = thumb_dir / "thumbcache_32.db"
    thumb.write_bytes(b"1234")
    monkeypatch.setattr("headroom.cli.confirm_interactive", lambda: True)
    log_path = tmp_path / "headroom.log"
    assert main(["--log", str(log_path), "clean", "--only", "user_temp"]) == 0
    out = capsys.readouterr().out
    assert "Removed 1 file" in out
    assert not target.exists()
    assert thumb.read_bytes() == b"1234"
    log_text = log_path.read_text(encoding="utf-8")
    assert "removed" in log_text
    assert "session.tmp" in log_text


def test_clean_only_rejects_paths(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--no-log", "clean", "--dry-run", "--only", r"C:\Users\me\Documents"]) == 2
    assert "unknown cleanup target" in capsys.readouterr().err


def test_confirm_requires_exact_yes() -> None:
    answers = iter(["yes", "Y", " YES "])

    def read(_prompt: str) -> str:
        return next(answers)

    assert confirm_interactive(input_fn=read, isatty=lambda: True) is False
    assert confirm_interactive(input_fn=read, isatty=lambda: True) is False
    assert confirm_interactive(input_fn=read, isatty=lambda: True) is True
    assert confirm_interactive(isatty=lambda: False) is False
    assert confirm_interactive(input_fn=lambda _prompt: (_ for _ in ()).throw(EOFError()), isatty=lambda: True) is False


def test_watch_once(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    monkeypatch.setattr("headroom.watch.sample_system", lambda: make_sample(ram_percent=10))
    config = tmp_path / "fast.toml"
    config.write_text(
        "[thresholds]\nram_percent = 85\nram_sustained_seconds = 0\n",
        encoding="utf-8",
    )
    assert main(["--no-log", "--config", str(config), "watch", "--once", "--no-tray"]) == 0
    out = capsys.readouterr().out
    assert "Headroom watch" in out
    assert "RAM 10.0%" in out

    monkeypatch.setattr("headroom.watch.sample_system", lambda: make_sample(ram_percent=96))
    assert main(["--no-log", "--config", str(config), "watch", "--once", "--no-tray"]) == 0
    err = capsys.readouterr().err
    assert "RAM is high" in err


def test_missing_config_exits_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--no-log", "--config", str(tmp_path / "nope.toml"), "status"]) == 2
    assert "not found" in capsys.readouterr().err


def test_preview_formatter_marks_skips() -> None:
    preview = Preview(
        groups=(
            GroupPreview(
                id="user_temp",
                description="User temporary files (%TEMP%)",
                blocked=True,
                note="temp path includes protected folder 'Documents'",
                paths=(),
                total_bytes=0,
                files=0,
                links=0,
                errors=0,
                supported=False,
            ),
        )
    )
    text = format_preview(preview, dry_run=True)
    assert "SKIPPED" in text
    assert "Documents" in text
    assert "Nothing will be deleted" in text
