"""Profile loading, rule merging, read-only analysis, and approved moves."""

from __future__ import annotations

import os
import sys
import time
from datetime import date
from pathlib import Path

import pytest

from headroom.analyze import (
    classify_entry,
    format_analysis,
    importance,
    load_analysis,
    save_analysis,
    scan,
)
from headroom.cli import main
from headroom.firstrun import maybe_offer_first_run
from headroom.review import (
    ReviewError,
    confirm_move,
    MOVE_PROMPT,
    format_review,
    group_suggestions,
    move_records,
    parse_group_selection,
    recycle_file,
    suggestions_for,
)
from headroom.rules import (
    PROFILE_NAMES,
    Candidate,
    RuleError,
    active_profiles_path,
    append_user_text,
    current_semester,
    decide,
    load_merged_rules,
    load_profile,
    load_profile_text,
    merge_rules,
    parse_rules,
    read_active_profiles,
    read_user_text,
    semesters_in_path,
    state_dir,
    write_active_profiles,
)
from headroom.safety import KNOWN_CLEANUP_IDS
from headroom.analyze import FileRecord
from headroom.walk import classify_path

DAY = 86400
SEASON_NAMES = {1: "Spring", 2: "Summer", 3: "Fall", 4: "Winter"}


def _season_folder(year: int, season: int) -> str:
    return f"{SEASON_NAMES[season]} {year}"


def _touch(path: Path, when: float) -> None:
    os.utime(path, (when, when))


def _candidate(path: str, *, accessed: float, size: int = 100, category: str = "old", is_dir: bool = False) -> Candidate:
    return Candidate(path=path, size=size, accessed=accessed, category=category, is_dir=is_dir)


def _record(path: Path, *, size: int | None = None, accessed: float, category: str = "old", is_dir: bool = False) -> FileRecord:
    actual = size if size is not None else (0 if is_dir else path.stat().st_size)
    return FileRecord(
        path=str(path),
        size=actual,
        accessed=accessed,
        category=category,
        importance=importance(category, accessed, accessed),
        is_dir=is_dir,
        base_category=category,
    )


def _snapshot(root: Path) -> dict[str, bytes]:
    found: dict[str, bytes] = {}
    for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
        for name in filenames:
            path = Path(dirpath) / name
            if path.is_symlink():
                continue
            found[str(path)] = path.read_bytes()
    return found


def _symlink(src: Path, dst: Path) -> None:
    try:
        os.symlink(src, dst, target_is_directory=src.is_dir())
    except OSError as exc:
        pytest.skip(f"symlinks are not available: {exc}")


def test_cleanup_allowlist_is_unchanged() -> None:
    assert KNOWN_CLEANUP_IDS == (
        "user_temp",
        "windows_temp",
        "chrome_cache",
        "edge_cache",
        "firefox_cache",
        "thumbnail_cache",
        "recycle_bin",
    )


def test_shipped_profiles_parse_as_plain_text() -> None:
    assert PROFILE_NAMES == ("student", "developer", "gamer")
    for name in PROFILE_NAMES:
        text = load_profile_text(name)
        assert text.startswith("#")
        rules = load_profile(name)
        assert rules
        assert any(rule.action == "keep" for rule in rules)
        assert any(rule.action == "toss" for rule in rules)
        assert all(rule.source == name for rule in rules)
    student = load_profile_text("student")
    assert "semester: past" in student
    assert "syllabus" in student
    assert "course-shell" in student
    developer = load_profile_text("developer")
    assert "node_modules" in developer
    assert "newer-than: 14d" in developer
    assert "older-than: 14d" in developer
    gamer = load_profile_text("gamer")
    assert "steamapps" in gamer
    assert "shadercache" in gamer
    assert "Saved Games" in gamer or "saved games" in gamer


def test_rule_lines_require_a_condition() -> None:
    with pytest.raises(RuleError, match="name, path"):
        parse_rules("toss\n", source="user")
    with pytest.raises(RuleError, match="keep or toss"):
        parse_rules("delete name: *.pdf\n", source="user")
    with pytest.raises(RuleError, match="expected pairs"):
        parse_rules("keep everything\n", source="user")
    with pytest.raises(RuleError, match="semester"):
        parse_rules("toss semester: next\n", source="user")
    with pytest.raises(RuleError, match="unknown category"):
        parse_rules("toss category: photos\n", source="user")
    with pytest.raises(RuleError, match="twice"):
        parse_rules("keep name: a name: b\n", source="user")
    with pytest.raises(RuleError, match="older-than"):
        parse_rules("toss older-than: -1d\n", source="user")
    assert parse_rules("# comment\n\n", source="user") == []


