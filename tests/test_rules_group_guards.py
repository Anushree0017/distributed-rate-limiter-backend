"""Guards the existing `/rules` API gained once a rule can be grouped (Phase
5 Part 2, Step 10): a grouped rule's `algorithm`/`params`/`priority` are
rejected on `PATCH /rules/{id}` (managed by the group), `overrides` is only
valid on a grouped rule, `POST /rules` can't set `group_id`/`overrides`
directly, and `DELETE /rules/{id}` on a member leaves the group intact.
"""
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from core.settings import settings
from main import app
from tests.conftest import admin_auth_headers, get_test_database_url, get_test_redis_url

_HEADERS = admin_auth_headers()


@pytest_asyncio.fixture(autouse=True)
async def _point_app_at_test_redis_and_clean_up(monkeypatch):
    monkeypatch.setenv("REDIS_URL", get_test_redis_url())
    settings.reload()
    yield

    engine = create_async_engine(get_test_database_url())
    async with engine.connect() as conn:
        await conn.execute(text("TRUNCATE rule_groups, rule_history, rules RESTART IDENTITY CASCADE"))
        await conn.commit()
    await engine.dispose()


def _algorithm_id(client: TestClient, name: str = "FixedWindow") -> str:
    algorithms = client.get("/api/v1/algorithms").json()
    return next(a["id"] for a in algorithms if a["name"] == name)


def _grouped_rule(client: TestClient) -> dict:
    algorithm_id = _algorithm_id(client)
    group = client.post(
        "/api/v1/groups",
        json={
            "client_id": "default",
            "name": "grp-guard-test",
            "algorithm_id": algorithm_id,
            "identifier_types": ["api_key"],
            "params": {"limit": 100, "window_seconds": 60},
            "created_by": "jane.doe",
            "members": [{"endpoint": "/guarded"}],
        },
    ).json()
    return client.get(f"/api/v1/groups/{group['id']}").json()["members"][0]


def test_post_rules_rejects_group_id_field():
    with TestClient(app) as client:
        client.headers.update(_HEADERS)
        response = client.post(
            "/api/v1/rules",
            json={
                "client_id": "default",
                "endpoint": "/x",
                "identifier_types": ["global"],
                "algorithm_id": _algorithm_id(client),
                "created_by": "jane.doe",
                "group_id": "00000000-0000-0000-0000-000000000000",
            },
        )
        assert response.status_code == 422


def test_post_rules_rejects_overrides_field():
    with TestClient(app) as client:
        client.headers.update(_HEADERS)
        response = client.post(
            "/api/v1/rules",
            json={
                "client_id": "default",
                "endpoint": "/x",
                "identifier_types": ["global"],
                "algorithm_id": _algorithm_id(client),
                "created_by": "jane.doe",
                "overrides": {},
            },
        )
        assert response.status_code == 422


