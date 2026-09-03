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
