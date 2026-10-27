"""Roomy: moods, grime, frame budget, and the watch snapshot. Tk is not opened."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from headroom.config import Config, ConfigError, config_from_mapping
from headroom.pet import (
    DUST_BYTES,
    IDLE_FRAME_MS,
    LITE_CHECK_MS,
    MENU_ACTIONS,
    PAUSED_CHECK_MS,
    PET_NAMES,
    SLIME_BYTES,
    WALK_FRAME_MS,
    Animator,
    FrameDeck,
    PetSession,
    PetSettings,
    Skin,
    WatchSnapshot,
    bubble_text,
    choose_mood,
    cleanable_total,
    cleanup_should_sparkle,
    consume_sparkle,
    covers_screen,
    display_frame,
    frame_delay,
    grime_level,
    launch_pet_if_needed,
    mark_sparkle,
    measure_pet_cost,
    menu_entries,
    publish_watch_sample,
    read_watch_sample,
    review_should_sparkle,
    wake_delay,
)
from headroom.pets.draw import COLUMNS, FRAME_H, FRAME_W, ROW_NAMES, render_frame
from headroom.render import format_self
from headroom.rules import state_dir
from headroom.selfcheck import (
    PET_CPU_BUDGET_PERCENT,
    PET_RSS_BUDGET_BYTES,
    SelfUsage,
    read_pet,
)
from tests.conftest import make_sample

MB = 1024 * 1024


def _session(tmp_path: Path, **kwargs) -> PetSession:
    skin = Skin("roomy", tmp_path / "roomy.png")
    return PetSession(Config(), PetSettings(), skin, tmp_path, **kwargs)


def test_row_order_includes_radar_grime_and_sparkle() -> None:
    assert ROW_NAMES == (
        "power",
        "happy",
        "walk",
        "tired",
        "stuffed",
        "sleep",
        "scan",
        "flicker",
        "dusty",
        "slimy",
        "sparkle",
    )
    assert len(ROW_NAMES) == 11
    assert COLUMNS == 4
    assert FRAME_W == 112 and FRAME_H == 88


def test_scan_face_is_a_green_grid_and_tower_is_a_different_body() -> None:
    scan = render_frame("roomy", ROW_NAMES.index("scan"), 1)
    bright = 0
    for red, green, blue in scan.px:
        if green > 140 and green > red and green > blue:
            bright += 1
    assert bright > 8
    assert render_frame("roomy", 1, 0).rgb() != render_frame("tower", 1, 0).rgb()
    dusty = render_frame("roomy", ROW_NAMES.index("dusty"), 0)
    assert (176, 164, 148) in dusty.px
    slimy = render_frame("roomy", ROW_NAMES.index("slimy"), 2)
    assert (88, 196, 62) in slimy.px
    sparkle = render_frame("roomy", ROW_NAMES.index("sparkle"), 0)
    assert (255, 255, 255) in sparkle.px


def test_grime_thresholds_are_strict() -> None:
    assert grime_level(DUST_BYTES) == "clean"
    assert grime_level(DUST_BYTES + 1) == "dusty"
    assert grime_level(SLIME_BYTES) == "dusty"
    assert grime_level(SLIME_BYTES + 1) == "slimy"
    assert cleanable_total(DUST_BYTES, 1) == DUST_BYTES + 1
    assert cleanup_should_sparkle(dry_run=True, freed_bytes=50) is False
    assert cleanup_should_sparkle(dry_run=False, freed_bytes=0) is False
    assert cleanup_should_sparkle(dry_run=False, freed_bytes=1) is True
    assert review_should_sparkle(archived=0, recycled=0) is False
    assert review_should_sparkle(archived=1, recycled=0) is True
    assert review_should_sparkle(archived=0, recycled=2) is True


def test_mood_priority_and_bubbles() -> None:
    config = Config()
    healthy = make_sample(ram_percent=10)
    tired = make_sample(ram_percent=90)
    idle = config.idle_after_seconds
    assert choose_mood(healthy, config, idle_for=idle, scanning=True, grime="slimy") == "scan"
    low_disk = make_sample(
        ram_percent=90,
        disks=(
            make_sample().disks[0].__class__(
                mount="C:\\",
                device="C:",
                total=100,
                used=90,
                free=10,
                key="C:",
            ),
        ),
    )
    assert choose_mood(low_disk, config, idle_for=None, scanning=False, grime="dusty") == "stuffed"
    assert choose_mood(tired, config, idle_for=idle, scanning=False, grime="slimy") == "tired"
    assert choose_mood(healthy, config, idle_for=idle, scanning=False, grime="slimy") == "sleep"
    assert choose_mood(healthy, config, idle_for=idle - 1, scanning=False, grime="slimy") == "slimy"
    assert choose_mood(healthy, config, idle_for=0, scanning=False, grime="dusty") == "dusty"
    assert choose_mood(healthy, config, idle_for=0, scanning=False, grime="clean") == "happy"
    assert choose_mood(make_sample(ram_percent=config.ram_percent), config, idle_for=None, scanning=False) == "happy"

    class Top:
        name = "Chrome"
        rss_bytes = 3 * 1024**3

    assert bubble_text(healthy, Top(), config, "tired", muted=False) == "Chrome is eating 3.0 GB"
    assert bubble_text(healthy, Top(), config, "tired", muted=True) is None
    stuffed = bubble_text(low_disk, None, config, "stuffed", muted=False)
    assert stuffed is not None and stuffed.startswith("C: is ")
    assert bubble_text(healthy, None, config, "scan", muted=False) == "Scanning."
    assert "2 GB" in (bubble_text(healthy, None, config, "dusty", muted=False) or "")
    assert "10 GB" in (bubble_text(healthy, None, config, "slimy", muted=False) or "")
    assert bubble_text(healthy, None, config, "happy", muted=False) is None


def test_animator_walks_only_when_happy_and_sparkle_holds_still() -> None:
    walking = Animator(power_left=0, flicker_left=0, mood="happy", gait="walk", gait_left=4, x=20)
    walking.advance(0, 400)
    assert walking.x == 24
    assert walking.current() == ("walk", walking.phase % 4)

    dusty = Animator(power_left=0, flicker_left=0, mood="dusty", gait="walk", gait_left=1, x=20)
    dusty.advance(0, 400)
    assert dusty.x == 20
    assert dusty.gait == "idle"

    spark = Animator(power_left=0, flicker_left=0, mood="dusty", x=20)
    spark.play_sparkle()
    assert spark.current()[0] == "sparkle"
    spark.advance(0, 400)
    assert spark.x == 20
    assert spark.sparkle_left == 3

    lite = Animator(power_left=0, flicker_left=0, mood="happy", gait="idle", phase=3)
    assert display_frame(lite, lite=True) == ("happy", 0)
    lite.play_sparkle()
    assert display_frame(lite, lite=True)[0] == "sparkle"


def test_frame_budget_matches_the_fps_bands() -> None:
    assert 6 <= 1000 / WALK_FRAME_MS <= 8
    assert 1 <= 1000 / IDLE_FRAME_MS <= 2
    assert frame_delay(True, gait="walk", mood="happy") == WALK_FRAME_MS
    assert frame_delay(True, gait="idle", mood="happy") == IDLE_FRAME_MS
    assert frame_delay(True, gait="idle", mood="scan") == IDLE_FRAME_MS
    assert frame_delay(False) is None
    assert frame_delay(True, mood="sleep") is None
    assert frame_delay(True, fullscreen=True, gait="walk") is None
    assert frame_delay(True, lite=True) is None
    assert frame_delay(True, lite=True, intro=True) == WALK_FRAME_MS
    assert wake_delay(False) is None
    assert wake_delay(True, mood="sleep") == PAUSED_CHECK_MS
    assert wake_delay(True, fullscreen=True) == PAUSED_CHECK_MS
    assert wake_delay(True, lite=True) == LITE_CHECK_MS
    assert wake_delay(True, gait="walk", mood="happy") is None


def test_fullscreen_cover_and_menu_stay_read_only() -> None:
    assert covers_screen(1920, 1080, 1920, 1080) is True
    assert covers_screen(800, 600, 1920, 1080) is False
    assert covers_screen(0, 1080, 1920, 1080) is False
    actions = [action for _label, action in menu_entries(False)]
    assert actions == list(MENU_ACTIONS)
    assert "clean" not in actions
    assert "Mute" in menu_entries(False)[2][0]
    assert "Unmute" in menu_entries(True)[2][0]
    source = Path(__import__("headroom.pet", fromlist=["pet"]).__file__).read_text(encoding="utf-8")
    assert "move_records" not in source
    assert "discover_groups" not in source or "measure_cleanable_bytes" in source
    assert "def apply(" not in source


def test_watch_snapshot_is_reused_and_a_stale_one_is_ignored(tmp_path: Path) -> None:
    sample = make_sample(ram_percent=12.5, captured_at=time.time())
    publish_watch_sample(sample, 4.0, tmp_path)
    snap = read_watch_sample(tmp_path, now=sample.captured_at + 1, max_age=30)
    assert snap is not None
    assert snap.ram_percent == 12.5
    assert snap.idle_for == 4.0
    assert snap.as_sample().memory.ram_percent == 12.5
    assert read_watch_sample(tmp_path, now=sample.captured_at + 10_000, max_age=30) is None

    calls: list[str] = []
    session = _session(tmp_path)

    def fail_probe():
        calls.append("probe")
        raise AssertionError("the pet should not sample while the snapshot is fresh")

    session.sample_health(
        now=1,
        probe=fail_probe,
        top=lambda _n: calls.append("top"),
        idle=lambda: calls.append("idle"),
        scanning=lambda: False,
        grime_bytes=0,
    )
    assert calls == []
    assert session.uses_shared is True
    assert session.animator.mood == "happy"
    assert session.shared_reads == 1


def test_high_ram_asks_for_the_top_process_once(tmp_path: Path) -> None:
    sample = make_sample(ram_percent=91, captured_at=time.time())
    publish_watch_sample(sample, None, tmp_path)
    session = _session(tmp_path)
    tops: list[int] = []

    class Row:
        name = "Chrome"
        rss_bytes = 3 * 1024**3

    def top(_limit: int):
        tops.append(1)
        return [Row()]

    session.sample_health(
        now=10,
        probe=lambda: (_ for _ in ()).throw(AssertionError("probe")),
        top=top,
        idle=lambda: None,
        scanning=lambda: False,
        grime_bytes=SLIME_BYTES + 5,
    )
    session.sample_health(
        now=12,
        probe=lambda: (_ for _ in ()).throw(AssertionError("probe")),
        top=top,
        idle=lambda: None,
        scanning=lambda: False,
        grime_bytes=SLIME_BYTES + 5,
    )
    assert tops == [1]
    assert session.animator.mood == "tired"
    assert session.bubble() == "Chrome is eating 3.0 GB"


def test_sparkle_marker_clears_grime_until_remeasured(tmp_path: Path) -> None:
    mark_sparkle(tmp_path)
    assert consume_sparkle(tmp_path) is True
    assert consume_sparkle(tmp_path) is False
    mark_sparkle(tmp_path)
    sample = make_sample(ram_percent=10, captured_at=time.time())
    publish_watch_sample(sample, 0, tmp_path)
    session = _session(tmp_path)
    session.animator.power_left = 0
    session.grime = "slimy"
    session.animator.mood = "slimy"
    session.sample_health(
        now=5,
        probe=lambda: sample,
        top=lambda _n: [],
        idle=lambda: 0,
        scanning=lambda: False,
        snapshot=lambda *_args, **_kwargs: None,
        grime_bytes=0,
    )
    assert session.animator.sparkle_left == 4
    assert session.grime == "clean"
    assert session.animator.current()[0] == "sparkle"


def test_lite_config_and_autostart_flag() -> None:
    config = config_from_mapping({"widget": {"autostart": True, "lite": True}})
    assert config.widget_autostart is True
    assert config.widget_lite is True
    with pytest.raises(ConfigError):
        config_from_mapping({"widget": {"bounce": True}})
    launched: list[str] = []
    assert launch_pet_if_needed(config, once=True, launch=lambda: launched.append("no")) is False
    assert launch_pet_if_needed(Config(widget_autostart=True), once=False, launch=lambda: launched.append("yes"))
    assert launched == ["yes"]
    assert "tower" in PET_NAMES


def test_sprites_are_cropped_once() -> None:
    class Photo:
        def __init__(self, owner, **kwargs) -> None:
            self.owner = owner
            self.kwargs = kwargs
            self.tk = owner

        def call(self, *args) -> None:
            self.owner.copies.append(args)

    class Toolkit:
        def __init__(self) -> None:
            self.files: list[str] = []
            self.copies: list[tuple] = []

        def PhotoImage(self, **kwargs):
            if "file" in kwargs:
                self.files.append(kwargs["file"])
            return Photo(self, **kwargs)

        def call(self, *args) -> None:
            self.copies.append(args)

    skin = Skin("roomy", Path("roomy.png"), rows={"happy": 0, "sleep": 1}, columns=2)
    deck = FrameDeck(Toolkit(), skin)
    assert len(deck.frames) == 4
    assert deck.loads == 1
    assert deck.get("happy", 0) is deck.get("happy", 0)
    assert deck.get("missing", 3) is deck.get("happy", 0)


def test_self_report_includes_roomy_budget() -> None:
    text = format_self(
        SelfUsage(pid=1, rss_bytes=20 * MB, cpu_percent=0.2),
        None,
        None,
    )
    assert "Roomy is not running" in text
    assert "30" in text
    assert "2%" in text
    shown = format_self(
        SelfUsage(pid=1, rss_bytes=20 * MB, cpu_percent=0.2),
        SelfUsage(pid=2, rss_bytes=18 * MB, cpu_percent=0.3),
        SelfUsage(pid=3, rss_bytes=8 * MB, cpu_percent=0.4),
    )
    assert "Roomy" in shown
    assert "under 30 MB" in shown
    assert "under 2%" in shown
    assert PET_RSS_BUDGET_BYTES == 30 * MB
    assert PET_CPU_BUDGET_PERCENT == 2.0
    assert read_pet(Path("/no/such/headroom-pet")) is None


def test_watch_loop_publishes_the_sample_it_already_took() -> None:
    from headroom.watch import run_watch_loop

    sample = make_sample(ram_percent=22, captured_at=time.time())
    run_watch_loop(
        Config(),
        probe=lambda: sample,
        iterations=1,
        sleep=lambda _seconds: None,
        notifier=type("N", (), {"notify": lambda self, alert: None})(),
    )
    snap = read_watch_sample(state_dir(), now=sample.captured_at + 1, max_age=30)
    assert snap is not None
    assert snap.ram_percent == 22


def test_idle_loop_stays_inside_the_cpu_and_ram_budget() -> None:
    cost = measure_pet_cost(seconds=2.5)
    assert cost.ticks >= 2
    assert cost.cpu_percent < PET_CPU_BUDGET_PERCENT
    assert cost.extra_rss_bytes < PET_RSS_BUDGET_BYTES
