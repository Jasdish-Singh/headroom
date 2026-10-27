"""Thresholds for sustained RAM pressure and low free space.

RAM must stay above the configured percent for the full sustained window.
A single spike does not notify. Disk alerts fire when a watched drive crosses
into a worse state, and they do not repeat while it stays there.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from headroom.config import Config
from headroom.formatutil import format_bytes, format_duration, format_percent
from headroom.probe import Sample

_RANK = {"ok": 0, "warn": 1, "critical": 2}


@dataclass
class AlertState:
    ram_high_since: float | None = None
    ram_alerted: bool = False
    disk_level: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Alert:
    level: str
    code: str
    title: str
    message: str


def disk_pressure(free_bytes: int, config: Config) -> str:
    """Return ``ok``, ``warn``, or ``critical``. Equal to a line is still ok."""
    if free_bytes < config.disk_critical_free_bytes:
        return "critical"
    if free_bytes < config.disk_warn_free_bytes:
        return "warn"
    return "ok"


def evaluate(sample: Sample, state: AlertState, config: Config, now: float) -> list[Alert]:
    """Update ``state`` and return alerts that should be delivered now."""
    alerts: list[Alert] = []
    alerts.extend(_ram_alerts(sample, state, config, now))
    alerts.extend(_disk_alerts(sample, state, config))
    return alerts


def _ram_alerts(
    sample: Sample,
    state: AlertState,
    config: Config,
    now: float,
) -> list[Alert]:
    percent = sample.memory.ram_percent
    if percent > config.ram_percent:
        if state.ram_high_since is None:
            state.ram_high_since = now
        elapsed = now - state.ram_high_since
        if elapsed >= config.ram_sustained_seconds and not state.ram_alerted:
            state.ram_alerted = True
            window = format_duration(config.ram_sustained_seconds)
            return [
                Alert(
                    level="warning",
                    code="ram_sustained",
                    title="Headroom: RAM is high",
                    message=(
                        f"RAM has stayed above {format_percent(config.ram_percent)} "
                        f"for {window} (now {format_percent(percent)}). "
                        "Run `headroom top` to see which processes are using it."
                    ),
                )
            ]
        return []
    state.ram_high_since = None
    state.ram_alerted = False
    return []


def _disk_alerts(sample: Sample, state: AlertState, config: Config) -> list[Alert]:
    alerts: list[Alert] = []
    seen: set[str] = set()
    watched = set(config.drives)
    for disk in sample.disks:
        if disk.key not in watched or disk.key in seen:
            continue
        seen.add(disk.key)
        level = disk_pressure(disk.free, config)
        previous = state.disk_level.get(disk.key, "ok")
        state.disk_level[disk.key] = level
        if _RANK[level] <= _RANK[previous]:
            continue
        if level == "critical":
            alerts.append(
                Alert(
                    level="critical",
                    code="disk_critical",
                    title=f"Headroom: {disk.key} is critically low",
                    message=(
                        f"{disk.key} has {format_bytes(disk.free)} free, under the "
                        f"{format_bytes(config.disk_critical_free_bytes)} critical line. "
                        "Run `headroom clean --dry-run` to preview safe cleanup."
                    ),
                )
            )
        elif level == "warn":
            alerts.append(
                Alert(
                    level="warning",
                    code="disk_low",
                    title=f"Headroom: {disk.key} is low on space",
                    message=(
                        f"{disk.key} has {format_bytes(disk.free)} free, under the "
                        f"{format_bytes(config.disk_warn_free_bytes)} warning line."
                    ),
                )
            )
    for key in list(state.disk_level):
        if key not in seen and key in watched:
            # The drive disappeared. Forget it so a later return can alert again.
            del state.disk_level[key]
    return alerts