def test_rule_conditions_and_sizes() -> None:
    rules = parse_rules(
        'toss ext: py older-than: 14D larger-than: 1.5KB name: "draft*"\n',
        source="user",
    )
    rule = rules[0]
    assert rule.ext == ".py"
    assert rule.older_than_days == 14
    assert rule.larger_than_bytes == int(1.5 * 1024)
    assert rule.name == "draft*"
    now = 1_800_000_000.0
    today = date(2026, 10, 26)
    matched = _candidate("notes/draft_final.py", accessed=now - 14 * DAY, size=2048)
    assert decide(rules, matched, now=now, today=today) == "toss"
    recent = _candidate("notes/draft_final.py", accessed=now - 13 * DAY, size=2048)
    assert decide(rules, recent, now=now, today=today) == "unmatched"
    small = _candidate("notes/draft_final.py", accessed=now - 20 * DAY, size=100)
    assert decide(rules, small, now=now, today=today) == "unmatched"


def test_semester_calendar_and_past_rule() -> None:
    assert current_semester(date(2026, 1, 1)) == (2026, 1)
    assert current_semester(date(2026, 5, 15)) == (2026, 1)
    assert current_semester(date(2026, 5, 16)) == (2026, 2)
    assert current_semester(date(2026, 8, 15)) == (2026, 2)
    assert current_semester(date(2026, 8, 16)) == (2026, 3)
    assert current_semester(date(2026, 12, 15)) == (2026, 3)
    assert current_semester(date(2026, 12, 16)) == (2026, 4)
    assert semesters_in_path(r"School\2020 Fall\homework") == [(2020, 3)]
    assert semesters_in_path("Autumn 2019 notes") == [(2019, 3)]

    today = date(2026, 10, 26)
    now = 1_800_000_000.0
    rules = parse_rules("toss path: */homework/* semester: past\n", source="student")
    past = _candidate("School/Fall 2020/homework/essay.docx", accessed=now)
    current = _candidate("School/Fall 2026/homework/essay.docx", accessed=now)
    both = _candidate("School/Fall 2020/Fall 2026/homework/essay.docx", accessed=now)
    assert decide(rules, past, now=now, today=today) == "toss"
    assert decide(rules, current, now=now, today=today) == "unmatched"
    assert decide(rules, both, now=now, today=today) == "unmatched"


def test_student_profile_keeps_course_material() -> None:
    rules = load_profile("student")
    today = date.today()
    now = time.time()
    year, season = current_semester(today)
    past = _season_folder(year - 1, season)
    current = _season_folder(year, season)

    def verdict(path: str) -> str:
        return decide(rules, _candidate(path, accessed=now, category="school"), now=now, today=today)

    assert verdict(f"Documents/{past}/CS101/homework/essay.docx") == "toss"
    assert verdict(f"Documents/{past}/CS101/assignments/lab1.pdf") == "toss"
    assert verdict(f"Documents/{past}/CS101/homework/syllabus.pdf") == "keep"
    assert verdict(f"Documents/{past}/CS101/modules/lecture.pdf") == "keep"
    assert verdict(f"Documents/{past}/course-shell/index.html") == "keep"
    assert verdict(f"Documents/{current}/CS101/homework/essay.docx") == "unmatched"
    modules = parse_rules("keep path: */modules/*\n", source="student")
    assert decide(modules, _candidate("proj/node_modules/index.js", accessed=now), now=now, today=today) == "unmatched"


def test_developer_profile_keeps_source_and_fresh_builds() -> None:
    rules = load_profile("developer")
    today = date.today()
    now = time.time()

    def verdict(path: str, *, days: float = 0, is_dir: bool = False, category: str = "code") -> str:
        item = _candidate(path, accessed=now - days * DAY, category=category, is_dir=is_dir)
        return decide(rules, item, now=now, today=today)

    assert verdict("proj/node_modules", is_dir=True, category="caches") == "toss"
    assert verdict("proj/node_modules/leftpad/index.js", category="caches") == "toss"
    assert verdict("proj/src/main.py") == "keep"
    assert verdict("proj/pyproject.toml") == "keep"
    assert verdict("proj/build/app.o", days=15, category="old") == "toss"
    assert verdict("proj/build/app.o", days=14, category="old") == "toss"
    assert verdict("proj/build/fresh.o", days=1, category="old") == "keep"
    assert verdict("proj/src/main.pyc", days=20, category="caches") == "toss"
    assert verdict("proj/.git/config", category="code") == "keep"


