"""Roomy, a read-only desktop pet.

The sprite sits above the taskbar, walks a little, and changes face with the
PC's health. Clicking opens the local dashboard. The menu can show a report,
mute the bubble, hide, or quit. It never deletes, moves, or cleans.

The frame budget is about 7 fps while walking and about 1.5 fps while idle.
Hidden, asleep, fullscreen, and lite mode do not animate. Sprites load once.
Health comes from the watch loop's snapshot when that file is fresh.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path

from headroom.config import Config
from headroom.pets.draw import COLUMNS, FRAME_H, FRAME_W, ROW_NAMES
from headroom.plugins import SKINS, iter_plugins
from headroom.rules import state_dir

# 1000/143 is about 7 fps, inside the 6-8 fps walk budget.
WALK_FRAME_MS = 143
# 1000/667 is about 1.5 fps, inside the 1-2 fps idle budget.
IDLE_FRAME_MS = 667
PAUSED_CHECK_MS = 5000
LITE_CHECK_MS = 5000
SHARED_POLL_SECONDS = 5.0
SOLO_POLL_SECONDS = 15.0
TASKBAR_MARGIN = 72
WALK_SPEED = 4
DRAG_THRESHOLD = 5
KEY_COLOR = "#ff00ff"
PET_NAMES = ("roomy", "midnight", "tower")

MENU_ACTIONS = ("dashboard", "report", "mute", "hide", "quit")
STATS_NAME = "watch-stats.json"
SPARKLE_NAME = "sparkle.pending"
WIDGET_ENABLED_NAME = "widget.enabled"


@dataclass
class Skin:
    name: str
    sheet: Path
    frame_width: int = FRAME_W
    frame_height: int = FRAME_H
    columns: int = COLUMNS
    rows: dict[str, int] | None = None

    def row_index(self, name: str) -> int:
        table = self.rows or {row: index for index, row in enumerate(ROW_NAMES)}
        if name not in table:
            raise KeyError(name)
        return table[name]


@dataclass
class PetSettings:
    x: int | None = None
    y: int | None = None
    skin: str = "roomy"
    muted: bool = False

    def to_json(self) -> str:
        return json.dumps(
            {"x": self.x, "y": self.y, "skin": self.skin, "muted": self.muted}
        )


@dataclass
class Animator:
    """Spawn plays the power-on row, then a flicker when the mood changes."""

    mood: str = "happy"
    phase: int = 0
    power_left: int = 4
    flicker_left: int = 0
    flicker_after_power: bool = False
    sparkle_left: int = 0
    gait: str = "idle"
    gait_left: int = 12
    x: int = 48
    direction: int = 1

    def set_mood(self, mood: str) -> None:
        if mood == self.mood:
            return
        self.mood = mood
        if self.power_left > 0:
            self.flicker_after_power = True
            return
        if self.sparkle_left > 0:
            return
        self.flicker_left = 2

    def play_sparkle(self) -> None:
        self.sparkle_left = 4
        self.gait = "idle"

    def busy_intro(self) -> bool:
        return self.power_left > 0 or self.flicker_left > 0 or self.sparkle_left > 0

    def current(self) -> tuple[str, int]:
        if self.power_left > 0:
            return "power", 4 - self.power_left
        if self.sparkle_left > 0:
            return "sparkle", (4 - self.sparkle_left) % 4
        if self.flicker_left > 0:
            return "flicker", (2 - self.flicker_left) % 4
        if self.gait == "walk" and self.mood == "happy":
            return "walk", self.phase % 4
        row = self.mood if self.mood in ROW_NAMES else "happy"
        return row, self.phase % 4

    def advance(self, left: int, right: int) -> None:
        if self.power_left > 0:
            self.power_left -= 1
            if self.power_left == 0 and self.flicker_after_power:
                self.flicker_left = 2
                self.flicker_after_power = False
            return
        if self.sparkle_left > 0:
            self.sparkle_left -= 1
            return
        if self.flicker_left > 0:
            self.flicker_left -= 1
            return
        self.phase += 1
        if self.mood != "happy":
            self.gait = "idle"
            return
        self.gait_left -= 1
        if self.gait_left <= 0:
            if self.gait == "walk":
                self.gait = "idle"
                self.gait_left = 12
            else:
                self.gait = "walk"
                self.gait_left = 8
        if self.gait != "walk":
            return
        nxt = self.x + self.direction * WALK_SPEED
        if nxt < left:
            self.x = left
            self.direction = 1
        elif nxt > right:
            self.x = right
            self.direction = -1
        else:
            self.x = nxt


def cleanup_should_sparkle(*, dry_run: bool, freed_bytes: int) -> bool:
    """A real cleanup that freed bytes shakes the dust off. A dry run does not."""
    return (not dry_run) and freed_bytes > 0


def review_should_sparkle(*, archived: int, recycled: int) -> bool:
    """An approved review that moved something shakes the dust off."""
    return archived + recycled > 0


def choose_mood(
    sample,
    config: Config,
    *,
    idle_for: float | None,
    scanning: bool,
) -> str:
    """Scanning and high RAM win over idle. The face no longer reads disk or grime."""
    if scanning:
        return "scan"
    if sample.memory.ram_percent > config.ram_percent:
        return "tired"
    if idle_for is not None and idle_for >= config.idle_after_seconds:
        return "sleep"
    return "happy"


def bubble_text(sample, top, config: Config, mood: str, *, muted: bool) -> str | None:
    """A short RAM readout. Mute, sleep, power-on, scanning, and sparkle stay quiet."""
    del top, config
    if muted or mood in {"sleep", "power", "scan", "sparkle"}:
        return None
    percent = int(round(float(sample.memory.ram_percent)))
    return f"RAM {percent}% used"


def pointer_kind(dx: int, dy: int, threshold: int = DRAG_THRESHOLD) -> str:
    if dx * dx + dy * dy >= threshold * threshold:
        return "drag"
    return "click"


def menu_entries(muted: bool) -> tuple[tuple[str, str], ...]:
    mute_label = "Unmute" if muted else "Mute"
    return (
        ("Open dashboard", "dashboard"),
        ("Report", "report"),
        (mute_label, "mute"),
        ("Hide", "hide"),
        ("Quit", "quit"),
    )


def should_launch_pet(
    config: Config, *, once: bool, directory: Path | None = None
) -> bool:
    """Roomy auto-starts when the dashboard toggle is on, and stays off when it is off.

    The persisted toggle wins over ``widget.autostart``. When it was never set,
    the config flag decides.
    """
    enabled = pet_enabled(directory)
    if enabled is None:
        enabled = bool(config.widget_autostart)
    return enabled and not once


def default_origin(screen_w: int, screen_h: int) -> tuple[int, int]:
    x = max(8, (screen_w - FRAME_W) // 2)
    y = max(8, screen_h - FRAME_H - TASKBAR_MARGIN)
    return x, y


def frame_delay(
    visible: bool,
    *,
    mood: str = "happy",
    gait: str = "idle",
    lite: bool = False,
    fullscreen: bool = False,
    intro: bool = False,
) -> int | None:
    """Milliseconds until the next sprite frame.

    Walking stays near 7 fps. Idle blinks and bobs near 1.5 fps. Hidden,
    asleep, a fullscreen app, and lite mode (once the intro finishes) draw
    nothing on a timer.
    """
    if not visible or fullscreen:
        return None
    if intro:
        return WALK_FRAME_MS
    if lite or mood == "sleep":
        return None
    if gait == "walk" and mood == "happy":
        return WALK_FRAME_MS
    return IDLE_FRAME_MS


def wake_delay(
    visible: bool,
    *,
    mood: str = "happy",
    gait: str = "idle",
    lite: bool = False,
    fullscreen: bool = False,
    intro: bool = False,
) -> int | None:
    """Slow check used while frames are stopped, so sleep and fullscreen can end."""
    if frame_delay(
        visible,
        mood=mood,
        gait=gait,
        lite=lite,
        fullscreen=fullscreen,
        intro=intro,
    ) is not None:
        return None
    if not visible:
        return None
    if lite:
        return LITE_CHECK_MS
    return PAUSED_CHECK_MS


def covers_screen(width: int, height: int, screen_w: int, screen_h: int, *, margin: int = 2) -> bool:
    """True when a foreground window covers the display."""
    if screen_w <= 0 or screen_h <= 0 or width <= 0 or height <= 0:
        return False
    return width >= screen_w - margin and height >= screen_h - margin


def foreground_size() -> tuple[int, int] | None:
    """Pixel size of the foreground window. Unknown platforms return None."""
    if os.name != "nt":
        return None
    import ctypes

    class _Rect(ctypes.Structure):
        _fields_ = (
            ("left", ctypes.c_long),
            ("top", ctypes.c_long),
            ("right", ctypes.c_long),
            ("bottom", ctypes.c_long),
        )

    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    rect = _Rect()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    return int(rect.right - rect.left), int(rect.bottom - rect.top)


def foreground_covers(screen_w: int, screen_h: int, reader=None) -> bool:
    read = foreground_size if reader is None else reader
    try:
        size = read()
    except Exception:
        return False
    if size is None:
        return False
    return covers_screen(int(size[0]), int(size[1]), screen_w, screen_h)


def display_frame(animator: Animator, *, lite: bool) -> tuple[str, int]:
    """Lite mode holds column 0 except during power-on, flicker, and sparkle."""
    row, column = animator.current()
    if lite and not animator.busy_intro():
        return row, 0
    return row, column


def apply_window_flags(root, *, topmost: bool = True) -> None:
    root.overrideredirect(True)
    root.attributes("-topmost", topmost)
    try:
        # Windows hides the magenta key. Other window systems keep the color.
        root.attributes("-transparentcolor", KEY_COLOR)
    except Exception:
        pass
    root.configure(bg=KEY_COLOR)


def settings_path(directory: Path | None = None) -> Path:
    return (state_dir() if directory is None else directory) / "pet.json"


def load_settings(directory: Path | None = None) -> PetSettings:
    path = settings_path(directory)
    if not path.is_file():
        return PetSettings()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return PetSettings()
    if not isinstance(raw, dict):
        return PetSettings()
    x = raw.get("x")
    y = raw.get("y")
    return PetSettings(
        x=int(x) if isinstance(x, int) else None,
        y=int(y) if isinstance(y, int) else None,
        skin=str(raw.get("skin") or "roomy"),
        muted=bool(raw.get("muted", False)),
    )


def save_settings(settings: PetSettings, directory: Path | None = None) -> None:
    path = settings_path(directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(settings.to_json(), encoding="utf-8")


def pet_enabled(directory: Path | None = None) -> bool | None:
    """The dashboard toggle. True on, False off, None when it was never set."""
    path = (state_dir() if directory is None else directory) / WIDGET_ENABLED_NAME
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if text == "1":
        return True
    if text == "0":
        return False
    return None


def set_pet_enabled(enabled: bool, directory: Path | None = None) -> None:
    folder = state_dir() if directory is None else directory
    path = folder / WIDGET_ENABLED_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("1\n" if enabled else "0\n", encoding="utf-8")


def pet_running(directory: Path | None = None) -> bool:
    """True when a live Roomy answers its loopback control port."""
    folder = state_dir() if directory is None else directory
    path = folder / "pet.port"
    if not path.is_file():
        return False
    try:
        parts = path.read_text(encoding="utf-8").split()
        port = int(parts[0])
        pid = int(parts[1]) if len(parts) > 1 else None
    except (OSError, ValueError):
        return False
    if pid is not None and not _pid_alive(pid):
        return False
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


@dataclass(frozen=True)
class WatchSnapshot:
    """One watch-loop sample the pet can read without calling psutil."""

    ram_percent: float
    ram_total: int
    disks: tuple[tuple[str, int, int], ...]
    idle_for: float | None
    top_name: str | None
    top_rss: int | None
    captured_at: float

    def as_sample(self):
        """A small stand-in for a probe sample. The pet does not import psutil for this."""
        from types import SimpleNamespace

        memory = SimpleNamespace(ram_percent=self.ram_percent, ram_total=self.ram_total)
        disks = tuple(
            SimpleNamespace(
                mount=key,
                device="",
                total=total,
                used=max(0, total - free),
                free=free,
                key=key,
            )
            for key, free, total in self.disks
        )
        return SimpleNamespace(memory=memory, disks=disks, captured_at=self.captured_at)


def publish_watch_sample(sample, idle_for: float | None, directory: Path, top=None) -> None:
    """Atomically replace the snapshot the pet reads."""
    disks = tuple(
        {"key": disk.key, "free": int(disk.free), "total": int(disk.total)}
        for disk in sample.disks
    )
    payload = {
        "ram_percent": float(sample.memory.ram_percent),
        "ram_total": int(sample.memory.ram_total),
        "disks": disks,
        "idle_for": None if idle_for is None else float(idle_for),
        "top_name": None if top is None else str(top.name),
        "top_rss": None if top is None else int(top.rss_bytes),
        "captured_at": float(sample.captured_at),
    }
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / STATS_NAME
    temporary = directory / f"{STATS_NAME}.tmp"
    temporary.write_text(json.dumps(payload), encoding="utf-8")
    temporary.replace(target)


def read_watch_sample(
    directory: Path,
    *,
    now: float | None = None,
    max_age: float = 90.0,
) -> WatchSnapshot | None:
    path = directory / STATS_NAME
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    try:
        captured = float(raw["captured_at"])
        ram_percent = float(raw["ram_percent"])
        ram_total = int(raw["ram_total"])
    except (KeyError, TypeError, ValueError):
        return None
    current = time.time() if now is None else now
    if current - captured > max_age:
        return None
    disks: list[tuple[str, int, int]] = []
    for item in raw.get("disks") or []:
        if not isinstance(item, dict):
            continue
        try:
            disks.append((str(item["key"]), int(item["free"]), int(item["total"])))
        except (KeyError, TypeError, ValueError):
            continue
    idle_raw = raw.get("idle_for")
    idle_for = float(idle_raw) if isinstance(idle_raw, (int, float)) else None
    top_name = raw.get("top_name")
    top_rss = raw.get("top_rss")
    return WatchSnapshot(
        ram_percent=ram_percent,
        ram_total=ram_total,
        disks=tuple(disks),
        idle_for=idle_for,
        top_name=str(top_name) if isinstance(top_name, str) and top_name else None,
        top_rss=int(top_rss) if isinstance(top_rss, int) else None,
        captured_at=captured,
    )


def shared_max_age(config: Config) -> float:
    """Stay on the watch snapshot across a slow battery interval."""
    return max(90.0, float(config.interval_seconds) * 3.0)


class FrameDeck:
    """Crop every frame once. Later ticks reuse the same photo objects."""

    def __init__(self, tk, skin: Skin) -> None:
        self.loads = 0
        self.frames: dict[tuple[str, int], object] = {}
        sheet = tk.PhotoImage(file=str(skin.sheet))
        self.loads += 1
        table = skin.rows or {row: index for index, row in enumerate(ROW_NAMES)}
        for name, index in table.items():
            for column in range(skin.columns):
                x0 = column * skin.frame_width
                y0 = index * skin.frame_height
                x1 = x0 + skin.frame_width
                y1 = y0 + skin.frame_height
                frame = tk.PhotoImage(width=skin.frame_width, height=skin.frame_height)
                frame.tk.call(frame, "copy", sheet, "-from", x0, y0, x1, y1, "-to", 0, 0)
                self.frames[(name, column)] = frame
        self._sheet = sheet

    def get(self, row: str, column: int):
        found = self.frames.get((row, column))
        if found is not None:
            return found
        return self.frames.get(("happy", 0)) or self.frames.get(("power", 0))


def builtin_skins() -> dict[str, Skin]:
    folder = Path(__file__).resolve().parent / "pets"
    return {name: Skin(name, folder / f"{name}.png") for name in PET_NAMES}


def skin_from_value(value: object, fallback: str) -> Skin | None:
    if not isinstance(value, dict):
        return None
    name = str(value.get("name") or fallback).strip()
    sheet = value.get("sheet")
    if not name or not isinstance(sheet, str):
        return None
    path = Path(sheet)
    if not path.is_file():
        return None
    rows = value.get("rows")
    parsed: dict[str, int] | None = None
    if isinstance(rows, dict):
        parsed = {str(key): int(item) for key, item in rows.items()}
    return Skin(name, path, rows=parsed)


def load_skins(entries=None) -> dict[str, Skin]:
    """Built-in names win. A plugin that fails to load is skipped."""
    found = builtin_skins()
    protected = set(PET_NAMES)
    for plugin in iter_plugins(SKINS, entries):
        if plugin.name.casefold() in protected:
            continue
        try:
            skin = skin_from_value(plugin.fn(), plugin.name)
        except Exception:
            continue
        if skin is None or skin.name.casefold() in protected:
            continue
        found[skin.name] = skin
    return found


def mark_sparkle(directory: Path | None = None) -> None:
    path = _sparkle_path(directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("1\n", encoding="utf-8")


def consume_sparkle(directory: Path | None = None) -> bool:
    path = _sparkle_path(directory)
    if not path.is_file():
        return False
    path.unlink(missing_ok=True)
    return True


def mark_scan(directory: Path | None = None) -> None:
    path = _scan_path(directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{os.getpid()}\n", encoding="utf-8")


def clear_scan(directory: Path | None = None) -> None:
    path = _scan_path(directory)
    if not path.is_file():
        return
    try:
        owner = int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return
    if owner == os.getpid():
        path.unlink(missing_ok=True)


def scan_running(directory: Path | None = None) -> bool:
    path = _scan_path(directory)
    if not path.is_file():
        return False
    try:
        pid = int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    if pid == os.getpid() or _pid_alive(pid):
        return True
    path.unlink(missing_ok=True)
    return False


def report_text() -> str:
    from headroom.history import HistoryStore, build_report, default_history_path
    from headroom.render import format_report

    path = default_history_path()
    if not path.is_file():
        return "No history yet. Leave headroom watch running to record samples.\n"
    store = HistoryStore(path)
    try:
        import time

        return format_report(build_report(store, Config(), now=time.time()))
    finally:
        store.close()


def open_dashboard(state: dict[str, object], opener, start_server) -> str:
    """Open the loopback dashboard. ``start_server`` returns its URL."""
    existing = state.get("url")
    if isinstance(existing, str) and existing:
        opener(existing)
        return existing
    url = str(start_server())
    if not (url.startswith("http://127.0.0.1:") or url.startswith("http://localhost:")):
        raise RuntimeError("the dashboard stays on this computer")
    state["url"] = url
    opener(url)
    return url


_SERVERS: list[object] = []


def start_dashboard_server() -> str:
    from headroom.dashboard import bind_dashboard, new_token

    token = new_token()
    server = bind_dashboard("127.0.0.1", 0, token, recycle=lambda _path: False)
    _SERVERS.append(server)
    threading.Thread(target=server.serve_forever, name="headroom-dashboard", daemon=True).start()
    port = int(server.server_address[1])
    return f"http://127.0.0.1:{port}/?token={token}"


def launch_pet_if_needed(
    config: Config, *, once: bool, launch=None, directory: Path | None = None
) -> bool:
    if not should_launch_pet(config, once=once, directory=directory):
        return False
    if launch is None:
        launch_pet_detached(lite=bool(config.widget_lite), directory=directory)
    else:
        launch()
    return True


def launch_pet_detached(lite: bool = False, directory: Path | None = None) -> None:
    folder = state_dir() if directory is None else directory
    if pet_running(folder):
        send_control("show", folder)
        return
    from headroom.autostart import app_command

    cmd = app_command("pet")
    if lite:
        cmd.append("--lite")
    kwargs: dict[str, object] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.Popen(cmd, **kwargs)  # noqa: S603


class ControlServer:
    """Loopback commands: show, hide, quit. Anything else is ignored."""

    def __init__(self, directory: Path, on_command) -> None:
        self._on = on_command
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(4)
        self._sock.settimeout(0.2)
        self.port = int(self._sock.getsockname()[1])
        self._port_path = directory / "pet.port"
        self._port_path.write_text(f"{self.port} {os.getpid()}\n", encoding="utf-8")
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="headroom-pet-control", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass
        try:
            text = self._port_path.read_text(encoding="utf-8").strip()
        except OSError:
            text = ""
        if text.split()[:2] == [str(self.port), str(os.getpid())]:
            self._port_path.unlink(missing_ok=True)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, addr = self._sock.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            if addr[0] not in {"127.0.0.1", "::1"}:
                conn.close()
                continue
            try:
                data = conn.recv(64).decode("utf-8", "replace").strip()
            finally:
                conn.close()
            if data in {"show", "hide", "quit"}:
                try:
                    self._on(data)
                except Exception:
                    continue


def send_control(command: str, directory: Path | None = None) -> bool:
    path = (state_dir() if directory is None else directory) / "pet.port"
    if not path.is_file():
        return False
    try:
        port_text = path.read_text(encoding="utf-8").split()[0]
        port = int(port_text)
    except (OSError, ValueError, IndexError):
        return False
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3) as conn:
            conn.sendall(command.encode("utf-8") + b"\n")
        return True
    except OSError:
        return False


def run_pet(
    config: Config,
    *,
    skin: str | None = None,
    directory: Path | None = None,
    lite: bool = False,
) -> None:
    import tkinter

    folder = state_dir() if directory is None else directory
    settings = load_settings(folder)
    if skin:
        settings = replace(settings, skin=skin)
    skins = load_skins()
    chosen = skins.get(settings.skin) or skins["roomy"]
    logic = PetSession(config, settings, chosen, folder, lite=lite or bool(config.widget_lite))
    root = tkinter.Tk()
    root.title("Roomy")
    apply_window_flags(root)
    screen_w = int(root.winfo_screenwidth())
    screen_h = int(root.winfo_screenheight())
    if settings.x is None or settings.y is None:
        origin_x, origin_y = default_origin(screen_w, screen_h)
        logic.animator.x = settings.x if settings.x is not None else origin_x
        pos_y = settings.y if settings.y is not None else origin_y
        logic.settings = replace(logic.settings, x=logic.animator.x, y=pos_y)
    else:
        logic.animator.x = settings.x
        pos_y = settings.y
    root.geometry(f"{FRAME_W}x{FRAME_H + 36}+{logic.animator.x}+{max(0, pos_y - 36)}")
    view = PetView(root, tkinter, logic, screen_w, screen_h)
    server = ControlServer(folder, lambda command: root.after(0, view.control, command))
    try:
        view.schedule()
        root.mainloop()
    finally:
        server.close()
        logic.persist()


class PetSession:
    """Health, mood, and bubble. The view asks it what to draw."""

    def __init__(
        self,
        config: Config,
        settings: PetSettings,
        skin: Skin,
        directory: Path,
        *,
        lite: bool = False,
    ) -> None:
        self.config = config
        self.settings = settings
        self.skin = skin
        self.directory = directory
        self.lite = lite
        self.animator = Animator(mood="happy", x=settings.x or 48)
        self.visible = True
        self.sample = None
        self.top = None
        self.last_health: float | None = None
        self.probes = 0
        self.shared_reads = 0
        self.uses_shared = False
        self._top_at: float | None = None
        self.dashboard: dict[str, object] = {}

    def sample_health(
        self,
        *,
        now: float,
        probe,
        top,
        idle,
        scanning,
        snapshot=None,
    ) -> None:
        self.probes += 1
        self.last_health = now
        if consume_sparkle(self.directory):
            self.animator.play_sparkle()
        snap = None
        if snapshot is not False:
            reader = read_watch_sample if snapshot is None else snapshot
            try:
                snap = reader(self.directory, now=time.time(), max_age=shared_max_age(self.config))
            except TypeError:
                snap = reader()
        if snap is not None:
            self.shared_reads += 1
            self.uses_shared = True
            self.sample = snap.as_sample()
            if snap.top_name:
                from headroom.processes import ProcessMem

                self.top = ProcessMem(pid=0, name=snap.top_name, rss_bytes=snap.top_rss or 0)
                self._top_at = now
            idle_for = snap.idle_for
        else:
            self.uses_shared = False
            try:
                self.sample = probe()
            except Exception:
                return
            idle_for = _call_idle(idle)
        if self.sample is None:
            return
        if not (snap is not None and snap.top_name):
            if self.sample.memory.ram_percent > self.config.ram_percent:
                if self._top_at is None or now - self._top_at >= SOLO_POLL_SECONDS:
                    self.top = _call_top(top)
                    self._top_at = now
            else:
                self.top = None
                self._top_at = None
        try:
            active = bool(scanning())
        except Exception:
            active = False
        self.animator.set_mood(
            choose_mood(
                self.sample,
                self.config,
                idle_for=idle_for,
                scanning=active,
            )
        )

    def due(self, now: float) -> bool:
        interval = SHARED_POLL_SECONDS if self.uses_shared else SOLO_POLL_SECONDS
        return self.last_health is None or now - self.last_health >= interval

    def bubble(self) -> str | None:
        if self.sample is None:
            return None
        return bubble_text(
            self.sample,
            self.top,
            self.config,
            self.animator.mood,
            muted=self.settings.muted,
        )

    def toggle_mute(self) -> bool:
        self.settings = replace(self.settings, muted=not self.settings.muted)
        self.persist()
        return self.settings.muted

    def persist(self) -> None:
        self.settings = replace(self.settings, x=self.animator.x, skin=self.skin.name)
        save_settings(self.settings, self.directory)


class PetView:
    def __init__(self, root, tk, session: PetSession, screen_w: int, screen_h: int = 0) -> None:
        self.root = root
        self.tk = tk
        self.session = session
        self.screen_w = screen_w
        self.screen_h = screen_h
        self._after_id = None
        self._press: tuple[int, int] | None = None
        self._moved = False
        self._fullscreen = False
        self._parked = False
        self._shown: tuple[str, int] | None = None
        self.image = None
        self.deck = FrameDeck(tk, session.skin)
        self.sprite = tk.Label(root, bg=KEY_COLOR, bd=0, highlightthickness=0)
        self.sprite.pack(side="bottom")
        self.bubble = tk.Label(
            root,
            bg="#f6edd8",
            fg="#2c2820",
            font=("Segoe UI", 9),
            padx=6,
            pady=2,
        )
        self.menu = tk.Menu(root, tearoff=0)
        self._fill_menu()
        self.sprite.bind("<ButtonPress-1>", self._press_left)
        self.sprite.bind("<B1-Motion>", self._drag)
        self.sprite.bind("<ButtonRelease-1>", self._release_left)
        self.root.bind("<Button-3>", self._popup)
        self.root.bind("<Button-2>", self._popup)
        self._show_frame()

    def _fill_menu(self) -> None:
        self.menu.delete(0, "end")
        for label, action in menu_entries(self.session.settings.muted):
            self.menu.add_command(label=label, command=lambda name=action: self.perform(name))

    def _timing(self) -> dict[str, object]:
        animator = self.session.animator
        return {
            "visible": self.session.visible,
            "mood": animator.mood,
            "gait": animator.gait,
            "lite": self.session.lite,
            "fullscreen": self._fullscreen,
            "intro": animator.busy_intro(),
        }

    def schedule(self) -> None:
        timing = self._timing()
        delay = frame_delay(**timing)  # type: ignore[arg-type]
        if delay is None:
            delay = wake_delay(**timing)  # type: ignore[arg-type]
        if delay is None:
            return
        self._after_id = self.root.after(delay, self._tick)

    def _tick(self) -> None:
        if not self.session.visible:
            return
        self._fullscreen = foreground_covers(self.screen_w, self.screen_h)
        self._sync_fullscreen()
        now = time.monotonic()
        if self.session.due(now):
            self.session.sample_health(
                now=now,
                probe=_sample,
                top=_top,
                idle=_idle,
                scanning=lambda: scan_running(self.session.directory),
            )
        timing = self._timing()
        animate = frame_delay(**timing) is not None  # type: ignore[arg-type]
        if not animate:
            if self.session.lite and not self._fullscreen:
                self._paint(force=False)
            self.schedule()
            return
        self._paint(force=True)
        right = self.screen_w - FRAME_W - 8
        if right < 8:
            right = 8
        self.session.animator.advance(8, right)
        self.schedule()

    def _paint(self, *, force: bool) -> None:
        row, column = display_frame(self.session.animator, lite=self.session.lite)
        if not force and self._shown == (row, column):
            self._sync_bubble()
            return
        self._show_frame(row, column)
        self._sync_bubble()

    def _sync_bubble(self) -> None:
        text = self.session.bubble()
        if text:
            self.bubble.configure(text=text)
            self.bubble.pack(side="top")
        else:
            self.bubble.pack_forget()

    def _sync_fullscreen(self) -> None:
        if self._fullscreen and not self._parked:
            self._parked = True
            try:
                self.root.attributes("-topmost", False)
                self.root.withdraw()
            except Exception:
                return
            return
        if not self._fullscreen and self._parked and self.session.visible:
            self._parked = False
            try:
                self.root.deiconify()
                self.root.attributes("-topmost", True)
            except Exception:
                return

    def _show_frame(self, row: str = "power", column: int = 0) -> None:
        image = self.deck.get(row, column)
        if image is None:
            return
        self.image = image
        self._shown = (row, column)
        self.sprite.configure(image=image)

    def perform(self, action: str) -> None:
        if action not in MENU_ACTIONS:
            return
        if action == "dashboard":
            open_dashboard(self.session.dashboard, _browser, start_dashboard_server)
            return
        if action == "report":
            self._show_report(report_text())
            return
        if action == "mute":
            self.session.toggle_mute()
            self._fill_menu()
            return
        if action == "hide":
            self.hide()
            return
        if action == "quit":
            self.quit()

    def _show_report(self, text: str) -> None:
        window = self.tk.Toplevel(self.root)
        window.title("Headroom report")
        widget = self.tk.Text(window, width=72, height=18, wrap="word")
        widget.insert("1.0", text)
        widget.configure(state="disabled")
        widget.pack()

    def hide(self) -> None:
        self.session.visible = False
        if self._after_id is not None:
            try:
                self.root.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None
        self.root.withdraw()

    def show(self) -> None:
        self.session.visible = True
        self.root.deiconify()
        self.root.attributes("-topmost", True)
        self.schedule()

    def quit(self) -> None:
        self.session.visible = False
        self.session.persist()
        self.root.destroy()

    def control(self, command: str) -> None:
        if command == "show":
            self.show()
        elif command == "hide":
            self.hide()
        elif command == "quit":
            self.quit()

    def _press_left(self, event) -> None:
        self._press = (event.x_root, event.y_root)
        self._moved = False
        self._origin = (self.root.winfo_x(), self.root.winfo_y())

    def _drag(self, event) -> None:
        if self._press is None:
            return
        dx = event.x_root - self._press[0]
        dy = event.y_root - self._press[1]
        if pointer_kind(dx, dy) == "drag":
            self._moved = True
            x = self._origin[0] + dx
            y = self._origin[1] + dy
            self.root.geometry(f"+{x}+{y}")
            self.session.animator.x = x
            self.session.settings = replace(self.session.settings, x=x, y=y)

    def _release_left(self, event) -> None:
        if self._press is None:
            return
        dx = event.x_root - self._press[0]
        dy = event.y_root - self._press[1]
        self._press = None
        if pointer_kind(dx, dy) == "click" and not self._moved:
            self.perform("dashboard")
            return
        self.session.persist()

    def _popup(self, event) -> None:
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()


def _crop_photo(tk, path: Path, x0: int, y0: int, x1: int, y1: int):
    """Slice one frame out of a sheet using Tk's photo copy."""
    sheet = tk.PhotoImage(file=str(path))
    frame = tk.PhotoImage(width=x1 - x0, height=y1 - y0)
    frame.tk.call(frame, "copy", sheet, "-from", x0, y0, x1, y1, "-to", 0, 0)
    frame._sheet = sheet  # keep the source alive
    return frame


