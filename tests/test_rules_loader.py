"""Tests for `services/rules_loader.py` — fetch + poll loop. Steps 2-4 of
plan-part2.md's testing plan.
"""
import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from repositories.algorithm_repository import AlgorithmRepository
from repositories.rule_repository import RuleRepository
from services.rules_cache import RulesCache
from services.rules_loader import fetch_all_rules_from_db, run_poll_loop
from tests.conftest import get_test_database_url, get_test_redis_url


@pytest.fixture(autouse=True)
async def _redis_env(monkeypatch):
    monkeypatch.setenv("REDIS_URL", get_test_redis_url())
    yield


@pytest.fixture(autouse=True)
async def _cleanup_rules():
    yield
    engine = create_async_engine(get_test_database_url())
    async with engine.connect() as conn:
        await conn.execute(text("TRUNCATE rule_history, rules RESTART IDENTITY CASCADE"))
        await conn.commit()
    await engine.dispose()


async def test_fetch_all_rules_from_db_returns_plain_dicts(db_session):
    algorithm = (await AlgorithmRepository(db_session).list_all())[0]
    from model.rule import Rule
    from model.rule_status import RuleStatus

    await RuleRepository(db_session).create(
        Rule(
            endpoint="/checkout",
            identifier_type="user_id",
            identifier_value="user-1",
            algorithm_id=algorithm.id,
            params={"limit": 5},
            status=RuleStatus.ACTIVE.value,
            priority=100,
            version=1,
            created_by="jane.doe",
        )
    )

    rules = await fetch_all_rules_from_db()
    assert len(rules) == 1
    rule = rules[0]
    assert isinstance(rule["id"], str)
    assert rule["endpoint"] == "/checkout"
    assert rule["algorithm_name"] == algorithm.name
    assert rule["params"] == {"limit": 5}


# --- Startup wiring: app must fail to boot if the initial fetch fails ------


def test_app_fails_to_start_if_initial_rules_fetch_fails(monkeypatch):
    async def _boom():
        raise RuntimeError("DB unreachable")

    monkeypatch.setattr("main.fetch_all_rules_from_db", _boom)

    from main import app

    with pytest.raises(RuntimeError, match="DB unreachable"):
        with TestClient(app):
            pytest.fail("app must not finish starting up")


def test_app_starts_and_serves_once_initial_rules_fetch_succeeds():
    from main import app

    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200


# --- Polling correctness / resilience --------------------------------------


async def test_poll_loop_picks_up_a_new_rule_within_one_interval(db_session):
    cache = RulesCache()
    cache.load_all([])
    task = asyncio.create_task(run_poll_loop(cache, interval_seconds=0.05))
    try:
        await asyncio.sleep(0.02)
        assert cache.get_by_lookup_key("/checkout", "user_id", "user-1") is None

        algorithm = (await AlgorithmRepository(db_session).list_all())[0]
        from model.rule import Rule
        from model.rule_status import RuleStatus

        await RuleRepository(db_session).create(
            Rule(
                endpoint="/checkout",
                identifier_type="user_id",
                identifier_value="user-1",
                algorithm_id=algorithm.id,
                params={"limit": 5},
                status=RuleStatus.ACTIVE.value,
                priority=100,
                version=1,
                created_by="jane.doe",
            )
        )

        await asyncio.sleep(0.2)
        assert cache.get_by_lookup_key("/checkout", "user_id", "user-1") is not None
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_poll_loop_survives_a_failed_cycle_and_recovers(monkeypatch):
    cache = RulesCache()
    cache.load_all([{"id": "keep-me", "endpoint": "/x", "identifier_type": "global",
                      "identifier_value": None, "algorithm_id": "a", "algorithm_name": "FixedWindow",
                      "params": {}, "status": "active", "priority": 100, "version": 1}])

    calls = {"n": 0}

    async def _flaky_fetch():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient DB failure")
        return []

    monkeypatch.setattr("services.rules_loader.fetch_all_rules_from_db", _flaky_fetch)

    task = asyncio.create_task(run_poll_loop(cache, interval_seconds=0.05))
    try:
        # First cycle fails -> cache must be untouched.
        await asyncio.sleep(0.08)
        assert cache.get("keep-me") is not None

        # Second cycle succeeds -> full replace (empty list), cache updates.
        await asyncio.sleep(0.08)
        assert cache.get("keep-me") is None
        assert calls["n"] >= 2
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_poll_loop_reraises_cancellation():
    cache = RulesCache()
    cache.load_all([])
    task = asyncio.create_task(run_poll_loop(cache, interval_seconds=60))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
