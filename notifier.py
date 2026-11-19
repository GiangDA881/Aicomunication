"""Zero-dependency Windows 10/11 Toast Notification sender via WinRT PowerShell."""
from __future__ import annotations

import subprocess
import sys
import xml.sax.saxutils as saxutils


def send_windows_toast(title: str, body: str, subtitle: str = "Team Discord AI") -> bool:
    """Display a non-blocking Windows Toast notification in the bottom-right corner."""
    if sys.platform != "win32":
        return False

    t = saxutils.escape((title or "Team AI")[:100])
    b = saxutils.escape((body or "")[:240])
    s = saxutils.escape((subtitle or "")[:80])

    ps_script = f"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$xml = @"
<toast duration="short">
  <visual>
    <binding template="ToastGeneric">
      <text>{t}</text>
      <text>{b}</text>
      <text placement="attribution">{s}</text>
    </binding>
  </visual>
  <audio src="ms-winsoundevent:Notification.Default" />
</toast>
"@
$doc = New-Object Windows.Data.Xml.Dom.XmlDocument
$doc.LoadXml($xml)
$toast = New-Object Windows.UI.Notifications.ToastNotification $doc
$appId = '{{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}}\\WindowsPowerShell\\v1.0\\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show($toast)
"""
    try:
        subprocess.Popen(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps_script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=0x08000000,  # CREATE_NO_WINDOW
        )
        return True
    except Exception:
        return False
