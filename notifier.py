"""Zero-dependency Windows 10/11 Toast Notification sender via WinRT PowerShell.

Security: Untrusted message text is NEVER interpolated into PowerShell code or XML strings.
All dynamic text is passed strictly through process environment variables and assigned via
XML DOM `.InnerText` inside a static, constant PowerShell script (immune to PS/XML injection).
"""
from __future__ import annotations

import os
import subprocess
import sys

_STATIC_TOAST_PS = r"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$xml = @'
<toast duration="short">
  <visual>
    <binding template="ToastGeneric">
      <text></text>
      <text></text>
      <text placement="attribution"></text>
    </binding>
  </visual>
  <audio src="ms-winsoundevent:Notification.Default" />
</toast>
'@
$doc = New-Object Windows.Data.Xml.Dom.XmlDocument
$doc.LoadXml($xml)
$nodes = $doc.GetElementsByTagName('text')
$nodes.Item(0).InnerText = $env:TCHAT_TOAST_TITLE
$nodes.Item(1).InnerText = $env:TCHAT_TOAST_BODY
$nodes.Item(2).InnerText = $env:TCHAT_TOAST_SUBTITLE
$toast = New-Object Windows.UI.Notifications.ToastNotification $doc
$appId = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show($toast)
"""


def send_windows_toast(title: str, body: str, subtitle: str = "Team Discord AI") -> bool:
    """Display a non-blocking, injection-safe Windows Toast notification."""
    if sys.platform != "win32":
        return False

    env = os.environ.copy()
    env["TCHAT_TOAST_TITLE"] = (title or "Team AI").replace("\x00", "")[:100]
    env["TCHAT_TOAST_BODY"] = (body or "").replace("\x00", "")[:240]
    env["TCHAT_TOAST_SUBTITLE"] = (subtitle or "").replace("\x00", "")[:80]

    try:
        subprocess.Popen(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", _STATIC_TOAST_PS],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=0x08000000,  # CREATE_NO_WINDOW
        )
        return True
    except Exception:
        return False