def test_gamer_profile_keeps_installs_and_saves() -> None:
    rules = load_profile("gamer")
    today = date.today()
    now = time.time()

    def verdict(path: str, *, days: float = 0) -> str:
        return decide(rules, _candidate(path, accessed=now - days * DAY, category="games"), now=now, today=today)

    assert verdict("Games/Steam/steamapps/common/Game/game.exe") == "keep"
    assert verdict("Downloads/GameSetup.exe", days=40) == "toss"
    assert verdict("Downloads/GameSetup.exe", days=1) == "unmatched"
    assert verdict("Downloads/game.iso", days=31) == "toss"
    assert verdict("NVIDIA/shadercache/a.bin") == "toss"
    assert verdict("Users/Saved Games/shadercache/slot.dat") == "keep"
    assert verdict("Steam/steamapps/common/setup.exe", days=40) == "keep"
    assert verdict("Users/Saved Games/Skyrim/slot1.dat") == "keep"


def test_merge_appends_and_user_rules_win() -> None:
    today = date.today()
    now = time.time()
    year, season = current_semester(today)
    past = _season_folder(year - 1, season)
    essay = _candidate(f"Documents/{past}/CS101/homework/essay.docx", accessed=now, category="school")
    essay_py = _candidate(f"Documents/{past}/CS101/homework/essay.py", accessed=now, category="code")
    student = load_profile("student")
    developer = load_profile("developer")
    assert decide(student, essay, now=now, today=today) == "toss"
    assert decide(student, essay_py, now=now, today=today) == "toss"
    combined = merge_rules([student, developer])
    assert combined[: len(student)] == list(student)
    assert decide(combined, essay_py, now=now, today=today) == "keep"
    user = parse_rules("keep name: *essay*\n", source="user")
    overridden = merge_rules([student, user])
    assert decide(overridden, essay, now=now, today=today) == "keep"
    assert overridden[-1].source == "user"


def test_active_profiles_and_user_file_round_trip(tmp_path: Path) -> None:
    directory = tmp_path / "state"
    with pytest.raises(RuleError, match="unknown profile"):
        write_active_profiles(["student", "nope"], directory)
    assert not active_profiles_path(directory).exists()
    assert write_active_profiles(["Developer", "student", "developer"], directory) == ["developer", "student"]
    assert read_active_profiles(directory) == ["developer", "student"]
    assert write_active_profiles(["none"], directory) == []
    assert read_active_profiles(directory) == []

    with pytest.raises(RuleError, match="no keep or toss"):
        append_user_text("# just a note\n", directory)
    assert read_user_text(directory) == ""
    append_user_text("keep name: thesis\n", directory)
    with pytest.raises(RuleError):
        append_user_text("toss\n", directory)
    assert read_user_text(directory) == "keep name: thesis\n"
    append_user_text("toss ext: .tmp older-than: 10d", directory)
    saved = read_user_text(directory)
    assert "thesis" in saved
    assert "ext: .tmp" in saved
    write_active_profiles(["student"], directory)
    merged = load_merged_rules(directory)
    assert merged[0].source == "student"
    assert merged[-1].source == "user"
    assert merged[-1].text.startswith("toss ext:")


def test_importance_and_categories() -> None:
    now = 1_800_000_000.0
    assert importance("school", now, now) == 90
    assert importance("school", now - 200 * DAY, now) == 65
    assert importance("code", now, now) == 95
    assert importance("caches", now - 200 * DAY, now) == 0
    assert importance("old", now - 14 * DAY, now) == 35
    assert importance("old", now - 15 * DAY, now) == 25
    assert importance("old", now - 180 * DAY, now) == 10
    assert classify_entry("/proj/src/main.py", is_dir=False, inside_project=True) == "code"
    assert classify_entry("/proj/node_modules/index.js", is_dir=False, inside_project=True) == "caches"
    assert classify_entry("/Games/Steam/steamapps/common/Game/game.exe", is_dir=False, inside_project=False) == "games"
    assert classify_entry("/Downloads/setup.exe", is_dir=False, inside_project=False) == "installers"
    assert classify_entry("/Downloads/photo.jpg", is_dir=False, inside_project=False) == "media"
    assert classify_entry("/Documents/Fall 2020/homework/essay.docx", is_dir=False, inside_project=False) == "school"
    assert classify_entry("/Downloads/notes.bin", is_dir=False, inside_project=False) == "downloads"
    assert classify_entry("/misc/random.bin", is_dir=False, inside_project=False) == "old"
    assert classify_entry("/AppData/NVIDIA/DXCache", is_dir=True, inside_project=False) == "caches"
    assert classify_entry("/home/Saved Games/Skyrim/save.dat", is_dir=False, inside_project=False) == "games"


