"""Step 11: an integrity test asserting the group invariant
(`rule.params == {**group.params, **rule.overrides}`, and matching
`algorithm`/`identifier_types`/`priority`) after every group/member
operation, plus a concurrent-edit test (two overlapping group edits end in a
consistent state, not a corrupted mix of both).
"""
import asyncio

import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from core.settings import settings
from main import app
from repositories.algorithm_repository import AlgorithmRepository
from repositories.rule_group_repository import RuleGroupRepository
from repositories.rule_repository import RuleRepository
from services.rule_group_service import RuleGroupService
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


def _assert_group_invariant(group: dict, members: list[dict]) -> None:
    for member in members:
        expected_params = {**group["params"], **member["overrides"]}
        assert member["params"] == expected_params, (
            f"member {member['endpoint']} params {member['params']} != "
            f"expected {expected_params} from group.params={group['params']} overrides={member['overrides']}"
        )


def test_group_invariant_holds_after_create_patch_members_and_move():
    with TestClient(app) as client:
        algorithm_id = _algorithm_id(client)
        create_response = client.post(
            "/api/v1/groups",
            json={
                "name": "grp-invariant",
                "algorithm_id": algorithm_id,
                "identifier_types": ["api_key"],
                "params": {"limit": 100, "window_seconds": 60},
                "created_by": "jane.doe",
                "members": [{"endpoint": "/i-a"}, {"endpoint": "/i-b", "overrides": {"limit": 20}}],
            },
        )
        group_id = create_response.json()["id"]

        def _snapshot():
            detail = client.get(f"/api/v1/groups/{group_id}").json()
            return detail, detail["members"]

        group, members = _snapshot()
        _assert_group_invariant(group, members)

        client.patch(
            f"/api/v1/groups/{group_id}",
            json={"params": {"limit": 200, "window_seconds": 60}, "updated_by": "jane.doe"},
        )
        group, members = _snapshot()
        _assert_group_invariant(group, members)

        # Add /i-c via the pure-addition endpoint, then remove /i-b via
        # DELETE /rules/{id} — the two operations that replace what the
        # removed PUT .../members used to do in one shot.
        add_response = client.post(
            f"/api/v1/groups/{group_id}/members",
            json={"members": [{"endpoint": "/i-c", "overrides": {"limit": 5}}]},
        )
        assert add_response.status_code == 201
        i_b_rule_id = next(m["rule_id"] for m in members if m["endpoint"] == "/i-b")
        delete_response = client.delete(f"/api/v1/rules/{i_b_rule_id}")
        assert delete_response.status_code == 204

        group, members = _snapshot()
        _assert_group_invariant(group, members)
        assert {m["endpoint"] for m in members} == {"/i-a", "/i-c"}

        rule = client.post(
            "/api/v1/rules",
            json={
                "endpoint": "/i-standalone",
                "identifier_types": ["ip"],
                "algorithm_id": algorithm_id,
                "params": {"limit": 1, "window_seconds": 1},
                "created_by": "jane.doe",
            },
        ).json()
        client.post(
            f"/api/v1/rules/{rule['id']}/move-to-group",
            json={"group_id": group_id, "updated_by": "jane.doe"},
        )
        group, members = _snapshot()
        _assert_group_invariant(group, members)
        moved = next(m for m in members if m["endpoint"] == "/i-standalone")
        assert moved["params"] == group["params"]

        # And every member's algorithm/identifier_types/priority mirror the group's.
        for m in members:
            rule_detail = client.get(f"/api/v1/rules/{m['rule_id']}").json()
            assert rule_detail["algorithm"]["id"] == group["algorithm"]["id"]
            assert rule_detail["identifier_types"] == group["identifier_types"]
            assert rule_detail["priority"] == group["priority"]


async def test_two_overlapping_group_param_edits_end_in_a_consistent_state():
    """Two concurrent `update_group` calls on the same group, both changing
    `params` — the row lock (`SELECT ... FOR UPDATE`) must serialize them so
    the final state matches *one* of the two edits' params exactly, applied
    to *every* member, never a mix (e.g. group.params from edit B with a
    member still holding edit A's recomputed params).
    """
    engine = create_async_engine(get_test_database_url())
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async def _make_service():
        session = session_factory()
        return session, RuleGroupService(RuleGroupRepository(session), RuleRepository(session), AlgorithmRepository(session))

    setup_session, setup_service = await _make_service()
    try:
        from dto.rule_group_dto import GroupMemberInputDTO, RuleGroupCreateRequestDTO, RuleGroupUpdateRequestDTO

        algo_repo = AlgorithmRepository(setup_session)
        algorithms = await algo_repo.list_all()
        fixed_window = next(a for a in algorithms if a.name == "FixedWindow")

        group = await setup_service.create_group(
            RuleGroupCreateRequestDTO(
                name="grp-concurrent",
                algorithm_id=fixed_window.id,
                identifier_types=["api_key"],
                params={"limit": 100, "window_seconds": 60},
                created_by="jane.doe",
                members=[GroupMemberInputDTO(endpoint="/c-a"), GroupMemberInputDTO(endpoint="/c-b")],
            )
        )
        group_id = group.id
    finally:
        await setup_session.close()

    session_a, service_a = await _make_service()
    session_b, service_b = await _make_service()

    async def _edit_a():
        await service_a.update_group(
            group_id, RuleGroupUpdateRequestDTO(params={"limit": 111, "window_seconds": 60}, updated_by="a")
        )

    async def _edit_b():
        await service_b.update_group(
            group_id, RuleGroupUpdateRequestDTO(params={"limit": 222, "window_seconds": 60}, updated_by="b")
        )

    try:
        await asyncio.gather(_edit_a(), _edit_b())
    finally:
        await session_a.close()
        await session_b.close()

    verify_session, verify_service = await _make_service()
    try:
        final_group, final_members = await verify_service.get_group_with_members(group_id)
        assert final_group.params["limit"] in (111, 222)
        for member in final_members:
            assert member.params["limit"] == final_group.params["limit"]
    finally:
        await verify_session.close()

    await engine.dispose()
