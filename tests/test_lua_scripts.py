"""Raw `EVAL` tests per Lua script, bypassing the Python wrapper classes
entirely — isolates Lua logic bugs from Python/redis-py integration bugs
(redis_guidelines.md §11).
"""
import uuid
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).parent.parent / "services" / "rate_limiter" / "scripts"


def _load(name: str) -> str:
    return (_SCRIPTS_DIR / f"{name}.lua").read_text()


async def test_fixed_window_allow_deny_boundary(redis_client):
    script = _load("fixed_window")
    key = "rl:fixed_window:test:client_id:c1"

    first = await redis_client.eval(script, 1, key, 1000, 2)
    second = await redis_client.eval(script, 1, key, 1000, 2)
    third = await redis_client.eval(script, 1, key, 1000, 2)

    assert first[0] == 1 and second[0] == 1
    assert third[0] == 0
    assert third[3] > 0  # retry_after_ms


async def test_fixed_window_sets_ttl(redis_client):
    script = _load("fixed_window")
    key = "rl:fixed_window:test:client_id:c2"

    await redis_client.eval(script, 1, key, 1000, 5)

    ttl_ms = await redis_client.pttl(key)
    assert 0 < ttl_ms <= 1000


async def test_token_bucket_allow_deny_boundary(redis_client):
    script = _load("token_bucket")
    key = "rl:token_bucket:test:client_id:c1"

    first = await redis_client.eval(script, 1, key, 1, 0.001)
    second = await redis_client.eval(script, 1, key, 1, 0.001)

    assert first[0] == 1
    assert second[0] == 0
    assert second[3] > 0


async def test_leaky_bucket_allow_deny_boundary(redis_client):
    script = _load("leaky_bucket")
    key = "rl:leaky_bucket:test:client_id:c1"

    first = await redis_client.eval(script, 1, key, 1, 0.001)
    second = await redis_client.eval(script, 1, key, 1, 0.001)

    assert first[0] == 1
    assert second[0] == 0
    assert second[3] > 0


async def test_sliding_window_counter_allow_deny_boundary(redis_client):
    script = _load("sliding_window_counter")
    key = "rl:sliding_window_counter:test:client_id:c1"

    first = await redis_client.eval(script, 1, key, 60_000, 1)
    second = await redis_client.eval(script, 1, key, 60_000, 1)

    assert first[0] == 1
    assert second[0] == 0


async def test_sliding_window_log_allow_deny_boundary(redis_client):
    script = _load("sliding_window_log")
    key = "rl:sliding_window_log:test:client_id:c1"

    first = await redis_client.eval(script, 1, key, 1000, 2, uuid.uuid4().hex)
    second = await redis_client.eval(script, 1, key, 1000, 2, uuid.uuid4().hex)
    third = await redis_client.eval(script, 1, key, 1000, 2, uuid.uuid4().hex)

    assert first[0] == 1 and second[0] == 1
    assert third[0] == 0
    assert third[3] > 0


async def test_sliding_window_log_rejects_duplicate_request_id(redis_client):
    """A repeated member (should never happen in practice — Python generates
    a fresh uuid4 per call) collapses to one ZSET entry rather than erroring,
    since ZADD on an existing member just updates its score.
    """
    script = _load("sliding_window_log")
    key = "rl:sliding_window_log:test:client_id:c2"
    same_id = uuid.uuid4().hex

    await redis_client.eval(script, 1, key, 1000, 5, same_id)
    await redis_client.eval(script, 1, key, 1000, 5, same_id)

    assert await redis_client.zcard(key) == 1
