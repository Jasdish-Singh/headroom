from __future__ import annotations

import os
from pathlib import Path

import pytest

from headroom.cleanup import (
    CleanupGroup,
    DiscoverContext,
    RecycleMeasure,
    UnavailableRecycleBin,
    apply,
    build_preview,
    discover_groups,
)
from headroom.safety import SafetyError
from headroom.watch import tray_preview_text
from headroom.config import Config
from tests.conftest import memory_logger


def _symlink(src: Path, dst: Path) -> None:
    try:
        os.symlink(src, dst, target_is_directory=src.is_dir())
    except OSError as exc:
        pytest.skip(f"symlinks are not available: {exc}")


def _apply(groups, context, recycle=None):
    logger, messages = memory_logger()
    stats = apply(
        groups,
        logger=logger,
        recycle_bin=recycle or UnavailableRecycleBin("not in this test"),
        context=context,
    )
    return stats, messages


def test_dry_run_preview_does_not_delete(tmp_path: Path) -> None:
    temp = tmp_path / "Temp"
    temp.mkdir(exist_ok=True)
    target = temp / "cache.tmp"
    target.write_text("keep", encoding="utf-8")
    groups, context = discover_groups(("user_temp",), {"TEMP": str(temp), "TMP": str(temp)})
    preview = build_preview(groups, UnavailableRecycleBin("no"), context)
    assert preview.has_work
    assert preview.total_bytes == 4
    assert target.read_text(encoding="utf-8") == "keep"


def test_user_temp_deletes_contents_and_leaves_the_root(tmp_path: Path) -> None:
    temp = tmp_path / "work" / "Temp"
    nested = temp / "sub" / "deep"
    nested.mkdir(parents=True)
    (nested / "a.txt").write_text("hello", encoding="utf-8")
    (temp / "b.txt").write_bytes(b"xyz")
    groups, context = discover_groups(("user_temp",), {"TEMP": str(temp)})
    stats, messages = _apply(groups, context)
    assert stats.removed_files == 2
    assert stats.freed_bytes == len("hello") + 3
    assert temp.is_dir()
    assert not (temp / "sub").exists()
    assert not (temp / "b.txt").exists()
    assert any("removed" in message and "a.txt" in message for message in messages)
    assert any(message.startswith("cleanup-summary") for message in messages)