def test_scan_is_read_only_and_categorizes(tmp_path: Path) -> None:
    now = time.time()
    today = date.today()
    year, season = current_semester(today)
    past = _season_folder(year - 1, season)
    root = tmp_path / "pc"
    homework = root / "Documents" / past / "CS101" / "homework"
    modules = root / "Documents" / past / "CS101" / "modules"
    downloads = root / "Downloads"
    project = root / "dev" / "demo"
    games = root / "Games" / "Steam"
    saves = root / "Games" / "Saved Games" / "Skyrim"
    dup = root / "dup"
    for directory in (homework, modules, downloads, project / "src", project / "node_modules" / "pkg", project / "build", games / "steamapps" / "common" / "Game", games / "shadercache", saves, dup):
        directory.mkdir(parents=True)
    files = {
        homework / "essay.docx": b"essay-body",
        homework / "syllabus.pdf": b"syllabus",
        modules / "lecture.pdf": b"lecture",
        root / "Documents" / "notes.txt": b"notes",
        downloads / "GameSetup.exe": b"installer-bytes",
        downloads / "photo.jpg": b"jpg",
        downloads / "notes.bin": b"download",
        project / "pyproject.toml": b"[project]\nname='demo'\n",
        project / "src" / "main.py": b"print('hi')\n",
        project / "node_modules" / "pkg" / "index.js": b"module.exports = 1\n",
        project / "build" / "app.o": b"old-object",
        project / "build" / "fresh.o": b"new-object",
        games / "steamapps" / "common" / "Game" / "game.exe": b"game",
        games / "shadercache" / "cache.bin": b"cache",
        saves / "slot1.dat": b"save",
    }
    for path, body in files.items():
        path.write_bytes(body)
        _touch(path, now)
    _touch(downloads / "GameSetup.exe", now - 40 * DAY)
    _touch(project / "build" / "app.o", now - 20 * DAY)
    _touch(root / "Documents" / "notes.txt", now - 200 * DAY)
    same = b"same-bytes-same-bytes"
    different = b"zzzz-bytes-same-size"
    (dup / "same-a.bin").write_bytes(same)
    (dup / "same-b.bin").write_bytes(same)
    (dup / "different.bin").write_bytes(different)
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"do-not-read-or-move")
    link = root / "shortcut.txt"
    try:
        os.symlink(outside, link)
        linked = True
    except OSError:
        linked = False
    expected = dict(files)
    expected[dup / "same-a.bin"] = same
    expected[dup / "same-b.bin"] = same
    expected[dup / "different.bin"] = different

    analysis = scan([root], now=now)
    report = format_analysis(analysis)
    assert analysis.records
    assert "Read-only. Nothing was moved or deleted." in report
    assert _snapshot(root) == {str(path): body for path, body in expected.items()}
    assert outside.read_bytes() == b"do-not-read-or-move"
    if linked:
        assert analysis.skipped_links >= 1
        assert link.is_symlink()
    assert all("outside.txt" not in record.path for record in analysis.records)
    assert not (tmp_path / "archive").exists()

    by_name = {Path(record.path).name: record for record in analysis.records}
    assert by_name["essay.docx"].category == "school"
    assert by_name["essay.docx"].importance == 90
    assert by_name["photo.jpg"].category == "media"
    assert by_name["notes.bin"].category == "downloads"
    assert by_name["GameSetup.exe"].category == "installers"
    assert by_name["main.py"].category == "code"
    assert by_name["game.exe"].category == "games"
    assert by_name["slot1.dat"].category == "games"
    assert by_name["notes.txt"].stale is True
    assert by_name["node_modules"].is_dir is True
    assert by_name["node_modules"].category == "caches"
    assert "index.js" not in by_name
    assert by_name["shadercache"].category == "caches"
    original, copy = sorted((by_name["same-a.bin"], by_name["same-b.bin"]), key=lambda item: item.path)
    assert original.duplicate_of is None
    assert copy.duplicate_of == original.path
    assert copy.category == "duplicates"
    assert analysis.totals["duplicates"].count == 0
    assert by_name["different.bin"].duplicate_of is None

    student = suggestions_for(analysis, load_profile("student"), now=now, today=today)
    student_names = {Path(item.record.path).name for item in student}
    assert "essay.docx" in student_names
    assert "syllabus.pdf" not in student_names
    assert "lecture.pdf" not in student_names

    developer = suggestions_for(analysis, load_profile("developer"), now=now, today=today)
    developer_names = {Path(item.record.path).name for item in developer}
    assert "node_modules" in developer_names
    assert "app.o" in developer_names
    assert "fresh.o" not in developer_names
    assert "main.py" not in developer_names

    gamer = suggestions_for(analysis, load_profile("gamer"), now=now, today=today)
    gamer_names = {Path(item.record.path).name for item in gamer}
    assert "GameSetup.exe" in gamer_names
    assert "shadercache" in gamer_names
    assert "game.exe" not in gamer_names
    assert "slot1.dat" not in gamer_names

    user = merge_rules([load_profile("student"), parse_rules("keep name: *essay*\n", source="user")])
    kept = {Path(item.record.path).name for item in suggestions_for(analysis, user, now=now, today=today)}
    assert "essay.docx" not in kept
    review = format_review(group_suggestions(student), archive=tmp_path / "archive")
    assert "Dry run. Nothing will be moved." in review
    assert "essay.docx" in review
    assert "type MOVE" in review


