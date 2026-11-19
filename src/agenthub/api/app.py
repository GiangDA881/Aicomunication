from __future__ import annotations

import json
import secrets
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from agenthub.api.deps import get_db, get_principal
from agenthub.authz import Principal
from agenthub.config import get_settings
from agenthub.db import get_sessionmaker
from agenthub.errors import AgentHubError
from agenthub.models import OAuthState, ProjectRole, utcnow
from agenthub.security import verify_github_signature
from agenthub.services import agents, bus, debate, github, identity, knowledge, runtime, tasks
from agenthub.services import context as ctx_svc


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with get_sessionmaker()() as db:
        await runtime.recover_stale_executions(db)
    yield


app = FastAPI(title="AgentHub", version="0.1.0", lifespan=lifespan)


@app.exception_handler(AgentHubError)
async def _err(_: Request, exc: AgentHubError) -> JSONResponse:
    return JSONResponse({"error": type(exc).__name__, "detail": str(exc)}, status_code=exc.status_code)


@app.exception_handler(ValueError)
async def _verr(_: Request, exc: ValueError) -> JSONResponse:
    return JSONResponse({"error": "ValueError", "detail": str(exc)}, status_code=422)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


# ---------- Discord OAuth2 (identify) ----------

@app.get("/auth/discord/login")
async def discord_login(db: AsyncSession = Depends(get_db)) -> RedirectResponse:
    s = get_settings()
    if not s.discord_client_id:
        raise HTTPException(503, "Discord OAuth is not configured")
    state = secrets.token_urlsafe(24)
    db.add(OAuthState(state=state))
    q = urlencode({"client_id": s.discord_client_id, "response_type": "code", "scope": "identify",
                   "redirect_uri": f"{s.public_base_url}/auth/discord/callback", "state": state,
                   "prompt": "none"})
    return RedirectResponse(f"https://discord.com/oauth2/authorize?{q}")


