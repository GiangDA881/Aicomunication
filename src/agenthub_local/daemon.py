"""Per-member wake daemon: `agenthub-local daemon`.

Connects to the Discord Gateway with this member's own bot token and waits (no LLM, no tokens).
When the agent is @mentioned it records the message in the local inbox, reacts 👀 and shows a
desktop notification. The IDE agent then reads it through the MCP tool `hub_inbox` / `hub_wait`.

Loop protection: mentions from other bots are ignored past the hop limit, and each bot can wake
this agent at most WAKE_LIMIT times per WAKE_WINDOW seconds. Bots receive the content of
messages that mention them without the privileged Message Content intent.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import time
from collections import defaultdict, deque

import discord

from agenthub_local.config import Config
from agenthub_local.hub import meta_of
from agenthub_local.inbox import Inbox
from agenthub_local.notify import notify
from agenthub_local.projects import ProjectRegistry

log = logging.getLogger("agenthub.daemon")
WAKE_LIMIT, WAKE_WINDOW = 5, 600


class WakeFilter:
    def __init__(self, max_hops: int, clock=time.monotonic) -> None:
        self.max_hops, self.clock = max_hops, clock
        self.recent: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, author_id: str, author_is_bot: bool, hop: int) -> bool:
        if not author_is_bot:
            return True
        if hop >= self.max_hops:
            return False
        q = self.recent[author_id]
        now = self.clock()
        while q and now - q[0] > WAKE_WINDOW:
            q.popleft()
        if len(q) >= WAKE_LIMIT:
            return False
        q.append(now)
        return True


def _raw_message(m: discord.Message) -> dict:
    return {"embeds": [e.to_dict() for e in m.embeds]}


class WakeDaemon(discord.Client):
    def __init__(self, cfg: Config, wake_cmd: list[str] | None = None) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        intents.guild_messages = True
        super().__init__(intents=intents)
        self.cfg, self.inbox = cfg, Inbox(cfg.home)
        self.filter = WakeFilter(cfg.max_hops)
        self.wake_cmd = wake_cmd

    async def on_ready(self) -> None:
        log.info("listening as %s in guild %s", self.user, self.cfg.guild_id)

    async def on_message(self, m: discord.Message) -> None:
        if m.guild is None or str(m.guild.id) != self.cfg.guild_id or m.author.id == self.user.id:
            return
        if self.user not in m.mentions:
            return
        hop = int(meta_of(_raw_message(m)).get("hop", 0) or 0)
        if not self.filter.allow(str(m.author.id), m.author.bot, hop):
            log.info("ignored mention from %s (hop %s / rate limit)", m.author, hop)
            return
        channel_name = getattr(m.channel, "name", "")
        ws = ProjectRegistry(self.cfg.home, self.cfg.upload_root).resolve(str(m.channel.id), m.content or "")
        item = {"id": str(m.id), "channel_id": str(m.channel.id), "channel": channel_name,
                "author": m.author.name, "author_id": str(m.author.id), "bot": m.author.bot,
                "hop": hop, "content": m.content[:3000], "url": m.jump_url,
                "ts": m.created_at.isoformat()}
        if ws.get("workspace_path"):
            item["project_id"] = ws["project_id"]
            item["workspace_path"] = ws["workspace_path"]
        self.inbox.append(item)
        try:
            await m.add_reaction("👀")
        except discord.HTTPException:
            pass
        proj_tag = f" [{ws['project_name']}]" if ws.get("project_name") else ""
        notify(f"{m.author.display_name} → {self.user.name}{proj_tag}", m.clean_content[:200])
        if self.wake_cmd:
            # Fixed argv, no shell, message text NOT passed: the agent reads it from the inbox.
            await asyncio.create_subprocess_exec(*self.wake_cmd, stdout=asyncio.subprocess.DEVNULL,
                                                 stderr=asyncio.subprocess.DEVNULL)


def run(cfg: Config, wake_cmd: str | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    cfg.home.mkdir(parents=True, exist_ok=True)
    pid_file = cfg.home / "daemon.pid"
    pid_file.write_text(str(os.getpid()), encoding="utf-8")
    try:
        WakeDaemon(cfg, shlex.split(wake_cmd) if wake_cmd else None).run(cfg.token, log_handler=None)
    finally:
        try:
            pid_file.unlink(missing_ok=True)
        except OSError:
            pass
