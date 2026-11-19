"""Users (keyed by immutable Discord ID) and projects."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agenthub.authz import Principal, require_maintainer
from agenthub.errors import Conflict, NotFound
from agenthub.models import Project, ProjectMember, ProjectRole, User
from agenthub.security import hash_api_key, new_api_key
from agenthub.services import audit


async def upsert_discord_user(db: AsyncSession, discord_id: str, display_name: str) -> User:
    if not discord_id.isdigit():
        raise ValueError("discord_id must be the numeric snowflake, not a username")
    user = await db.scalar(select(User).where(User.discord_id == discord_id))
    if user is None:
        user = User(discord_id=discord_id, display_name=display_name[:100])
        db.add(user)
    else:
        user.display_name = display_name[:100]  # display only; never used for identity
    await db.flush()
    return user


async def issue_api_key(db: AsyncSession, user: User) -> str:
    key, digest = new_api_key()
    user.api_key_hash = digest
    await audit.record(db, user.id, "user.api_key.issue", "user", user.id, allowed=True)
    await db.flush()
    return key


async def user_by_api_key(db: AsyncSession, key: str) -> User | None:
    if not key:
        return None
    return await db.scalar(select(User).where(User.api_key_hash == hash_api_key(key)))


async def get_user(db: AsyncSession, user_id: str) -> User:
    user = await db.get(User, user_id)
    if user is None:
        raise NotFound("user not found")
    return user


async def create_project(db: AsyncSession, p: Principal, name: str, goals: str = "") -> Project:
    if await db.scalar(select(Project).where(Project.name == name)):
        raise Conflict(f"project {name!r} exists")
    project = Project(name=name, goals=goals)
    db.add(project)
    await db.flush()
    db.add(ProjectMember(project_id=project.id, user_id=p.user_id, role=ProjectRole.maintainer))
    await audit.record(db, p.user_id, "project.create", "project", project.id, allowed=True)
    await db.flush()
    return project


async def add_member(
    db: AsyncSession, p: Principal, project_id: str, user_id: str, role: ProjectRole
) -> ProjectMember:
    await require_maintainer(db, p, project_id)
    existing = await db.scalar(
        select(ProjectMember).where(
            ProjectMember.project_id == project_id, ProjectMember.user_id == user_id
        )
    )
    if existing:
        existing.role = role
        member = existing
    else:
        member = ProjectMember(project_id=project_id, user_id=user_id, role=role)
        db.add(member)
    await audit.record(
        db, p.user_id, "project.member.set", "project", project_id, allowed=True,
        member=user_id, role=role.value,
    )
    await db.flush()
    return member


async def get_project(db: AsyncSession, project_id: str) -> Project:
    project = await db.get(Project, project_id)
    if project is None:
        raise NotFound("project not found")
    return project


async def project_by_name(db: AsyncSession, name: str) -> Project:
    project = await db.scalar(select(Project).where(Project.name == name))
    if project is None:
        raise NotFound(f"project {name!r} not found")
    return project
