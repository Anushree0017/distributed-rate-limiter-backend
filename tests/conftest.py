"""Shared fixtures for Redis-backed tests.

Every algorithm here runs its check-and-increment logic as a Lua script that
calls `redis.call("TIME")`, and `fakeredis`'s EVAL/TIME fidelity is too
incomplete to trust for that (redis_guidelines.md §11) — so these tests run
against a real, already-running local Redis instance rather than a fake.

Point `TEST_REDIS_URL` at a scratch database if the default (db 15 on
localhost, kept separate from `REDIS_URL`'s db 0 so tests never collide with
dev data) doesn't fit your setup. Start Redis locally with:

    docker compose up -d redis

Each test gets a freshly flushed database.
"""
import os

import pytest_asyncio
from redis.asyncio import Redis

_DEFAULT_TEST_REDIS_URL = "redis://localhost:6379/15"


def get_test_redis_url() -> str:
    """Public so app-level tests (`test_health.py`, `test_integration.py`)
    can point `REDIS_URL` at the same scratch database before booting the
    full app via `TestClient`.
    """
    return os.getenv("TEST_REDIS_URL", _DEFAULT_TEST_REDIS_URL)


@pytest_asyncio.fixture
async def redis_client():
    client = Redis.from_url(get_test_redis_url())
    await client.flushdb()
    try:
        yield client
    finally:
        await client.flushdb()
        await client.aclose()


# ---------------------------------------------------------------------------
# Postgres fixtures for the rules-CRUD service (Phase 3).
#
# Same philosophy as the Redis fixtures above: a real, already-running local
# Postgres rather than a fake/testcontainer (project convention — see
# CLAUDE.md's "no testcontainers" note for Redis, which applies here too).
# Point `TEST_DATABASE_URL` at a scratch database if the default (a
# `rate_limiter_test` database on localhost, kept separate from the dev
# `DATABASE_URL`) doesn't fit your setup. Create it once with:
#
#     docker exec <postgres-container> psql -U postgres -c "CREATE DATABASE rate_limiter_test;"
#
# Migrations run once per test session; `rules`/`rule_history` are truncated
# after every test so tests never see each other's rows. `algorithms` is
# left alone — it's seeded reference data, not per-test state.
# ---------------------------------------------------------------------------
import subprocess

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

_DEFAULT_TEST_DATABASE_URL = "postgresql+asyncpg://postgres:postgres@localhost:5432/rate_limiter_test"


def get_test_database_url() -> str:
    """Public so app-level tests can point `DATABASE_URL` at the same
    scratch database before booting the full app via `TestClient`.
    """
    return os.getenv("TEST_DATABASE_URL", _DEFAULT_TEST_DATABASE_URL)


@pytest.fixture(scope="session", autouse=True)
def _run_migrations():
    import sys

    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        check=True,
        env={**os.environ, "DATABASE_URL": get_test_database_url()},
    )


@pytest_asyncio.fixture
async def db_session():
    engine = create_async_engine(get_test_database_url())
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        try:
            yield session
        finally:
            await session.rollback()
            await session.execute(text("TRUNCATE rule_history, rules RESTART IDENTITY CASCADE"))
            await session.commit()
    await engine.dispose()