def test_scan_skips_links_and_caps_the_review_list(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    now = time.time()
    root = tmp_path / "root"
    linked = root / "linked"
    linked.mkdir(parents=True)
    secret = linked / "secret.txt"
    secret.write_bytes(b"hidden")
    big = root / "big.bin"
    small = root / "small.bin"
    big.write_bytes(b"b" * 50)
    small.write_bytes(b"s")
    _touch(big, now - 200 * DAY)
    _touch(small, now - 200 * DAY)
    real_classify = classify_path

    def fake(path: Path) -> str:
        if Path(path) == linked:
            return "link"
        return real_classify(Path(path))

    monkeypatch.setattr("headroom.analyze.classify_path", fake)
    analysis = scan([root], now=now, max_records=1)
    assert analysis.skipped_links >= 1
    assert analysis.capped is True
    assert len(analysis.records) == 1
    assert analysis.records[0].path == str(big)
    assert analysis.stale_count == 2
    assert sum(total.count for total in analysis.totals.values()) == 2
    assert secret.read_bytes() == b"hidden"
    assert "largest 1 items" in format_analysis(analysis)

    monkeypatch.setattr("headroom.analyze._HASH_LIMIT", 4)
    left = root / "left.bin"
    right = root / "right.bin"
    left.write_bytes(b"aaaaXXX")
    right.write_bytes(b"aaaaYYY")
    hashed = scan([left, right], now=now)
    assert [record for record in hashed.records if record.duplicate_of] == []
    twin = root / "twin.bin"
    twin.write_bytes(b"aaaaXXX")
    hashed = scan([left, right, twin], now=now)
    copies = [record for record in hashed.records if record.duplicate_of]
    assert [(Path(item.path).name, Path(item.duplicate_of).name) for item in copies] == [("twin.bin", "left.bin")]


def test_review_moves_only_approved_files_and_archives(tmp_path: Path) -> None:
    now = time.time()
    root = tmp_path / "pc"
    downloads = root / "Downloads"
    downloads.mkdir(parents=True)
    folder = downloads / "oldbuild"
    folder.mkdir()
    child = folder / "a.o"
    child.write_bytes(b"abc")
    good = downloads / "old.iso"
    good.write_bytes(b"iso")
    changed = downloads / "changed.iso"
    changed.write_bytes(b"xyz")
    missing = downloads / "gone.bin"
    state = tmp_path / "state"
    archive = state / "archive"
    folder_record = _record(folder, size=3, accessed=now, category="caches", is_dir=True)
    child_record = _record(child, accessed=now, category="caches")
    good_record = _record(good, accessed=now, category="installers")
    changed_record = _record(changed, size=999, accessed=now, category="installers")
    missing_record = FileRecord(
        path=str(missing),
        size=3,
        accessed=now,
        category="old",
        importance=25,
        base_category="old",
    )

    stats = move_records(
        [folder_record, child_record],
        [root],
        archive,
        recycle=lambda _path: False,
        state=state,
    )
    assert stats.archived == 1
    assert stats.recycled == 0
    assert not folder.exists()
    assert (archive / "Downloads" / "oldbuild" / "a.o").read_bytes() == b"abc"

    stats = move_records(
        [good_record, changed_record, missing_record],
        [root],
        archive,
        recycle=lambda _path: False,
        state=state,
    )
    assert stats.archived == 1
    assert stats.skipped == 2
    assert not good.exists()
    assert (archive / "Downloads" / "old.iso").read_bytes() == b"iso"
    assert changed.read_bytes() == b"xyz"

    recycled: list[Path] = []

    def recycle(path: Path) -> bool:
        recycled.append(path)
        return True

    another = downloads / "another.bin"
    another.write_bytes(b"stay")
    stats = move_records(
        [_record(another, accessed=now)],
        [root],
        archive,
        recycle=recycle,
        state=state,
    )
    assert stats.recycled == 1
    assert stats.archived == 0
    assert recycled == [another]
    assert another.read_bytes() == b"stay"


def test_review_refuses_illegal_paths_before_any_move(tmp_path: Path) -> None:
    now = time.time()
    root = tmp_path / "pc"
    downloads = root / "Downloads"
    downloads.mkdir(parents=True)
    inside = downloads / "inside.bin"
    inside.write_bytes(b"yes")
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"nope")
    state = tmp_path / "state"
    archive = state / "archive"
    inside_record = _record(inside, accessed=now)
    outside_record = _record(outside, accessed=now)
    with pytest.raises(ReviewError, match="refusing to move"):
        move_records([inside_record, outside_record], [root], archive, recycle=lambda _path: False, state=state)
    assert inside.read_bytes() == b"yes"
    assert outside.read_bytes() == b"nope"
    assert not archive.exists()

    dotted = FileRecord(
        path=str(root / "Downloads" / ".." / "outside.bin"),
        size=4,
        accessed=now,
        category="old",
        importance=25,
        base_category="old",
    )
    with pytest.raises(ReviewError):
        move_records([dotted], [root], archive, recycle=lambda _path: False, state=state)
    assert outside.read_bytes() == b"nope"

    with pytest.raises(ReviewError):
        move_records(
            [_record(root, size=0, accessed=now, is_dir=True)],
            [root],
            archive,
            recycle=lambda _path: False,
            state=state,
        )
    assert inside.exists()