def _sample():
    from headroom.probe import sample

    return sample()


def _top(limit: int):
    from headroom.processes import top_processes

    return top_processes(limit)


def _idle():
    from headroom.schedule import idle_seconds

    return idle_seconds()


def _browser(url: str) -> None:
    import webbrowser

    webbrowser.open(url)


def _scan_path(directory: Path | None) -> Path:
    return (state_dir() if directory is None else directory) / "scan.running"


def _sparkle_path(directory: Path | None) -> Path:
    return (state_dir() if directory is None else directory) / SPARKLE_NAME


def _call_idle(idle) -> float | None:
    try:
        return idle()
    except Exception:
        return None


def _call_top(top):
    try:
        rows = top(1)
    except Exception:
        return None
    return rows[0] if rows else None


@dataclass(frozen=True)
class PetCost:
    """One measured stretch of the idle pet loop, without a Tk window."""

    seconds: float
    cpu_percent: float
    rss_bytes: int
    extra_rss_bytes: int
    ticks: int


def measure_pet_cost(seconds: float = 4.0) -> PetCost:
    """Preload the sheet once, then tick at the idle rate.

    CPU is the steady state after the sheet is in memory. ``extra_rss_bytes``
    is the resident growth from that preload, which is what the pet adds on
    top of the interpreter that is already running.
    """
    import psutil

    from headroom.pets.draw import read_png

    proc = psutil.Process()
    before_rss = int(proc.memory_info().rss)
    sheet = Path(__file__).resolve().parent / "pets" / "roomy.png"
    _width, _height, rgb = read_png(sheet)
    held = [rgb[offset : offset + 128] for offset in range(0, min(len(rgb), 128 * 32), 128)]
    loaded_rss = int(proc.memory_info().rss)
    before = proc.cpu_times()
    animator = Animator(power_left=0, mood="happy", gait="idle")
    ticks = 0
    started = time.perf_counter()
    deadline = started + max(0.0, seconds)
    while time.perf_counter() < deadline:
        animator.current()
        animator.advance(0, 400)
        _ = held[ticks % len(held)]
        ticks += 1
        time.sleep(IDLE_FRAME_MS / 1000)
    elapsed = max(0.001, time.perf_counter() - started)
    after = proc.cpu_times()
    used = (after.user + after.system) - (before.user + before.system)
    rss = int(proc.memory_info().rss)
    return PetCost(
        seconds=elapsed,
        cpu_percent=100.0 * used / elapsed,
        rss_bytes=rss,
        extra_rss_bytes=max(0, loaded_rss - before_rss, rss - before_rss),
        ticks=ticks,
    )


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, 0, pid)
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


# Imported by draw consumers that want the key color name without tk.
TRANSPARENT_COLOR = KEY_COLOR
