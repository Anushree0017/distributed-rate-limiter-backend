"""End-to-end tests for the `/groups` HTTP layer and the group-related
`/rules/{id}/detach` + `/rules/{id}/move-to-group` endpoints (Phase 5 Part 2).
Boots the real app against the scratch Postgres database from
`tests/conftest.py`, same pattern as `test_rules_api.py`.
"""
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from core.settings import settings
from main import app
from tests.conftest import get_test_database_url, get_test_redis_url


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


def _create_group(client: TestClient, **overrides) -> dict:
    body = {
        "name": "grp-checkout",
        "algorithm_id": _algorithm_id(client),
        "identifier_types": ["api_key"],
        "params": {"limit": 100, "window_seconds": 60},
        "created_by": "jane.doe",
    }
    body.update(overrides)
    response = client.post("/api/v1/groups", json=body)
    assert response.status_code == 201, response.text
    return response.json()


def test_create_group_with_members_and_one_override_computes_effective_params():
    with TestClient(app) as client:
        group = _create_group(
            client,
            members=[
                {"endpoint": "/a"},
                {"endpoint": "/b"},
                {"endpoint": "/c"},
                {"endpoint": "/d", "overrides": {"limit": 300}},
            ],
        )
        detail = client.get(f"/api/v1/groups/{group['id']}").json()
        members_by_endpoint = {m["endpoint"]: m for m in detail["members"]}

        assert members_by_endpoint["/a"]["params"] == {"limit": 100, "window_seconds": 60}
        assert members_by_endpoint["/d"]["params"] == {"limit": 300, "window_seconds": 60}
        assert members_by_endpoint["/d"]["overrides"] == {"limit": 300}
        assert len(detail["members"]) == 4


def test_patch_base_propagates_to_inheriting_members_but_not_overridden_one():
    with TestClient(app) as client:
        group = _create_group(
            client,
            members=[{"endpoint": "/a"}, {"endpoint": "/d", "overrides": {"limit": 300}}],
        )
        before = {m["endpoint"]: m["rule_id"] for m in client.get(f"/api/v1/groups/{group['id']}").json()["members"]}

        patch_response = client.patch(
            f"/api/v1/groups/{group['id']}",
            json={"params": {"limit": 150, "window_seconds": 60}, "updated_by": "jane.doe"},
        )
        assert patch_response.status_code == 200

        detail = client.get(f"/api/v1/groups/{group['id']}").json()
        members_by_endpoint = {m["endpoint"]: m for m in detail["members"]}

        assert members_by_endpoint["/a"]["params"] == {"limit": 150, "window_seconds": 60}
        assert members_by_endpoint["/d"]["params"] == {"limit": 300, "window_seconds": 60}
        # UUIDs unchanged after the edit.
        after = {m["endpoint"]: m["rule_id"] for m in detail["members"]}
        assert before == after


