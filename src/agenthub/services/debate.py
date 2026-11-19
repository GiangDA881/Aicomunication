"""Bounded multi-agent debate.

Flow: independent proposals (round 0, no agent sees another's) -> critique/rebuttal rounds that see
the transcript -> synthesis -> human approval -> verified decision record.

Hard stops: max_rounds, token budget, USD budget, no new objections, fewer than two usable
proposals. Agent consensus is reported, never treated as verification.
"""

from __future__ import annotations

import json
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agenthub import authz
from agenthub.authz import Principal
from agenthub.errors import AgentUnavailable, BudgetExceeded, Conflict, InvalidTransition, NotFound
from agenthub.models import (
    AgentDefinition,
    Debate,
    DebateStatus,
    DebateTurn,
    KnowledgeKind,
    Project,
)
from agenthub.services import agents as agent_svc
from agenthub.services import audit, bus, knowledge, runtime

ROLES = {"proposer", "critic", "alternative", "verifier", "reviewer", "synthesizer"}

ROLE_BRIEF = {
    "proposer": "Propose a concrete solution. Give evidence or reasoning for each claim.",
    "critic": "Find the weakest assumptions and failure modes. Be specific; cite evidence.",
    "alternative": "Propose a materially different approach and compare trade-offs.",
    "verifier": "Identify testable claims and say how each would be verified; flag unsupported ones.",
    "reviewer": "Assess technical soundness, risks, and fit with verified project decisions.",
}

OBJECTION_RE = re.compile(r"NEW_OBJECTIONS:\s*(yes|no)", re.IGNORECASE)


def _wrap(agent_name: str, role: str, text: str) -> str:
    # Other agents' output is untrusted input for the reader.
    return f'<untrusted source="agent:{agent_name}" role="{role}">\n{text}\n</untrusted>'


async def create(
    db: AsyncSession, p: Principal, project_id: str, problem: str,
    participants: list[dict[str, str]], *, max_rounds: int = 2, budget_tokens: int = 50_000,
    budget_usd: float = 1.0, task_id: str | None = None,
) -> Debate:
    await authz.require_member(db, p, project_id)
    if not 0 <= max_rounds <= 5:
        raise ValueError("max_rounds must be 0..5")
    roles = [x["role"] for x in participants]
    if set(roles) - ROLES:
        raise ValueError(f"unknown roles: {sorted(set(roles) - ROLES)}")
    if roles.count("synthesizer") != 1:
        raise ValueError("exactly one synthesizer is required")
    if len([r for r in roles if r != "synthesizer"]) < 2:
        raise ValueError("at least two non-synthesizer participants are required")
    for x in participants:
        agent = await agent_svc.get_agent(db, x["agent_id"])
        if agent.project_id != project_id:
            raise NotFound(f"agent {agent.id} not in project")
        await authz.require_collaborate(db, p, agent)
    project = await db.get(Project, project_id)
    assert project is not None
    d = Debate(project_id=project_id, created_by=p.user_id, problem=problem, participants=participants,
               max_rounds=max_rounds, budget_tokens=budget_tokens, budget_usd=budget_usd,
               context_version=project.context_version, task_id=task_id)
    db.add(d)
    await db.flush()
    await bus.publish(db, project_id=project_id, message_type="debate.started", debate_id=d.id,
                      sender_user_id=p.user_id, recipient_agent_ids=[x["agent_id"] for x in participants],
                      payload={"problem": problem[:500], "max_rounds": max_rounds})
    await audit.record(db, p.user_id, "debate.create", "debate", d.id, allowed=True)
    await db.commit()
    return d


def _over_budget(d: Debate) -> bool:
    return d.used_tokens >= d.budget_tokens or d.used_usd >= d.budget_usd


async def _turn(
    db: AsyncSession, d: Debate, agent: AgentDefinition, role: str, kind: str, rnd: int, prompt: str,
) -> DebateTurn:
    turn = DebateTurn(debate_id=d.id, round=rnd, agent_id=agent.id, role=role, kind=kind)
    remaining = max(1, d.budget_tokens - d.used_tokens)
    try:
        res = await runtime.run(
            db, d.created_by, agent, prompt, task_id=d.task_id, debate_id=d.id, shared=True,
            include_history=False, extra_params={"max_tokens": min(2048, remaining)},
        )
        turn.execution_id = res.execution_id
        d.used_tokens += res.input_tokens + res.output_tokens
        d.used_usd += res.cost_usd
        if res.status == "succeeded":
            turn.content = res.text
        else:
            turn.status, turn.error = "failed", res.error
    except (AgentUnavailable, BudgetExceeded, Conflict) as exc:
        turn.status, turn.error = "failed", f"{type(exc).__name__}: {exc}"
    db.add(turn)
    await db.flush()
    if turn.status == "failed":
        await bus.publish(db, project_id=d.project_id, message_type="agent.failed", debate_id=d.id,
                          sender_agent_id=agent.id, payload={"round": rnd, "error": turn.error})
    else:
        await bus.publish(db, project_id=d.project_id, message_type=kind if kind in bus.MESSAGE_TYPES else "proposal",
                          debate_id=d.id, sender_agent_id=agent.id,
                          recipient_agent_ids=[x["agent_id"] for x in d.participants],
                          payload={"round": rnd, "role": role, "text": turn.content[:4000]})
    await db.commit()
    return turn


