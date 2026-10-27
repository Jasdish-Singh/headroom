"""Console and file logging. Removal details go to the file, alerts to both."""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from pathlib import Path


def default_log_path(
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    environ = os.environ if env is None else env
    home_dir = Path.home() if home is None else home
    if os.name == "nt":
        local = environ.get("LOCALAPPDATA")
        if local:
            return Path(local) / "Headroom" / "headroom.log"
    state = environ.get("XDG_STATE_HOME")
    if state:
        return Path(state) / "headroom" / "headroom.log"
    return home_dir / ".local" / "share" / "headroom" / "headroom.log"


def configure_logging(log_path: Path | None, verbose: bool) -> logging.Logger:
    logger = logging.getLogger("headroom")
    logger.handlers.clear()
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    console = logging.StreamHandler()
    console.setLevel(logging.DEBUG if verbose else logging.WARNING)
    console.setFormatter(formatter)
    logger.addHandler(console)
    if log_path is not None:
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.FileHandler(log_path, encoding="utf-8")
        except OSError as exc:
            logger.warning("could not open log file %s: %s", log_path, exc)
        else:
            file_handler.setLevel(logging.INFO)
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)
    return logger
