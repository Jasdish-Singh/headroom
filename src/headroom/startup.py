"""Startup audit: Run keys, Startup folders, and logon tasks.

Disabling an entry moves it aside or removes the value only after the caller has
a backup record. Restore puts that record back. This module does not delete a
file permanently.
"""

from __future__ import annotations

import csv
import io
import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

MB = 1024 * 1024

_IMPACT: tuple[tuple[str, str, int], ...] = (
    ("discord", "high", 250 * MB),
    ("steam", "high", 180 * MB),
    ("epic", "high", 160 * MB),
    ("teams", "high", 300 * MB),
    ("spotify", "medium", 150 * MB),
    ("slack", "medium", 200 * MB),
    ("onedrive", "medium", 100 * MB),
    ("dropbox", "medium", 120 * MB),
    ("chrome", "medium", 80 * MB),
    ("nvidia", "low", 40 * MB),
    ("securityhealth", "low", 30 * MB),
)


class StartupError(Exception):
    """A startup entry could not be changed."""


@dataclass
class StartupEntry:
    id: str
    source: str
    name: str
    command: str
    location: str
    enabled: bool
    impact: str
    ram_bytes: int
    payload: dict = field(default_factory=dict)


def estimate_impact(name: str, command: str, running: dict[str, int] | None = None) -> tuple[str, int]:
    text = f"{name} {command}".casefold()
    for key, impact, ram in _IMPACT:
        if key in text:
            if running:
                for proc_name, rss in running.items():
                    if key in proc_name.casefold():
                        return impact, int(rss)
            return impact, ram
    if running:
        for proc_name, rss in running.items():
            if proc_name.casefold() in text or name.casefold() in proc_name.casefold():
                return "medium", int(rss)
    return "low", 20 * MB


def entries_from_run_values(hive: str, key: str, values: list[tuple[str, str]]) -> list[StartupEntry]:
    entries: list[StartupEntry] = []
    for name, command in values:
        impact, ram = estimate_impact(name, command)
        entries.append(
            StartupEntry(
                id=f"run:{hive}:{name}",
                source="run",
                name=name,
                command=command,
                location=f"{hive}\\{key}",
                enabled=True,
                impact=impact,
                ram_bytes=ram,
                payload={"kind": "run", "hive": hive, "key": key, "name": name, "command": command},
            )
        )
    return entries


def entries_from_folder(folder: Path, *, label: str) -> list[StartupEntry]:
    if not folder.is_dir() or folder.is_symlink():
        return []
    entries: list[StartupEntry] = []
    try:
        children = sorted(folder.iterdir(), key=lambda item: item.name.casefold())
    except OSError:
        return []
    for child in children:
        if child.is_symlink() or not child.is_file():
            continue
        impact, ram = estimate_impact(child.stem, str(child))
        entries.append(
            StartupEntry(
                id=f"folder:{label}:{child.name}",
                source="folder",
                name=child.name,
                command=str(child),
                location=str(folder),
                enabled=True,
                impact=impact,
                ram_bytes=ram,
                payload={"kind": "file", "path": str(child)},
            )
        )
    return entries


def entries_from_schtasks_csv(text: str) -> list[StartupEntry]:
    """Parse ``schtasks /query /fo csv`` output. Only logon tasks are kept."""
    if not text.strip():
        return []
    reader = csv.DictReader(io.StringIO(text))
    entries: list[StartupEntry] = []
    for row in reader:
        if not row:
            continue
        folded = {(key or "").strip().casefold(): (value or "").strip() for key, value in row.items()}
        schedule = " ".join(
            folded.get(key, "")
            for key in ("logon mode", "schedule type", "scheduled task state", "status", "taskname", "task name")
        )
        task_name = folded.get("taskname") or folded.get("task name") or ""
        if not task_name:
            continue
        blob = " ".join(folded.values()).casefold()
        if "logon" not in blob and "at logon" not in blob and "onlogon" not in blob:
            continue
        command = folded.get("task to run") or folded.get("actions") or task_name
        impact, ram = estimate_impact(task_name, command)
        enabled = folded.get("scheduled task state", folded.get("status", "")).casefold() not in {"disabled"}
        entries.append(
            StartupEntry(
                id=f"task:{task_name}",
                source="task",
                name=task_name,
                command=command,
                location="Task Scheduler",
                enabled=enabled,
                impact=impact,
                ram_bytes=ram,
                payload={"kind": "task", "name": task_name, "schedule": schedule},
            )
        )
    return entries


def format_startup(entries: list[StartupEntry]) -> str:
    lines = [
        "Headroom startup",
        "Listing only. Nothing is disabled until you type DISABLE.",
        "headroom startup restore puts a disabled entry back from the backup.",
        "",
    ]
    if not entries:
        lines.append("No startup entries were found.")
        return "\n".join(lines) + "\n"
    lines.append(f"{'IMPACT':<8} {'RAM':>10}  {'SOURCE':<8}  NAME")
    for entry in entries:
        state = "" if entry.enabled else " (disabled)"
        lines.append(
            f"{entry.impact:<8} {entry.ram_bytes:10d}  {entry.source:<8}  {entry.name}{state}"
        )
        lines.append(f"           {entry.command}")
        lines.append(f"           id {entry.id}")
    lines.append("")
    lines.append("Disable one entry with: headroom startup disable ID")
    return "\n".join(lines) + "\n"