async def run(db: AsyncSession, p: Principal, debate_id: str) -> Debate:
    d = await db.get(Debate, debate_id)
    if d is None:
        raise NotFound("debate not found")
    if d.created_by != p.user_id:
        await authz.require_maintainer(db, p, d.project_id)
    if d.status != DebateStatus.running:
        raise InvalidTransition(f"debate is {d.status.value}")
    if await db.scalar(select(DebateTurn.id).where(DebateTurn.debate_id == d.id).limit(1)):
        raise InvalidTransition("debate already ran")

    agents: dict[str, AgentDefinition] = {}
    for x in d.participants:
        agents[x["agent_id"]] = await agent_svc.get_agent(db, x["agent_id"])
    debaters = [x for x in d.participants if x["role"] != "synthesizer"]
    synth = next(x for x in d.participants if x["role"] == "synthesizer")
    transcript: list[DebateTurn] = []

    # Round 0: independent proposals. Each prompt contains only the problem and the agent's role.
    for x in debaters:
        if _over_budget(d):
            d.stop_reason = "budget"
            break
        role = x["role"]
        prompt = (
            f"DEBATE PROBLEM:\n{d.problem}\n\nYour role: {role}. {ROLE_BRIEF.get(role, '')}\n"
            "Work independently: you have not seen any other participant's answer. "
            "Cite verified knowledge by [id]. List assumptions and uncertainties explicitly."
        )
        transcript.append(await _turn(db, d, agents[x["agent_id"]], role, "proposal", 0, prompt))

    ok = [t for t in transcript if t.status == "ok"]
    if len(ok) < 2 and not d.stop_reason:
        d.stop_reason = "insufficient_proposals"

    # Critique / rebuttal rounds.
    rnd = 0
    while not d.stop_reason and rnd < d.max_rounds:
        rnd += 1
        any_objection = False
        for x in debaters:
            if _over_budget(d):
                d.stop_reason = "budget"
                break
            others = "\n\n".join(
                _wrap(agents[t.agent_id].name, t.role, t.content)
                for t in transcript if t.status == "ok" and t.agent_id != x["agent_id"]
            )
            mine = "\n\n".join(t.content for t in transcript
                               if t.status == "ok" and t.agent_id == x["agent_id"])
            prompt = (
                f"DEBATE PROBLEM:\n{d.problem}\n\nRound {rnd}. Your role: {x['role']}. "
                f"{ROLE_BRIEF.get(x['role'], '')}\n\nYour earlier contributions:\n{mine or '(none)'}\n\n"
                f"Other participants:\n{others}\n\n"
                "Critique the others or rebut critiques of your position, with evidence. Do not agree "
                "just to converge. Finish with a line `NEW_OBJECTIONS: yes` or `NEW_OBJECTIONS: no`."
            )
            t = await _turn(db, d, agents[x["agent_id"]], x["role"], "critique", rnd, prompt)
            transcript.append(t)
            m = OBJECTION_RE.search(t.content or "")
            if t.status == "ok" and (m is None or m.group(1).lower() == "yes"):
                any_objection = True
        if not d.stop_reason and not any_objection:
            d.stop_reason = "no_new_objections"
    if not d.stop_reason:
        d.stop_reason = "max_rounds"

    # Synthesis (runs even after a budget stop so the humans get a summary; capped small).
    synthesis: dict[str, Any] = {}
    synth_turn = None
    if len(ok) >= 1:
        body = "\n\n".join(_wrap(agents[t.agent_id].name, f"{t.role}/{t.kind}/r{t.round}", t.content)
                           for t in transcript if t.status == "ok")
        prompt = (
            f"DEBATE PROBLEM:\n{d.problem}\n\nTranscript:\n{body}\n\n"
            "You are the synthesizer. Do not pick a side by majority; weigh evidence. Reply with ONLY "
            "a JSON object with keys: agreements (list), disagreements (list of {topic, positions}), "
            "uncertainties (list), recommended_next_actions (list), recommendation (string), "
            "claims_to_verify (list)."
        )
        synth_turn = await _turn(db, d, agents[synth["agent_id"]], "synthesizer", "synthesis", rnd + 1, prompt)
        if synth_turn.status == "ok":
            synthesis = _parse_json(synth_turn.content)

    d.summary = _summary(d, agents, transcript, synthesis, synth_turn)
    d.status = DebateStatus.awaiting_approval if ok else DebateStatus.failed
    await bus.publish(db, project_id=d.project_id, message_type="debate.finished", debate_id=d.id,
                      sender_user_id=p.user_id, payload={"stop_reason": d.stop_reason,
                                                         "status": d.status.value})
    await db.commit()
    return d


