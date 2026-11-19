"""Task lifecycle, handoffs and evidence-gated approval.

A task is never approved because an agent says it is done. Approval needs:
- state `review` (reached through a structured handoff),
- at least one *verified* passing evidence item (from GitHub/CI webhooks or a human reviewer),
- no verified failing evidence,
- the reviewer confirming each acceptance criterion,
- a maintainer as reviewer,
- no unresolved context drift, unless the reviewer explicitly overrides with a note.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agenthub import authz
from agenthub.authz import Principal
from agenthub.errors import Forbidden, InvalidTransition, NotFound
from agenthub.models import (
    Counter,
    Project,
    ProjectRole,
    Task,
    TaskEvent,
    TaskEvidence,
    TaskState,
)
from agenthub.services import agents as agent_svc
from agenthub.services import audit, bus
from agenthub.services import context as ctx_svc

S = TaskState
TRANSITIONS: dict[TaskState, set[TaskState]] = {
    S.backlog: {S.assigned, S.cancelled},
    S.assigned: {S.in_progress, S.blocked, S.cancelled, S.backlog},
    S.in_progress: {S.review, S.blocked, S.failed, S.cancelled},
    S.review: {S.approved, S.in_progress, S.failed, S.cancelled},
    S.approved: {S.completed, S.in_progress},
    S.blocked: {S.assigned, S.in_progress, S.cancelled},
    S.failed: {S.assigned, S.cancelled},
    S.completed: set(),
    S.cancelled: set(),
}


def view(t: Task, evidence: list[TaskEvidence] | None = None) -> dict[str, Any]:
    out = {
        "id": t.id, "key": f"AH-{t.short_id}", "short_id": t.short_id, "project_id": t.project_id,
        "title": t.title, "objectives": t.objectives, "acceptance_criteria": t.acceptance_criteria,
        "human_owner_id": t.human_owner_id, "assigned_agent_ids": t.assigned_agent_ids,
        "depends_on": t.depends_on, "state": t.state.value, "context_version": t.context_version,
        "repo_sha": t.repo_sha, "deliverables": t.deliverables, "open_questions": t.open_questions,
        "handoff_report": t.handoff_report,
    }
    if evidence is not None:
        out["evidence"] = [
            {"kind": e.kind, "reference": e.reference, "passed": e.passed, "verified": e.verified,
             "by_agent": e.submitted_by_agent_id, "by_user": e.submitted_by_user_id}
            for e in evidence
        ]
    return out


async def _next_short_id(db: AsyncSession) -> int:
    c = await db.get(Counter, "task", with_for_update=True)
    if c is None:
        c = Counter(name="task", value=0)
        db.add(c)
    c.value += 1
    await db.flush()
    return c.value


async def get_task(db: AsyncSession, ref: str | int) -> Task:
    t = None
    if isinstance(ref, int) or (isinstance(ref, str) and ref.upper().removeprefix("AH-").isdigit()):
        n = ref if isinstance(ref, int) else int(ref.upper().removeprefix("AH-"))
        t = await db.scalar(select(Task).where(Task.short_id == n))
    else:
        t = await db.get(Task, ref)
    if t is None:
        raise NotFound(f"task {ref} not found")
    return t


async def evidence_of(db: AsyncSession, task_id: str) -> list[TaskEvidence]:
    return list(await db.scalars(select(TaskEvidence).where(TaskEvidence.task_id == task_id)
                                 .order_by(TaskEvidence.created_at)))


async def _transition(
    db: AsyncSession, t: Task, to: TaskState, *, user_id: str | None = None,
    agent_id: str | None = None, note: str = "",
) -> None:
    if to not in TRANSITIONS[t.state]:
        raise InvalidTransition(f"AH-{t.short_id}: {t.state.value} -> {to.value} is not allowed")
    db.add(TaskEvent(task_id=t.id, actor_user_id=user_id, actor_agent_id=agent_id,
                     from_state=t.state.value, to_state=to.value, note=note[:2000]))
    frm = t.state
    t.state = to
    await db.flush()
    await bus.publish(db, project_id=t.project_id, message_type="task.state", task_id=t.id,
                      sender_user_id=user_id, sender_agent_id=agent_id,
                      recipient_agent_ids=list(t.assigned_agent_ids or []),
                      payload={"key": f"AH-{t.short_id}", "title": t.title, "from": frm.value,
                               "to": to.value, "note": note[:300]})


async def _require_owner_or_maintainer(db: AsyncSession, p: Principal, t: Task, action: str) -> None:
    if t.human_owner_id == p.user_id:
        return
    if await authz.project_role(db, p, t.project_id) == ProjectRole.maintainer:
        return
    await authz._deny(db, p, action, "task", t.id)


async def create_task(
    db: AsyncSession, p: Principal, project_id: str, title: str, objectives: str = "",
    acceptance_criteria: list[str] | None = None, depends_on: list[str] | None = None,
) -> Task:
    await authz.require_member(db, p, project_id)
    deps = []
    for d in depends_on or []:
        dep = await get_task(db, d)
        if dep.project_id != project_id:
            raise NotFound(f"dependency {d} not in project")
        deps.append(dep.id)
    t = Task(short_id=await _next_short_id(db), project_id=project_id, title=title[:200],
             objectives=objectives, acceptance_criteria=acceptance_criteria or [],
             human_owner_id=p.user_id, depends_on=deps, state=TaskState.backlog)
    db.add(t)
    await db.flush()
    db.add(TaskEvent(task_id=t.id, actor_user_id=p.user_id, to_state="backlog", note="created"))
    await audit.record(db, p.user_id, "task.create", "task", t.id, allowed=True)
    return t


async def assign(db: AsyncSession, p: Principal, task_ref: str, agent_ids: list[str]) -> Task:
    t = await get_task(db, task_ref)
    await _require_owner_or_maintainer(db, p, t, "task.assign")
    for aid in agent_ids:
        agent = await agent_svc.get_agent(db, aid)
        if agent.project_id != t.project_id:
            raise NotFound(f"agent {aid} not in project")
        await authz.require_collaborate(db, p, agent)
    project = await db.get(Project, t.project_id)
    assert project is not None
    t.assigned_agent_ids = list(dict.fromkeys(agent_ids))
    t.context_version = project.context_version
    t.repo_sha = project.repo_head_sha
    await _transition(db, t, TaskState.assigned, user_id=p.user_id,
                      note=f"assigned to {len(agent_ids)} agent(s)")
    await bus.publish(db, project_id=t.project_id, message_type="task.assigned", task_id=t.id,
                      sender_user_id=p.user_id, recipient_agent_ids=t.assigned_agent_ids,
                      payload={"key": f"AH-{t.short_id}", "title": t.title})
    return t


async def start(db: AsyncSession, p: Principal, task_ref: str) -> Task:
    t = await get_task(db, task_ref)
    await _require_owner_or_maintainer(db, p, t, "task.start")
    for dep_id in t.depends_on or []:
        dep = await db.get(Task, dep_id)
        if dep is None or dep.state != TaskState.completed:
            await _transition(db, t, TaskState.blocked, user_id=p.user_id,
                              note=f"waiting on dependency AH-{dep.short_id if dep else '?'}")
            return t
    await _transition(db, t, TaskState.in_progress, user_id=p.user_id)
    return t


async def submit_handoff(
    db: AsyncSession, p: Principal, task_ref: str, agent_id: str, report: dict[str, Any]
) -> dict[str, Any]:
    """Structured handoff from an assigned agent. Submitted by a human who may run that agent.

    report keys: summary (str), changes (list[str]), evidence (list[{kind, reference, passed}]),
    open_questions (list[str]), context_version (int), referenced_record_ids (list[str]),
    next_steps (list[str])
    """
    t = await get_task(db, task_ref)
    agent = await agent_svc.get_agent(db, agent_id)
    await authz.require_execute(db, p, agent)
    if agent_id not in (t.assigned_agent_ids or []):
        raise Forbidden("agent is not assigned to this task")
    if not str(report.get("summary", "")).strip():
        raise ValueError("handoff needs a summary")
    drift = await ctx_svc.check_drift(db, t.project_id, report.get("context_version"),
                                      report.get("referenced_record_ids") or [])
    for ev in report.get("evidence") or []:
        db.add(TaskEvidence(task_id=t.id, kind=str(ev.get("kind", "document"))[:32],
                            reference=str(ev.get("reference", ""))[:2000], passed=ev.get("passed"),
                            verified=False, submitted_by_agent_id=agent_id))
    t.handoff_report = {
        "agent_id": agent_id, "summary": report["summary"], "changes": report.get("changes", []),
        "open_questions": report.get("open_questions", []), "next_steps": report.get("next_steps", []),
        "context_version": report.get("context_version"), "drift": drift.to_dict(),
    }
    t.open_questions = list(report.get("open_questions") or [])
    t.deliverables = list(report.get("changes") or [])
    await _transition(db, t, TaskState.review, user_id=p.user_id, agent_id=agent_id,
                      note="handoff submitted" + (" (CONTEXT DRIFT DETECTED)" if drift.stale else ""))
    await bus.publish(db, project_id=t.project_id, message_type="handoff", task_id=t.id,
                      sender_agent_id=agent_id, payload={"key": f"AH-{t.short_id}", **t.handoff_report})
    return {"task": view(t), "drift": drift.to_dict()}


async def add_review_evidence(
    db: AsyncSession, p: Principal, task_ref: str, kind: str, reference: str, passed: bool
) -> TaskEvidence:
    """A human maintainer records evidence they checked themselves (verified)."""
    t = await get_task(db, task_ref)
    await authz.require_maintainer(db, p, t.project_id)
    ev = TaskEvidence(task_id=t.id, kind=kind[:32], reference=reference[:2000], passed=passed,
                      verified=True, submitted_by_user_id=p.user_id)
    db.add(ev)
    await db.flush()
    return ev


async def add_system_evidence(
    db: AsyncSession, task_id: str, kind: str, reference: str, passed: bool | None
) -> TaskEvidence:
    """Evidence from a trusted integration (GitHub webhook with a valid signature)."""
    ev = TaskEvidence(task_id=task_id, kind=kind[:32], reference=reference[:2000], passed=passed,
                      verified=True)
    db.add(ev)
    await db.flush()
    return ev


async def approve(
    db: AsyncSession, p: Principal, task_ref: str, criteria_met: list[bool],
    note: str = "", override_drift: bool = False,
) -> Task:
    t = await get_task(db, task_ref)
    await authz.require_maintainer(db, p, t.project_id)
    if t.state != TaskState.review:
        raise InvalidTransition(f"AH-{t.short_id} is {t.state.value}, not review")
    problems = []
    evidence = await evidence_of(db, t.id)
    verified = [e for e in evidence if e.verified]
    if not any(e.passed for e in verified):
        problems.append("no verified passing evidence (agent claims alone are not enough)")
    if any(e.passed is False for e in verified):
        problems.append("verified evidence includes a failure")
    crit = t.acceptance_criteria or []
    if len(criteria_met) != len(crit) or not all(criteria_met):
        problems.append(f"reviewer must confirm all {len(crit)} acceptance criteria")
    drift = (t.handoff_report or {}).get("drift") or {}
    if drift.get("stale") and not (override_drift and note.strip()):
        problems.append("handoff was based on outdated context; re-run or override with a note")
    if problems:
        await audit.record(db, p.user_id, "task.approve", "task", t.id, allowed=False, problems=problems)
        raise InvalidTransition("; ".join(problems))
    await _transition(db, t, TaskState.approved, user_id=p.user_id, note=note or "approved")
    await audit.record(db, p.user_id, "task.approve", "task", t.id, allowed=True)
    return t


async def request_changes(db: AsyncSession, p: Principal, task_ref: str, note: str) -> Task:
    t = await get_task(db, task_ref)
    await authz.require_maintainer(db, p, t.project_id)
    await _transition(db, t, TaskState.in_progress, user_id=p.user_id, note=f"changes requested: {note}")
    return t


async def complete(db: AsyncSession, p: Principal, task_ref: str) -> Task:
    t = await get_task(db, task_ref)
    await authz.require_maintainer(db, p, t.project_id)
    await _transition(db, t, TaskState.completed, user_id=p.user_id)
    return t


async def set_state(db: AsyncSession, p: Principal, task_ref: str, state: str, note: str = "") -> Task:
    """blocked / failed / cancelled by the task owner or a maintainer."""
    to = TaskState(state)
    if to not in {TaskState.blocked, TaskState.failed, TaskState.cancelled, TaskState.backlog}:
        raise InvalidTransition("use the dedicated command for this state")
    t = await get_task(db, task_ref)
    await _require_owner_or_maintainer(db, p, t, f"task.{state}")
    await _transition(db, t, to, user_id=p.user_id, note=note)
    return t


async def list_tasks(db: AsyncSession, p: Principal, project_id: str, state: str | None = None) -> list[Task]:
    await authz.require_member(db, p, project_id)
    q = select(Task).where(Task.project_id == project_id)
    if state:
        q = q.where(Task.state == TaskState(state))
    return list(await db.scalars(q.order_by(Task.short_id)))


async def history(db: AsyncSession, p: Principal, task_ref: str) -> list[dict[str, Any]]:
    t = await get_task(db, task_ref)
    await authz.require_member(db, p, t.project_id)
    rows = await db.scalars(select(TaskEvent).where(TaskEvent.task_id == t.id).order_by(TaskEvent.created_at))
    return [{"from": r.from_state, "to": r.to_state, "by_user": r.actor_user_id,
             "by_agent": r.actor_agent_id, "note": r.note, "at": r.created_at.isoformat()} for r in rows]


async def stale_tasks(db: AsyncSession, project_id: str) -> list[Task]:
    """Active tasks whose recorded context or repo revision is behind the project."""
    project = await db.get(Project, project_id)
    assert project is not None
    active = [TaskState.assigned, TaskState.in_progress, TaskState.review]
    rows = await db.scalars(select(Task).where(Task.project_id == project_id, Task.state.in_(active)))
    return [t for t in rows
            if (t.context_version and t.context_version < project.context_version)
            or (t.repo_sha and project.repo_head_sha and t.repo_sha != project.repo_head_sha)]
