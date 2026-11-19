"""Desktop notifications without ever interpolating message text into a shell or script.

The text travels as environment variables / argv to a constant script, so a Discord message such
as `$(Remove-Item ...)` or `"; rm -rf ~` is displayed, never executed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

_PS = r"""
$ErrorActionPreference = 'Stop'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$t = [Security.SecurityElement]::Escape($env:AH_TITLE)
$b = [Security.SecurityElement]::Escape($env:AH_BODY)
$doc = New-Object Windows.Data.Xml.Dom.XmlDocument
$doc.LoadXml('<toast><visual><binding template="ToastGeneric"><text>' + $t + '</text><text>' + $b + '</text></binding></visual></toast>')
$app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show((New-Object Windows.UI.Notifications.ToastNotification $doc))
"""

_OSA = 'on run argv\ndisplay notification (item 2 of argv) with title (item 1 of argv)\nend run'


def build_command(title: str, body: str, platform: str = sys.platform) -> tuple[list[str], dict[str, str]] | None:
    title, body = title[:100], body[:240]
    if platform == "win32":
        env = {**os.environ, "AH_TITLE": title, "AH_BODY": body}
        return ["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", _PS], env
    if platform == "darwin":
        return ["osascript", "-e", _OSA, title, body], dict(os.environ)
    if shutil.which("notify-send"):
        return ["notify-send", "--", title, body], dict(os.environ)
    return None


def notify(title: str, body: str) -> bool:
    built = build_command(title, body)
    if built is None:
        return False
    cmd, env = built
    try:
        subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=0x08000000 if sys.platform == "win32" else 0)
        return True
    except OSError:
        return False