def _parse_json(text: str) -> dict[str, Any]:
    m = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not m:
        return {}
    try:
        val = json.loads(m.group(0))
        return val if isinstance(val, dict) else {}
    except json.JSONDecodeError:
        return {}


def _summary(
    d: Debate, agents: dict[str, AgentDefinition], transcript: list[DebateTurn],
    synthesis: dict[str, Any], synth_turn: DebateTurn | None,
) -> dict[str, Any]:
    """Deterministic fields come from the record; analytic fields from the synthesizer, if parsed."""
    return {
        "problem": d.problem,
        "context_version": d.context_version,
        "participants": [
            {"agent_id": x["agent_id"], "name": agents[x["agent_id"]].name, "role": x["role"],
             "provider": agents[x["agent_id"]].provider, "model": agents[x["agent_id"]].model}
            for x in d.participants
        ],
        "proposals": [
            {"agent": agents[t.agent_id].name, "role": t.role, "text": t.content[:3000]}
            for t in transcript if t.kind == "proposal" and t.status == "ok"
        ],
        "failures": [
            {"agent": agents[t.agent_id].name, "round": t.round, "error": t.error}
            for t in transcript + ([synth_turn] if synth_turn else []) if t.status == "failed"
        ],
        "rounds_completed": max([t.round for t in transcript] or [0]),
        "stop_reason": d.stop_reason,
        "budget": {"tokens_used": d.used_tokens, "tokens_limit": d.budget_tokens,
                   "usd_used": round(d.used_usd, 6), "usd_limit": d.budget_usd},
        "synthesis_parsed": bool(synthesis),
        "agreements": synthesis.get("agreements", []),
        "disagreements": synthesis.get("disagreements", []),
        "uncertainties": synthesis.get("uncertainties", []),
        "claims_to_verify": synthesis.get("claims_to_verify", []),
        "recommended_next_actions": synthesis.get("recommended_next_actions", []),
        "recommendation": synthesis.get("recommendation", ""),
        "synthesis_raw": (synth_turn.content[:4000] if synth_turn and not synthesis else None),
        "note": "Agreement between agents is not evidence of correctness.",
        "final_decision": None,
    }


async def approve(db: AsyncSession, p: Principal, debate_id: str, decision: str) -> Debate:
    d = await db.get(Debate, debate_id)
    if d is None:
        raise NotFound("debate not found")
    await authz.require_maintainer(db, p, d.project_id)
    if d.status != DebateStatus.awaiting_approval:
        raise InvalidTransition(f"debate is {d.status.value}")
    rec = await knowledge._propose_internal(
        db, project_id=d.project_id, kind=KnowledgeKind.decision,
        title=f"Decision: {d.problem[:150]}", body=decision, author_user_id=p.user_id,
        source=f"debate:{d.id}", related_debate_id=d.id,
        related_task_ids=[d.task_id] if d.task_id else [],
    )
    await knowledge.verify(db, p, rec.id)
    d.status, d.approved_by, d.decision_record_id = DebateStatus.approved, p.user_id, rec.id
    d.summary = {**(d.summary or {}), "final_decision": decision, "decision_record_id": rec.id}
    await bus.publish(db, project_id=d.project_id, message_type="decision.approved", debate_id=d.id,
                      sender_user_id=p.user_id, recipient_agent_ids=[x["agent_id"] for x in d.participants],
                      payload={"record_id": rec.id, "decision": decision[:1000]})
    await audit.record(db, p.user_id, "debate.approve", "debate", d.id, allowed=True)
    await db.commit()
    return d


async def reject(db: AsyncSession, p: Principal, debate_id: str, reason: str) -> Debate:
    d = await db.get(Debate, debate_id)
    if d is None:
        raise NotFound("debate not found")
    await authz.require_maintainer(db, p, d.project_id)
    if d.status != DebateStatus.awaiting_approval:
        raise InvalidTransition(f"debate is {d.status.value}")
    d.status = DebateStatus.rejected
    d.summary = {**(d.summary or {}), "rejected_reason": reason}
    await db.commit()
    return d


async def get(db: AsyncSession, p: Principal, debate_id: str) -> dict[str, Any]:
    d = await db.get(Debate, debate_id)
    if d is None:
        raise NotFound("debate not found")
    await authz.require_member(db, p, d.project_id)
    turns = await db.scalars(select(DebateTurn).where(DebateTurn.debate_id == d.id)
                             .order_by(DebateTurn.created_at))
    return {
        "id": d.id, "status": d.status.value, "stop_reason": d.stop_reason, "summary": d.summary,
        "turns": [{"round": t.round, "agent_id": t.agent_id, "role": t.role, "kind": t.kind,
                   "status": t.status, "content": t.content, "error": t.error} for t in turns],
    }
