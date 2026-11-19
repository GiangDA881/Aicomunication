"""Deny-by-default authorization.

Every function here either returns normally (allowed) or raises Forbidden. Service methods call
these before touching data. Nothing here reads prompts or model output: authorization is decided
only from database state about humans, projects, agents and grants.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agenthub.errors import Forbidden
from agenthub.models import AgentDefinition, AgentGrant, ProjectMember, ProjectRole, User
from agenthub.services import audit

PERM_EXECUTE = "execute"
PERM_COLLABORATE = "collaborate"
GRANTABLE = {PERM_EXECUTE, PERM_COLLABORATE}


@dataclass(frozen=True)
class Principal:
    user_id: str
    discord_id: str
    is_admin: bool = False

    @classmethod
    def of(cls, user: User) -> Principal:
        return cls(user_id=user.id, discord_id=user.discord_id, is_admin=user.is_admin)


async def _deny(db: AsyncSession, p: Principal, action: str, rtype: str, rid: str | None) -> None:
    await audit.record(db, p.user_id, action, rtype, rid, allowed=False)
    await db.commit()  # keep the denial in the audit log even if the caller rolls back
    raise Forbidden(f"{action} on {rtype} {rid or ''} is not permitted".strip())


async def project_role(db: AsyncSession, p: Principal, project_id: str) -> ProjectRole | None:
    row = await db.scalar(
        select(ProjectMember.role).where(
            ProjectMember.project_id == project_id, ProjectMember.user_id == p.user_id
        )
    )
    return row


async def require_member(db: AsyncSession, p: Principal, project_id: str) -> None:
    if await project_role(db, p, project_id) is None:
        await _deny(db, p, "project.access", "project", project_id)


async def require_maintainer(db: AsyncSession, p: Principal, project_id: str) -> None:
    if await project_role(db, p, project_id) != ProjectRole.maintainer:
        await _deny(db, p, "project.maintain", "project", project_id)


async def _grant_perms(db: AsyncSession, p: Principal, agent_id: str) -> set[str]:
    perms = await db.scalar(
        select(AgentGrant.permissions).where(
            AgentGrant.agent_id == agent_id, AgentGrant.grantee_id == p.user_id
        )
    )
    return set(perms or [])


async def require_owner(db: AsyncSession, p: Principal, agent: AgentDefinition, action: str) -> None:
    """Reconfigure, delete, credentials, private memory, private traces: owner only."""
    if agent.owner_id != p.user_id:
        await _deny(db, p, action, "agent", agent.id)


async def require_execute(db: AsyncSession, p: Principal, agent: AgentDefinition) -> None:
    if agent.owner_id == p.user_id:
        return
    await require_member(db, p, agent.project_id)
    if PERM_EXECUTE not in await _grant_perms(db, p, agent.id):
        await _deny(db, p, "agent.execute", "agent", agent.id)


async def require_collaborate(db: AsyncSession, p: Principal, agent: AgentDefinition) -> None:
    """Including an agent in a debate or assigning it a task."""
    if agent.owner_id == p.user_id:
        return
    await require_member(db, p, agent.project_id)
    if PERM_COLLABORATE not in await _grant_perms(db, p, agent.id):
        await _deny(db, p, "agent.collaborate", "agent", agent.id)


async def can_view_agent(db: AsyncSession, p: Principal, agent: AgentDefinition) -> bool:
    """Public profile (name, role, model, status) is visible to project members."""
    return agent.owner_id == p.user_id or await project_role(db, p, agent.project_id) is not None
