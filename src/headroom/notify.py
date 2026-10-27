"""Alert delivery: log, and a Windows toast when PowerShell can show one.

Toast text is passed in environment variables and XML-escaped in PowerShell.
It is not interpolated into the shell command.
"""

from __future__ import annotations

import logging
import os
import subprocess
from collections.abc import Callable, Mapping, Sequence

from headroom.alerts import Alert
from headroom.config import Config

_TOAST_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$title = $env:HEADROOM_TOAST_TITLE
$body = $env:HEADROOM_TOAST_BODY
if (-not $title) { $title = 'Headroom' }
if (-not $body) { $body = '' }
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
function Escape([string]$s) {
  if ($null -eq $s) { return '' }
  return [System.Security.SecurityElement]::Escape($s)
}
$xmlText = @"
<toast>
  <visual>
    <binding template="ToastGeneric">
      <text>$(Escape $title)</text>
      <text>$(Escape $body)</text>
    </binding>
  </visual>
</toast>
"@
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml($xmlText)
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
$appId = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show($toast)
"""

ToastRunner = Callable[[Sequence[str], Mapping[str, str]], object]


class LogNotifier:
    def __init__(self, logger: logging.Logger | None = None) -> None:
        self.logger = logger or logging.getLogger("headroom")

    def notify(self, alert: Alert) -> None:
        if alert.level == "critical":
            self.logger.critical("%s: %s", alert.title, alert.message)
        else:
            self.logger.warning("%s: %s", alert.title, alert.message)


class WindowsToastNotifier:
    """Best-effort toast. On other operating systems this does nothing."""

    def __init__(self, *, enabled: bool | None = None, runner: ToastRunner | None = None) -> None:
        self.enabled = os.name == "nt" if enabled is None else enabled
        self.runner = runner

    def notify(self, alert: Alert) -> None:
        if not self.enabled:
            return
        try:
            send_toast(alert.title, alert.message, runner=self.runner)
        except Exception:
            logging.getLogger("headroom").warning("Windows toast failed", exc_info=True)


class Fanout:
    def __init__(self, notifiers: list[object]) -> None:
        self.notifiers = notifiers

    def notify(self, alert: Alert) -> None:
        for notifier in self.notifiers:
            try:
                notify = getattr(notifier, "notify")
                notify(alert)
            except Exception:
                logging.getLogger("headroom").warning(
                    "notifier %s failed",
                    type(notifier).__name__,
                    exc_info=True,
                )


def build_notifiers(config: Config, logger: logging.Logger | None = None) -> Fanout:
    log = logger or logging.getLogger("headroom")
    notifiers: list[object] = [LogNotifier(log)]
    if config.toast:
        notifiers.append(WindowsToastNotifier())
    return Fanout(notifiers)


def send_toast(title: str, body: str, *, runner: ToastRunner | None = None) -> None:
    """Show a toast. ``runner`` is for tests and receives ``(argv, env)``."""
    clean_title = " ".join(title.split()) or "Headroom"
    clean_body = " ".join(body.split())
    env = os.environ.copy()
    env["HEADROOM_TOAST_TITLE"] = clean_title
    env["HEADROOM_TOAST_BODY"] = clean_body
    command = [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        _TOAST_SCRIPT,
    ]
    if runner is not None:
        runner(command, env)
        return
    if os.name != "nt":
        return
    kwargs: dict[str, object] = {
        "env": env,
        "capture_output": True,
        "timeout": 20,
        "check": False,
        "creationflags": 0x08000000,  # CREATE_NO_WINDOW
    }
    subprocess.run(command, **kwargs)  # type: ignore[arg-type]