def test_patch_params_on_grouped_rule_is_rejected():
    with TestClient(app) as client:
        client.headers.update(_HEADERS)
        member = _grouped_rule(client)
        response = client.patch(
            f"/api/v1/rules/{member['rule_id']}",
            json={"params": {"limit": 1, "window_seconds": 1}, "updated_by": "jane.doe"},
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "RULE_MANAGED_BY_GROUP"


def test_patch_priority_on_grouped_rule_is_rejected():
    with TestClient(app) as client:
        client.headers.update(_HEADERS)
        member = _grouped_rule(client)
        response = client.patch(
            f"/api/v1/rules/{member['rule_id']}",
            json={"priority": 1, "updated_by": "jane.doe"},
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "RULE_MANAGED_BY_GROUP"


def test_patch_overrides_on_standalone_rule_is_rejected():
    with TestClient(app) as client:
        client.headers.update(_HEADERS)
        rule = client.post(
            "/api/v1/rules",
            json={
                "client_id": "default",
                "endpoint": "/standalone",
                "identifier_types": ["global"],
                "algorithm_id": _algorithm_id(client),
                "params": {"limit": 10, "window_seconds": 10},
                "created_by": "jane.doe",
            },
        ).json()
        response = client.patch(
            f"/api/v1/rules/{rule['id']}",
            json={"overrides": {"limit": 5}, "updated_by": "jane.doe"},
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "OVERRIDES_REQUIRE_GROUP"


def test_patch_overrides_on_grouped_rule_recomputes_params():
    with TestClient(app) as client:
        client.headers.update(_HEADERS)
        member = _grouped_rule(client)
        response = client.patch(
            f"/api/v1/rules/{member['rule_id']}",
            json={"overrides": {"limit": 33}, "updated_by": "jane.doe"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["overrides"] == {"limit": 33}
        assert body["params"] == {"limit": 33, "window_seconds": 60}


def test_patch_status_on_grouped_rule_still_allowed():
    with TestClient(app) as client:
        client.headers.update(_HEADERS)
        member = _grouped_rule(client)
        response = client.patch(
            f"/api/v1/rules/{member['rule_id']}",
            json={"status": "inactive", "updated_by": "jane.doe"},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "inactive"


def test_delete_member_rule_leaves_group_intact():
    with TestClient(app) as client:
        client.headers.update(_HEADERS)
        member = _grouped_rule(client)
        group_id = client.get(f"/api/v1/rules/{member['rule_id']}").json()["group_id"]

        response = client.delete(f"/api/v1/rules/{member['rule_id']}")
        assert response.status_code == 204

        group_response = client.get(f"/api/v1/groups/{group_id}")
        assert group_response.status_code == 200
        assert group_response.json()["members"] == []


def test_detach_standalone_rule_is_rejected():
    with TestClient(app) as client:
        client.headers.update(_HEADERS)
        rule = client.post(
            "/api/v1/rules",
            json={
                "client_id": "default",
                "endpoint": "/standalone2",
                "identifier_types": ["global"],
                "algorithm_id": _algorithm_id(client),
                "params": {"limit": 10, "window_seconds": 10},
                "created_by": "jane.doe",
            },
        ).json()
        response = client.patch(
            f"/api/v1/rules/{rule['id']}/detach",
            json={"algorithm": "FixedWindow", "params": {"limit": 5, "window_seconds": 5}},
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "RULE_NOT_IN_GROUP"


def test_detach_with_unknown_algorithm_name_is_rejected():
    with TestClient(app) as client:
        client.headers.update(_HEADERS)
        member = _grouped_rule(client)
        response = client.patch(
            f"/api/v1/rules/{member['rule_id']}/detach",
            json={"algorithm": "NotARealAlgorithm", "params": {}},
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "ALGORITHM_NOT_FOUND"


def test_detach_with_invalid_params_for_chosen_algorithm_is_rejected_and_writes_nothing():
    with TestClient(app) as client:
        client.headers.update(_HEADERS)
        member = _grouped_rule(client)
        response = client.patch(
            f"/api/v1/rules/{member['rule_id']}/detach",
            json={"algorithm": "TokenBucket", "params": {"totally": "wrong"}},
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "INVALID_RULE_PARAMS"

        # Nothing changed: still grouped.
        unchanged = client.get(f"/api/v1/rules/{member['rule_id']}").json()
        assert unchanged["group_id"] is not None


def test_move_to_group_rejects_invalid_override_key():
    with TestClient(app) as client:
        client.headers.update(_HEADERS)
        algorithm_id = _algorithm_id(client)
        rule = client.post(
            "/api/v1/rules",
            json={
                "client_id": "default",
                "endpoint": "/to-move",
                "identifier_types": ["global"],
                "algorithm_id": algorithm_id,
                "params": {"limit": 10, "window_seconds": 10},
                "created_by": "jane.doe",
            },
        ).json()
        group = client.post(
            "/api/v1/groups",
            json={
                "client_id": "default",
                "name": "grp-move-bad-override",
                "algorithm_id": algorithm_id,
                "identifier_types": ["global"],
                "params": {"limit": 100, "window_seconds": 60},
                "created_by": "jane.doe",
            },
        ).json()
        response = client.post(
            f"/api/v1/rules/{rule['id']}/move-to-group",
            json={"group_id": group["id"], "overrides": {"nope": 1}, "updated_by": "jane.doe"},
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "INVALID_OVERRIDE_KEYS"
