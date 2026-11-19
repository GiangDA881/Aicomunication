from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import Depends, Header, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from agenthub.authz import Principal
from agenthub.db import get_sessionmaker
from agenthub.services import identity


async def get_db() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_principal(
    authorization: str = Header(default=""), db: AsyncSession = Depends(get_db)
) -> Principal:
    # Deny by default: every route except /health, OAuth and the signed GitHub webhook needs a key.
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "missing bearer token")
    user = await identity.user_by_api_key(db, authorization.removeprefix("Bearer ").strip())
    if user is None:
        raise HTTPException(401, "invalid token")
    return Principal.of(user)
