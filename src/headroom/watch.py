"""Monitor loop and optional tray icon.

The tray can show status and a dry-run cleanup preview. It does not delete.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable, Mapping

from headroom.alerts import AlertState, evaluate
from headroom.cleanup import build_preview, default_recycle_backend, discover_groups
from headroom.config import Config
from headroom.notify import Fanout
from headroom.probe import Sample, sample as sample_system
from headroom.render import format_preview, watch_line
from headroom.schedule import choose_interval, detect_resume, idle_seconds, on_battery, under_pressure

Notifier = object


def run_watch_loop(
    config: Config,
    *,
    probe: Callable[[], Sample] | None = None,
    notifier: Notifier | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    iterations: int | None = None,
    stop_event: threading.Event | None = None,
    on_line: Callable[[str], None] | None = None,
    logger: logging.Logger | None = None,
    adaptive: bool = False,
    tracker: object | None = None,
    track_deep: bool = False,
    on_battery_fn: Callable[[], bool] | None = None,
    idle_fn: Callable[[], float | None] | None = None,
    wall_clock: Callable[[], float] = time.time,
    mono_clock: Callable[[], float] = time.monotonic,
) -> AlertState:
    """Sample until ``iterations`` is reached or ``stop_event`` is set.

    The first sample is taken before the first sleep, so it lands well inside
    10 seconds of start. Later samples follow the normal interval. Priority is
    lowered only for the open-ended loop. A fixed ``iterations`` count (tests
    and ``--once``) leaves the process priority alone. Adaptive intervals stay
    off unless ``adaptive`` is set, so a plain loop sleeps ``interval_seconds``.
    """
    read = probe or sample_system
    log = logger or logging.getLogger("headroom")
    deliver = notifier
    if deliver is None:
        from headroom.notify import build_notifiers

        deliver = build_notifiers(config, log)
    if iterations is None:
        from headroom.schedule import lower_priority

        lower_priority()
    state = AlertState()
    write = on_line or (lambda line: print(line, flush=True))
    completed = 0
    attempts = 0
    prev_wall: float | None = None
    prev_mono: float | None = None
    while True:
        if stop_event is not None and stop_event.is_set():
            break
        attempts += 1
        wall = wall_clock()
        mono = mono_clock()
        resumed = False
        if prev_wall is not None and prev_mono is not None and detect_resume(prev_wall, prev_mono, wall, mono):
            state.ram_high_since = None
            state.ram_alerted = False
            resumed = True
            log.info("sleep or resume detected; timers reset")
        prev_wall = wall
        prev_mono = mono
        current: Sample | None = None
        battery = False
        idle: float | None = None
        if adaptive or track_deep:
            battery, idle = _environment(on_battery_fn, idle_fn, log)
        try:
            current = read()
        except Exception:
            log.exception("sample failed")
        else:
            alerts = evaluate(current, state, config, clock())
            if tracker is not None:
                try:
                    extra = tracker.observe(  # type: ignore[attr-defined]
                        current,
                        resumed=resumed,
                        deep=track_deep,
                        idle_seconds=idle,
                        slow_cadence=not under_pressure(current, config),
                    )
                    alerts.extend(extra)
                except Exception:
                    log.exception("history update failed")
            for alert in alerts:
                try:
                    getattr(deliver, "notify")(alert)
                except Exception:
                    log.exception("notify failed")
            write(watch_line(current, config))
            try:
                _publish_for_pet(current, idle, config)
            except Exception:
                log.debug("pet snapshot failed", exc_info=True)
            completed += 1
            if iterations is None and tracker is not None:
                try:
                    from headroom.weekly import maybe_weekly

                    maybe_weekly(
                        tracker.store,
                        config,
                        deliver,
                        now=wall_clock(),
                        logger=log,
                    )
                except Exception:
                    log.exception("weekly summary failed")
        if iterations is not None and (completed >= iterations or attempts >= iterations):
            break
        seconds = float(config.interval_seconds)
        if adaptive and current is not None:
            seconds = float(
                choose_interval(
                    config,
                    on_battery=battery,
                    idle_seconds=idle,
                    under_pressure=under_pressure(current, config),
                )
            )
        _interruptible_sleep(seconds, sleep, stop_event)
        if stop_event is not None and stop_event.is_set():
            break
    return state


def run_tray(
    config: Config,
    *,
    probe: Callable[[], Sample] | None = None,
    notifier: Fanout | None = None,
    logger: logging.Logger | None = None,
    stop_event: threading.Event | None = None,
    tracker: object | None = None,
    adaptive: bool = False,
    track_deep: bool = False,
    session: object | None = None,
) -> None:
    """Block on a tray icon. Raises ``ImportError`` when tray dependencies are missing."""
    try:
        import pystray
        from PIL import Image, ImageDraw
    except ImportError as exc:
        raise ImportError(
            "tray support requires pystray and Pillow. Install with: pip install \"headroom[tray]\""
        ) from exc

    log = logger or logging.getLogger("headroom")
    holder: dict[str, Sample | None] = {"sample": None}
    stop = threading.Event() if stop_event is None else stop_event
    icon_box: dict[str, object] = {}

    def wrapped_probe() -> Sample:
        current = (probe or sample_system)()
        holder["sample"] = current
        icon = icon_box.get("icon")
        if icon is not None:
            try:
                icon.title = watch_line(current, config)  # type: ignore[attr-defined]
            except Exception:
                log.debug("could not update tray title", exc_info=True)
        return current

    def loop() -> None:
        from headroom.service import supervise

        supervise(
            lambda: run_watch_loop(
                config,
                probe=wrapped_probe,
                notifier=notifier,
                stop_event=stop,
                logger=log,
                on_line=lambda line: log.info("%s", line),
                tracker=tracker,
                adaptive=adaptive,
                track_deep=track_deep,
            ),
            stop,
            log,
        )

    thread = threading.Thread(target=loop, name="headroom-watch", daemon=True)
    thread.start()

    def show_status(icon: object, item: object) -> None:
        del item
        current = holder["sample"]
        message = "No sample yet." if current is None else watch_line(current, config)
        log.warning("status %s", message)
        try:
            icon.notify(message, "Headroom")  # type: ignore[attr-defined]
        except Exception:
            log.info("%s", message)

    def preview_cleanup(icon: object, item: object) -> None:
        del icon, item
        try:
            text = tray_preview_text(config)
        except Exception:
            log.exception("cleanup preview failed")
            return
        log.info("\n%s", text)
        print(text, flush=True)

    def show_pet(icon: object, item: object) -> None:
        del icon, item
        try:
            from headroom.pet import launch_pet_detached

            launch_pet_detached(lite=bool(config.widget_lite))
        except Exception:
            log.exception("could not show Roomy")

    def quit_app(icon: object, item: object) -> None:
        del item
        stop.set()
        icon.stop()  # type: ignore[attr-defined]

    def stop_icon() -> None:
        icon = icon_box.get("icon")
        if icon is None:
            return
        try:
            icon.stop()  # type: ignore[attr-defined]
        except Exception:
            log.debug("could not stop tray", exc_info=True)

    if session is not None:
        add = getattr(session, "add_callback", None)
        if add is not None:
            add(stop_icon)

    menu = pystray.Menu(
        pystray.MenuItem("Show status", show_status),
        pystray.MenuItem("Show Roomy", show_pet),
        pystray.MenuItem("Preview safe cleanup", preview_cleanup),
        pystray.MenuItem("Quit", quit_app),
    )
    icon = pystray.Icon("headroom", _draw_icon(Image, ImageDraw), "Headroom", menu)
    icon_box["icon"] = icon
    icon.run()
    stop.set()
    thread.join(timeout=5)


def tray_preview_text(config: Config, env: Mapping[str, str] | None = None) -> str:
    """Dry-run text for the tray. This function does not delete."""
    groups, context = discover_groups(config.enabled_cleanup, os.environ if env is None else env)
    preview = build_preview(groups, default_recycle_backend(), context)
    return format_preview(preview, dry_run=True)


def _draw_icon(image_module: object, draw_module: object) -> object:
    image = image_module.new("RGB", (64, 64), "#1c2430")  # type: ignore[attr-defined]
    draw = draw_module.Draw(image)  # type: ignore[attr-defined]
    draw.rounded_rectangle((6, 6, 58, 58), radius=10, fill="#243044")
    draw.rectangle((16, 40, 48, 50), fill="#3d4f66")
    draw.rectangle((16, 26, 36, 50), fill="#3ddc97")
    return image


def _publish_for_pet(current: Sample, idle: float | None, config: Config) -> None:
    """Hand the sample the loop already took to Roomy. The pet does not resample it."""
    del config
    from headroom.pet import publish_watch_sample
    from headroom.rules import state_dir

    publish_watch_sample(current, idle, state_dir())


def _environment(
    on_battery_fn: Callable[[], bool] | None,
    idle_fn: Callable[[], float | None] | None,
    log: logging.Logger,
) -> tuple[bool, float | None]:
    try:
        battery = on_battery() if on_battery_fn is None else bool(on_battery_fn())
    except Exception:
        log.debug("battery check failed", exc_info=True)
        battery = False
    try:
        idle = idle_seconds() if idle_fn is None else idle_fn()
    except Exception:
        log.debug("idle check failed", exc_info=True)
        idle = None
    return battery, idle


def _interruptible_sleep(
    seconds: float,
    sleep: Callable[[float], None],
    stop_event: threading.Event | None,
) -> None:
    if stop_event is None:
        sleep(seconds)
        return
    deadline = time.monotonic() + seconds
    while not stop_event.is_set():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        sleep(min(0.25, remaining))