def test_review_does_not_follow_a_symlink(tmp_path: Path) -> None:
    now = time.time()
    root = tmp_path / "pc"
    (root / "Downloads").mkdir(parents=True)
    target = tmp_path / "target.bin"
    target.write_bytes(b"target")
    link = root / "Downloads" / "link.bin"
    _symlink(target, link)
    kept = root / "Downloads" / "kept.bin"
    kept.write_bytes(b"kept")
    state = tmp_path / "state"
    with pytest.raises(ReviewError, match="refusing to move"):
        move_records(
            [_record(link, size=6, accessed=now), _record(kept, accessed=now)],
            [root],
            state / "archive",
            recycle=lambda _path: False,
            state=state,
        )
    assert target.read_bytes() == b"target"
    assert kept.read_bytes() == b"kept"
    assert link.is_symlink()


def test_recycle_file_is_not_used_off_windows(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    target.write_text("a", encoding="utf-8")
    if os.name == "nt":
        pytest.skip("Windows Recycle Bin is covered by passing a fake recycler")
    assert recycle_file(target) is False
    assert target.read_text(encoding="utf-8") == "a"


def test_group_selection_and_move_confirmation() -> None:
    assert parse_group_selection("  ", 3) == []
    assert parse_group_selection("all", 3) == [0, 1, 2]
    assert parse_group_selection("1, 2", 2) == [0, 1]
    assert parse_group_selection("1,1", 1) == [0]
    with pytest.raises(ReviewError):
        parse_group_selection("0", 1)
    with pytest.raises(ReviewError):
        parse_group_selection("nope", 1)
    assert MOVE_PROMPT == "Type MOVE to move the selected files: "
    assert confirm_move("MOVE") is True
    assert confirm_move(" MOVE ") is True
    assert confirm_move("move") is False
    assert confirm_move("YES") is False
    assert confirm_move("") is False


def test_cli_profiles_rules_analyze_and_review(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path / "pc"
    project = root / "app"
    (project / "src").mkdir(parents=True)
    (project / "node_modules").mkdir()
    source = project / "src" / "main.py"
    source.write_text("print(1)\n", encoding="utf-8")
    module = project / "node_modules" / "index.js"
    module.write_text("module.exports = 1\n", encoding="utf-8")

    assert main(["--no-log", "profile", "list"]) == 0
    listed = capsys.readouterr().out
    assert "student" in listed and "developer" in listed and "gamer" in listed
    assert main(["profile", "list", "--no-log"]) == 0
    assert "Starter profiles" in capsys.readouterr().out

    assert main(["--no-log", "profile", "use", "student", "developer"]) == 0
    assert "student, developer" in capsys.readouterr().out
    assert read_active_profiles() == ["student", "developer"]
    assert main(["--no-log", "rules", "add", "keep", "name:", "thesis"]) == 0
    assert "override" in capsys.readouterr().out
    assert main(["--no-log", "profile", "show"]) == 0
    merged = capsys.readouterr().out
    assert "[student]" in merged
    assert "[developer]" in merged
    assert "keep name: thesis" in merged
    assert merged.index("[student]") < merged.index("[developer]") < merged.index("keep name: thesis")
    assert "Last matching rule wins" in merged
    assert main(["--no-log", "rules", "show"]) == 0
    assert capsys.readouterr().out.strip() == "keep name: thesis"
    assert main(["--no-log", "profile", "use", "nope"]) == 2
    assert "unknown profile" in capsys.readouterr().err
    assert read_active_profiles() == ["student", "developer"]

    rule_file = tmp_path / "mine.rules"
    rule_file.write_text("# notes\nkeep name: *notes*\n", encoding="utf-8")
    assert main(["--no-log", "rules", "paste", "--file", str(rule_file)]) == 0
    assert "*notes*" in read_user_text()

    class _Stdin:
        def isatty(self) -> bool:
            return False

        def read(self) -> str:
            return "keep ext: .md\n"

    previous_stdin = sys.stdin
    sys.stdin = _Stdin()
    try:
        assert main(["--no-log", "rules", "paste"]) == 0
    finally:
        sys.stdin = previous_stdin
    assert "ext: .md" in read_user_text()

    assert main(["--no-log", "analyze", "--root", str(root)]) == 0
    report = capsys.readouterr().out
    assert "Read-only. Nothing was moved or deleted." in report
    assert "Dry run. Nothing will be moved." in report
    assert source.read_text(encoding="utf-8") == "print(1)\n"
    assert module.is_file()
    loaded = load_analysis()
    assert loaded is not None
    assert any(Path(record.path).name == "node_modules" for record in loaded.records)

    assert main(["--no-log", "review"]) == 0
    dry = capsys.readouterr().out
    assert "Dry run. Nothing will be moved." in dry
    assert "node_modules" in dry
    assert module.is_file()

    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert main(["--no-log", "review", "--apply", "--group", "1"]) == 2
    refused = capsys.readouterr()
    assert "interactive terminal" in refused.err
    assert "Dry run" in refused.out
    assert module.is_file()

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("headroom.cli.recycle_file", lambda _path: False)
    answers = iter(["MOVE"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))
    assert main(["--no-log", "review", "--apply", "--group", "1"]) == 0
    moved = capsys.readouterr().out
    assert "Nothing will be moved." in moved
    assert state_dir().joinpath("archive", "app", "node_modules", "index.js").is_file()
    assert not module.exists()
    assert source.is_file()

    write_active_profiles(["developer"])
    assert main(["--no-log", "profile", "use", "none"]) == 0
    assert read_active_profiles() == []
    assert "thesis" in read_user_text()
    assert main(["--no-log", "analyze", "--root", str(tmp_path / "missing")]) == 2


def test_cli_review_can_cancel_and_ask_per_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path / "pc"
    bundle = root / "pkg" / "node_modules"
    bundle.mkdir(parents=True)
    target = bundle / "index.js"
    target.write_text("x", encoding="utf-8")
    assert main(["--no-log", "profile", "use", "developer"]) == 0
    capsys.readouterr()
    assert main(["--no-log", "analyze", "--root", str(root)]) == 0
    capsys.readouterr()

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("headroom.cli.recycle_file", lambda _path: False)
    monkeypatch.setattr("builtins.input", lambda _prompt="": "move")
    assert main(["--no-log", "review", "--apply", "--group", "all"]) == 0
    assert "Nothing moved." in capsys.readouterr().out
    assert target.is_file()

    monkeypatch.setattr("builtins.input", lambda _prompt="": "")
    assert main(["--no-log", "review", "--apply"]) == 0
    assert "Nothing moved." in capsys.readouterr().out
    assert target.is_file()

    monkeypatch.setattr("builtins.input", lambda _prompt="": "MOVE")
    assert main(["--no-log", "review", "--apply", "--each", "--group", "1"]) == 0
    assert not target.exists()
    assert state_dir().joinpath("archive", "pkg", "node_modules", "index.js").is_file()


def test_review_without_analysis_does_not_move(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--no-log", "review", "--apply"]) == 0
    assert "No analysis yet" in capsys.readouterr().out
    assert not state_dir().joinpath("archive").exists()


def test_first_run_offers_once_and_never_deletes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    for command in ("watch", "install", "uninstall", "analyze", "profile", "rules", "review"):
        maybe_offer_first_run(
            command,
            isatty=lambda: True,
            input_fn=lambda _prompt="": (_ for _ in ()).throw(AssertionError("prompted")),
            state=tmp_path / command,
        )
        assert not (tmp_path / command / "first-run.offered").exists()

    state = tmp_path / "declined"
    answers = iter(["n"])
    maybe_offer_first_run(
        "status",
        isatty=lambda: False,
        input_fn=lambda _prompt="": next(answers),
        state=state,
    )
    assert not (state / "first-run.offered").exists()
    maybe_offer_first_run(
        "status",
        isatty=lambda: True,
        input_fn=lambda _prompt="": next(answers),
        state=state,
    )
    offered = capsys.readouterr().out
    assert "only reads files" in offered
    assert "Skipped" in offered
    assert (state / "first-run.offered").is_file()
    assert not (state / "analysis.json").exists()
    maybe_offer_first_run(
        "clean",
        isatty=lambda: True,
        input_fn=lambda _prompt="": (_ for _ in ()).throw(AssertionError("asked twice")),
        state=state,
    )

    home = tmp_path / "user"
    year, season = current_semester(date.today())
    past = _season_folder(year - 1, season)
    essay = home / "Documents" / past / "homework" / "essay.docx"
    essay.parent.mkdir(parents=True)
    essay.write_bytes(b"semester-essay")
    secret = home / "secret.bin"
    secret.write_bytes(b"not-in-documents")
    accepted = tmp_path / "accepted"
    prompts = iter(["yes", "student, developer"])
    maybe_offer_first_run(
        "top",
        isatty=lambda: True,
        input_fn=lambda _prompt="": next(prompts),
        state=accepted,
        home=home,
        now=time.time(),
        today=date.today(),
    )
    body = capsys.readouterr().out
    assert "Read-only" in body
    assert "Dry run" in body
    assert essay.read_bytes() == b"semester-essay"
    assert secret.read_bytes() == b"not-in-documents"
    assert read_active_profiles(accepted) == ["student", "developer"]
    saved = load_analysis(accepted)
    assert saved is not None
    assert any(record.path.endswith("essay.docx") for record in saved.records)
    assert all("secret.bin" not in record.path for record in saved.records)
    assert not (accepted / "archive").exists()

    bad = tmp_path / "bad"
    bad_prompts = iter(["y", "wizard"])
    maybe_offer_first_run(
        "self",
        isatty=lambda: True,
        input_fn=lambda _prompt="": next(bad_prompts),
        state=bad,
        home=home,
        now=time.time(),
        today=date.today(),
    )
    assert "unknown profile" in capsys.readouterr().err
    assert read_active_profiles(bad) == []
    assert load_analysis(bad) is not None


def test_status_does_not_offer_when_pytest_is_not_a_tty(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    from tests.conftest import make_sample

    monkeypatch.setattr("headroom.probe.sample", lambda: make_sample())
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert main(["--no-log", "status"]) == 0
    assert "First run" not in capsys.readouterr().out
    assert not (state_dir() / "first-run.offered").exists()


def test_analysis_json_round_trip(tmp_path: Path) -> None:
    root = tmp_path / "docs"
    root.mkdir()
    (root / "notes.txt").write_text("hello", encoding="utf-8")
    analysis = scan([root], now=time.time())
    save_analysis(analysis, tmp_path)
    loaded = load_analysis(tmp_path)
    assert loaded is not None
    assert [record.path for record in loaded.records] == [record.path for record in analysis.records]
    assert loaded.totals["school"].count == analysis.totals["school"].count
