from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from agenthub.models import AuditLog


async def record(
    db: AsyncSession,
    actor_user_id: str | None,
    action: str,
    resource_type: str,
    resource_id: str | None,
    allowed: bool,
    **detail: Any,
) -> None:
    db.add(
        AuditLog(
            actor_user_id=actor_user_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            allowed=allowed,
            detail=detail,
        )
    )
    await db.flush()
