"""Discord command handlers, independent of discord.py so they can be tested directly.

Identity always comes from the interaction's author ID supplied by Discord, never from text the
user typed. All authorization happens in the service layer.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agenthub.authz import Principal
from agenthub.errors import AgentHubError, NotFound
from agenthub.models import Project, ProjectRole
from agenthub.services import agents, debate, identity, knowledge, runtime, tasks

DISCORD_LIMIT = 1900


def clip(text: str, n: int = DISCORD_LIMIT) -> str:
    return text if len(text) <= n else text[: n - 40] + "\n… (truncated; full text kept in AgentHub)"


@dataclass
class Caller:
    discord_id: str
    display_name: str
    channel_id: str


async def principal(db: AsyncSession, c: Caller) -> Principal:
    user = await identity.upsert_discord_user(db, c.discord_id, c.display_name)
    return Principal.of(user)


async def project_for(db: AsyncSession, c: Caller, name: str | None) -> Project:
    if name:
        return await identity.project_by_name(db, name)
    p = await db.scalar(select(Project).where(Project.discord_channel_id == c.channel_id))
    if p is None:
        raise NotFound("this channel is not bound to a project; pass project:<name> or run /project create")
    return p


async def safe(coro) -> str:
    try:
        return await coro
    except AgentHubError as e:
        return f"⛔ {type(e).__name__}: {e}"
    except ValueError as e:
        return f"⛔ {e}"


# ----- project -----

async def project_create(db: AsyncSession, c: Caller, name: str, goals: str) -> str:
    p = await principal(db, c)
    pr = await identity.create_project(db, p, name, goals)
    pr.discord_channel_id = c.channel_id
    return f"Project **{pr.name}** created and bound to this channel. You are maintainer."


async def project_add_member(db: AsyncSession, c: Caller, member_discord_id: str, member_name: str,
                             role: str, project: str | None) -> str:
    p = await principal(db, c)
    pr = await project_for(db, c, project)
    user = await identity.upsert_discord_user(db, member_discord_id, member_name)
    await identity.add_member(db, p, pr.id, user.id, ProjectRole(role))
    return f"<@{member_discord_id}> is now **{role}** of {pr.name}."


async def project_link_repo(db: AsyncSession, c: Caller, repo: str, branch: str, project: str | None) -> str:
    from agenthub.authz import require_maintainer
    p = await principal(db, c)
    pr = await project_for(db, c, project)
    await require_maintainer(db, p, pr.id)
    pr.github_repo, pr.repo_default_branch = repo, branch
    return f"{pr.name} now tracks `{repo}` ({branch})."


# ----- agents -----

async def agent_create(db: AsyncSession, c: Caller, name: str, provider: str, model: str, role: str,
                       instructions: str, project: str | None) -> str:
    p = await principal(db, c)
    pr = await project_for(db, c, project)
    a = await agents.create_agent(db, p, pr.id, name, provider, model, role=role,
                                  system_instructions=instructions)
    return (f"Agent **{a.name}** (`{a.id}`) created: {provider}/{model}, role {role}.\n"
            "Set its API key privately with `/agent key` (the reply is only visible to you).")


async def agent_key(db: AsyncSession, c: Caller, agent_ref: str, api_key: str) -> str:
    p = await principal(db, c)
    a = await agents.find_agent(db, p, agent_ref)
    fp = await agents.set_credential(db, p, a.id, api_key)
    return f"Key stored encrypted for **{a.name}** (fingerprint …{fp})."


async def agent_list(db: AsyncSession, c: Caller, project: str | None) -> str:
    p = await principal(db, c)
    pr = await project_for(db, c, project)
    rows = await agents.list_visible_agents(db, p, pr.id)
    if not rows:
        return "No agents yet."
    lines = [f"`{a.id[:8]}` **{a.name}** · {a.role} · {a.provider}/{a.model} · {a.status.value}"
             + (" · *yours*" if a.owner_id == p.user_id else f" · owner <@{(await identity.get_user(db, a.owner_id)).discord_id}>")
             for a in rows]
    return clip("\n".join(lines))


async def agent_grant(db: AsyncSession, c: Caller, agent_ref: str, grantee_discord_id: str,
                      grantee_name: str, permissions: str) -> str:
    p = await principal(db, c)
    a = await agents.find_agent(db, p, agent_ref)
    grantee = await identity.upsert_discord_user(db, grantee_discord_id, grantee_name)
    perms = [x.strip() for x in permissions.split(",") if x.strip()]
    await agents.grant(db, p, a.id, grantee.id, perms)
    return f"<@{grantee_discord_id}> may now **{', '.join(perms)}** with {a.name}."


async def agent_ask(db: AsyncSession, c: Caller, agent_ref: str, prompt: str, task: str | None) -> str:
    p = await principal(db, c)
    a = await agents.find_agent(db, p, agent_ref)
    task_id = (await tasks.get_task(db, task)).id if task else None
    res = await runtime.execute(db, p, a.id, prompt, task_id=task_id, shared=True)
    return clip(f"**{a.name}** (ctx v{res.context_version}, {res.input_tokens + res.output_tokens} tok, "
                f"${res.cost_usd:.4f}):\n{res.text}")


# ----- knowledge -----

async def kn_propose(db: AsyncSession, c: Caller, kind: str, title: str, body: str,
                     supersedes: str | None, project: str | None) -> str:
    p = await principal(db, c)
    pr = await project_for(db, c, project)
    r = await knowledge.propose(db, p, pr.id, kind=kind, title=title, body=body, supersedes_id=supersedes)
    return f"Proposed `{r.id}` ({kind}) **{title}**. A maintainer must `/knowledge verify` it."


async def kn_verify(db: AsyncSession, c: Caller, record_id: str) -> str:
    p = await principal(db, c)
    r = await knowledge.verify(db, p, record_id)
    return f"✅ `{r.id}` **{r.title}** verified. Project context is now v{r.verified_in_version}."


async def kn_list(db: AsyncSession, c: Caller, status: str, project: str | None) -> str:
    p = await principal(db, c)
    pr = await project_for(db, c, project)
    rows = await knowledge.list_records(db, p, pr.id, status=status or "verified", limit=25)
    head = f"**{pr.name}** context v{pr.context_version} · {status or 'verified'}:\n"
    return clip(head + ("\n".join(f"`{r.id[:8]}` [{r.kind.value} r{r.revision}] {r.title}" for r in rows)
                        or "(none)"))


# ----- tasks -----

async def task_create(db: AsyncSession, c: Caller, title: str, objectives: str, criteria: str,
                      project: str | None) -> str:
    p = await principal(db, c)
    pr = await project_for(db, c, project)
    crit = [x.strip() for x in criteria.split(";") if x.strip()]
    t = await tasks.create_task(db, p, pr.id, title, objectives, crit)
    return f"Created **AH-{t.short_id}** {t.title} ({len(crit)} acceptance criteria)."


async def task_assign(db: AsyncSession, c: Caller, task: str, agent_refs: str) -> str:
    p = await principal(db, c)
    ids = [(await agents.find_agent(db, p, r.strip())).id for r in agent_refs.split(",") if r.strip()]
    t = await tasks.assign(db, p, task, ids)
    return f"AH-{t.short_id} assigned (context v{t.context_version})."


async def task_status(db: AsyncSession, c: Caller, task: str) -> str:
    p = await principal(db, c)
    t = await tasks.get_task(db, task)
    hist = await tasks.history(db, p, task)
    ev = await tasks.evidence_of(db, t.id)
    lines = [f"**AH-{t.short_id} {t.title}** — `{t.state.value}` (ctx v{t.context_version})"]
    if t.handoff_report:
        lines.append(f"Handoff: {t.handoff_report.get('summary', '')[:500]}")
        if (t.handoff_report.get("drift") or {}).get("stale"):
            lines.append("⚠️ Handoff used outdated context.")
    lines.append("Evidence: " + (", ".join(
        f"{e.kind}{'✔' if e.passed else '✘' if e.passed is False else '?'}{'' if e.verified else '(unverified)'}"
        for e in ev) or "none"))
    lines += [f"· {h['from'] or '∅'} → {h['to']} {h['note'][:80]}" for h in hist[-6:]]
    return clip("\n".join(lines))


async def task_approve(db: AsyncSession, c: Caller, task: str, all_criteria_met: bool, note: str) -> str:
    p = await principal(db, c)
    t = await tasks.get_task(db, task)
    met = [all_criteria_met] * len(t.acceptance_criteria or [])
    t = await tasks.approve(db, p, task, met, note)
    return f"✅ AH-{t.short_id} approved."


# ----- debates -----

async def debate_start(db: AsyncSession, c: Caller, problem: str, participants: str, rounds: int,
                       budget_tokens: int, project: str | None) -> str:
    """participants: 'agentA:proposer, agentB:critic, agentC:synthesizer'"""
    p = await principal(db, c)
    pr = await project_for(db, c, project)
    parts = []
    for item in participants.split(","):
        ref, _, role = item.strip().partition(":")
        a = await agents.find_agent(db, p, ref.strip())
        parts.append({"agent_id": a.id, "role": role.strip() or "proposer"})
    d = await debate.create(db, p, pr.id, problem, parts, max_rounds=rounds, budget_tokens=budget_tokens)
    d = await debate.run(db, p, d.id)
    return format_debate(d.id, d.summary or {}, d.status.value)


def format_debate(debate_id: str, s: dict, status: str) -> str:
    tokens = s.get("budget", {}).get("tokens_used")
    lines = [f"**Debate `{debate_id[:8]}`** · {status} · stop: {s.get('stop_reason')} · {tokens} tok"]
    lines.append("Participants: " + ", ".join(f"{x['name']} ({x['role']}, {x['model']})"
                                              for x in s.get("participants", [])))
    if s.get("recommendation"):
        lines.append(f"Recommendation: {s['recommendation']}")
    for d in s.get("disagreements", [])[:5]:
        lines.append(f"↔ {d.get('topic') if isinstance(d, dict) else d}")
    for u in s.get("uncertainties", [])[:5]:
        lines.append(f"? {u}")
    for f in s.get("failures", []):
        lines.append(f"⚠️ {f['agent']} failed in round {f['round']}")
    lines.append("A maintainer can `/debate approve` with the final decision.")
    return clip("\n".join(lines))


async def debate_approve(db: AsyncSession, c: Caller, debate_id: str, decision: str) -> str:
    p = await principal(db, c)
    d = await debate.approve(db, p, debate_id, decision)
    return f"✅ Decision stored as verified record `{d.decision_record_id}`."
