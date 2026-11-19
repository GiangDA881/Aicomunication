"""Durable coordination bus (ADR-0003).

Discord shows a readable projection of these events; this table is the system of record.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agenthub.config import get_settings
from agenthub.errors import Conflict, NotFound
from agenthub.models import (
    AgentDefinition,
    DeliveryStatus,
    Event,
    EventDelivery,
    Project,
    new_id,
    utcnow,
)

MESSAGE_TYPES = {
    "proposal", "critique", "rebuttal", "synthesis", "question", "answer", "handoff",
    "review_request", "task.assigned", "task.state", "decision.approved", "context.updated",
    "repo.updated", "agent.response", "agent.failed", "debate.started", "debate.finished",
}


def envelope(e: Event) -> dict[str, Any]:
    return {
        "event_id": e.event_id, "project_id": e.project_id, "task_id": e.task_id,
        "debate_id": e.debate_id, "sender_agent_id": e.sender_agent_id,
        "sender_user_id": e.sender_user_id, "recipient_agent_ids": e.recipient_agent_ids,
        "message_type": e.message_type, "context_version": str(e.context_version),
        "parent_event_id": e.parent_event_id, "hop": e.hop, "payload": e.payload,
        "timestamp": e.timestamp.isoformat(),
    }


async def publish(
    db: AsyncSession, *, project_id: str, message_type: str, payload: dict[str, Any],
    sender_agent_id: str | None = None, sender_user_id: str | None = None,
    recipient_agent_ids: list[str] | None = None, task_id: str | None = None,
    debate_id: str | None = None, parent_event_id: str | None = None,
    idempotency_key: str | None = None, ttl_seconds: int = 3600,
) -> Event:
    if message_type not in MESSAGE_TYPES:
        raise ValueError(f"unknown message_type {message_type!r}")
    key = idempotency_key or new_id()
    existing = await db.scalar(select(Event).where(Event.idempotency_key == key))
    if existing is not None:
        return existing  # dedup: same logical event published twice
    hop = 0
    if parent_event_id:
        parent = await db.get(Event, parent_event_id)
        if parent is None:
            raise NotFound("parent event not found")
        hop = parent.hop + 1
        if hop > get_settings().max_event_hops:
            raise Conflict(f"hop limit {get_settings().max_event_hops} reached; refusing to relay")
    recipients = list(dict.fromkeys(recipient_agent_ids or []))
    if recipients:
        rows = await db.scalars(select(AgentDefinition).where(AgentDefinition.id.in_(recipients)))
        found = {a.id: a for a in rows}
        bad = [r for r in recipients if r not in found or found[r].project_id != project_id]
        if bad:
            raise NotFound(f"recipients not in project: {bad}")
        if sender_agent_id in recipients:
            recipients.remove(sender_agent_id)  # no self-delivery (loop guard)
    project = await db.get(Project, project_id)
    assert project is not None
    e = Event(
        idempotency_key=key, project_id=project_id, task_id=task_id, debate_id=debate_id,
        sender_agent_id=sender_agent_id, sender_user_id=sender_user_id,
        recipient_agent_ids=recipients, message_type=message_type,
        context_version=project.context_version, parent_event_id=parent_event_id, hop=hop,
        payload=payload,
    )
    db.add(e)
    await db.flush()
    deadline = utcnow() + timedelta(seconds=ttl_seconds)
    for rid in recipients:
        db.add(EventDelivery(event_id=e.event_id, recipient_agent_id=rid, deadline=deadline))
    await db.flush()
    return e


async def pending_for(db: AsyncSession, agent_id: str, limit: int = 50) -> list[Event]:
    q = (
        select(Event)
        .join(EventDelivery, EventDelivery.event_id == Event.event_id)
        .where(EventDelivery.recipient_agent_id == agent_id,
               EventDelivery.status == DeliveryStatus.pending)
        .order_by(Event.timestamp)
        .limit(limit)
    )
    if db.bind.dialect.name == "postgresql":
        q = q.with_for_update(skip_locked=True, of=EventDelivery)
    return list(await db.scalars(q))


async def _delivery(db: AsyncSession, event_id: str, agent_id: str) -> EventDelivery:
    d = await db.scalar(select(EventDelivery).where(
        EventDelivery.event_id == event_id, EventDelivery.recipient_agent_id == agent_id))
    if d is None:
        raise NotFound("delivery not found")
    return d


async def ack(db: AsyncSession, event_id: str, agent_id: str) -> None:
    d = await _delivery(db, event_id, agent_id)
    if d.status == DeliveryStatus.pending:
        d.status, d.acked_at = DeliveryStatus.acked, utcnow()
        d.attempts += 1
    await db.flush()


async def fail(db: AsyncSession, event_id: str, agent_id: str, error: str, max_attempts: int = 3) -> None:
    d = await _delivery(db, event_id, agent_id)
    d.attempts += 1
    d.last_error = error[:500]
    if d.attempts >= max_attempts:
        d.status = DeliveryStatus.failed
    await db.flush()


async def expire_overdue(db: AsyncSession) -> int:
    now = utcnow()
    rows = list(await db.scalars(select(EventDelivery).where(
        EventDelivery.status == DeliveryStatus.pending, EventDelivery.deadline < now)))
    for d in rows:
        d.status = DeliveryStatus.expired
    await db.flush()
    return len(rows)


async def unprojected(db: AsyncSession, project_id: str | None = None, limit: int = 20) -> list[Event]:
    q = select(Event).where(Event.projected_to_discord.is_(False))
    if project_id:
        q = q.where(Event.project_id == project_id)
    return list(await db.scalars(q.order_by(Event.timestamp).limit(limit)))


async def mark_projected(db: AsyncSession, event_ids: list[str]) -> None:
    for eid in event_ids:
        e = await db.get(Event, eid)
        if e:
            e.projected_to_discord = True
    await db.flush()


async def thread(db: AsyncSession, project_id: str, task_id: str | None = None,
                 debate_id: str | None = None) -> list[Event]:
    q = select(Event).where(Event.project_id == project_id)
    if task_id:
        q = q.where(Event.task_id == task_id)
    if debate_id:
        q = q.where(Event.debate_id == debate_id)
    return list(await db.scalars(q.order_by(Event.timestamp)))