def test_symlinks_are_removed_without_touching_targets(tmp_path: Path) -> None:
    temp = tmp_path / "Temp"
    temp.mkdir(exist_ok=True)
    (temp / "junk.txt").write_text("x", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("keep", encoding="utf-8")
    (outside / "big.bin").write_bytes(b"z" * 1000)
    _symlink(outside, temp / "escape")
    _symlink(secret, temp / "filelink")
    groups, context = discover_groups(("user_temp",), {"TEMP": str(temp)})
    preview = build_preview(groups, UnavailableRecycleBin("no"), context)
    assert preview.total_bytes == 1
    stats, _messages = _apply(groups, context)
    assert stats.removed_links == 2
    assert secret.read_text(encoding="utf-8") == "keep"
    assert (outside / "big.bin").read_bytes() == b"z" * 1000
    assert not (temp / "junk.txt").exists()
    assert not (temp / "escape").exists()
    assert not (temp / "filelink").exists()
    assert temp.is_dir()


def test_locked_file_is_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    temp = tmp_path / "Temp"
    temp.mkdir(exist_ok=True)
    locked = temp / "locked.txt"
    locked.write_text("stay", encoding="utf-8")
    gone = temp / "gone.txt"
    gone.write_text("bye", encoding="utf-8")
    real = __import__("headroom.cleanup", fromlist=["unlink_file"]).unlink_file

    def fake(path: Path) -> None:
        if path.name == "locked.txt":
            raise PermissionError("sharing violation")
        real(path)

    monkeypatch.setattr("headroom.cleanup.unlink_file", fake)
    groups, context = discover_groups(("user_temp",), {"TEMP": str(temp)})
    stats, messages = _apply(groups, context)
    assert locked.read_text(encoding="utf-8") == "stay"
    assert not gone.exists()
    assert stats.removed_files == 1
    assert stats.skipped == 1
    assert any("skipped" in message and "locked.txt" in message for message in messages)


def test_preflight_rejects_documents_before_any_delete(tmp_path: Path) -> None:
    good = tmp_path / "Temp"
    good.mkdir(exist_ok=True)
    (good / "gone.txt").write_text("x", encoding="utf-8")
    documents = tmp_path / "Documents"
    documents.mkdir()
    secret = documents / "taxes.txt"
    secret.write_text("keep", encoding="utf-8")
    groups = [
        CleanupGroup("user_temp", "temp", "directories", paths=(good,)),
        CleanupGroup("user_temp", "temp", "directories", paths=(documents,)),
    ]
    context = DiscoverContext(env={}, home=tmp_path)
    with pytest.raises(SafetyError, match="protected"):
        _apply(groups, context)
    assert (good / "gone.txt").read_text(encoding="utf-8") == "x"
    assert secret.read_text(encoding="utf-8") == "keep"


def test_discover_blocks_temp_pointing_at_documents(tmp_path: Path) -> None:
    documents = tmp_path / "Documents"
    documents.mkdir()
    secret = documents / "taxes.txt"
    secret.write_text("keep", encoding="utf-8")
    groups, context = discover_groups(("user_temp",), {"TEMP": str(documents)})
    assert groups[0].blocked
    assert groups[0].paths == ()
    _apply(groups, context)
    assert secret.read_text(encoding="utf-8") == "keep"


def test_discover_blocks_shared_tmp() -> None:
    groups, _context = discover_groups(("user_temp",), {"TEMP": "/tmp"})
    assert groups[0].blocked
    assert groups[0].paths == ()


def test_windows_temp_is_only_systemroot_temp(tmp_path: Path) -> None:
    system = tmp_path / "Windows"
    target = system / "Temp"
    target.mkdir(parents=True)
    (target / "old.tmp").write_text("x", encoding="utf-8")
    elsewhere = tmp_path / "elsewhere" / "Temp"
    elsewhere.mkdir(parents=True)
    kept = elsewhere / "keep.txt"
    kept.write_text("keep", encoding="utf-8")
    groups, context = discover_groups(("windows_temp",), {"SystemRoot": str(system)})
    assert groups[0].paths == (target,)
    _apply(groups, context)
    assert not (target / "old.tmp").exists()
    assert target.is_dir()
    assert kept.read_text(encoding="utf-8") == "keep"

    forged = [CleanupGroup("windows_temp", "win", "directories", paths=(elsewhere,))]
    with pytest.raises(SafetyError):
        _apply(forged, context)
    assert kept.read_text(encoding="utf-8") == "keep"


def test_windows_temp_inside_documents_is_blocked(tmp_path: Path) -> None:
    system = tmp_path / "Documents" / "Windows"
    target = system / "Temp"
    target.mkdir(parents=True)
    secret = target / "nope.txt"
    secret.write_text("keep", encoding="utf-8")
    groups, context = discover_groups(("windows_temp",), {"SystemRoot": str(system)})
    assert groups[0].blocked
    _apply(groups, context)
    assert secret.read_text(encoding="utf-8") == "keep"


def test_browser_cleanup_keeps_profile_data(tmp_path: Path) -> None:
    local = tmp_path / "Local"
    cases = {
        "chrome_cache": local / "Google" / "Chrome" / "User Data" / "Default",
        "edge_cache": local / "Microsoft" / "Edge" / "User Data" / "Profile 1",
        "firefox_cache": local / "Mozilla" / "Firefox" / "Profiles" / "abc.default-release",
    }
    leaves = {
        "chrome_cache": ("Cache", "Code Cache", "GPUCache"),
        "edge_cache": ("Cache", "Code Cache", "GPUCache"),
        "firefox_cache": ("cache2",),
    }
    for target_id, profile in cases.items():
        for leaf in leaves[target_id]:
            cache = profile / leaf
            cache.mkdir(parents=True)
            (cache / "data.bin").write_bytes(b"x" * 8)
        (profile / "Bookmarks").write_text("keep-bookmarks", encoding="utf-8")
        (profile / "Login Data").write_text("keep-secrets", encoding="utf-8")
        (profile / "History").write_text("keep-history", encoding="utf-8")
    env = {"LOCALAPPDATA": str(local)}
    groups, context = discover_groups(("chrome_cache", "edge_cache", "firefox_cache"), env)
    stats, _messages = _apply(groups, context)
    assert stats.removed_files == 3 + 3 + 1
    for target_id, profile in cases.items():
        assert (profile / "Bookmarks").read_text(encoding="utf-8") == "keep-bookmarks"
        assert (profile / "Login Data").read_text(encoding="utf-8") == "keep-secrets"
        assert (profile / "History").read_text(encoding="utf-8") == "keep-history"
        for leaf in leaves[target_id]:
            cache = profile / leaf
            assert cache.is_dir()
            assert not (cache / "data.bin").exists()


def test_browser_cache_symlink_is_not_followed(tmp_path: Path) -> None:
    local = tmp_path / "Local"
    profile = local / "Google" / "Chrome" / "User Data" / "Default"
    profile.mkdir(parents=True)
    real_cache = tmp_path / "real-cache"
    real_cache.mkdir()
    secret = real_cache / "keep.bin"
    secret.write_bytes(b"abc")
    _symlink(real_cache, profile / "Cache")
    (profile / "Bookmarks").write_text("keep", encoding="utf-8")
    groups, context = discover_groups(("chrome_cache",), {"LOCALAPPDATA": str(local)})
    assert groups[0].paths == ()
    _apply(groups, context)
    assert secret.read_bytes() == b"abc"
    assert (profile / "Bookmarks").read_text(encoding="utf-8") == "keep"


def test_thumbnail_cleanup_is_only_thumbcache_files(tmp_path: Path) -> None:
    local = tmp_path / "Local"
    explorer = local / "Microsoft" / "Windows" / "Explorer"
    explorer.mkdir(parents=True)
    thumb = explorer / "thumbcache_96.db"
    thumb.write_bytes(b"abcd")
    icon = explorer / "iconcache_16.db"
    icon.write_bytes(b"zzzz")
    ini = explorer / "desktop.ini"
    ini.write_text("nope", encoding="utf-8")
    groups, context = discover_groups(("thumbnail_cache",), {"LOCALAPPDATA": str(local)})
    assert [path.name for path in groups[0].paths] == ["thumbcache_96.db"]
    _apply(groups, context)
    assert not thumb.exists()
    assert icon.read_bytes() == b"zzzz"
    assert ini.read_text(encoding="utf-8") == "nope"
    assert explorer.is_dir()


def test_unknown_target_id_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(Exception, match="unknown cleanup target"):
        discover_groups(("C:/Users/me/Documents",), {"TEMP": str(tmp_path / "Temp")})


def test_recycle_bin_empties_only_after_apply() -> None:
    class FakeBin:
        def __init__(self) -> None:
            self.emptied = False

        def measure(self) -> RecycleMeasure:
            return RecycleMeasure(50, 2, True, "")

        def empty(self) -> None:
            self.emptied = True

    fake = FakeBin()
    groups, context = discover_groups(("recycle_bin",), {})
    preview = build_preview(groups, fake, context)
    assert preview.has_work
    assert preview.total_bytes == 50
    assert fake.emptied is False
    stats, messages = _apply(groups, context, recycle=fake)
    assert fake.emptied is True
    assert stats.recycle_emptied is True
    assert any("recycle_bin" in message for message in messages)


def test_unavailable_recycle_bin_is_not_emptied() -> None:
    class ExplodingBin(UnavailableRecycleBin):
        def empty(self) -> None:
            raise AssertionError("empty should not be called")

    backend = ExplodingBin("Recycle Bin is available on Windows only")
    groups, context = discover_groups(("recycle_bin",), {})
    preview = build_preview(groups, backend, context)
    assert preview.has_work is False
    assert "Windows only" in preview.groups[0].note
    stats, _messages = _apply(groups, context, recycle=backend)
    assert stats.recycle_emptied is False


def test_tray_preview_does_not_delete(tmp_path: Path) -> None:
    temp = tmp_path / "Temp"
    temp.mkdir(exist_ok=True)
    target = temp / "a.txt"
    target.write_text("keep", encoding="utf-8")
    text = tray_preview_text(
        Config(enabled_cleanup=("user_temp",)),
        env={"TEMP": str(temp), "TMP": str(temp)},
    )
    assert "Dry run" in text
    assert "Nothing will be deleted" in text
    assert target.read_text(encoding="utf-8") == "keep"