def test_group_name_uniqueness_is_case_insensitive():
    with TestClient(app) as client:
        _create_group(client, name="grp-unique")
        response = client.post(
            "/api/v1/groups",
            json={
                "name": "GRP-UNIQUE",
                "algorithm_id": _algorithm_id(client),
                "identifier_types": ["api_key"],
                "params": {"limit": 100, "window_seconds": 60},
                "created_by": "jane.doe",
            },
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "GROUP_NAME_CONFLICT"


def test_member_override_with_unknown_key_is_rejected_and_writes_nothing():
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/groups",
            json={
                "name": "grp-bad-override",
                "algorithm_id": _algorithm_id(client),
                "identifier_types": ["api_key"],
                "params": {"limit": 100, "window_seconds": 60},
                "created_by": "jane.doe",
                "members": [{"endpoint": "/x", "overrides": {"nope": 1}}],
            },
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "INVALID_OVERRIDE_KEYS"
        assert client.get("/api/v1/groups", params={"name_contains": "grp-bad-override"}).json()["total"] == 0


def test_patch_group_cannot_change_algorithm_or_identifier_types():
    with TestClient(app) as client:
        group = _create_group(client)
        response = client.patch(
            f"/api/v1/groups/{group['id']}",
            json={"algorithm_id": _algorithm_id(client, "TokenBucket"), "updated_by": "jane.doe"},
        )
        assert response.status_code == 422
        response = client.patch(
            f"/api/v1/groups/{group['id']}",
            json={"identifier_types": ["ip"], "updated_by": "jane.doe"},
        )
        assert response.status_code == 422


def test_add_members_appends_without_touching_existing_members():
    with TestClient(app) as client:
        group = _create_group(client, members=[{"endpoint": "/a"}, {"endpoint": "/b"}])
        response = client.post(
            f"/api/v1/groups/{group['id']}/members",
            json={"members": [{"endpoint": "/new"}, {"endpoint": "/new2", "overrides": {"limit": 5}}]},
        )
        assert response.status_code == 201
        body = response.json()
        assert {e["endpoint"] for e in body["created"]} == {"/new", "/new2"}
        assert body["conflicts"] == []

        detail = client.get(f"/api/v1/groups/{group['id']}").json()
        endpoints = {m["endpoint"] for m in detail["members"]}
        assert endpoints == {"/a", "/b", "/new", "/new2"}
        new2 = next(m for m in detail["members"] if m["endpoint"] == "/new2")
        assert new2["params"]["limit"] == 5


def test_add_members_conflict_reports_all_and_writes_nothing():
    with TestClient(app) as client:
        algorithm_id = _algorithm_id(client)
        # A standalone rule that will conflict with the group's identifier scope.
        client.post(
            "/api/v1/rules",
            json={
                "endpoint": "/taken",
                "identifier_types": ["api_key"],
                "algorithm_id": algorithm_id,
                "params": {"limit": 10, "window_seconds": 60},
                "created_by": "jane.doe",
            },
        )
        group = _create_group(client, members=[{"endpoint": "/a"}])

        response = client.post(
            f"/api/v1/groups/{group['id']}/members",
            json={"members": [{"endpoint": "/ok"}, {"endpoint": "/taken"}]},
        )
        assert response.status_code == 409
        body = response.json()
        assert body["created"] == []
        assert body["conflicts"][0]["endpoint"] == "/taken"

        # Nothing written: /a is still the only member, /ok wasn't created either
        # (all-or-nothing even though only /taken conflicted).
        detail = client.get(f"/api/v1/groups/{group['id']}").json()
        assert {m["endpoint"] for m in detail["members"]} == {"/a"}


def test_add_members_rejects_invalid_override_key_and_writes_nothing():
    with TestClient(app) as client:
        group = _create_group(client, members=[{"endpoint": "/a"}])
        response = client.post(
            f"/api/v1/groups/{group['id']}/members",
            json={"members": [{"endpoint": "/new", "overrides": {"nope": 1}}]},
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "INVALID_OVERRIDE_KEYS"

        detail = client.get(f"/api/v1/groups/{group['id']}").json()
        assert {m["endpoint"] for m in detail["members"]} == {"/a"}


def test_detach_member_leaves_group_intact_and_rule_becomes_standalone():
    with TestClient(app) as client:
        algorithm_id = _algorithm_id(client, "TokenBucket")
        group = _create_group(client, members=[{"endpoint": "/a"}, {"endpoint": "/b"}])
        rule_id = next(m["rule_id"] for m in client.get(f"/api/v1/groups/{group['id']}").json()["members"] if m["endpoint"] == "/a")

        response = client.patch(
            f"/api/v1/rules/{rule_id}/detach",
            json={"algorithm": "TokenBucket", "params": {"capacity": 10, "refill_rate": 2}},
        )
        assert response.status_code == 200
        detached = response.json()
        assert detached["group_id"] is None
        assert detached["overrides"] is None
        assert detached["id"] == rule_id  # UUID unchanged
        assert detached["algorithm"]["id"] == algorithm_id
        assert detached["params"] == {"capacity": 10, "refill_rate": 2}

        detail = client.get(f"/api/v1/groups/{group['id']}").json()
        assert {m["endpoint"] for m in detail["members"]} == {"/b"}


def test_delete_group_detach_mode_keeps_member_rules_standalone():
    with TestClient(app) as client:
        group = _create_group(client, members=[{"endpoint": "/a"}])
        rule_id = client.get(f"/api/v1/groups/{group['id']}").json()["members"][0]["rule_id"]

        response = client.delete(f"/api/v1/groups/{group['id']}", params={"members": "detach"})
        assert response.status_code == 204

        assert client.get(f"/api/v1/groups/{group['id']}").status_code == 404
        rule = client.get(f"/api/v1/rules/{rule_id}").json()
        assert rule["group_id"] is None
        assert rule["status"] == "active"


def test_delete_group_delete_mode_removes_member_rules():
    with TestClient(app) as client:
        group = _create_group(client, name="grp-delete-mode", members=[{"endpoint": "/a"}])
        rule_id = client.get(f"/api/v1/groups/{group['id']}").json()["members"][0]["rule_id"]

        response = client.delete(f"/api/v1/groups/{group['id']}", params={"members": "delete"})
        assert response.status_code == 204

        assert client.get(f"/api/v1/rules/{rule_id}").status_code == 404


def test_check_on_member_endpoint_returns_effective_limit(monkeypatch):
    import time

    # Group members are just flat `rules` rows — `/check` only sees them
    # after the next rules-cache poll, same as any other rule change. Poll
    # fast so the test doesn't wait on the real (15 min default) interval.
    # `time.sleep` (not an awaited call) is deliberate: TestClient runs the
    # app's lifespan/scheduler on its own background loop/thread, so
    # `await`ing a DB call from *this* test's loop would attach to a
    # different event loop than the one the app's engine was built on.
    monkeypatch.setenv("RULES_POLL_INTERVAL_SECONDS", "1")
    settings.reload()

    with TestClient(app) as client:
        _create_group(
            client,
            name="grp-effective-check",
            members=[{"endpoint": "/effective-check", "overrides": {"limit": 7}}],
        )
        time.sleep(1.5)

        response = client.post(
            "/api/v1/check",
            json={
                "endpoint": "/effective-check",
                "identifiers": [{"type": "api_key", "value": "check-key-abc123"}],
            },
        )
        assert response.status_code == 200
        assert response.json()["limit"] == 7


def test_create_standalone_rule_move_into_group_then_move_to_another_group():
    with TestClient(app) as client:
        algorithm_id = _algorithm_id(client)
        rule = client.post(
            "/api/v1/rules",
            json={
                "endpoint": "/movable",
                "identifier_types": ["ip"],
                "algorithm_id": algorithm_id,
                "params": {"limit": 10, "window_seconds": 30},
                "created_by": "jane.doe",
            },
        ).json()
        rule_id, endpoint = rule["id"], rule["endpoint"]

        group_a = _create_group(client, name="grp-move-a", identifier_types=["api_key"])
        move_response = client.post(
            f"/api/v1/rules/{rule_id}/move-to-group",
            json={"group_id": group_a["id"], "updated_by": "jane.doe"},
        )
        assert move_response.status_code == 200
        moved = move_response.json()
        assert moved["id"] == rule_id
        assert moved["endpoint"] == endpoint
        assert moved["group_id"] == group_a["id"]
        assert moved["identifier_types"] == ["api_key"]
        assert moved["params"] == {"limit": 100, "window_seconds": 60}  # group's base params

        # A member of group_a can take its own overrides.
        override_response = client.patch(
            f"/api/v1/rules/{rule_id}", json={"overrides": {"limit": 55}, "updated_by": "jane.doe"}
        )
        assert override_response.status_code == 200
        assert override_response.json()["params"]["limit"] == 55

        group_b = _create_group(client, name="grp-move-b", identifier_types=["api_key"])
        move_again = client.post(
            f"/api/v1/rules/{rule_id}/move-to-group",
            json={"group_id": group_b["id"], "updated_by": "jane.doe"},
        )
        assert move_again.status_code == 200
        assert move_again.json()["group_id"] == group_b["id"]
        assert move_again.json()["id"] == rule_id
        assert move_again.json()["endpoint"] == endpoint


def test_move_to_group_conflicting_scope_is_rejected_and_writes_nothing():
    with TestClient(app) as client:
        algorithm_id = _algorithm_id(client)
        # Standalone rule at /conflict-move for api_key.
        client.post(
            "/api/v1/rules",
            json={
                "endpoint": "/conflict-move",
                "identifier_types": ["api_key"],
                "algorithm_id": algorithm_id,
                "params": {"limit": 10, "window_seconds": 60},
                "created_by": "jane.doe",
            },
        )
        # A second, standalone rule at the same endpoint but a different
        # identifier scope, which we'll try to move into an api_key group.
        rule_to_move = client.post(
            "/api/v1/rules",
            json={
                "endpoint": "/conflict-move",
                "identifier_types": ["ip"],
                "algorithm_id": algorithm_id,
                "params": {"limit": 10, "window_seconds": 60},
                "created_by": "jane.doe",
            },
        ).json()

        group = _create_group(client, name="grp-conflict-move", identifier_types=["api_key"])
        response = client.post(
            f"/api/v1/rules/{rule_to_move['id']}/move-to-group",
            json={"group_id": group["id"], "updated_by": "jane.doe"},
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "SCOPE_CONFLICT"

        # Nothing written: rule is still standalone with its original scope.
        unchanged = client.get(f"/api/v1/rules/{rule_to_move['id']}").json()
        assert unchanged["group_id"] is None
        assert unchanged["identifier_types"] == ["ip"]
