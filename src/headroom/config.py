"""TOML and YAML configuration. Unknown keys and unknown cleanup ids are errors."""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from headroom.safety import KNOWN_CLEANUP_IDS

GB = 1024**3
MB = 1024**2


class ConfigError(Exception):
    """The config file is missing, unreadable, or invalid."""


@dataclass(frozen=True)
class Config:
    interval_seconds: int = 30
    ram_percent: float = 85.0
    ram_sustained_seconds: int = 120
    disk_warn_free_bytes: int = 30 * GB
    disk_critical_free_bytes: int = 15 * GB
    drives: tuple[str, ...] = ("C:",)
    top_processes: int = 10
    scan_top: int = 20
    toast: bool = True
    enabled_cleanup: tuple[str, ...] = KNOWN_CLEANUP_IDS
    log_path: Path | None = None
    battery_interval_seconds: int = 120
    idle_interval_seconds: int = 90
    pressure_interval_seconds: int = 10
    idle_after_seconds: int = 300
    history_max_bytes: int = 32 * 1024 * 1024
    process_every: int = 5
    large_file_bytes: int = 500 * MB
    fast_drop_bytes: int = 2 * GB
    fast_drop_window_seconds: int = 3600
    fast_drop_max_gap_seconds: int = 600
    leak_window_seconds: int = 6 * 3600
    leak_growth_bytes: int = 400 * MB
    deep_scan_dirs_per_tick: int = 25
    deep_scan_budget_seconds: float = 0.2
    tracking_paths: tuple[str, ...] = ()
    widget_autostart: bool = False
    widget_lite: bool = False


def normalize_drive(mount: str) -> str:
    """Compare ``C:``, ``C:\\``, and ``c:/`` as the same drive."""
    text = mount.strip()
    if len(text) >= 2 and text[1] == ":":
        return text[0].upper() + ":"
    trimmed = text.rstrip("\\/")
    return trimmed if trimmed else text


