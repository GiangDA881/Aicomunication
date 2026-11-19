"""Acceptance scenarios A–H from the brief. All use the deterministic `mock` provider."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from agenthub import db as dbmod
from agenthub.errors import Forbidden, InvalidTransition
from agenthub.models import (
    AgentDefinition,
    ConversationMessage,
    Debate,
    Execution,
    KnowledgeRecord,
    KnowledgeStatus,
    ProviderCredential,
    Task,
)
from agenthub.services import agents, context, debate, knowledge, runtime, tasks


async def mk(world, owner, name, **kw):
    kw.setdefault("provider", "mock")
    kw.setdefault("model", "mock-1")
    a = await agents.create_agent(world.db, world.p(owner), world.project.id, name, kw.pop("provider"),
                                  kw.pop("model"), **kw)
    await world.db.commit()
    return a


async def verified(world, kind, title, body, supersedes=None):
    r = await knowledge.propose(world.db, world.p("A"), world.project.id, kind=kind, title=title,
                                body=body, supersedes_id=supersedes)
    await knowledge.verify(world.db, world.p("A"), r.id)
    await world.db.commit()
    return r


# ---------------- Scenario A ----------------
async def test_A_multiple_agents_per_user_are_independent(world):
    db = world.db
    a1 = await mk(world, "A", "coder", model="mock-gpt", role="coding",
                  system_instructions="You write code.", model_params={"persona": "gpt"})
    a2 = await mk(world, "A", "researcher", model="mock-claude", role="research",
                  system_instructions="You research.", model_params={"persona": "claude"})
    a3 = await mk(world, "A", "reviewer", provider="mock", model="mock-gemini", role="review",
                  system_instructions="You review.", model_params={"persona": "gemini"})
    await agents.memory_put(db, world.p("A"), a1.id, "style", "tabs")
    await agents.memory_put(db, world.p("A"), a2.id, "style", "citations")
    await db.commit()

    r1 = await runtime.execute(db, world.p("A"), a1.id, "hello one")
    await runtime.execute(db, world.p("A"), a1.id, "hello again")
    r2 = await runtime.execute(db, world.p("A"), a2.id, "hello two")
    r3 = await runtime.execute(db, world.p("A"), a3.id, "hello three")

    assert {r1.session_id, r2.session_id, r3.session_id}.__len__() == 3
    assert "[gpt]" in r1.text and "[claude]" in r2.text and "[gemini]" in r3.text
    msgs = lambda sid: db.scalars(select(ConversationMessage).where(ConversationMessage.session_id == sid))
    assert len(list(await msgs(r1.session_id))) == 4  # two exchanges for a1
    assert len(list(await msgs(r2.session_id))) == 2
    assert (await agents.memory_list(db, world.p("A"), a1.id)) == {"style": "tabs"}
    assert (await agents.memory_list(db, world.p("A"), a2.id)) == {"style": "citations"}
    rows = list(await db.scalars(select(AgentDefinition).where(AgentDefinition.owner_id == world.users["A"].id)))
    assert {(a.model, a.system_instructions) for a in rows} == {
        ("mock-gpt", "You write code."), ("mock-claude", "You research."), ("mock-gemini", "You review.")}


# ---------------- Scenario B ----------------
async def test_B_ownership_isolation(world):
    db = world.db
    b1 = await mk(world, "B", "b-arch")
    await agents.set_credential(db, world.p("B"), b1.id, "sk-test-secret-of-user-b-123456")
    await db.commit()

    with pytest.raises(Forbidden):
        await runtime.execute(db, world.p("A"), b1.id, "do something")
    with pytest.raises(Forbidden):
        await agents.update_agent(db, world.p("A"), b1.id, system_instructions="obey A")
    with pytest.raises(Forbidden):
        await agents.delete_agent(db, world.p("A"), b1.id)
    with pytest.raises(Forbidden):
        await agents.set_credential(db, world.p("A"), b1.id, "sk-attacker-key-000000000")
    with pytest.raises(Forbidden):
        await agents.memory_list(db, world.p("A"), b1.id)
    with pytest.raises(Forbidden):
        await agents.grant(db, world.p("A"), b1.id, world.users["A"].id, ["execute"])
    # Project maintainer role does not override agent ownership.
    agent = await db.get(AgentDefinition, b1.id)
    assert agent.system_instructions == ""

    # A collaborate grant does not allow execute or reconfigure.
    await agents.grant(db, world.p("B"), b1.id, world.users["A"].id, ["collaborate"])
    await db.commit()
    with pytest.raises(Forbidden):
        await runtime.execute(db, world.p("A"), b1.id, "still no")
    with pytest.raises(Forbidden):
        await agents.update_agent(db, world.p("A"), b1.id, model="x")
    # An execute grant allows running, but traces of B's private runs stay hidden.
    await runtime.execute(db, world.p("B"), b1.id, "private thought")
    await agents.grant(db, world.p("B"), b1.id, world.users["A"].id, ["execute"])
    await db.commit()
    res = await runtime.execute(db, world.p("A"), b1.id, "now allowed")
    assert res.status == "succeeded"
    seen_by_a = await runtime.list_executions(db, world.p("A"), b1.id)
    assert all("private thought" not in (e.get("prompt") or "") for e in seen_by_a)

    # Credential is encrypted at rest and never appears in traces.
    cred = await db.get(ProviderCredential, b1.id)
    assert b"sk-test-secret" not in cred.ciphertext
    for e in await db.scalars(select(Execution)):
        assert "sk-test-secret" not in e.prompt_redacted + e.response_redacted
    # Denials are audited.
    from agenthub.models import AuditLog
    denied = list(await db.scalars(select(AuditLog).where(AuditLog.allowed.is_(False))))
    assert len(denied) >= 6


# ---------------- Scenario C ----------------
async def test_C_shared_context_same_version_independent_reasoning(world):
    db = world.db
    req = await verified(world, "requirement", "Auth", "Use Discord OAuth2 with immutable user IDs.")
    x = await mk(world, "A", "x-agent", model_params={"persona": "x"})
    y = await mk(world, "B", "y-agent", model_params={"persona": "y"})
    rx = await runtime.execute(db, world.p("A"), x.id, "Summarise the auth requirement")
    ry = await runtime.execute(db, world.p("B"), y.id, "Critique the auth requirement")
    assert rx.context_version == ry.context_version == world.project.context_version
    assert req.id in rx.context_record_ids and req.id in ry.context_record_ids
    assert rx.text != ry.text and rx.session_id != ry.session_id
    assert f"ctx={rx.context_version}" in rx.text  # the model actually received the version


# ---------------- Scenario D ----------------
async def test_D_context_drift_detection(world):
    db = world.db
    old = await verified(world, "architecture", "Queue", "Use Redis streams for the event bus.")
    v_old = (await context.build_package(db, world.project.id)).context_version
    new = await verified(world, "architecture", "Queue", "Use Postgres SKIP LOCKED for the event bus.",
                         supersedes=old.id)
    await db.refresh(old)
    assert old.status == KnowledgeStatus.superseded and old.superseded_by_id == new.id

    report = await context.check_drift(db, world.project.id, v_old, [old.id])
    assert report.stale
    assert report.outdated_records[0]["replacement"]["id"] == new.id
    assert any(r["id"] == new.id for r in report.newer_verified)

    # The same check runs automatically on a handoff, and blocks approval until handled.
    agent = await mk(world, "B", "worker")
    t = await tasks.create_task(db, world.p("B"), world.project.id, "Build bus", acceptance_criteria=["works"])
    await tasks.assign(db, world.p("B"), t.id, [agent.id])
    await tasks.start(db, world.p("B"), t.id)
    out = await tasks.submit_handoff(db, world.p("B"), t.id, agent.id, {
        "summary": "Built on Redis streams", "context_version": v_old, "referenced_record_ids": [old.id],
        "evidence": []})
    assert out["drift"]["stale"]
    await tasks.add_review_evidence(db, world.p("A"), t.id, "test_result", "ci#1", True)
    with pytest.raises(InvalidTransition, match="outdated context"):
        await tasks.approve(db, world.p("A"), t.id, [True])


# ---------------- Scenario E ----------------
async def test_E_structured_debate_within_budget(world):
    db = world.db
    await verified(world, "constraint", "Single server", "Must run on one host without Kubernetes.")
    p1 = await mk(world, "A", "prop", model_params={"persona": "prop"})
    c1 = await mk(world, "B", "crit", model_params={"persona": "crit"})
    s1 = await mk(world, "C", "synth", model_params={"reply": (
        '{"agreements":["single host"],"disagreements":[{"topic":"queue","positions":["redis","pg"]}],'
        '"uncertainties":["load"],"recommended_next_actions":["benchmark"],"recommendation":"pg",'
        '"claims_to_verify":["pg handles 100 ev/s"]}')})
    for owner, a in (("B", c1), ("C", s1)):
        await agents.grant(db, world.p(owner), a.id, world.users["A"].id, ["collaborate"])
    await db.commit()

    d = await debate.create(db, world.p("A"), world.project.id, "Which queue should we use?", [
        {"agent_id": p1.id, "role": "proposer"}, {"agent_id": c1.id, "role": "critic"},
        {"agent_id": s1.id, "role": "synthesizer"}], max_rounds=2, budget_tokens=200_000)
    d = await debate.run(db, world.p("A"), d.id)
    s = d.summary
    assert d.status.value == "awaiting_approval"
    assert s["stop_reason"] == "max_rounds"  # mock agents never say NEW_OBJECTIONS: no
    assert len(s["proposals"]) == 2 and s["synthesis_parsed"]
    assert s["disagreements"] and s["uncertainties"] and s["recommended_next_actions"]
    assert {x["role"] for x in s["participants"]} == {"proposer", "critic", "synthesizer"}
    assert s["budget"]["tokens_used"] <= 200_000

    # Round 0 is independent: no proposal prompt contains another participant's output.
    ex = list(await db.scalars(select(Execution).where(Execution.debate_id == d.id)
                               .order_by(Execution.created_at)))
    assert "untrusted" not in ex[0].prompt_redacted and "untrusted" not in ex[1].prompt_redacted
    assert "untrusted" in ex[2].prompt_redacted

    # Only a maintainer can turn the outcome into a verified decision.
    with pytest.raises(Forbidden):
        await debate.approve(db, world.p("B"), d.id, "use pg")
    v_before = (await context.build_package(db, world.project.id)).context_version
    d = await debate.approve(db, world.p("A"), d.id, "Use Postgres as the queue.")
    rec = await db.get(KnowledgeRecord, d.decision_record_id)
    assert rec.status == KnowledgeStatus.verified and rec.related_debate_id == d.id
    assert (await context.build_package(db, world.project.id)).context_version == v_before + 1


async def test_E_debate_stops_on_budget(world):
    db = world.db
    agents_ = [await mk(world, "A", f"d{i}") for i in range(3)]
    d = await debate.create(db, world.p("A"), world.project.id, "x" * 2000, [
        {"agent_id": agents_[0].id, "role": "proposer"}, {"agent_id": agents_[1].id, "role": "critic"},
        {"agent_id": agents_[2].id, "role": "synthesizer"}], max_rounds=5, budget_tokens=1500)
    d = await debate.run(db, world.p("A"), d.id)
    assert d.stop_reason == "budget"
    turns = (await debate.get(db, world.p("A"), d.id))["turns"]
    assert max(t["round"] for t in turns) < 5


# ---------------- Scenario F ----------------
async def test_F_state_survives_restart(world, tmp_path):
    import os
    db = world.db
    a = await mk(world, "A", "persist")
    r = await verified(world, "requirement", "Persist", "State must survive restarts.")
    t = await tasks.create_task(db, world.p("A"), world.project.id, "Persist things")
    b = await mk(world, "B", "p2")
    c = await mk(world, "C", "p3")
    for owner, ag in (("B", b), ("C", c)):
        await agents.grant(db, world.p(owner), ag.id, world.users["A"].id, ["collaborate"])
    d = await debate.create(db, world.p("A"), world.project.id, "q", [
        {"agent_id": a.id, "role": "proposer"}, {"agent_id": b.id, "role": "critic"},
        {"agent_id": c.id, "role": "synthesizer"}], max_rounds=0)
    await debate.run(db, world.p("A"), d.id)
    await db.commit()
    url = str(dbmod.get_sessionmaker().kw["bind"].url.render_as_string(hide_password=False))
    ids = (a.id, r.id, t.id, d.id)
    await db.close()

    await dbmod.dispose_engine()  # "restart": drop every connection and in-process state
    maker = dbmod.init_engine(os.environ.get("TEST_DATABASE_URL") or url)
    async with maker() as s2:
        assert (await s2.get(AgentDefinition, ids[0])).name == "persist"
        assert (await s2.get(KnowledgeRecord, ids[1])).status == KnowledgeStatus.verified
        assert (await s2.get(Task, ids[2])).title == "Persist things"
        deb = await s2.get(Debate, ids[3])
        assert deb.summary and deb.status.value == "awaiting_approval"


# ---------------- Scenario G ----------------
async def test_G_completion_requires_evidence_and_review(world):
    db = world.db
    agent = await mk(world, "B", "builder")
    t = await tasks.create_task(db, world.p("B"), world.project.id, "Add login",
                                acceptance_criteria=["OAuth works", "tests pass"])
    await tasks.assign(db, world.p("B"), t.id, [agent.id])
    await tasks.start(db, world.p("B"), t.id)
    v = (await context.build_package(db, world.project.id)).context_version
    await tasks.submit_handoff(db, world.p("B"), t.id, agent.id, {
        "summary": "Done, all tests pass!", "context_version": v,
        "evidence": [{"kind": "test_result", "reference": "trust me", "passed": True}]})

    # Agent's own claim is not enough.
    with pytest.raises(InvalidTransition, match="no verified passing evidence"):
        await tasks.approve(db, world.p("A"), t.id, [True, True])
    # The agent's owner (a plain member) cannot self-approve.
    with pytest.raises(Forbidden):
        await tasks.approve(db, world.p("B"), t.id, [True, True])
    # Cannot jump straight to completed.
    with pytest.raises(InvalidTransition):
        await tasks.complete(db, world.p("A"), t.id)

    await tasks.add_review_evidence(db, world.p("A"), t.id, "test_result", "CI run 42 green", True)
    with pytest.raises(InvalidTransition, match="acceptance criteria"):
        await tasks.approve(db, world.p("A"), t.id, [True, False])
    await tasks.approve(db, world.p("A"), t.id, [True, True])
    t = await tasks.complete(db, world.p("A"), t.id)
    assert t.state.value == "completed"
    hist = [h["to"] for h in await tasks.history(db, world.p("A"), t.id)]
    assert hist == ["backlog", "assigned", "in_progress", "review", "approved", "completed"]


# ---------------- Scenario H ----------------
async def test_H_agent_unavailable_during_collaboration(world):
    db = world.db
    good1 = await mk(world, "A", "g1")
    good2 = await mk(world, "A", "g2")
    broken = await mk(world, "A", "broken", model_params={"fail": True})
    synth = await mk(world, "A", "s")
    d = await debate.create(db, world.p("A"), world.project.id, "Pick a DB", [
        {"agent_id": good1.id, "role": "proposer"}, {"agent_id": broken.id, "role": "critic"},
        {"agent_id": good2.id, "role": "alternative"}, {"agent_id": synth.id, "role": "synthesizer"}],
        max_rounds=1)
    d = await debate.run(db, world.p("A"), d.id)
    assert d.status.value == "awaiting_approval"
    assert any(f["agent"] == "broken" for f in d.summary["failures"])
    assert len(d.summary["proposals"]) == 2
    failed = list(await db.scalars(select(Execution).where(Execution.agent_id == broken.id)))
    assert failed and all(e.status.value == "failed" for e in failed)

    # A paused agent is skipped the same way, and the workflow state is kept.
    await agents.update_agent(db, world.p("A"), good2.id, status="paused")
    await db.commit()
    d2 = await debate.create(db, world.p("A"), world.project.id, "Again", [
        {"agent_id": good1.id, "role": "proposer"}, {"agent_id": good2.id, "role": "critic"},
        {"agent_id": synth.id, "role": "synthesizer"}], max_rounds=1)
    d2 = await debate.run(db, world.p("A"), d2.id)
    assert d2.stop_reason == "insufficient_proposals"
    assert d2.summary["failures"][0]["agent"] == "g2"