class MemoryCatalog:
    """In-memory catalog used by tests and as the shape of a reversible store."""

    def __init__(self, entries: list[StartupEntry]) -> None:
        self.entries = {entry.id: entry for entry in entries}
        self.disabled: dict[str, StartupEntry] = {}

    def list_entries(self) -> list[StartupEntry]:
        rows = list(self.entries.values()) + list(self.disabled.values())
        rows.sort(key=lambda item: (item.source, item.name.casefold()))
        return rows

    def disable(self, entry_id: str) -> StartupEntry:
        entry = self.entries.get(entry_id)
        if entry is None:
            raise StartupError(f"unknown startup entry {entry_id}")
        self.entries.pop(entry_id)
        disabled = StartupEntry(
            id=entry.id,
            source=entry.source,
            name=entry.name,
            command=entry.command,
            location=entry.location,
            enabled=False,
            impact=entry.impact,
            ram_bytes=entry.ram_bytes,
            payload=dict(entry.payload),
        )
        self.disabled[entry_id] = disabled
        return disabled

    def restore(self, entry: StartupEntry) -> None:
        self.disabled.pop(entry.id, None)
        restored = StartupEntry(
            id=entry.id,
            source=entry.source,
            name=entry.name,
            command=entry.command,
            location=entry.location,
            enabled=True,
            impact=entry.impact,
            ram_bytes=entry.ram_bytes,
            payload=dict(entry.payload),
        )
        self.entries[entry.id] = restored


class FileCatalog:
    """Disable a Startup-folder file by moving it into the backup directory."""

    def __init__(self, folder: Path, backup: Path, *, label: str = "user") -> None:
        self.folder = folder
        self.backup = backup
        self.label = label

    def list_entries(self) -> list[StartupEntry]:
        return entries_from_folder(self.folder, label=self.label)

    def disable(self, entry_id: str) -> StartupEntry:
        match = {entry.id: entry for entry in self.list_entries()}
        entry = match.get(entry_id)
        if entry is None:
            raise StartupError(f"unknown startup entry {entry_id}")
        source = Path(str(entry.payload["path"]))
        if source.is_symlink():
            raise StartupError(f"refusing to move a link: {source}")
        destination = self.backup / source.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(os.fspath(source), os.fspath(destination))
        payload = dict(entry.payload)
        payload["disabled_path"] = str(destination)
        return StartupEntry(
            id=entry.id,
            source=entry.source,
            name=entry.name,
            command=entry.command,
            location=entry.location,
            enabled=False,
            impact=entry.impact,
            ram_bytes=entry.ram_bytes,
            payload=payload,
        )

    def restore(self, entry: StartupEntry) -> None:
        disabled = entry.payload.get("disabled_path")
        original = entry.payload.get("path")
        if not disabled or not original:
            raise StartupError(f"backup for {entry.id} has no file path")
        source = Path(str(disabled))
        target = Path(str(original))
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(os.fspath(source), os.fspath(target))


def pop_run_value(values: dict[str, str], name: str) -> str:
    if name not in values:
        raise StartupError(f"run value {name} is not present")
    return values.pop(name)


def put_run_value(values: dict[str, str], name: str, command: str) -> None:
    values[name] = command


def backup_path(directory: Path | None = None) -> Path:
    from headroom.rules import state_dir

    return (state_dir() if directory is None else directory) / "startup-backup.json"


def append_backup(entry: StartupEntry, directory: Path | None = None) -> None:
    path = backup_path(directory)
    rows = _read_backup(path)
    rows = [row for row in rows if row.get("id") != entry.id]
    rows.append(_entry_json(entry))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows), encoding="utf-8")


def load_backup(directory: Path | None = None) -> list[StartupEntry]:
    return [_entry_from_json(row) for row in _read_backup(backup_path(directory))]


def remove_backup(entry_ids: set[str], directory: Path | None = None) -> None:
    path = backup_path(directory)
    rows = [row for row in _read_backup(path) if str(row.get("id")) not in entry_ids]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows), encoding="utf-8")


def _entry_json(entry: StartupEntry) -> dict:
    return {
        "id": entry.id,
        "source": entry.source,
        "name": entry.name,
        "command": entry.command,
        "location": entry.location,
        "enabled": entry.enabled,
        "impact": entry.impact,
        "ram_bytes": entry.ram_bytes,
        "payload": entry.payload,
    }


def _entry_from_json(row: dict) -> StartupEntry:
    return StartupEntry(
        id=str(row["id"]),
        source=str(row.get("source", "")),
        name=str(row.get("name", "")),
        command=str(row.get("command", "")),
        location=str(row.get("location", "")),
        enabled=bool(row.get("enabled", False)),
        impact=str(row.get("impact", "low")),
        ram_bytes=int(row.get("ram_bytes", 0)),
        payload=dict(row.get("payload") or {}),
    )