def candidate_config_paths(
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> list[Path]:
    """Config files in search order. The first one that exists wins.

    ``--config`` skips this list. A ``headroom.toml`` in the working directory
    is not loaded on its own, so a random project file cannot change thresholds.
    """
    environ = os.environ if env is None else env
    home_dir = Path.home() if home is None else home
    paths: list[Path] = []
    appdata = environ.get("APPDATA")
    if appdata:
        base = Path(appdata) / "Headroom"
        paths.extend(
            [base / "config.toml", base / "config.yaml", base / "config.yml"]
        )
    xdg = environ.get("XDG_CONFIG_HOME")
    config_home = Path(xdg) if xdg else home_dir / ".config"
    base = config_home / "headroom"
    paths.extend([base / "config.toml", base / "config.yaml", base / "config.yml"])
    return paths


def load_config(
    path: Path | None = None,
    *,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> tuple[Config, Path | None]:
    """Return ``(config, path)``. ``path`` is None when defaults are used."""
    if path is not None:
        if not path.is_file():
            raise ConfigError(f"config file not found: {path}")
        return config_from_mapping(_read_mapping(path), source=str(path)), path
    for candidate in candidate_config_paths(env=env, home=home):
        if candidate.is_file():
            return (
                config_from_mapping(_read_mapping(candidate), source=str(candidate)),
                candidate,
            )
    return Config(), None


def config_from_mapping(data: object, *, source: str = "config") -> Config:
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ConfigError(f"{source} must be a mapping of tables")
    allowed_sections = {
        "monitor",
        "thresholds",
        "report",
        "notifications",
        "cleanup",
        "logging",
        "tracking",
        "widget",
    }
    unknown_sections = sorted(set(data) - allowed_sections)
    if unknown_sections:
        raise ConfigError(
            f"unknown config section: {', '.join(unknown_sections)}"
        )
    monitor = _table(data, "monitor")
    thresholds = _table(data, "thresholds")
    report = _table(data, "report")
    notifications = _table(data, "notifications")
    cleanup = _table(data, "cleanup")
    logging_table = _table(data, "logging")
    tracking = _table(data, "tracking")
    widget = _table(data, "widget")
    _reject_unknown(
        monitor,
        {
            "interval_seconds",
            "battery_interval_seconds",
            "idle_interval_seconds",
            "pressure_interval_seconds",
            "idle_after_seconds",
        },
        "monitor",
    )
    _reject_unknown(
        thresholds,
        {
            "ram_percent",
            "ram_sustained_seconds",
            "disk_warn_free_gb",
            "disk_critical_free_gb",
            "drives",
        },
        "thresholds",
    )
    _reject_unknown(report, {"top_processes", "scan_top"}, "report")
    _reject_unknown(notifications, {"toast"}, "notifications")
    _reject_unknown(cleanup, {"enabled"}, "cleanup")
    _reject_unknown(logging_table, {"path"}, "logging")
    _reject_unknown(
        tracking,
        {
            "history_max_mb",
            "process_every",
            "large_file_mb",
            "fast_drop_gb",
            "fast_drop_window_seconds",
            "fast_drop_max_gap_seconds",
            "leak_window_seconds",
            "leak_growth_mb",
            "deep_scan_dirs_per_tick",
            "deep_scan_budget_seconds",
            "paths",
        },
        "tracking",
        note="See config.example.toml for the tracking table.",
    )
    _reject_unknown(
        widget,
        {"autostart", "lite"},
        "widget",
        note="autostart and lite are set here.",
    )

    defaults = Config()
    interval = _bounded_int(monitor, "interval_seconds", defaults.interval_seconds)
    battery_interval = _bounded_int(
        monitor, "battery_interval_seconds", defaults.battery_interval_seconds
    )
    idle_interval = _bounded_int(
        monitor, "idle_interval_seconds", defaults.idle_interval_seconds
    )
    pressure_interval = _bounded_int(
        monitor, "pressure_interval_seconds", defaults.pressure_interval_seconds
    )
    idle_after = _bounded_int(monitor, "idle_after_seconds", defaults.idle_after_seconds)
    ram_percent = float(
        _number(
            thresholds,
            "ram_percent",
            defaults.ram_percent,
            minimum=1,
            maximum=100,
        )
    )
    sustained = _number(
        thresholds,
        "ram_sustained_seconds",
        defaults.ram_sustained_seconds,
        minimum=0,
        maximum=86400,
        integer=True,
    )
    warn_gb = float(
        _number(
            thresholds,
            "disk_warn_free_gb",
            defaults.disk_warn_free_bytes / GB,
            minimum=0,
        )
    )
    critical_gb = float(
        _number(
            thresholds,
            "disk_critical_free_gb",
            defaults.disk_critical_free_bytes / GB,
            minimum=0,
        )
    )
    if critical_gb >= warn_gb:
        raise ConfigError(
            "disk_critical_free_gb must be lower than disk_warn_free_gb "
            f"({critical_gb} >= {warn_gb})"
        )
    drives = _drives(thresholds.get("drives", list(defaults.drives)))
    top_processes = _number(
        report,
        "top_processes",
        defaults.top_processes,
        minimum=1,
        maximum=100,
        integer=True,
    )
    scan_top = _number(
        report,
        "scan_top",
        defaults.scan_top,
        minimum=1,
        maximum=500,
        integer=True,
    )
    toast = _bool(notifications, "toast", defaults.toast)
    enabled = _enabled(cleanup.get("enabled", list(defaults.enabled_cleanup)))
    log_path = _log_path(logging_table.get("path", None))
    history_max_mb = _number(
        tracking,
        "history_max_mb",
        defaults.history_max_bytes / MB,
        minimum=1,
        maximum=4096,
        integer=True,
    )
    process_every = _number(
        tracking,
        "process_every",
        defaults.process_every,
        minimum=1,
        maximum=1000,
        integer=True,
    )
    large_file_mb = _number(
        tracking,
        "large_file_mb",
        defaults.large_file_bytes / MB,
        minimum=1,
        maximum=1024 * 1024,
        integer=True,
    )
    fast_drop_gb = _number(
        tracking,
        "fast_drop_gb",
        defaults.fast_drop_bytes / GB,
        minimum=0,
        maximum=100_000,
    )
    fast_drop_window = _bounded_int(
        tracking, "fast_drop_window_seconds", defaults.fast_drop_window_seconds, maximum=86400 * 14
    )
    fast_drop_gap = _bounded_int(
        tracking, "fast_drop_max_gap_seconds", defaults.fast_drop_max_gap_seconds
    )
    leak_window = _bounded_int(
        tracking, "leak_window_seconds", defaults.leak_window_seconds, maximum=86400 * 30
    )
    leak_growth_mb = _number(
        tracking,
        "leak_growth_mb",
        defaults.leak_growth_bytes / MB,
        minimum=1,
        maximum=1024 * 1024,
        integer=True,
    )
    deep_dirs = _number(
        tracking,
        "deep_scan_dirs_per_tick",
        defaults.deep_scan_dirs_per_tick,
        minimum=1,
        maximum=10_000,
        integer=True,
    )
    deep_budget = _number(
        tracking,
        "deep_scan_budget_seconds",
        defaults.deep_scan_budget_seconds,
        minimum=0.01,
        maximum=30,
    )
    paths = _tracking_paths(tracking.get("paths", list(defaults.tracking_paths)))
    widget_autostart = _bool(widget, "autostart", defaults.widget_autostart)
    widget_lite = _bool(widget, "lite", defaults.widget_lite)
    return Config(
        interval_seconds=int(interval),
        ram_percent=ram_percent,
        ram_sustained_seconds=int(sustained),
        disk_warn_free_bytes=int(warn_gb * GB),
        disk_critical_free_bytes=int(critical_gb * GB),
        drives=drives,
        top_processes=int(top_processes),
        scan_top=int(scan_top),
        toast=toast,
        enabled_cleanup=enabled,
        log_path=log_path,
        battery_interval_seconds=int(battery_interval),
        idle_interval_seconds=int(idle_interval),
        pressure_interval_seconds=int(pressure_interval),
        idle_after_seconds=int(idle_after),
        history_max_bytes=int(history_max_mb) * MB,
        process_every=int(process_every),
        large_file_bytes=int(large_file_mb) * MB,
        fast_drop_bytes=int(float(fast_drop_gb) * GB),
        fast_drop_window_seconds=int(fast_drop_window),
        fast_drop_max_gap_seconds=int(fast_drop_gap),
        leak_window_seconds=int(leak_window),
        leak_growth_bytes=int(leak_growth_mb) * MB,
        deep_scan_dirs_per_tick=int(deep_dirs),
        deep_scan_budget_seconds=float(deep_budget),
        tracking_paths=paths,
        widget_autostart=widget_autostart,
        widget_lite=widget_lite,
    )


def _read_mapping(path: Path) -> object:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise ConfigError(f"could not read {path}: {exc}") from exc
    suffix = path.suffix.lower()
    if not text.strip():
        return {}
    try:
        if suffix == ".toml":
            return tomllib.loads(text)
        if suffix in {".yaml", ".yml"}:
            return _load_yaml(text, path)
    except ConfigError:
        raise
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"could not read {path}: {exc}") from exc
    raise ConfigError(f"config file must end in .toml, .yaml, or .yml: {path}")


def _load_yaml(text: str, path: Path) -> object:
    try:
        import yaml
    except ImportError as exc:
        raise ConfigError(
            "PyYAML is required to read a YAML config. Install headroom with its "
            "dependencies, or use a .toml file."
        ) from exc
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"could not read {path}: {exc}") from exc


