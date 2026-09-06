"""End-to-end tests for the rules-CRUD HTTP layer, per
`.claude/plans/phase3/api-endpoints.md`. Boots the real app (Redis is still
required at startup even though these endpoints don't touch it) against the
scratch Postgres database from `tests/conftest.py`.
"""
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from main import app
from tests.conftest import get_test_database_url, get_test_redis_url

# `DATABASE_URL` -> scratch DB and per-test engine freshness are handled by
# conftest.py's session-wide autouse fixtures now (needed by every test that
# boots the app, not just this file). This file only needs REDIS_URL set and
# `rules`/`rule_history` truncated between tests.


@pytest_asyncio.fixture(autouse=True)
async def _point_app_at_test_redis_and_clean_up(monkeypatch):
    monkeypatch.setenv("REDIS_URL", get_test_redis_url())
    yield

    engine = create_async_engine(get_test_database_url())
    async with engine.connect() as conn:
        await conn.execute(text("TRUNCATE rule_history, rules RESTART IDENTITY CASCADE"))
        await conn.commit()
    await engine.dispose()


def _get_algorithm_id(client: TestClient) -> str:
    return client.get("/api/v1/algorithms").json()[0]["id"]


def test_identifiers_endpoint_returns_the_static_enum():
    with TestClient(app) as client:
        response = client.get("/api/v1/rules/identifiers")
    assert response.status_code == 200
    assert "global" in response.json()["identifier_types"]
    assert "user_id" in response.json()["identifier_types"]


def test_algorithms_endpoint_lists_seeded_algorithms():
    with TestClient(app) as client:
        response = client.get("/api/v1/algorithms")
    assert response.status_code == 200
    names = {a["name"] for a in response.json()}
    assert "TokenBucket" in names


def test_create_get_update_delete_rule_round_trip():
    with TestClient(app) as client:
        algorithm_id = _get_algorithm_id(client)

        create_response = client.post(
            "/api/v1/rules",
            json={
                "endpoint": "/checkout",
                "identifier_type": "user_id",
                "identifier_value": "user-42",
                "algorithm_id": algorithm_id,
                "params": {"limit": 100, "window_seconds": 60},
                "created_by": "jane.doe",
            },
        )
        assert create_response.status_code == 201
        rule = create_response.json()
        assert rule["version"] == 1
        assert rule["status"] == "active"
        assert rule["algorithm"]["id"] == algorithm_id

        get_response = client.get(f"/api/v1/rules/{rule['id']}")
        assert get_response.status_code == 200
        assert get_response.json()["endpoint"] == "/checkout"

        update_response = client.patch(
            f"/api/v1/rules/{rule['id']}",
            json={"priority": 50, "updated_by": "jane.doe", "expected_version": 1},
        )
        assert update_response.status_code == 200
        updated = update_response.json()
        assert updated["priority"] == 50
        assert updated["version"] == 2

        delete_response = client.delete(f"/api/v1/rules/{rule['id']}")
        assert delete_response.status_code == 204

        assert client.get(f"/api/v1/rules/{rule['id']}").status_code == 404


def test_create_conflicting_scope_returns_409():
    with TestClient(app) as client:
        algorithm_id = _get_algorithm_id(client)
        body = {
            "endpoint": "/orders",
            "identifier_type": "user_id",
            "identifier_value": "user-1",
            "algorithm_id": algorithm_id,
            "created_by": "jane.doe",
        }
        first = client.post("/api/v1/rules", json=body)
        assert first.status_code == 201

        second = client.post("/api/v1/rules", json=body)
        assert second.status_code == 409
        assert second.json()["error"]["code"] == "SCOPE_CONFLICT"


def test_create_missing_identifier_value_returns_422():
    with TestClient(app) as client:
        algorithm_id = _get_algorithm_id(client)
        response = client.post(
            "/api/v1/rules",
            json={
                "endpoint": "/orders",
                "identifier_type": "user_id",
                "algorithm_id": algorithm_id,
                "created_by": "jane.doe",
            },
        )
    assert response.status_code == 422


def test_create_unknown_algorithm_returns_422():
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/rules",
            json={
                "endpoint": "/orders",
                "identifier_type": "global",
                "algorithm_id": "00000000-0000-0000-0000-000000000000",
                "created_by": "jane.doe",
            },
        )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "ALGORITHM_NOT_FOUND"


def test_get_missing_rule_returns_404():
    with TestClient(app) as client:
        response = client.get("/api/v1/rules/00000000-0000-0000-0000-000000000000")
    assert response.status_code == 404


def test_update_version_mismatch_returns_409():
    with TestClient(app) as client:
        algorithm_id = _get_algorithm_id(client)
        create_response = client.post(
            "/api/v1/rules",
            json={
                "endpoint": "/orders",
                "identifier_type": "global",
                "algorithm_id": algorithm_id,
                "created_by": "jane.doe",
            },
        )
        rule_id = create_response.json()["id"]

        response = client.patch(
            f"/api/v1/rules/{rule_id}",
            json={"priority": 1, "updated_by": "jane.doe", "expected_version": 99},
        )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "VERSION_CONFLICT"


def test_list_rules_paginates():
    with TestClient(app) as client:
        algorithm_id = _get_algorithm_id(client)
        for i in range(3):
            client.post(
                "/api/v1/rules",
                json={
                    "endpoint": f"/list-endpoint-{i}",
                    "identifier_type": "global",
                    "algorithm_id": algorithm_id,
                    "created_by": "jane.doe",
                },
            )

        response = client.get("/api/v1/rules", params={"page": 1, "page_size": 2})
    assert response.status_code == 200
    body = response.json()
    assert body["total"] >= 3
    assert len(body["items"]) == 2
