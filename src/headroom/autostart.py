"""Register Headroom to start at Windows sign-in, windowless, and restart it if it dies.

Task Scheduler is preferred. If ``schtasks`` refuses the task, a Startup-folder
VBScript launches the same command with a hidden window. Other platforms refuse
without writing anything.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from headroom.logsetup import default_log_path

TASK_NAME = "Headroom"

_CREATE_NO_WINDOW = 0x08000000
_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200


class AutostartError(Exception):
    """Auto-start was not changed."""


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


def resolve_pythonw(executable: Path | None = None) -> Path:
    """Prefer pythonw so the tray has no console window."""
    exe = Path(sys.executable) if executable is None else Path(executable)
    if exe.name.lower() == "pythonw.exe":
        return exe
    sibling = exe.with_name("pythonw.exe")
    if sibling.is_file():
        return sibling
    return exe


def app_command(subcommand: str, executable: Path | None = None) -> list[str]:
    """How to start one Headroom subcommand at logon.

    A frozen build is ``headroom.exe watch``. A normal install is
    ``pythonw.exe -m headroom watch``.
    """
    if executable is None and getattr(sys, "frozen", False):
        return [str(Path(sys.executable)), subcommand]
    return [str(resolve_pythonw(executable)), "-m", "headroom", subcommand]


def watch_command(executable: Path | None = None) -> list[str]:
    return app_command("watch", executable)


def logon_commands(*, include_pet: bool = False, executable: Path | None = None) -> list[list[str]]:
    """Watch, plus Roomy when ``widget.autostart`` is on.

    The pet action carries ``--if-enabled`` so the logon launch honors the
    dashboard on/off toggle instead of forcing Roomy back on after a reboot.
    """
    commands = [app_command("watch", executable)]
    if include_pet:
        pet = app_command("pet", executable)
        pet.append("--if-enabled")
        commands.append(pet)
    return commands


def xml_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def windows_cmdline(parts: list[str]) -> str:
    quoted: list[str] = []
    for part in parts:
        if part == "" or any(ch in part for ch in ' \t"'):
            quoted.append('"' + part.replace('"', r"\"") + '"')
        else:
            quoted.append(part)
    return " ".join(quoted)


def _exec_block(command: list[str]) -> str:
    if not command:
        raise ValueError("command is empty")
    executable = xml_escape(command[0])
    arguments = xml_escape(windows_cmdline(command[1:]))
    return (
        "    <Exec>\n"
        f"      <Command>{executable}</Command>\n"
        f"      <Arguments>{arguments}</Arguments>\n"
        "    </Exec>"
    )


def build_task_xml(command: list[str], commands: list[list[str]] | None = None) -> str:
    """Task Scheduler XML: logon trigger, restart on failure, no battery stop, no time limit."""
    rows = [list(item) for item in commands] if commands is not None else [list(command)]
    if not rows or any(not row for row in rows):
        raise ValueError("command is empty")
    actions = "\n".join(_exec_block(row) for row in rows)
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Headroom RAM and disk monitor</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>999</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
{actions}
  </Actions>
</Task>
"""


def vbs_source(command: list[str], commands: list[list[str]] | None = None) -> str:
    rows = [list(item) for item in commands] if commands is not None else [list(command)]
    lines = []
    for row in rows:
        cmdline = windows_cmdline(row).replace('"', '""')
        lines.append(f'CreateObject("Wscript.Shell").Run "{cmdline}", 0, False')
    return "\r\n".join(lines) + "\r\n"


