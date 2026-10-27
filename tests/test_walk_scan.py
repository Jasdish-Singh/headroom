from __future__ import annotations

import hashlib
import os
import stat
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from headroom.analyze import scan
from headroom.dupes import find_duplicates
from headroom.safety import SafetyError, assert_contained
from headroom.scan import scan_path
from headroom.walk import (
    classify_from_stat,
    hash_file,
    list_child_names,
    measure_tree,
    plan_deletions,
)


def _symlink(src: Path, dst: Path) -> None:
    try:
        os.symlink(src, dst, target_is_directory=src.is_dir())
    except OSError as exc:
        pytest.skip(f"symlinks are not available: {exc}")


def test_reparse_point_is_a_link_even_when_it_looks_like_a_directory() -> None:
    junction = SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
    normal_dir = SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x10)
    normal_file = SimpleNamespace(st_mode=stat.S_IFREG, st_file_attributes=0x20)
    assert classify_from_stat(is_symlink=False, stat_result=junction, stat_error=False) == "link"
    assert classify_from_stat(is_symlink=False, stat_result=normal_dir, stat_error=False) == "dir"
    assert classify_from_stat(is_symlink=False, stat_result=normal_file, stat_error=False) == "file"
    assert classify_from_stat(is_symlink=True, stat_result=normal_dir, stat_error=False) == "link"
    assert classify_from_stat(is_symlink=False, stat_result=None, stat_error=True) == "error"


def test_hashes_read_raw_bytes(tmp_path: Path) -> None:
    folder = tmp_path / "Pictures"
    folder.mkdir()
    header = b"\x89PNG\r\n\x1a\n"
    first = folder / "cat.png"
    second = folder / "dog.png"
    first.write_bytes(header + b"cat-pixels\r\n")
    second.write_bytes(header + b"dog-pixels\r\n")
    assert hash_file(first) == hashlib.sha256(first.read_bytes()).hexdigest()
    assert hash_file(first, 9) == hashlib.sha256(first.read_bytes()[:9]).hexdigest()
    assert hash_file(first) != hash_file(second)
    assert hash_file(folder / "missing.png") is None

    groups, _skipped = find_duplicates([folder])
    assert groups == []
    records = scan([folder], now=time.time()).records
    assert [record.duplicate_of for record in records] == [None, None]


def test_scan_reports_immediate_children(tmp_path: Path) -> None:
    root = tmp_path / "data"
    big = root / "big"
    small = root / "small"
    big.mkdir(parents=True)
    small.mkdir()
    (big / "file.bin").write_bytes(b"x" * 1000)
    (small / "file.bin").write_bytes(b"x" * 10)
    (root / "note.txt").write_bytes(b"hi")
    result = scan_path(root, top=10)
    assert result.total_bytes == 1012
    assert result.skipped_links == 0
    assert [entry.name for entry in result.entries] == ["big", "small", "note.txt"]
    assert result.entries[0].size == 1000


def test_scan_skips_symlink_targets(tmp_path: Path) -> None:
    root = tmp_path / "data"
    root.mkdir()
    (root / "note.txt").write_bytes(b"hi")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "huge.bin").write_bytes(b"z" * 5000)
    _symlink(outside, root / "linked-dir")
    _symlink(outside / "huge.bin", root / "linked-file")

    result = scan_path(root, top=10)
    assert result.total_bytes == 2
    assert result.skipped_links == 2
    assert [entry.name for entry in result.entries] == ["note.txt"]

    with pytest.raises(SafetyError, match="link"):
        scan_path(root / "linked-dir", top=5)


def test_scan_top_omits_smaller_entries(tmp_path: Path) -> None:
    root = tmp_path / "data"
    root.mkdir()
    for index in range(3):
        (root / f"f{index}.txt").write_bytes(b"x" * (index + 1))
    result = scan_path(root, top=1)
    assert result.entries[0].name == "f2.txt"
    assert result.omitted == 2
    assert result.total_bytes == 6


@pytest.mark.skipif(os.name == "nt", reason="Windows may fall back to path scandir")
def test_scan_does_not_follow_paths_with_scandir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "data"
    nested = root / "nested"
    nested.mkdir(parents=True)
    (nested / "a.txt").write_text("hello", encoding="utf-8")
    real_scandir = os.scandir

    def guarded(argument):
        if isinstance(argument, int):
            return real_scandir(argument)
        raise AssertionError(f"scandir was called on a path: {argument!r}")

    monkeypatch.setattr(os, "scandir", guarded)
    result = scan_path(root, top=5)
    assert result.total_bytes == 5


@pytest.mark.skipif(os.name == "nt", reason="Windows may fall back to path scandir")
def test_measure_uses_file_descriptors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "Temp"
    root.mkdir(exist_ok=True)
    (root / "a.txt").write_bytes(b"abcd")
    real_scandir = os.scandir

    def guarded(argument):
        if isinstance(argument, int):
            return real_scandir(argument)
        raise AssertionError(f"scandir was called on a path: {argument!r}")

    monkeypatch.setattr(os, "scandir", guarded)
    assert measure_tree(root).bytes == 4


def test_list_child_names_does_not_open_a_symlink(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("keep", encoding="utf-8")
    link = tmp_path / "link"
    _symlink(outside, link)

    def forbidden(argument):
        raise AssertionError(f"should not scandir {argument!r}")

    monkeypatch.setattr(os, "scandir", forbidden)
    assert list_child_names(link) is None
    assert (outside / "secret.txt").read_text(encoding="utf-8") == "keep"


def test_plan_does_not_include_symlink_targets(tmp_path: Path) -> None:
    root = tmp_path / "Temp"
    root.mkdir(exist_ok=True)
    (root / "real.txt").write_text("x", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("keep", encoding="utf-8")
    _symlink(outside, root / "escape")
    planned = plan_deletions(root)
    names = [path.name for _, path in planned]
    assert "real.txt" in names
    assert "escape" in names
    assert "secret.txt" not in names
    for _, path in planned:
        assert_contained(root, path)


def test_unreadable_child_is_an_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "Temp"
    blocked = root / "blocked"
    blocked.mkdir(parents=True)
    (blocked / "secret.txt").write_text("x", encoding="utf-8")
    (root / "ok.txt").write_bytes(b"ab")
    real = list_child_names

    def fake(path: Path):
        if path == blocked:
            raise PermissionError("denied")
        return real(path)

    monkeypatch.setattr("headroom.walk.list_child_names", fake)
    measured = measure_tree(root)
    assert measured.bytes == 2
    assert measured.errors == 1