@app.get("/auth/discord/callback")
async def discord_callback(code: str, state: str, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    s = get_settings()
    row = await db.get(OAuthState, state)
    if row is None or row.created_at.replace(tzinfo=row.created_at.tzinfo or utcnow().tzinfo) < (
        utcnow() - timedelta(seconds=s.oauth_state_ttl_seconds)
    ):
        raise HTTPException(400, "invalid or expired state")
    await db.delete(row)
    async with httpx.AsyncClient(timeout=15) as client:
        tok = await client.post("https://discord.com/api/v10/oauth2/token", data={
            "grant_type": "authorization_code", "code": code,
            "redirect_uri": f"{s.public_base_url}/auth/discord/callback",
        }, auth=(s.discord_client_id, s.discord_client_secret))
        if tok.status_code != 200:
            raise HTTPException(400, "code exchange failed")
        me = await client.get("https://discord.com/api/v10/users/@me",
                              headers={"Authorization": f"Bearer {tok.json()['access_token']}"})
    if me.status_code != 200:
        raise HTTPException(400, "could not read Discord identity")
    info = me.json()
    user = await identity.upsert_discord_user(db, str(info["id"]), info.get("global_name") or info["username"])
    key = await identity.issue_api_key(db, user)
    # The Discord access token is discarded; only the immutable user ID is kept.
    return {"user_id": user.id, "discord_id": user.discord_id, "api_key": key,
            "note": "Store this key now; it is shown once."}


# ---------- Projects ----------

class ProjectIn(BaseModel):
    name: str
    goals: str = ""


class MemberIn(BaseModel):
    user_id: str
    role: ProjectRole = ProjectRole.member


@app.post("/projects")
async def create_project(body: ProjectIn, p: Principal = Depends(get_principal),
                         db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    pr = await identity.create_project(db, p, body.name, body.goals)
    return {"id": pr.id, "name": pr.name, "context_version": pr.context_version}


@app.post("/projects/{project_id}/members")
async def add_member(project_id: str, body: MemberIn, p: Principal = Depends(get_principal),
                     db: AsyncSession = Depends(get_db)) -> dict[str, str]:
    m = await identity.add_member(db, p, project_id, body.user_id, body.role)
    return {"user_id": m.user_id, "role": m.role.value}


# ---------- Agents ----------

class AgentIn(BaseModel):
    project_id: str
    name: str = Field(min_length=1, max_length=64)
    provider: str
    model: str
    description: str = ""
    role: str = "generalist"
    system_instructions: str = ""
    model_params: dict[str, Any] = {}
    base_url: str | None = None
    allowed_tools: list[str] = []
    max_concurrency: int = 1


class AgentPatch(BaseModel):
    model_config = {"extra": "forbid"}
    description: str | None = None
    role: str | None = None
    provider: str | None = None
    model: str | None = None
    model_params: dict[str, Any] | None = None
    base_url: str | None = None
    system_instructions: str | None = None
    allowed_tools: list[str] | None = None
    status: str | None = None
    max_concurrency: int | None = None
    budget_tokens: int | None = None
    budget_usd: float | None = None


class CredentialIn(BaseModel):
    api_key: str = Field(min_length=8, repr=False)


class GrantIn(BaseModel):
    grantee_id: str
    permissions: list[str]


class AskIn(BaseModel):
    prompt: str
    task_id: str | None = None
    session_id: str | None = None
    shared: bool = False


@app.post("/agents")
async def create_agent(body: AgentIn, p: Principal = Depends(get_principal),
                       db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    data = body.model_dump()
    a = await agents.create_agent(db, p, data.pop("project_id"), data.pop("name"),
                                  data.pop("provider"), data.pop("model"), **data)
    return agents.owner_view(a, False)


@app.get("/projects/{project_id}/agents")
async def list_agents(project_id: str, p: Principal = Depends(get_principal),
                      db: AsyncSession = Depends(get_db)) -> list[dict[str, Any]]:
    rows = await agents.list_visible_agents(db, p, project_id)
    return [agents.owner_view(a, await agents.has_credential(db, a.id)) if a.owner_id == p.user_id
            else agents.public_view(a) for a in rows]


@app.patch("/agents/{agent_id}")
async def update_agent(agent_id: str, body: AgentPatch, p: Principal = Depends(get_principal),
                       db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    a = await agents.update_agent(db, p, agent_id, **body.model_dump(exclude_none=True))
    return agents.owner_view(a, await agents.has_credential(db, a.id))


@app.delete("/agents/{agent_id}")
async def delete_agent(agent_id: str, p: Principal = Depends(get_principal),
                       db: AsyncSession = Depends(get_db)) -> dict[str, bool]:
    await agents.delete_agent(db, p, agent_id)
    return {"deleted": True}


@app.put("/agents/{agent_id}/credential")
async def set_credential(agent_id: str, body: CredentialIn, p: Principal = Depends(get_principal),
                         db: AsyncSession = Depends(get_db)) -> dict[str, str]:
    return {"fingerprint": await agents.set_credential(db, p, agent_id, body.api_key)}


@app.post("/agents/{agent_id}/grants")
async def grant(agent_id: str, body: GrantIn, p: Principal = Depends(get_principal),
                db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    g = await agents.grant(db, p, agent_id, body.grantee_id, body.permissions)
    return {"grantee_id": g.grantee_id, "permissions": g.permissions}


@app.post("/agents/{agent_id}/ask")
async def ask(agent_id: str, body: AskIn, p: Principal = Depends(get_principal),
              db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    res = await runtime.execute(db, p, agent_id, body.prompt, task_id=body.task_id,
                                session_id=body.session_id, shared=body.shared)
    return res.__dict__


@app.get("/agents/{agent_id}/executions")
async def executions(agent_id: str, p: Principal = Depends(get_principal),
                     db: AsyncSession = Depends(get_db)) -> list[dict[str, Any]]:
    return await runtime.list_executions(db, p, agent_id)


@app.get("/agents/{agent_id}/memory")
async def memory(agent_id: str, p: Principal = Depends(get_principal),
                 db: AsyncSession = Depends(get_db)) -> dict[str, str]:
    return await agents.memory_list(db, p, agent_id)


@app.get("/agents/{agent_id}/inbox")
async def inbox(agent_id: str, p: Principal = Depends(get_principal),
                db: AsyncSession = Depends(get_db)) -> list[dict[str, Any]]:
    a = await agents.get_agent(db, agent_id)
    from agenthub import authz
    await authz.require_execute(db, p, a)
    return [bus.envelope(e) for e in await bus.pending_for(db, agent_id)]


# ---------- Knowledge & context ----------

class KnowledgeIn(BaseModel):
    kind: str
    title: str
    body: str
    tags: list[str] = []
    source: str = ""
    evidence: list[str] = []
    related_task_ids: list[str] = []
    supersedes_id: str | None = None


class DriftIn(BaseModel):
    context_version: int | None = None
    record_ids: list[str] = []


@app.post("/projects/{project_id}/knowledge")
async def propose(project_id: str, body: KnowledgeIn, p: Principal = Depends(get_principal),
                  db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    return knowledge.view(await knowledge.propose(db, p, project_id, **body.model_dump()))


@app.get("/projects/{project_id}/knowledge")
async def list_knowledge(project_id: str, status: str | None = None, kind: str | None = None,
                         p: Principal = Depends(get_principal),
                         db: AsyncSession = Depends(get_db)) -> list[dict[str, Any]]:
    return [knowledge.view(r) for r in await knowledge.list_records(db, p, project_id, status, kind)]


@app.post("/knowledge/{record_id}/verify")
async def verify(record_id: str, p: Principal = Depends(get_principal),
                 db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    return knowledge.view(await knowledge.verify(db, p, record_id))


@app.post("/knowledge/{record_id}/reject")
async def reject(record_id: str, p: Principal = Depends(get_principal),
                 db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    return knowledge.view(await knowledge.reject(db, p, record_id))


@app.post("/knowledge/{record_id}/deprecate")
async def deprecate(record_id: str, p: Principal = Depends(get_principal),
                    db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    return knowledge.view(await knowledge.deprecate(db, p, record_id))


@app.get("/projects/{project_id}/context")
async def context(project_id: str, task_id: str | None = None, p: Principal = Depends(get_principal),
                  db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    from agenthub import authz
    await authz.require_member(db, p, project_id)
    return (await ctx_svc.build_package(db, project_id, task_id)).to_dict()


@app.post("/projects/{project_id}/drift")
async def drift(project_id: str, body: DriftIn, p: Principal = Depends(get_principal),
                db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    from agenthub import authz
    await authz.require_member(db, p, project_id)
    return (await ctx_svc.check_drift(db, project_id, body.context_version, body.record_ids)).to_dict()


# ---------- Tasks ----------

class TaskIn(BaseModel):
    title: str
    objectives: str = ""
    acceptance_criteria: list[str] = []
    depends_on: list[str] = []


class AssignIn(BaseModel):
    agent_ids: list[str]


class HandoffIn(BaseModel):
    agent_id: str
    summary: str
    changes: list[str] = []
    evidence: list[dict[str, Any]] = []
    open_questions: list[str] = []
    next_steps: list[str] = []
    context_version: int | None = None
    referenced_record_ids: list[str] = []


class EvidenceIn(BaseModel):
    kind: str
    reference: str
    passed: bool


class ApproveIn(BaseModel):
    criteria_met: list[bool]
    note: str = ""
    override_drift: bool = False


class StateIn(BaseModel):
    state: str
    note: str = ""


@app.post("/projects/{project_id}/tasks")
async def create_task(project_id: str, body: TaskIn, p: Principal = Depends(get_principal),
                      db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    return tasks.view(await tasks.create_task(db, p, project_id, **body.model_dump()))


@app.get("/projects/{project_id}/tasks")
async def list_tasks(project_id: str, state: str | None = None, p: Principal = Depends(get_principal),
                     db: AsyncSession = Depends(get_db)) -> list[dict[str, Any]]:
    return [tasks.view(t) for t in await tasks.list_tasks(db, p, project_id, state)]


@app.get("/tasks/{ref}")
async def get_task(ref: str, p: Principal = Depends(get_principal),
                   db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    t = await tasks.get_task(db, ref)
    from agenthub import authz
    await authz.require_member(db, p, t.project_id)
    return {**tasks.view(t, await tasks.evidence_of(db, t.id)), "history": await tasks.history(db, p, ref)}


@app.post("/tasks/{ref}/assign")
async def assign(ref: str, body: AssignIn, p: Principal = Depends(get_principal),
                 db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    return tasks.view(await tasks.assign(db, p, ref, body.agent_ids))


@app.post("/tasks/{ref}/start")
async def start(ref: str, p: Principal = Depends(get_principal),
                db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    return tasks.view(await tasks.start(db, p, ref))


@app.post("/tasks/{ref}/handoff")
async def handoff(ref: str, body: HandoffIn, p: Principal = Depends(get_principal),
                  db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    data = body.model_dump()
    return await tasks.submit_handoff(db, p, ref, data.pop("agent_id"), data)


@app.post("/tasks/{ref}/evidence")
async def evidence(ref: str, body: EvidenceIn, p: Principal = Depends(get_principal),
                   db: AsyncSession = Depends(get_db)) -> dict[str, bool]:
    await tasks.add_review_evidence(db, p, ref, body.kind, body.reference, body.passed)
    return {"recorded": True}


@app.post("/tasks/{ref}/approve")
async def approve(ref: str, body: ApproveIn, p: Principal = Depends(get_principal),
                  db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    return tasks.view(await tasks.approve(db, p, ref, body.criteria_met, body.note, body.override_drift))


@app.post("/tasks/{ref}/complete")
async def complete(ref: str, p: Principal = Depends(get_principal),
                   db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    return tasks.view(await tasks.complete(db, p, ref))


@app.post("/tasks/{ref}/state")
async def set_state(ref: str, body: StateIn, p: Principal = Depends(get_principal),
                    db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    return tasks.view(await tasks.set_state(db, p, ref, body.state, body.note))


# ---------- Debates ----------

class DebateIn(BaseModel):
    problem: str
    participants: list[dict[str, str]]
    max_rounds: int = 2
    budget_tokens: int = 50_000
    budget_usd: float = 1.0
    task_id: str | None = None


class DecisionIn(BaseModel):
    decision: str


@app.post("/projects/{project_id}/debates")
async def create_debate(project_id: str, body: DebateIn, p: Principal = Depends(get_principal),
                        db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    data = body.model_dump()
    d = await debate.create(db, p, project_id, data.pop("problem"), data.pop("participants"), **data)
    d = await debate.run(db, p, d.id)
    return await debate.get(db, p, d.id)


@app.get("/debates/{debate_id}")
async def get_debate(debate_id: str, p: Principal = Depends(get_principal),
                     db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    return await debate.get(db, p, debate_id)


@app.post("/debates/{debate_id}/approve")
async def approve_debate(debate_id: str, body: DecisionIn, p: Principal = Depends(get_principal),
                         db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    await debate.approve(db, p, debate_id, body.decision)
    return await debate.get(db, p, debate_id)


# ---------- GitHub webhook ----------

@app.post("/webhooks/github")
async def github_webhook(
    request: Request, x_github_event: str = Header(default=""),
    x_github_delivery: str = Header(default=""), x_hub_signature_256: str = Header(default=""),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    body = await request.body()
    if not verify_github_signature(get_settings().github_webhook_secret, body, x_hub_signature_256):
        raise HTTPException(401, "bad signature")
    return await github.handle(db, x_github_event, x_github_delivery, json.loads(body))
