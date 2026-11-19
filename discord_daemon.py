"""24/7 Discord Gateway v10 WebSocket Listener + System-1 Gemini Decision Router + Notifier.

Runs lightly in the background (~25MB RAM):
  1. Maintains a persistent WebSocket to Discord Gateway v10 (GUILDS + GUILD_MESSAGES + MESSAGE_CONTENT).
  2. Saves every message in watched channels to local SQLite (`state/team_chat.db`).
  3. Instant `@mention` match (<1ms): wakes up our local AI agent via `inbox` + `wake.signal` + Windows Toast.
  4. If `IS_ROUTER=true` & `USE_GEMINI_DECISION=true`: routes untagged user messages using `gemini-decision-1`
     and posts a crisp `@AI-X` dispatch back to Discord so the right teammate's AI wakes up cleanly.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import websockets

from config import (
    LOGS_DIR,
    PID_PATH,
    Settings,
    get_db,
    load_roster,
    load_settings,
    push_inbox_item,
    set_agent_status,
)
from discord_api import DiscordRestClient
from notifier import send_windows_toast
from router import TaskRouter

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
        sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    except Exception:
        pass

GATEWAY_URL = "wss://gateway.discord.gg/?v=10&encoding=json"
# GUILDS (1<<0) | GUILD_MESSAGES (1<<9) | MESSAGE_CONTENT (1<<15) = 1 + 512 + 32768 = 33281
GATEWAY_INTENTS = 33281
LOG_FILE = LOGS_DIR / "daemon.log"


def log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


class TeamDiscordDaemon:
    def __init__(self):
        self.settings: Settings = load_settings()
        self.rest = DiscordRestClient(self.settings)
        self.router = TaskRouter(self.settings)
        self.bot_user_id: str = ""
        self.seq: Optional[int] = None
        self.session_id: Optional[str] = None
        self.running: bool = True
        self._recent_ai_hops: int = 0

    def _reload_settings(self) -> None:
        self.settings = load_settings()
        self.rest = DiscordRestClient(self.settings)
        self.router.settings = self.settings

    def _get_my_aliases(self) -> Tuple[List[str], str]:
        my_name = self.settings.agent_name.lower()
        aliases = {my_name, my_name.replace("ai-", ""), "ai-all", "all"}
        my_role_id = ""
        for ag in load_roster():
            if ag["name"].lower() == my_name:
                for a in ag.get("aliases", []):
                    aliases.add(a.lower())
                my_role_id = (ag.get("discord_role_id") or "").strip()
        return sorted(aliases), my_role_id

    def _detect_direct_mentions(self, content: str, raw_msg: Dict[str, Any]) -> Tuple[bool, bool]:
        """Returns (mentions_me, mentions_any_agent)."""
        text_low = (content or "").lower()
        aliases, my_role_id = self._get_my_aliases()
        roster = load_roster()

        mentions_me = False
        mentions_any = False

        # Check Discord native role mentions <@&ROLE_ID> or bot mention <@BOT_ID>
        if my_role_id and f"<@&{my_role_id}>" in content:
            mentions_me = True
            mentions_any = True
        if self.bot_user_id and (f"<@{self.bot_user_id}>" in content or f"<@!{self.bot_user_id}>" in content):
            # Native bot mention wakes the primary agent if no other @AI-Name is specified
            mentions_any = True
            if self.settings.is_router or len(roster) <= 1:
                mentions_me = True

        # Check text mentions like @AI-Thanh, @AI-Script, @AI-All
        for ag in roster:
            names_to_check = [ag["name"].lower()] + [a.lower() for a in ag.get("aliases", [])]
            for nm in names_to_check:
                if not nm:
                    continue
                if re.search(rf"(?<!\w)@{re.escape(nm)}(?!\w)", text_low):
                    mentions_any = True
                    if ag["name"].lower() == self.settings.agent_name.lower():
                        mentions_me = True

        if re.search(r"(?<!\w)@ai-all(?!\w)", text_low):
            mentions_me = True
            mentions_any = True

        return mentions_me, mentions_any

    def _is_own_message(self, content: str, author: Dict[str, Any], webhook_id: Optional[str]) -> bool:
        """Prevent self-echo loops whether sent via Webhook (username == agent_name) or Bot API (**[AI-Name]**)."""
        my_name = self.settings.agent_name.lower()
        uname = (author.get("username") or "").lower()
        low = (content or "").strip().lower()
        if webhook_id and uname == my_name:
            return True
        if author.get("id") == self.bot_user_id:
            if low.startswith(f"**[{my_name}]**"):
                return True
            if self.settings.is_router and ("**[ai-router]**" in low or low.startswith("📋 **[router") or low.startswith("🤔 **[router")):
                return True
        return False

    def _save_message(
        self,
        msg_id: str,
        channel_id: str,
        author_name: str,
        author_id: str,
        is_bot: bool,
        content: str,
        attachments: List[Dict[str, Any]],
        routed_to: str = "",
        router_conf: float = 0.0,
        urgency: float = 0.0,
        hop_count: int = 0,
    ) -> None:
        with get_db() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO messages
                    (id, channel_id, author_name, author_id, is_bot, content, attachments_json,
                     routed_to, router_confidence, urgency, hop_count, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    msg_id,
                    channel_id,
                    author_name,
                    author_id,
                    1 if is_bot else 0,
                    content,
                    json.dumps(attachments, ensure_ascii=False),
                    routed_to,
                    router_conf,
                    urgency,
                    hop_count,
                    time.time(),
                ),
            )
            conn.commit()

    def _get_recent_context(self, channel_id: str, limit: int = 6) -> List[Dict[str, Any]]:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT author_name as author, content FROM messages WHERE channel_id = ? ORDER BY created_at DESC LIMIT ?",
                (channel_id, limit),
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def _wake_self(
        self,
        msg_id: str,
        channel_id: str,
        sender: str,
        content: str,
        attachments: List[Dict[str, Any]],
        reason: str,
        urgency: float,
    ) -> None:
        inserted = push_inbox_item(
            message_id=msg_id,
            channel_id=channel_id,
            sender=sender,
            content=content,
            attachments=attachments,
            reason=reason,
            urgency=urgency,
        )
        if not inserted:
            return
        log(f"WAKE [{self.settings.agent_name}] ({reason}, urgency={urgency}): {sender}: {content[:120]}")
        # React 👀 on Discord so the team immediately sees the AI caught the notification
        self.rest.add_reaction(channel_id, msg_id, "👀")
        # Fire Windows Toast notification
        if self.settings.enable_windows_toast:
            send_windows_toast(
                title=f"Discord → {self.settings.agent_name} (từ {sender})",
                body=content[:200] or "[Đính kèm file]",
                subtitle=f"Lý do: {reason} · Ưu tiên {urgency:.1f}/5",
            )

    async def handle_message_create(self, data: Dict[str, Any]) -> None:
        self._reload_settings()
        channel_id = str(data.get("channel_id", ""))
        if not self.settings.watch_all_channels and self.settings.channel_ids and channel_id not in self.settings.channel_ids:
            return

        msg_id = str(data.get("id", ""))
        content = (data.get("content") or "").strip()
        author = data.get("author") or {}
        author_id = str(author.get("id", ""))
        author_name = author.get("global_name") or author.get("username") or "unknown"
        is_bot = bool(author.get("bot", False))
        webhook_id = data.get("webhook_id")
        attachments = [
            {
                "filename": a.get("filename", ""),
                "url": a.get("url", ""),
                "size": a.get("size", 0),
                "content_type": a.get("content_type", ""),
            }
            for a in (data.get("attachments") or [])
        ]

        if not content and not attachments:
            return

        # Ignore our own echoed messages
        if self._is_own_message(content, author, webhook_id):
            self._save_message(msg_id, channel_id, author_name, author_id, True, content, attachments)
            return

        # Track consecutive AI-to-AI hops to prevent infinite reply loops
        if is_bot or webhook_id:
            self._recent_ai_hops += 1
        else:
            self._recent_ai_hops = 0

        if (is_bot or webhook_id) and self._recent_ai_hops > self.settings.max_ai_hops:
            log(f"HOP_GUARD triggered ({self._recent_ai_hops} > {self.settings.max_ai_hops}); pausing auto-wake.")
            self._save_message(msg_id, channel_id, author_name, author_id, True, content, attachments, hop_count=self._recent_ai_hops)
            return

        mentions_me, mentions_any = self._detect_direct_mentions(content, data)
        log(f"MSG ch={channel_id} from={author_name} bot={is_bot} me={mentions_me} any={mentions_any}: {content[:90]}")

        # Case 1: Directly @mentioned -> 0ms instant wakeup (no LLM call needed!)
        if mentions_me:
            self._save_message(
                msg_id, channel_id, author_name, author_id, is_bot, content, attachments,
                routed_to=self.settings.agent_name, router_conf=1.0, urgency=3.5, hop_count=self._recent_ai_hops,
            )
            self._wake_self(msg_id, channel_id, author_name, content, attachments, reason="direct_mention", urgency=3.5)
            return

        # Case 2: Mentioned another AI -> store for context, stay asleep
        if mentions_any:
            self._save_message(msg_id, channel_id, author_name, author_id, is_bot, content, attachments, hop_count=self._recent_ai_hops)
            return

        # Case 3: Untagged HUMAN message & this node is the Central Router (`gemini-decision-1`)
        if not is_bot and not webhook_id and self.settings.is_router and self.settings.use_gemini_decision:
            recent_ctx = self._get_recent_context(channel_id, limit=5)
            try:
                route_res = await asyncio.to_thread(
                    self.router.route,
                    sender=author_name,
                    content=content,
                    attachments=attachments,
                    recent_context=recent_ctx,
                )
            except Exception as exc:
                log(f"ROUTER_ERROR: {exc}")
                self._save_message(msg_id, channel_id, author_name, author_id, False, content, attachments)
                return

            decision = route_res["decision"]
            assignee = route_res["assignee"]
            conf = route_res["confidence"]
            urgency = route_res["urgency"]
            log(f"ROUTER ({route_res['elapsed_ms']}ms): {decision} -> {assignee} (conf={conf:.2f}, task={route_res['is_task_prob']:.2f}, urg={urgency})")

            self._save_message(
                msg_id, channel_id, author_name, author_id, False, content, attachments,
                routed_to=assignee if decision == "ASSIGN" else "", router_conf=conf, urgency=urgency,
            )

            if decision == "ASSIGN":
                mention_tag = self.rest.format_mention(assignee)
                pct = int(round(conf * 100))
                dispatch_text = (
                    f"📋 **[Router → {mention_tag}]** *(khớp {pct}% · ưu tiên {urgency:.1f}/5 · {route_res['elapsed_ms']}ms)* — "
                    f"Giao task từ **{author_name}** cho {mention_tag}"
                )
                try:
                    await asyncio.to_thread(
                        self.rest.send_message,
                        dispatch_text,
                        channel_id,
                        "",
                        None,
                        msg_id,
                        "AI-Router",
                        False,
                    )
                except Exception as exc:
                    log(f"ROUTER_DISPATCH_ERR: {exc}")

                # If routed to OURSELVES, wake self immediately (since we ignore our own router message)
                if assignee.lower() == self.settings.agent_name.lower():
                    self._wake_self(
                        msg_id, channel_id, author_name, content, attachments,
                        reason=f"gemini_decision_router ({pct}%)", urgency=urgency,
                    )
            elif decision == "CLARIFY":
                cands = route_res.get("top_candidates") or []
                cand_str = " hay ".join([f"{self.rest.format_mention(c[0])} ({int(c[1]*100)}%)" for c in cands[:2]])
                clarify_text = f"🤔 **[Router]** Việc này bạn muốn giao cho {cand_str} ạ? (Tag `@AI-Tên` để kích hoạt ngay)"
                try:
                    await asyncio.to_thread(
                        self.rest.send_message,
                        clarify_text,
                        channel_id,
                        "",
                        None,
                        msg_id,
                        "AI-Router",
                        False,
                    )
                except Exception as exc:
                    log(f"ROUTER_CLARIFY_ERR: {exc}")
            return

        # Default: just log to history
        self._save_message(msg_id, channel_id, author_name, author_id, is_bot, content, attachments)

    async def _heartbeat_loop(self, ws, interval_sec: float) -> None:
        while self.running:
            await asyncio.sleep(interval_sec)
            try:
                await ws.send(json.dumps({"op": 1, "d": self.seq}))
            except Exception:
                break

    async def run_forever(self) -> None:
        PID_PATH.write_text(str(os.getpid()), encoding="utf-8")
        set_agent_status(self.settings.agent_name, "IDLE", "Daemon online")
        log(
            f"Starting TeamDiscordDaemon | agent={self.settings.agent_name} | "
            f"channels={self.settings.channel_ids} | is_router={self.settings.is_router} | "
            f"use_decision={self.settings.use_gemini_decision}"
        )

        backoff = 2.0
        while self.running:
            hb_task = None
            try:
                async with websockets.connect(GATEWAY_URL, max_size=8 * 1024 * 1024, ping_interval=None) as ws:
                    backoff = 2.0
                    # 1. Receive HELLO (op=10)
                    hello = json.loads(await ws.recv())
                    hb_interval = hello["d"]["heartbeat_interval"] / 1000.0
                    hb_task = asyncio.create_task(self._heartbeat_loop(ws, hb_interval))

                    # 2. Send IDENTIFY (op=2) with visible Online presence
                    identify = {
                        "op": 2,
                        "d": {
                            "token": self.settings.bot_token,
                            "intents": GATEWAY_INTENTS,
                            "properties": {"os": "windows", "browser": "team-discord-mcp", "device": "team-discord-mcp"},
                            "presence": {
                                "status": "online",
                                "afk": False,
                                "activities": [
                                    {
                                        "name": f"{self.settings.agent_name} · Gemini Decision Router",
                                        "type": 3,
                                    }
                                ],
                            },
                        },
                    }
                    await ws.send(json.dumps(identify))

                    # 3. Event loop
                    async for raw in ws:
                        if not self.running:
                            break
                        pkt = json.loads(raw)
                        if pkt.get("s") is not None:
                            self.seq = pkt["s"]
                        op = pkt.get("op")
                        t = pkt.get("t")
                        d = pkt.get("d")

                        if op == 0:
                            if t == "READY":
                                self.session_id = d.get("session_id")
                                u = d.get("user") or {}
                                self.bot_user_id = str(u.get("id", ""))
                                log(f"CONNECTED to Discord Gateway as {u.get('username')} ({self.bot_user_id})")
                            elif t == "MESSAGE_CREATE":
                                asyncio.create_task(self.handle_message_create(d))
                        elif op == 7:  # RECONNECT
                            log("Discord requested RECONNECT.")
                            break
                        elif op == 9:  # INVALID SESSION
                            log("Discord INVALID SESSION; reconnecting fresh.")
                            await asyncio.sleep(3.0)
                            break
            except Exception as exc:
                log(f"WebSocket disconnected ({exc}); reconnecting in {backoff:.1f}s...")
            finally:
                if hb_task:
                    hb_task.cancel()
            if self.running:
                await asyncio.sleep(backoff)
                backoff = min(30.0, backoff * 1.5)

        try:
            PID_PATH.unlink(missing_ok=True)
        except Exception:
            pass


def main() -> None:
    daemon = TeamDiscordDaemon()
    if not daemon.settings.bot_token or "PASTE" in daemon.settings.bot_token:
        log("ERROR: DISCORD_BOT_TOKEN chưa được điền trong .env!")
        sys.exit(1)

    def _stop(*_args):
        daemon.running = False

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    asyncio.run(daemon.run_forever())


if __name__ == "__main__":
    main()
