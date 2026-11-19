"""Agent registry: definitions, grants, credentials, private memory."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from agenthub import authz
from agenthub.authz import Principal
from agenthub.config import get_settings
from agenthub.errors import Conflict, NotFound
from agenthub.models import (
    AgentDefinition,
    AgentGrant,
    AgentMemory,
    AgentStatus,
    ProviderCredential,
    utcnow,
)
from agenthub.providers.base import known_providers
from agenthub.security import decrypt_secret, encrypt_secret, fingerprint
from agenthub.services import audit

# Fields an owner may change via update_agent. Owner, project and usage counters are not editable.
EDITABLE = {
    "description", "role", "provider", "model", "model_params", "base_url",
    "system_instructions", "allowed_tools", "status", "max_concurrency",
    "budget_tokens", "budget_usd",
}

# Tools the platform actually implements. Anything else is rejected rather than silently stored.
KNOWN_TOOLS = {"knowledge.read", "knowledge.propose", "task.handoff", "memory.write"}


def public_view(agent: AgentDefinition) -> dict[str, Any]:
    """What non-owners may see."""
    return {
        "id": agent.id,
        "owner_id": agent.owner_id,
        "project_id": agent.project_id,
        "name": agent.name,
        "description": agent.description,
        "role": agent.role,
        "provider": agent.provider,
        "model": agent.model,
        "status": agent.status.value,
    }


def owner_view(agent: AgentDefinition, has_credential: bool) -> dict[str, Any]:
    return {
        **public_view(agent),
        "model_params": agent.model_params,
        "base_url": agent.base_url,
        "system_instructions": agent.system_instructions,
        "allowed_tools": agent.allowed_tools,
        "max_concurrency": agent.max_concurrency,
        "budget_tokens": agent.budget_tokens,
        "budget_usd": agent.budget_usd,
        "used_tokens": agent.used_tokens,
        "used_usd": round(agent.used_usd, 6),
        "has_credential": has_credential,
    }


def _validate(fields: dict[str, Any]) -> None:
    if "provider" in fields and fields["provider"] not in known_providers():
        raise ValueError(f"unknown provider {fields['provider']!r}")
    bad = set(fields.get("allowed_tools") or []) - KNOWN_TOOLS
    if bad:
        raise ValueError(f"unknown tools: {sorted(bad)}")
    if "max_concurrency" in fields and not 1 <= int(fields["max_concurrency"]) <= 16:
        raise ValueError("max_concurrency must be 1..16")


async def create_agent(
    db: AsyncSession, p: Principal, project_id: str, name: str, provider: str, model: str,
    **fields: Any,
) -> AgentDefinition:
    await authz.require_member(db, p, project_id)
    settings = get_settings()
    count = await db.scalar(
        select(func.count()).select_from(AgentDefinition).where(AgentDefinition.owner_id == p.user_id)
    )
    if (count or 0) >= settings.max_agents_per_user and not p.is_admin:
        raise Conflict(f"agent limit reached ({settings.max_agents_per_user})")
    unknown = set(fields) - EDITABLE
    if unknown:
        raise ValueError(f"unknown fields: {sorted(unknown)}")
    _validate({"provider": provider, **fields})
    if await db.scalar(
        select(AgentDefinition).where(
            AgentDefinition.owner_id == p.user_id, AgentDefinition.name == name
        )
    ):
        raise Conflict(f"you already have an agent named {name!r}")
    fields.setdefault("budget_tokens", settings.default_agent_budget_tokens)
    fields.setdefault("budget_usd", settings.default_agent_budget_usd)
    agent = AgentDefinition(
        owner_id=p.user_id, project_id=project_id, name=name, provider=provider, model=model,
        **fields,
    )
    db.add(agent)
    await db.flush()
    await audit.record(db, p.user_id, "agent.create", "agent", agent.id, allowed=True,
                       provider=provider, model=model)
    return agent


async def get_agent(db: AsyncSession, agent_id: str) -> AgentDefinition:
    agent = await db.get(AgentDefinition, agent_id)
    if agent is None:
        raise NotFound("agent not found")
    return agent


async def find_agent(db: AsyncSession, p: Principal, ref: str) -> AgentDefinition:
    """Resolve an agent by ID, or by name among the caller's own agents, or `owner_discord_id/name`."""
    agent = await db.get(AgentDefinition, ref)
    if agent:
        return agent
    from agenthub.models import User

    if "/" in ref:
        owner_ref, name = ref.split("/", 1)
        owner = await db.scalar(select(User).where(User.discord_id == owner_ref))
        owner_id = owner.id if owner else None
    else:
        owner_id, name = p.user_id, ref
    agent = await db.scalar(
        select(AgentDefinition).where(AgentDefinition.owner_id == owner_id, AgentDefinition.name == name)
    )
    if agent is None:
        raise NotFound(f"agent {ref!r} not found")
    return agent


async def list_visible_agents(db: AsyncSession, p: Principal, project_id: str) -> list[AgentDefinition]:
    await authz.require_member(db, p, project_id)
    return list(
        await db.scalars(
            select(AgentDefinition)
            .where(AgentDefinition.project_id == project_id)
            .order_by(AgentDefinition.created_at)
        )
    )


