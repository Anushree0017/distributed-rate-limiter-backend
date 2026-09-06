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
    import asyncio
    import sys

    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        check=True,
        env={**os.environ, "DATABASE_URL": get_test_database_url()},
    )

    # Data-only seed migrations (e.g. 0005_seed_sample_rules) are for real
    # environments — the test suite wants an empty `rules` table to start
    # from. `algorithms` is left seeded (reference data the tests rely on).
    async def _clear_rules():
        engine = create_async_engine(get_test_database_url())
        async with engine.connect() as conn:
            await conn.execute(text("TRUNCATE rule_history, rules RESTART IDENTITY CASCADE"))
            await conn.commit()
        await engine.dispose()

    asyncio.run(_clear_rules())


@pytest.fixture(scope="session", autouse=True)
def _point_every_test_at_the_scratch_database():
    """`main.py`'s lifespan now loads the rules cache from `DATABASE_URL` on
    *every* app boot (Phase 3 Part 2), not just in rules-CRUD-specific test
    files — so every test that boots the app via `TestClient` (test_health.py,
    test_integration.py, ...) needs `DATABASE_URL` pointed at the scratch
    database too, not just the dev one. Session-scoped so it's set before any
    test module imports/boots the app.
    """
    original = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = get_test_database_url()
    try:
        yield
    finally:
        if original is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = original


@pytest_asyncio.fixture(autouse=True)
async def _fresh_db_engine_per_test():
    """`core/db.py` caches its engine/session-factory at module scope, bound
    to whichever event loop created them — but each test gets its own event
    loop via `pytest-asyncio`. Force a fresh engine every test so an
    asyncpg connection is never reused across event loops.
    """
    import core.db

    core.db._engine = None
    core.db._session_factory = None
    yield
    core.db._engine = None
    core.db._session_factory = None


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
