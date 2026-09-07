"""Tests for `services/rules_loader.py` — DB fetch + the shared fetch+load
primitive used by both startup and the scheduled poll (see
`tests/test_scheduler.py` for the scheduler-owned wrapper around it).
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from repositories.algorithm_repository import AlgorithmRepository
from repositories.rule_repository import RuleRepository
from services.rules_cache import RulesCache
from services.rules_loader import fetch_all_rules_from_db, load_rules_into_cache
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


# --- load_rules_into_cache: the shared fetch+load primitive -----------------


async def test_load_rules_into_cache_replaces_cache_and_returns_rules(db_session):
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

    cache = RulesCache()
    cache.load_all([])
    loaded = await load_rules_into_cache(cache)

    assert len(loaded) == 1
    assert cache.get_by_lookup_key("/checkout", "user_id", "user-1") is not None


async def test_load_rules_into_cache_raises_on_failure(monkeypatch):
    cache = RulesCache()
    cache.load_all([{"id": "keep-me", "endpoint": "/x", "identifier_type": "global",
                      "identifier_value": None, "algorithm_id": "a", "algorithm_name": "FixedWindow",
                      "params": {}, "status": "active", "priority": 100, "version": 1}])

    async def _boom():
        raise RuntimeError("transient DB failure")

    monkeypatch.setattr("services.rules_loader.fetch_all_rules_from_db", _boom)

    with pytest.raises(RuntimeError, match="transient DB failure"):
        await load_rules_into_cache(cache)

    # Exception propagated before cache.load_all was ever called.
    assert cache.get("keep-me") is not None


# --- Startup wiring: app must fail to boot if the initial load fails -------


def test_app_fails_to_start_if_initial_rules_fetch_fails(monkeypatch):
    async def _boom(cache):
        raise RuntimeError("DB unreachable")

    monkeypatch.setattr("main.load_rules_into_cache", _boom)

    from main import app

    with pytest.raises(RuntimeError, match="DB unreachable"):
        with TestClient(app):
            pytest.fail("app must not finish starting up")


def test_app_starts_and_serves_once_initial_rules_fetch_succeeds():
    from main import app

    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
