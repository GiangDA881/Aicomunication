from __future__ import annotations

import os
import uuid

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import text

os.environ.setdefault("AGENTHUB_CREDENTIAL_KEY", Fernet.generate_key().decode())
os.environ.setdefault("AGENTHUB_GITHUB_WEBHOOK_SECRET", "test-webhook-secret")

from agenthub import db as dbmod
from agenthub.authz import Principal
from agenthub.config import get_settings
from agenthub.db import Base
from agenthub.models import ProjectRole
from agenthub.services import identity

get_settings.cache_clear()


def _url(tmp_path) -> str:
    # TEST_DATABASE_URL=postgresql+asyncpg://... runs the suite against Postgres.
    return os.environ.get("TEST_DATABASE_URL") or f"sqlite+aiosqlite:///{tmp_path}/test.db"


@pytest.fixture
async def maker(tmp_path):
    url = _url(tmp_path)
    m = dbmod.init_engine(url)
    engine = m.kw["bind"]
    async with engine.begin() as conn:
        if url.startswith("postgresql"):
            await conn.execute(text("DROP SCHEMA public CASCADE"))
            await conn.execute(text("CREATE SCHEMA public"))
        await conn.run_sync(Base.metadata.create_all)
    yield m
    await dbmod.dispose_engine()


@pytest.fixture
async def db(maker):
    async with maker() as s:
        yield s


class World:
    """Three users (A maintainer, B and C members) in one project."""

    def __init__(self, db, project, users):
        self.db, self.project, self.users = db, project, users

    def p(self, name: str) -> Principal:
        return Principal.of(self.users[name])


@pytest.fixture
async def world(db) -> World:
    users = {}
    for i, n in enumerate("ABC"):
        users[n] = await identity.upsert_discord_user(db, str(100000000000000000 + i), f"user{n}")
    project = await identity.create_project(db, Principal.of(users["A"]), f"proj-{uuid.uuid4().hex[:6]}",
                                            goals="Ship the collaboration platform")
    for n in "BC":
        await identity.add_member(db, Principal.of(users["A"]), project.id, users[n].id, ProjectRole.member)
    await db.commit()
    return World(db, project, users)
