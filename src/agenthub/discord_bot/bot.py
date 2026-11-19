"""Discord bot process (official API via discord.py). Run: `python -m agenthub.discord_bot.bot`.

- Slash commands map to handlers.py. Replies containing secrets are never sent; `/agent key`
  replies ephemerally and the key itself is never echoed.
- A background loop mirrors bus events into the project's channel (readable projection).
- Messages from bots are ignored, so agents cannot trigger each other through Discord; agent-to-agent
  traffic goes through the internal event bus with hop limits instead.
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import tasks as dtasks

from agenthub.config import get_settings
from agenthub.db import get_sessionmaker
from agenthub.discord_bot import handlers as h
from agenthub.models import Project
from agenthub.services import bus

log = logging.getLogger("agenthub.bot")


async def _run(interaction: discord.Interaction, fn, *args, ephemeral: bool = False) -> None:
    await interaction.response.defer(thinking=True, ephemeral=ephemeral)
    caller = h.Caller(str(interaction.user.id), interaction.user.display_name, str(interaction.channel_id))
    async with get_sessionmaker()() as db:
        try:
            text = await h.safe(fn(db, caller, *args))
            if text.startswith("⛔"):
                await db.rollback()
            else:
                await db.commit()
        except Exception:  # pragma: no cover - last-resort guard
            await db.rollback()
            log.exception("command failed")
            text = "⛔ internal error (logged)"
    await interaction.followup.send(h.clip(text), ephemeral=ephemeral,
                                    allowed_mentions=discord.AllowedMentions(everyone=False, roles=False))


class AgentHubBot(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()  # no privileged message-content intent needed
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self) -> None:
        register(self.tree)
        gid = get_settings().discord_guild_id
        if gid:
            guild = discord.Object(id=int(gid))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()
        self.project_events.start()

    @dtasks.loop(seconds=5)
    async def project_events(self) -> None:
        async with get_sessionmaker()() as db:
            events = await bus.unprojected(db, limit=20)
            done = []
            for e in events:
                project = await db.get(Project, e.project_id)
                cid = (project.discord_channel_id if project else None) or get_settings().discord_projection_channel_id
                channel = self.get_channel(int(cid)) if cid else None
                if channel is not None:
                    try:
                        await channel.send(h.clip(_render(e)),
                                           allowed_mentions=discord.AllowedMentions.none())
                    except discord.HTTPException as exc:  # rate limits are retried by discord.py
                        log.warning("projection failed: %s", exc)
                        break
                done.append(e.event_id)
            await bus.mark_projected(db, done)
            await db.commit()

    @project_events.before_loop
    async def _wait(self) -> None:
        await self.wait_until_ready()


def _render(e) -> str:
    p = e.payload or {}
    t = e.message_type
    if t == "task.state":
        return f"📋 {p.get('key')} {p.get('title')}: {p.get('from')} → **{p.get('to')}** {p.get('note', '')}"
    if t == "handoff":
        return f"📦 Handoff for {p.get('key')}: {p.get('summary', '')[:600]}"
    if t in {"proposal", "critique", "synthesis"}:
        return f"💬 [{t} · r{p.get('round')} · {p.get('role')}] {p.get('text', '')[:1500]}"
    if t == "agent.failed":
        return f"⚠️ Agent failed in round {p.get('round')}: {p.get('error')}"
    if t == "decision.approved":
        return f"✅ Decision approved: {p.get('decision', '')[:800]}"
    if t == "repo.updated":
        return f"🔀 Default branch now at `{str(p.get('sha'))[:10]}`"
    return f"• {t}: {str(p)[:600]}"


def register(tree: app_commands.CommandTree) -> None:
    project = app_commands.Group(name="project", description="Projects")
    agent = app_commands.Group(name="agent", description="Your agents")
    kn = app_commands.Group(name="knowledge", description="Shared knowledge")
    task = app_commands.Group(name="task", description="Tasks")
    deb = app_commands.Group(name="debate", description="Structured debates")

    @project.command(name="create", description="Create a project bound to this channel")
    async def p_create(i: discord.Interaction, name: str, goals: str = "") -> None:
        await _run(i, h.project_create, name, goals)

    @project.command(name="add-member", description="Add a member (maintainers only)")
    @app_commands.choices(role=[app_commands.Choice(name=r, value=r) for r in ("member", "maintainer")])
    async def p_add(i: discord.Interaction, user: discord.Member, role: str = "member",
                    project_name: str | None = None) -> None:
        await _run(i, h.project_add_member, str(user.id), user.display_name, role, project_name)

    @project.command(name="link-repo", description="Track a GitHub repo (owner/name)")
    async def p_repo(i: discord.Interaction, repo: str, branch: str = "main",
                     project_name: str | None = None) -> None:
        await _run(i, h.project_link_repo, repo, branch, project_name)

    @agent.command(name="create", description="Create an agent you own")
    @app_commands.choices(provider=[app_commands.Choice(name=x, value=x) for x in
                                    ("openai", "anthropic", "google", "openai_compatible", "mock")])
    async def a_create(i: discord.Interaction, name: str, provider: str, model: str,
                       role: str = "generalist", instructions: str = "",
                       project_name: str | None = None) -> None:
        await _run(i, h.agent_create, name, provider, model, role, instructions, project_name)

    @agent.command(name="key", description="Set your agent's API key (private)")
    async def a_key(i: discord.Interaction, agent_name: str, api_key: str) -> None:
        await _run(i, h.agent_key, agent_name, api_key, ephemeral=True)

    @agent.command(name="list", description="List agents in the project")
    async def a_list(i: discord.Interaction, project_name: str | None = None) -> None:
        await _run(i, h.agent_list, project_name)

    @agent.command(name="grant", description="Let someone execute/collaborate with your agent")
    async def a_grant(i: discord.Interaction, agent_name: str, user: discord.Member,
                      permissions: str = "collaborate") -> None:
        await _run(i, h.agent_grant, agent_name, str(user.id), user.display_name, permissions)

    @agent.command(name="ask", description="Ask an agent (yours or granted)")
    async def a_ask(i: discord.Interaction, agent_name: str, prompt: str, task_key: str | None = None) -> None:
        await _run(i, h.agent_ask, agent_name, prompt, task_key)

    @kn.command(name="propose", description="Propose a knowledge record")
    @app_commands.choices(kind=[app_commands.Choice(name=x, value=x) for x in
                                ("requirement", "architecture", "constraint", "terminology", "fact",
                                 "decision", "note")])
    async def k_propose(i: discord.Interaction, kind: str, title: str, body: str,
                        supersedes: str | None = None, project_name: str | None = None) -> None:
        await _run(i, h.kn_propose, kind, title, body, supersedes, project_name)

    @kn.command(name="verify", description="Verify a proposed record (maintainers)")
    async def k_verify(i: discord.Interaction, record_id: str) -> None:
        await _run(i, h.kn_verify, record_id)

    @kn.command(name="list", description="List knowledge")
    async def k_list(i: discord.Interaction, status: str = "verified", project_name: str | None = None) -> None:
        await _run(i, h.kn_list, status, project_name)

    @task.command(name="create", description="Create a task; criteria separated by ';'")
    async def t_create(i: discord.Interaction, title: str, objectives: str = "", criteria: str = "",
                       project_name: str | None = None) -> None:
        await _run(i, h.task_create, title, objectives, criteria, project_name)

    @task.command(name="assign", description="Assign agents (comma-separated names)")
    async def t_assign(i: discord.Interaction, task_key: str, agents: str) -> None:
        await _run(i, h.task_assign, task_key, agents)

    @task.command(name="status", description="Show task state, evidence and history")
    async def t_status(i: discord.Interaction, task_key: str) -> None:
        await _run(i, h.task_status, task_key)

    @task.command(name="approve", description="Approve a task in review (maintainers)")
    async def t_approve(i: discord.Interaction, task_key: str, all_criteria_met: bool, note: str = "") -> None:
        await _run(i, h.task_approve, task_key, all_criteria_met, note)

    @deb.command(name="start", description="Run a bounded debate: 'a1:proposer, a2:critic, a3:synthesizer'")
    async def d_start(i: discord.Interaction, problem: str, participants: str, rounds: int = 2,
                      budget_tokens: int = 50_000, project_name: str | None = None) -> None:
        await _run(i, h.debate_start, problem, participants, rounds, budget_tokens, project_name)

    @deb.command(name="approve", description="Approve a debate outcome as a verified decision")
    async def d_approve(i: discord.Interaction, debate_id: str, decision: str) -> None:
        await _run(i, h.debate_approve, debate_id, decision)

    for g in (project, agent, kn, task, deb):
        tree.add_command(g)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    token = get_settings().discord_bot_token
    if not token:
        raise SystemExit("AGENTHUB_DISCORD_BOT_TOKEN is not set")
    AgentHubBot().run(token, log_handler=None)


if __name__ == "__main__":
    main()