def startup_vbs_path(
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    environ = os.environ if env is None else env
    home_dir = Path.home() if home is None else home
    appdata = environ.get("APPDATA")
    if appdata:
        base = Path(appdata)
    else:
        base = home_dir / "AppData" / "Roaming"
    return base / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "Headroom.vbs"


def _state_dir(env: Mapping[str, str] | None, home: Path | None) -> Path:
    return default_log_path(env, home).parent


def install(
    *,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
    runner: Callable[[list[str]], CommandResult] | None = None,
    spawn: Callable[[list[str]], None] | None = None,
    command: list[str] | None = None,
    commands: list[list[str]] | None = None,
    include_pet: bool = False,
    start_now: bool = True,
    platform: str | None = None,
) -> str:
    """Register the logon task, or the Startup fallback. Raises off Windows."""
    plat = os.name if platform is None else platform
    if plat != "nt":
        raise AutostartError(
            "Auto-start is a Windows Task Scheduler feature. Nothing was changed."
        )
    environ = os.environ if env is None else env
    if commands is not None:
        launched = [list(item) for item in commands]
    elif command is not None:
        launched = [list(command)]
    else:
        launched = logon_commands(include_pet=include_pet)
    state = _state_dir(environ, home)
    state.mkdir(parents=True, exist_ok=True)
    xml_path = state / "Headroom.task.xml"
    xml_path.write_text(build_task_xml(launched[0], commands=launched), encoding="utf-16")
    run = _default_runner if runner is None else runner
    try:
        created = run(["schtasks", "/Create", "/TN", TASK_NAME, "/XML", str(xml_path), "/F"])
    except OSError as exc:
        created = CommandResult(1, stderr=str(exc))
    if created.returncode == 0:
        (state / "autostart.method").write_text("task\n", encoding="utf-8")
        if len(launched) > 1:
            note = "Registered the Headroom logon task. It starts watch and Roomy at sign-in."
        else:
            note = "Registered the Headroom logon task. It starts at sign-in with pythonw and the tray."
        if start_now:
            try:
                started = run(["schtasks", "/Run", "/TN", TASK_NAME])
            except OSError as exc:
                started = CommandResult(1, stderr=str(exc))
            if started.returncode != 0:
                note += " The task is registered, but starting it now failed. It still runs at the next sign-in."
            else:
                note += " It is running now."
        return note
    vbs_path = startup_vbs_path(environ, home)
    vbs_path.parent.mkdir(parents=True, exist_ok=True)
    vbs_path.write_text(vbs_source(launched[0], commands=launched), encoding="utf-8")
    (state / "autostart.method").write_text("startup\n", encoding="utf-8")
    if start_now:
        launcher = _default_spawn if spawn is None else spawn
        failed = False
        for item in launched:
            try:
                launcher(item)
            except OSError:
                failed = True
        if failed:
            return (
                "Task Scheduler rejected the task. Added a Startup-folder launcher at "
                f"{vbs_path}. Starting it now failed; it runs at the next sign-in."
            )
    return (
        "Task Scheduler rejected the task. Added a Startup-folder launcher at "
        f"{vbs_path}. It runs hidden at the next sign-in."
    )


def uninstall(
    *,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
    runner: Callable[[list[str]], CommandResult] | None = None,
    platform: str | None = None,
) -> str:
    plat = os.name if platform is None else platform
    if plat != "nt":
        raise AutostartError(
            "Auto-start is a Windows Task Scheduler feature. Nothing was changed."
        )
    environ = os.environ if env is None else env
    run = _default_runner if runner is None else runner
    notes: list[str] = []
    try:
        removed = run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"])
    except OSError as exc:
        removed = CommandResult(1, stderr=str(exc))
    if removed.returncode == 0:
        notes.append("Removed the Headroom logon task.")
    else:
        notes.append("No Headroom logon task was registered.")
    vbs_path = startup_vbs_path(environ, home)
    if vbs_path.is_file():
        vbs_path.unlink()
        notes.append(f"Removed the Startup launcher {vbs_path}.")
    state = _state_dir(environ, home)
    for name in ("autostart.method", "Headroom.task.xml"):
        path = state / name
        if path.is_file():
            path.unlink()
    return " ".join(notes)


def _default_runner(command: list[str]) -> CommandResult:
    kwargs: dict[str, object] = {}
    if os.name == "nt":
        kwargs["creationflags"] = _CREATE_NO_WINDOW
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        **kwargs,
    )
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


def _default_spawn(command: list[str]) -> None:
    if os.name != "nt":
        return
    subprocess.Popen(
        command,
        creationflags=_DETACHED_PROCESS | _CREATE_NO_WINDOW | _CREATE_NEW_PROCESS_GROUP,
        close_fds=True,
    )
