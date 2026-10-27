"""Shutdown signals and a crash restart for the long-running watch."""

from __future__ import annotations

import atexit
import ctypes
import logging
import os
import signal
import threading
import time
from collections.abc import Callable


class Session:
    """Stop flag shared by the watch loop, the tray, and OS shutdown notices.

    ``request_stop`` is idempotent. Callbacks are for waking a blocked tray,
    not for closing the database: the watch thread may still be writing.
    """

    def __init__(self) -> None:
        self.stop = threading.Event()
        self._stopped = False
        self._guard = threading.Lock()
        self._callbacks: list[Callable[[], None]] = []
        self._handler_ref: object | None = None
        self._signals_installed = False

    def add_callback(self, fn: Callable[[], None]) -> None:
        self._callbacks.append(fn)

    def request_stop(self) -> None:
        with self._guard:
            if self._stopped:
                return
            self._stopped = True
            self.stop.set()
            callbacks = list(self._callbacks)
        for fn in callbacks:
            try:
                fn()
            except Exception:
                logging.getLogger("headroom").debug("shutdown callback failed", exc_info=True)

    def install_signals(self) -> None:
        if self._signals_installed:
            return
        self._signals_installed = True

        def _handle(signum: int, frame: object) -> None:
            del signum, frame
            self.request_stop()

        for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
            sig = getattr(signal, name, None)
            if sig is None:
                continue
            try:
                signal.signal(sig, _handle)
            except (OSError, ValueError):
                continue
        self._install_windows_console()
        atexit.register(self.request_stop)

    def _install_windows_console(self) -> None:
        if os.name != "nt":
            return
        try:
            routine = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_uint)
        except Exception:
            return

        @routine
        def handler(ctrl_type: int) -> bool:
            # CTRL_C, BREAK, CLOSE, LOGOFF, SHUTDOWN.
            if ctrl_type in {0, 1, 2, 5, 6}:
                self.request_stop()
                return True
            return False

        self._handler_ref = handler
        try:
            ctypes.windll.kernel32.SetConsoleCtrlHandler(handler, True)
        except Exception:
            return


def supervise(
    work: Callable[[], None],
    stop: threading.Event,
    logger: logging.Logger,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Run ``work`` until it returns. Restart it after an unexpected exception."""
    delay = 2.0
    while not stop.is_set():
        try:
            work()
        except Exception:
            logger.exception("headroom stopped unexpectedly")
            if stop.is_set():
                return
            _interruptible_sleep(delay, sleep, stop)
            delay = min(delay * 2, 60.0)
            continue
        return


def _interruptible_sleep(
    seconds: float,
    sleep: Callable[[float], None],
    stop: threading.Event,
) -> None:
    deadline = time.monotonic() + seconds
    while not stop.is_set():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        sleep(min(0.25, remaining))