def _read_backup(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    return [row for row in data if isinstance(row, dict) and "id" in row]


def default_catalog():
    """The live Windows catalog, or an empty memory catalog elsewhere."""
    if os.name != "nt":
        return MemoryCatalog([])
    return WindowsCatalog()


class WindowsCatalog:
    """Read and reversibly disable real Windows startup entries."""

    def list_entries(self) -> list[StartupEntry]:
        entries: list[StartupEntry] = []
        entries.extend(_windows_run_entries())
        appdata = os.environ.get("APPDATA")
        if appdata:
            entries.extend(
                entries_from_folder(
                    Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup",
                    label="user",
                )
            )
        programdata = os.environ.get("PROGRAMDATA")
        if programdata:
            entries.extend(
                entries_from_folder(
                    Path(programdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "StartUp",
                    label="common",
                )
            )
        entries.extend(_windows_logon_tasks())
        return entries

    def disable(self, entry_id: str) -> StartupEntry:
        for entry in self.list_entries():
            if entry.id != entry_id:
                continue
            kind = entry.payload.get("kind")
            if kind == "run":
                command = _delete_run_value(str(entry.payload["hive"]), str(entry.payload["key"]), str(entry.payload["name"]))
                payload = dict(entry.payload)
                payload["command"] = command
                return _disabled(entry, payload)
            if kind == "file":
                catalog = FileCatalog(Path(entry.location), _disabled_dir(), label="user")
                return catalog.disable(entry.id)
            if kind == "task":
                _schtasks(["/Change", "/TN", str(entry.payload["name"]), "/DISABLE"])
                return _disabled(entry, dict(entry.payload))
            raise StartupError(f"cannot disable {entry_id}")
        raise StartupError(f"unknown startup entry {entry_id}")

    def restore(self, entry: StartupEntry) -> None:
        kind = entry.payload.get("kind")
        if kind == "run":
            _write_run_value(
                str(entry.payload["hive"]),
                str(entry.payload["key"]),
                str(entry.payload["name"]),
                str(entry.payload.get("command") or entry.command),
            )
            return
        if kind == "file":
            FileCatalog(Path(entry.location), _disabled_dir()).restore(entry)
            return
        if kind == "task":
            _schtasks(["/Change", "/TN", str(entry.payload["name"]), "/ENABLE"])
            return
        raise StartupError(f"cannot restore {entry.id}")


def _disabled(entry: StartupEntry, payload: dict) -> StartupEntry:
    return StartupEntry(
        id=entry.id,
        source=entry.source,
        name=entry.name,
        command=entry.command,
        location=entry.location,
        enabled=False,
        impact=entry.impact,
        ram_bytes=entry.ram_bytes,
        payload=payload,
    )


def _disabled_dir() -> Path:
    from headroom.rules import state_dir

    return state_dir() / "startup-disabled"


def _windows_run_entries() -> list[StartupEntry]:
    try:
        import winreg
    except ImportError:
        return []
    specs = (
        (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", "HKCU"),
        (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\RunOnce", "HKCU"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Run", "HKLM"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\RunOnce", "HKLM"),
    )
    entries: list[StartupEntry] = []
    for hive, key_path, label in specs:
        values: list[tuple[str, str]] = []
        try:
            with winreg.OpenKey(hive, key_path) as key:
                count = winreg.QueryInfoKey(key)[1]
                for index in range(count):
                    try:
                        name, command, _typ = winreg.EnumValue(key, index)
                    except OSError:
                        continue
                    values.append((str(name), str(command)))
        except OSError:
            continue
        entries.extend(entries_from_run_values(label, key_path, values))
    return entries


def _hive(label: str):
    import winreg

    if label == "HKCU":
        return winreg.HKEY_CURRENT_USER
    if label == "HKLM":
        return winreg.HKEY_LOCAL_MACHINE
    raise StartupError(f"unknown hive {label}")


def _delete_run_value(hive: str, key_path: str, name: str) -> str:
    import winreg

    with winreg.OpenKey(_hive(hive), key_path, 0, winreg.KEY_READ | winreg.KEY_SET_VALUE) as key:
        command, _typ = winreg.QueryValueEx(key, name)
        winreg.DeleteValue(key, name)
    return str(command)


def _write_run_value(hive: str, key_path: str, name: str, command: str) -> None:
    import winreg

    with winreg.OpenKey(_hive(hive), key_path, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, name, 0, winreg.REG_SZ, command)


def _windows_logon_tasks() -> list[StartupEntry]:
    try:
        import subprocess
    except ImportError:
        return []
    try:
        completed = subprocess.run(
            ["schtasks", "/Query", "/FO", "CSV", "/V"],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if completed.returncode != 0:
        return []
    return entries_from_schtasks_csv(completed.stdout)


def _schtasks(args: list[str]) -> None:
    import subprocess

    completed = subprocess.run(
        ["schtasks", *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "schtasks failed").strip()
        raise StartupError(detail)