async def update_agent(
    db: AsyncSession, p: Principal, agent_id: str, **fields: Any
) -> AgentDefinition:
    agent = await get_agent(db, agent_id)
    await authz.require_owner(db, p, agent, "agent.update")
    unknown = set(fields) - EDITABLE
    if unknown:
        raise ValueError(f"fields not editable: {sorted(unknown)}")
    _validate(fields)
    for k, v in fields.items():
        if k == "status":
            v = AgentStatus(v)
        setattr(agent, k, v)
    await audit.record(db, p.user_id, "agent.update", "agent", agent.id, allowed=True,
                       fields=sorted(fields))
    await db.flush()
    return agent


async def delete_agent(db: AsyncSession, p: Principal, agent_id: str) -> None:
    agent = await get_agent(db, agent_id)
    await authz.require_owner(db, p, agent, "agent.delete")
    await audit.record(db, p.user_id, "agent.delete", "agent", agent.id, allowed=True)
    await db.delete(agent)
    await db.flush()


async def set_credential(db: AsyncSession, p: Principal, agent_id: str, api_key: str) -> str:
    agent = await get_agent(db, agent_id)
    await authz.require_owner(db, p, agent, "agent.credential.set")
    cred = await db.get(ProviderCredential, agent_id)
    fp = fingerprint(api_key)
    if cred is None:
        db.add(ProviderCredential(agent_id=agent_id, ciphertext=encrypt_secret(api_key), fingerprint=fp))
    else:
        cred.ciphertext = encrypt_secret(api_key)
        cred.fingerprint = fp
        cred.updated_at = utcnow()
    await audit.record(db, p.user_id, "agent.credential.set", "agent", agent_id, allowed=True,
                       fingerprint=fp)
    await db.flush()
    return fp


async def has_credential(db: AsyncSession, agent_id: str) -> bool:
    return await db.get(ProviderCredential, agent_id) is not None


async def _load_credential_for_runtime(db: AsyncSession, agent_id: str) -> str | None:
    """Internal: only the runtime calls this, after authorization, to make one provider call."""
    cred = await db.get(ProviderCredential, agent_id)
    return decrypt_secret(cred.ciphertext) if cred else None


async def grant(
    db: AsyncSession, p: Principal, agent_id: str, grantee_id: str, permissions: list[str]
) -> AgentGrant:
    agent = await get_agent(db, agent_id)
    await authz.require_owner(db, p, agent, "agent.grant")
    bad = set(permissions) - authz.GRANTABLE
    if bad:
        raise ValueError(f"not grantable: {sorted(bad)} (allowed: {sorted(authz.GRANTABLE)})")
    row = await db.scalar(
        select(AgentGrant).where(AgentGrant.agent_id == agent_id, AgentGrant.grantee_id == grantee_id)
    )
    if row is None:
        row = AgentGrant(agent_id=agent_id, grantee_id=grantee_id, permissions=sorted(set(permissions)))
        db.add(row)
    else:
        row.permissions = sorted(set(permissions))
    await audit.record(db, p.user_id, "agent.grant", "agent", agent_id, allowed=True,
                       grantee=grantee_id, permissions=sorted(set(permissions)))
    await db.flush()
    return row


async def revoke(db: AsyncSession, p: Principal, agent_id: str, grantee_id: str) -> None:
    agent = await get_agent(db, agent_id)
    await authz.require_owner(db, p, agent, "agent.revoke")
    row = await db.scalar(
        select(AgentGrant).where(AgentGrant.agent_id == agent_id, AgentGrant.grantee_id == grantee_id)
    )
    if row:
        await db.delete(row)
    await audit.record(db, p.user_id, "agent.revoke", "agent", agent_id, allowed=True, grantee=grantee_id)
    await db.flush()


async def memory_put(db: AsyncSession, p: Principal, agent_id: str, key: str, value: str) -> None:
    agent = await get_agent(db, agent_id)
    await authz.require_owner(db, p, agent, "agent.memory.write")
    await _memory_put_internal(db, agent_id, key, value)


async def _memory_put_internal(db: AsyncSession, agent_id: str, key: str, value: str) -> None:
    row = await db.scalar(select(AgentMemory).where(AgentMemory.agent_id == agent_id, AgentMemory.key == key))
    if row is None:
        db.add(AgentMemory(agent_id=agent_id, key=key, value=value))
    else:
        row.value = value
        row.updated_at = utcnow()
    await db.flush()


async def memory_list(db: AsyncSession, p: Principal, agent_id: str) -> dict[str, str]:
    agent = await get_agent(db, agent_id)
    await authz.require_owner(db, p, agent, "agent.memory.read")
    return await _memory_internal(db, agent_id)


async def _memory_internal(db: AsyncSession, agent_id: str) -> dict[str, str]:
    rows = await db.scalars(select(AgentMemory).where(AgentMemory.agent_id == agent_id))
    return {r.key: r.value for r in rows}
