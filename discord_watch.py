"""CLI & Reactive Wakeup Helper for Team Discord MCP.

Commands:
  python discord_watch.py wait --timeout 3600    # Blocks at 0 tokens until a new Discord task arrives (Antigravity Reactive Wakeup!)
  python discord_watch.py send "Hello" --to AI-Script --file path.png
  python discord_watch.py inbox [--peek]
  python discord_watch.py status IDLE "Sẵn sàng nhận việc"
  python discord_watch.py test-router "Sửa lỗi logo tập 3 giúp anh" [--file img.png]
  python discord_watch.py ensure-daemon          # Starts discord_daemon.py in background if not already running
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from config import (
    BASE_DIR,
    PID_PATH,
    load_settings,
    pop_unread_inbox,
    set_agent_status,
)
from discord_api import DiscordRestClient
from router import TaskRouter

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
        sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    except Exception:
        pass


def is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            if k32.GetExitCodeProcess(h, ctypes.byref(code)):
                return code.value == STILL_ACTIVE
            return False
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def ensure_daemon_running() -> dict:
    if PID_PATH.exists():
        try:
            pid = int(PID_PATH.read_text(encoding="utf-8").strip())
            if is_pid_alive(pid):
                return {"status": "already_running", "pid": pid}
        except Exception:
            pass

    daemon_script = BASE_DIR / "discord_daemon.py"
    creationflags = 0x08000000 | 0x00000008 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW | DETACHED_PROCESS
    proc = subprocess.Popen(
        [sys.executable, str(daemon_script)],
        cwd=str(BASE_DIR),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        creationflags=creationflags,
    )
    # Wait briefly for PID file write
    time.sleep(1.0)
    return {"status": "started", "pid": proc.pid}


def cmd_wait(args: argparse.Namespace) -> None:
    ensure_daemon_running()
    deadline = time.time() + max(5, int(args.timeout))
    poll = max(0.2, float(args.poll))

    while time.time() < deadline:
        items = pop_unread_inbox(mark_read=not args.peek, limit=10)
        if items:
            print(json.dumps({"event": "new_discord_messages", "count": len(items), "messages": items}, ensure_ascii=False, indent=2))
            return
        time.sleep(poll)

    print(json.dumps({"event": "timeout", "count": 0, "messages": []}, ensure_ascii=False))


def cmd_send(args: argparse.Namespace) -> None:
    s = load_settings()
    rest = DiscordRestClient(s)
    res = rest.send_message(
        message=args.message,
        channel_id=args.channel or s.default_channel_id,
        mention=args.to or "",
        file_path=args.file,
        reply_to=args.reply_to,
    )
    print(json.dumps({"ok": True, "message_id": res.get("id"), "channel_id": res.get("channel_id")}, ensure_ascii=False))


def cmd_inbox(args: argparse.Namespace) -> None:
    items = pop_unread_inbox(mark_read=not args.peek, limit=args.limit)
    print(json.dumps({"count": len(items), "messages": items}, ensure_ascii=False, indent=2))


def cmd_status(args: argparse.Namespace) -> None:
    s = load_settings()
    out = set_agent_status(s.agent_name, args.state, args.note or "")
    print(json.dumps(out, ensure_ascii=False))


def cmd_test_router(args: argparse.Namespace) -> None:
    s = load_settings()
    router = TaskRouter(s)
    res = router.route(
        sender=args.sender,
        content=args.message,
        local_attachment_path=args.file,
    )
    print(json.dumps(res, ensure_ascii=False, indent=2))


def main() -> None:
    p = argparse.ArgumentParser(description="Team Discord Watch & CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    pw = sub.add_parser("wait", help="Block until an unread Discord message arrives for this agent")
    pw.add_argument("--timeout", type=int, default=3600)
    pw.add_argument("--poll", type=float, default=0.5)
    pw.add_argument("--peek", action="store_true")
    pw.set_defaults(func=cmd_wait)

    ps = sub.add_parser("send", help="Send a message to Discord")
    ps.add_argument("message")
    ps.add_argument("--to", default="", help="Mention target e.g. AI-Script")
    ps.add_argument("--channel", default="")
    ps.add_argument("--file", default=None)
    ps.add_argument("--reply-to", default=None)
    ps.set_defaults(func=cmd_send)

    pi = sub.add_parser("inbox", help="Fetch unread messages routed to this agent")
    pi.add_argument("--peek", action="store_true")
    pi.add_argument("--limit", type=int, default=10)
    pi.set_defaults(func=cmd_inbox)

    pst = sub.add_parser("status", help="Set this AI agent status (IDLE / BUSY / OFFLINE)")
    pst.add_argument("state", choices=["IDLE", "BUSY", "OFFLINE", "idle", "busy", "offline"])
    pst.add_argument("note", nargs="?", default="")
    pst.set_defaults(func=cmd_status)

    pr = sub.add_parser("test-router", help="Dry-run gemini-decision-1 task routing")
    pr.add_argument("message")
    pr.add_argument("--sender", default="Thanh")
    pr.add_argument("--file", default=None)
    pr.set_defaults(func=cmd_test_router)

    pd = sub.add_parser("ensure-daemon", help="Ensure background Discord WebSocket daemon is running")
    pd.set_defaults(func=lambda _a: print(json.dumps(ensure_daemon_running())))

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
