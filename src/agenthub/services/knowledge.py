"""Shared knowledge: proposed and verified records, revisions, and the project context version.

Rules (ADR-0004):
- Anyone in the project (or an agent during an execution) may *propose*.
- Only a human maintainer may verify, reject, supersede or deprecate.
- Every change to the verified set increments `projects.context_version`.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agenthub import authz
from agenthub.authz import Principal
from agenthub.errors import InvalidTransition, NotFound
from agenthub.models import KnowledgeKind, KnowledgeRecord, KnowledgeStatus, Project, new_id
from agenthub.services import audit


def view(r: KnowledgeRecord) -> dict[str, Any]:
    return {
        "id": r.id, "lineage_id": r.lineage_id, "revision": r.revision, "kind": r.kind.value,
        "title": r.title, "body": r.body, "tags": r.tags, "status": r.status.value,
        "namespace": r.namespace, "source": r.source, "evidence": r.evidence,
        "author_user_id": r.author_user_id, "author_agent_id": r.author_agent_id,
        "supersedes_id": r.supersedes_id, "superseded_by_id": r.superseded_by_id,
        "verified_by": r.verified_by, "verified_in_version": r.verified_in_version,
        "related_task_ids": r.related_task_ids, "related_debate_id": r.related_debate_id,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


async def _get(db: AsyncSession, record_id: str) -> KnowledgeRecord:
    r = await db.get(KnowledgeRecord, record_id)
    if r is None:
        raise NotFound("knowledge record not found")
    return r


async def _bump_version(db: AsyncSession, project_id: str) -> int:
    project = await db.get(Project, project_id, with_for_update=True)
    assert project is not None
    project.context_version += 1
    await db.flush()
    return project.context_version


async def _propose_internal(
    db: AsyncSession, *, project_id: str, kind: KnowledgeKind | str, title: str, body: str,
    author_user_id: str | None = None, author_agent_id: str | None = None,
    tags: list[str] | None = None, source: str = "", evidence: list[str] | None = None,
    related_task_ids: list[str] | None = None, related_debate_id: str | None = None,
    supersedes_id: str | None = None, namespace: str = "default",
) -> KnowledgeRecord:
    lineage, revision = new_id(), 1
    if supersedes_id:
        old = await _get(db, supersedes_id)
        if old.project_id != project_id:
            raise NotFound("knowledge record not found")
        lineage, revision = old.lineage_id, old.revision + 1
    r = KnowledgeRecord(
        project_id=project_id, kind=KnowledgeKind(kind), title=title[:200], body=body,
        tags=tags or [], source=source, evidence=evidence or [], namespace=namespace,
        related_task_ids=related_task_ids or [], related_debate_id=related_debate_id,
        author_user_id=author_user_id, author_agent_id=author_agent_id,
        lineage_id=lineage, revision=revision, supersedes_id=supersedes_id,
        status=KnowledgeStatus.proposed,
    )
    db.add(r)
    await db.flush()
    return r


async def propose(db: AsyncSession, p: Principal, project_id: str, **kw: Any) -> KnowledgeRecord:
    await authz.require_member(db, p, project_id)
    r = await _propose_internal(db, project_id=project_id, author_user_id=p.user_id, **kw)
    await audit.record(db, p.user_id, "knowledge.propose", "knowledge", r.id, allowed=True)
    return r


async def verify(db: AsyncSession, p: Principal, record_id: str) -> KnowledgeRecord:
    r = await _get(db, record_id)
    await authz.require_maintainer(db, p, r.project_id)
    if r.status != KnowledgeStatus.proposed:
        raise InvalidTransition(f"only proposed records can be verified (is {r.status.value})")
    if r.supersedes_id:
        old = await _get(db, r.supersedes_id)
        if old.status == KnowledgeStatus.verified:
            old.status = KnowledgeStatus.superseded
            old.superseded_by_id = r.id
    r.status = KnowledgeStatus.verified
    r.verified_by = p.user_id
    r.verified_in_version = await _bump_version(db, r.project_id)
    await audit.record(db, p.user_id, "knowledge.verify", "knowledge", r.id, allowed=True,
                       context_version=r.verified_in_version)
    return r


async def reject(db: AsyncSession, p: Principal, record_id: str, reason: str = "") -> KnowledgeRecord:
    r = await _get(db, record_id)
    await authz.require_maintainer(db, p, r.project_id)
    if r.status != KnowledgeStatus.proposed:
        raise InvalidTransition("only proposed records can be rejected")
    r.status = KnowledgeStatus.rejected
    await audit.record(db, p.user_id, "knowledge.reject", "knowledge", r.id, allowed=True, reason=reason)
    return r


async def deprecate(db: AsyncSession, p: Principal, record_id: str, reason: str = "") -> KnowledgeRecord:
    r = await _get(db, record_id)
    await authz.require_maintainer(db, p, r.project_id)
    if r.status != KnowledgeStatus.verified:
        raise InvalidTransition("only verified records can be deprecated")
    r.status = KnowledgeStatus.deprecated
    await _bump_version(db, r.project_id)
    await audit.record(db, p.user_id, "knowledge.deprecate", "knowledge", r.id, allowed=True, reason=reason)
    return r


async def list_records(
    db: AsyncSession, p: Principal, project_id: str, status: str | None = None,
    kind: str | None = None, tag: str | None = None, limit: int = 100,
) -> list[KnowledgeRecord]:
    await authz.require_member(db, p, project_id)
    return await _list_internal(db, project_id, status=status, kind=kind, tag=tag, limit=limit)


async def _list_internal(
    db: AsyncSession, project_id: str, status: str | None = None, kind: str | None = None,
    tag: str | None = None, limit: int = 100,
) -> list[KnowledgeRecord]:
    q = select(KnowledgeRecord).where(KnowledgeRecord.project_id == project_id)
    if status:
        q = q.where(KnowledgeRecord.status == KnowledgeStatus(status))
    if kind:
        q = q.where(KnowledgeRecord.kind == KnowledgeKind(kind))
    rows = list(await db.scalars(q.order_by(KnowledgeRecord.created_at.desc()).limit(limit * 4)))
    if tag:
        rows = [r for r in rows if tag in (r.tags or [])]  # JSON containment differs by backend
    return rows[:limit]


async def get_record(db: AsyncSession, p: Principal, record_id: str) -> KnowledgeRecord:
    r = await _get(db, record_id)
    await authz.require_member(db, p, r.project_id)
    return r


async def current_of(db: AsyncSession, record: KnowledgeRecord) -> KnowledgeRecord | None:
    """Follow superseded_by links to the current verified revision of a lineage."""
    seen = set()
    cur = record
    while cur.superseded_by_id and cur.id not in seen:
        seen.add(cur.id)
        nxt = await db.get(KnowledgeRecord, cur.superseded_by_id)
        if nxt is None:
            break
        cur = nxt
    return cur if cur.status == KnowledgeStatus.verified else None
