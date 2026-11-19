"""Lightweight Discord REST v10 + Webhook client (supports text, mentions, file uploads & reactions)."""
from __future__ import annotations

import json
import mimetypes
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from config import Settings, load_roster

API_BASE = "https://discord.com/api/v10"


class DiscordRestClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.headers = {
            "Authorization": f"Bot {settings.bot_token}",
            "User-Agent": "TeamDiscordMCP/1.0",
        }

    def get_me(self) -> Dict[str, Any]:
        r = httpx.get(f"{API_BASE}/users/@me", headers=self.headers, timeout=15.0)
        r.raise_for_status()
        return r.json()

    def list_channels(self) -> List[Dict[str, Any]]:
        guilds = httpx.get(f"{API_BASE}/users/@me/guilds", headers=self.headers, timeout=15.0).json()
        out: List[Dict[str, Any]] = []
        if not isinstance(guilds, list):
            return out
        for g in guilds:
            gid = g["id"]
            gname = g.get("name", "")
            chans = httpx.get(f"{API_BASE}/guilds/{gid}/channels", headers=self.headers, timeout=15.0).json()
            if not isinstance(chans, list):
                continue
            for c in chans:
                if isinstance(c, dict) and c.get("type") in (0, 5, 11, 12):
                    out.append({
                        "guild_id": gid,
                        "guild_name": gname,
                        "channel_id": c["id"],
                        "channel_name": c.get("name", ""),
                        "type": c.get("type"),
                    })
        return out

    def format_mention(self, mention: str) -> str:
        """Convert agent name (e.g. 'AI-Thanh') to Discord <@&role_id> if mapped, else '@AI-Thanh'."""
        m = (mention or "").strip()
        if not m:
            return ""
        if m.startswith("<@"):
            return m
        clean = m.lstrip("@")
        for ag in load_roster():
            if ag["name"].lower() == clean.lower() or clean.lower() in [a.lower() for a in ag.get("aliases", [])]:
                rid = (ag.get("discord_role_id") or "").strip()
                if rid:
                    return f"<@&{rid}>"
                return f"@{ag['name']}"
        return f"@{clean}"

    def send_message(
        self,
        message: str,
        channel_id: Optional[str] = None,
        mention: str = "",
        file_path: Optional[str] = None,
        reply_to: Optional[str] = None,
        sender_override: Optional[str] = None,
        use_webhook: bool = True,
    ) -> Dict[str, Any]:
        cid = (channel_id or self.settings.default_channel_id).strip()
        if not cid:
            raise ValueError("Chưa cấu hình DISCORD_CHANNEL_ID trong .env!")

        sender_name = sender_override or self.settings.agent_name
        mention_str = self.format_mention(mention)
        body = f"{mention_str} {message}".strip() if mention_str else message.strip()

        # Discord hard limit is 2000 chars per message
        if len(body) > 1950:
            body = body[:1940] + "\n…(truncated)"

        # Path A: Send via Webhook (renders custom AI display name e.g. 'AI-Thanh', 'AI-Minh' cleanly)
        if use_webhook and self.settings.webhook_url and cid == self.settings.default_channel_id and not reply_to:
            wh_url = self.settings.webhook_url
            sep = "&" if "?" in wh_url else "?"
            url = f"{wh_url}{sep}wait=true"
            payload: Dict[str, Any] = {
                "content": body,
                "username": sender_name,
            }
            if file_path:
                fp = Path(file_path)
                if not fp.exists():
                    raise FileNotFoundError(f"File not found: {fp}")
                mime = mimetypes.guess_type(str(fp))[0] or "application/octet-stream"
                with fp.open("rb") as f:
                    resp = httpx.post(
                        url,
                        data={"payload_json": json.dumps(payload, ensure_ascii=False)},
                        files={"files[0]": (fp.name, f, mime)},
                        timeout=60.0,
                    )
            else:
                resp = httpx.post(url, json=payload, timeout=20.0)
            resp.raise_for_status()
            return resp.json()

        # Path B: Send via Bot REST API (works in any channel + supports threaded replies)
        prefix = f"**[{sender_name}]** "
        full_content = body if body.startswith(prefix) else f"{prefix}{body}"
        if len(full_content) > 1990:
            full_content = full_content[:1980] + "…"

        payload = {"content": full_content}
        if reply_to:
            payload["message_reference"] = {
                "message_id": str(reply_to),
                "channel_id": cid,
                "fail_if_not_exists": False,
            }

        url = f"{API_BASE}/channels/{cid}/messages"
        if file_path:
            fp = Path(file_path)
            if not fp.exists():
                raise FileNotFoundError(f"File not found: {fp}")
            mime = mimetypes.guess_type(str(fp))[0] or "application/octet-stream"
            with fp.open("rb") as f:
                resp = httpx.post(
                    url,
                    headers=self.headers,
                    data={"payload_json": json.dumps(payload, ensure_ascii=False)},
                    files={"files[0]": (fp.name, f, mime)},
                    timeout=60.0,
                )
        else:
            resp = httpx.post(url, headers=self.headers, json=payload, timeout=20.0)
        resp.raise_for_status()
        return resp.json()

    def get_history(self, channel_id: Optional[str] = None, limit: int = 20) -> List[Dict[str, Any]]:
        cid = (channel_id or self.settings.default_channel_id).strip()
        if not cid:
            raise ValueError("Chưa cấu hình DISCORD_CHANNEL_ID!")
        r = httpx.get(
            f"{API_BASE}/channels/{cid}/messages",
            headers=self.headers,
            params={"limit": max(1, min(100, int(limit)))},
            timeout=15.0,
        )
        r.raise_for_status()
        msgs = r.json()
        out = []
        for m in reversed(msgs):
            author = m.get("author", {})
            out.append({
                "id": m["id"],
                "author": author.get("global_name") or author.get("username", "unknown"),
                "is_bot": bool(author.get("bot", False)),
                "content": m.get("content", ""),
                "attachments": [
                    {"filename": a.get("filename"), "url": a.get("url"), "content_type": a.get("content_type", "")}
                    for a in m.get("attachments", [])
                ],
                "timestamp": m.get("timestamp", ""),
            })
        return out

    def add_reaction(self, channel_id: str, message_id: str, emoji: str = "👀") -> bool:
        encoded = urllib.parse.quote(emoji)
        url = f"{API_BASE}/channels/{channel_id}/messages/{message_id}/reactions/{encoded}/@me"
        try:
            r = httpx.put(url, headers=self.headers, timeout=10.0)
            return r.status_code in (200, 204)
        except Exception:
            return False
