"""Bus, GitHub webhook, REST auth, redaction, Discord handlers."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from agenthub.errors import Conflict
from agenthub.models import DeliveryStatus, EventDelivery, TaskEvidence, utcnow
from agenthub.security import redact
from agenthub.services import agents, bus, identity, tasks


async def _agent(world, owner, name):
    a = await agents.create_agent(world.db, world.p(owner), world.project.id, name, "mock", "mock-1")
    await world.db.commit()
    return a


async def test_bus_dedup_hops_ack_expiry(world, monkeypatch):
    db = world.db
    a, b = await _agent(world, "A", "a"), await _agent(world, "B", "b")
    e1 = await bus.publish(db, project_id=world.project.id, message_type="question", sender_agent_id=a.id,
                           recipient_agent_ids=[b.id, a.id], payload={"q": "?"}, idempotency_key="k1")
    e1b = await bus.publish(db, project_id=world.project.id, message_type="question", sender_agent_id=a.id,
                            recipient_agent_ids=[b.id], payload={"q": "?"}, idempotency_key="k1")
    assert e1.event_id == e1b.event_id  # deduplicated
    assert e1.recipient_agent_ids == [b.id]  # sender removed from recipients
    assert [e.event_id for e in await bus.pending_for(db, b.id)] == [e1.event_id]
    await bus.ack(db, e1.event_id, b.id)
    assert await bus.pending_for(db, b.id) == []

    # Hop limit stops ping-pong between agents.
    parent = e1
    from agenthub.config import get_settings
    for i in range(get_settings().max_event_hops):
        sender, rcpt = (b, a) if i % 2 == 0 else (a, b)
        parent = await bus.publish(db, project_id=world.project.id, message_type="answer",
                                   sender_agent_id=sender.id, recipient_agent_ids=[rcpt.id],
                                   payload={}, parent_event_id=parent.event_id)
    with pytest.raises(Conflict, match="hop limit"):
        await bus.publish(db, project_id=world.project.id, message_type="answer", sender_agent_id=a.id,
                          recipient_agent_ids=[b.id], payload={}, parent_event_id=parent.event_id)

    # Timeouts: overdue deliveries expire; repeated failures are marked failed.
    e2 = await bus.publish(db, project_id=world.project.id, message_type="question",
                           sender_agent_id=a.id, recipient_agent_ids=[b.id], payload={}, ttl_seconds=1)
    d = await db.scalar(select(EventDelivery).where(EventDelivery.event_id == e2.event_id))
    d.deadline = utcnow() - timedelta(seconds=5)
    assert await bus.expire_overdue(db) >= 1
    assert d.status == DeliveryStatus.expired
    e3 = await bus.publish(db, project_id=world.project.id, message_type="question",
                           sender_agent_id=a.id, recipient_agent_ids=[b.id], payload={})
    for _ in range(3):
        await bus.fail(db, e3.event_id, b.id, "agent offline")
    d3 = await db.scalar(select(EventDelivery).where(EventDelivery.event_id == e3.event_id))
    assert d3.status == DeliveryStatus.failed and d3.attempts == 3


def test_redaction():
    text = "key sk-ant-api03-abcdefghijklmnop and ghp_abcdefghijklmnopqrstuvwxyz123 api_key=supersecret1"
    out = redact(text, ["supersecret1"])
    assert "sk-ant" not in out and "ghp_" not in out and "supersecret1" not in out


@pytest.fixture
async def client(maker):
    from agenthub.api.app import app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c


async def test_api_requires_auth_and_enforces_ownership(world, client):
    db = world.db
    key_a = await identity.issue_api_key(db, world.users["A"])
    key_b = await identity.issue_api_key(db, world.users["B"])
    await db.commit()
    assert (await client.get(f"/projects/{world.project.id}/agents")).status_code == 401
    assert (await client.get(f"/projects/{world.project.id}/agents",
                             headers={"Authorization": "Bearer nope"})).status_code == 401
    ha, hb = {"Authorization": f"Bearer {key_a}"}, {"Authorization": f"Bearer {key_b}"}
    r = await client.post("/agents", headers=hb, json={"project_id": world.project.id, "name": "bee",
                                                       "provider": "mock", "model": "mock-1"})
    assert r.status_code == 200, r.text
    aid = r.json()["id"]
    await client.put(f"/agents/{aid}/credential", headers=hb, json={"api_key": "sk-bee-secret-000000000000"})
    assert (await client.post(f"/agents/{aid}/ask", headers=ha, json={"prompt": "hi"})).status_code == 403
    assert (await client.patch(f"/agents/{aid}", headers=ha, json={"model": "x"})).status_code == 403
    listed = (await client.get(f"/projects/{world.project.id}/agents", headers=ha)).json()
    assert "system_instructions" not in listed[0] and "has_credential" not in listed[0]
    r = await client.post(f"/agents/{aid}/ask", headers=hb, json={"prompt": "hi"})
    assert r.status_code == 200 and "sk-bee" not in r.text


async def test_github_webhook_links_and_verified_evidence(world, client):
    db = world.db
    world.project.github_repo = "team/app"
    world.project.repo_head_sha = "0" * 40  # head known when the task is assigned
    await db.commit()
    a = await _agent(world, "B", "gh-agent")
    t = await tasks.create_task(db, world.p("B"), world.project.id, "Feature", acceptance_criteria=["ok"])
    await tasks.assign(db, world.p("B"), t.id, [a.id])
    await db.commit()

    async def send(event, payload, sig=None):
        body = json.dumps(payload).encode()
        good = "sha256=" + hmac.new(b"test-webhook-secret", body, hashlib.sha256).hexdigest()
        return await client.post("/webhooks/github", content=body, headers={
            "X-GitHub-Event": event, "X-GitHub-Delivery": f"d-{event}-{len(body)}",
            "X-Hub-Signature-256": sig or good, "Content-Type": "application/json"})

    repo = {"full_name": "team/app"}
    assert (await send("push", {"repository": repo}, sig="sha256=bad")).status_code == 401
    r = await send("push", {"repository": repo, "ref": "refs/heads/main", "after": "abc123" * 6,
                            "commits": [{"id": "c1", "message": f"AH-{t.short_id}: start", "url": "u"}]})
    assert r.json()["head"].startswith("abc123")
    pr = {"number": 7, "title": f"AH-{t.short_id} feature", "body": "", "merged": True, "state": "closed",
          "html_url": "https://github.com/team/app/pull/7", "head": {"ref": "x", "sha": "h1"},
          "base": {"ref": "main"}}
    await send("pull_request", {"repository": repo, "action": "opened", "pull_request": {**pr, "merged": False}})
    await send("check_run", {"repository": repo, "action": "completed", "check_run": {
        "head_sha": "h1", "name": "tests", "conclusion": "success", "html_url": "ci"}})
    await send("pull_request", {"repository": repo, "action": "closed", "pull_request": pr})
    ev = list(await db.scalars(select(TaskEvidence).where(TaskEvidence.task_id == t.id)))
    assert {(e.kind, e.passed, e.verified) for e in ev} == {("test_result", True, True), ("pr", True, True)}

    # The task was assigned before the push, so it is now flagged as based on an old revision.
    await db.refresh(world.project)
    stale = await tasks.stale_tasks(db, world.project.id)
    assert [s.id for s in stale] == [t.id]


async def test_discord_handlers_flow(world):
    from agenthub.discord_bot import handlers as h
    db = world.db
    ua, ub = world.users["A"], world.users["B"]
    ca = h.Caller(ua.discord_id, "A", "chan-1")
    cb = h.Caller(ub.discord_id, "B", "chan-1")
    world.project.discord_channel_id = "chan-1"
    out = await h.safe(h.agent_create(db, cb, "helper", "mock", "mock-1", "research", "", None))
    assert "created" in out
    out = await h.safe(h.agent_key(db, cb, "helper", "sk-discord-secret-0000000000"))
    assert "sk-discord" not in out and "fingerprint" in out
    out = await h.safe(h.agent_ask(db, ca, f"{ub.discord_id}/helper", "hi", None))
    assert out.startswith("⛔ Forbidden")
    out = await h.safe(h.task_create(db, cb, "Write docs", "", "has README; has examples", None))
    assert "AH-" in out
    key = out.split("**")[1].split()[0]
    assert "assigned" in await h.safe(h.task_assign(db, cb, key, "helper"))
    status = await h.safe(h.task_status(db, ca, key))
    assert "`assigned`" in status and "backlog → assigned" in status
    out = await h.safe(h.kn_verify(db, cb, "nonexistent"))
    assert out.startswith("⛔")