def _table(data: dict[object, object], name: str) -> dict[object, object]:
    if name not in data:
        return {}
    value = data[name]
    if not isinstance(value, dict):
        raise ConfigError(f"[{name}] must be a table")
    return value


def _reject_unknown(
    table: Mapping[object, object],
    allowed: set[str],
    section: str,
    *,
    note: str = "Paths cannot be added here.",
) -> None:
    unknown = sorted(str(key) for key in table if key not in allowed)
    if unknown:
        raise ConfigError(
            f"unknown {section} setting: {', '.join(unknown)}. {note}"
        )


def _bounded_int(
    table: Mapping[object, object],
    key: str,
    default: int,
    *,
    minimum: int = 5,
    maximum: int = 86400,
) -> int:
    return int(
        _number(table, key, default, minimum=minimum, maximum=maximum, integer=True)
    )


def _tracking_paths(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError("tracking.paths must be a list of folders")
    cleaned: list[str] = []
    for item in value:
        text = item.strip()
        if not text:
            raise ConfigError("tracking.paths contains an empty path")
        if text in cleaned:
            raise ConfigError(f"tracking.paths lists {text} more than once")
        cleaned.append(text)
    return tuple(cleaned)


def _number(
    table: Mapping[object, object],
    key: str,
    default: float,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
    integer: bool = False,
) -> float:
    if key not in table:
        return default
    value = table[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        kind = "whole number" if integer else "number"
        raise ConfigError(f"{key} must be a {kind}")
    if integer:
        if isinstance(value, float) and not value.is_integer():
            raise ConfigError(f"{key} must be a whole number")
        value = int(value)
    if minimum is not None and value < minimum:
        raise ConfigError(f"{key} must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise ConfigError(f"{key} must be <= {maximum}")
    return float(value)


def _bool(table: Mapping[object, object], key: str, default: bool) -> bool:
    if key not in table:
        return default
    value = table[key]
    if not isinstance(value, bool):
        raise ConfigError(f"{key} must be true or false")
    return value


def _drives(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or not all(isinstance(item, str) for item in value):
        raise ConfigError("drives must be a non-empty list of drive letters or mount points")
    normalized: list[str] = []
    for item in value:
        key = normalize_drive(item)
        if not key:
            raise ConfigError("drives contains an empty mount point")
        if key in normalized:
            raise ConfigError(f"drives lists {key} more than once")
        normalized.append(key)
    return tuple(normalized)


def _enabled(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError("cleanup.enabled must be a list of built-in target names")
    if len(set(value)) != len(value):
        raise ConfigError("cleanup.enabled lists a target more than once")
    unknown = [item for item in value if item not in KNOWN_CLEANUP_IDS]
    if unknown:
        known = ", ".join(KNOWN_CLEANUP_IDS)
        raise ConfigError(
            "unknown cleanup target "
            + ", ".join(unknown)
            + ". Config can disable built-in targets, not add paths. "
            f"Known targets: {known}"
        )
    return tuple(value)


def _log_path(value: object) -> Path | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigError("logging.path must be a string")
    if value.strip() == "":
        return None
    expanded = os.path.expandvars(os.path.expanduser(value))
    return Path(expanded)
