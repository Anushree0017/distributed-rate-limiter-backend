import uuid
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.exc import IntegrityError

from core.exceptions import AlgorithmNotFoundError, RuleNotFoundError, ScopeConflictError, VersionConflictError
from dto.rule_dto import RuleCreateRequest, RuleUpdateRequest
from model.rule import Rule
from model.rule_status import RuleStatus
from services.rule_service import RuleService


def _rule(**overrides) -> Rule:
    defaults = dict(
        id=uuid.uuid4(),
        endpoint="/checkout",
        identifier_type="user_id",
        identifier_value="user-1",
        algorithm_id=uuid.uuid4(),
        params={"limit": 100},
        status=RuleStatus.ACTIVE.value,
        priority=100,
        version=1,
        created_by="jane.doe",
        updated_by=None,
    )
    defaults.update(overrides)
    return Rule(**defaults)


def _service(rule_repo=None, algorithm_repo=None) -> RuleService:
    return RuleService(rule_repo or AsyncMock(), algorithm_repo or AsyncMock())


async def test_create_rejects_unknown_algorithm():
    algorithm_repo = AsyncMock()
    algorithm_repo.get_by_id.return_value = None
    service = _service(algorithm_repo=algorithm_repo)

    request = RuleCreateRequest(
        endpoint="/checkout",
        identifier_type="user_id",
        identifier_value="user-1",
        algorithm_id=uuid.uuid4(),
        created_by="jane.doe",
    )
    with pytest.raises(AlgorithmNotFoundError):
        await service.create_rule(request)


def test_create_request_requires_identifier_value_unless_global():
    with pytest.raises(ValueError):
        RuleCreateRequest(
            endpoint="/checkout",
            identifier_type="user_id",
            algorithm_id=uuid.uuid4(),
            created_by="jane.doe",
        )

    # global is fine without identifier_value
    RuleCreateRequest(
        endpoint="/checkout",
        identifier_type="global",
        algorithm_id=uuid.uuid4(),
        created_by="jane.doe",
    )


async def test_create_maps_db_race_to_scope_conflict():
    algorithm_repo = AsyncMock()
    algorithm_repo.get_by_id.return_value = object()
    rule_repo = AsyncMock()
    rule_repo.create.side_effect = IntegrityError("stmt", {}, Exception("dup"))
    service = _service(rule_repo, algorithm_repo)

    request = RuleCreateRequest(
        endpoint="/checkout",
        identifier_type="user_id",
        identifier_value="user-1",
        algorithm_id=uuid.uuid4(),
        created_by="jane.doe",
    )
    with pytest.raises(ScopeConflictError):
        await service.create_rule(request)


async def test_get_rule_raises_not_found():
    rule_repo = AsyncMock()
    rule_repo.get_by_id.return_value = None
    service = _service(rule_repo)

    with pytest.raises(RuleNotFoundError):
        await service.get_rule(uuid.uuid4())


async def test_update_rejects_version_mismatch():
    rule_repo = AsyncMock()
    rule_repo.get_by_id.return_value = _rule(version=3)
    service = _service(rule_repo)

    with pytest.raises(VersionConflictError):
        await service.update_rule(uuid.uuid4(), RuleUpdateRequest(updated_by="jane.doe", expected_version=2))


async def test_update_activating_rule_checks_for_scope_conflict():
    rule_repo = AsyncMock()
    rule_repo.get_by_id.return_value = _rule(status=RuleStatus.INACTIVE.value)
    rule_repo.find_active_conflict.return_value = _rule()
    service = _service(rule_repo)

    with pytest.raises(ScopeConflictError):
        await service.update_rule(uuid.uuid4(), RuleUpdateRequest(updated_by="jane.doe", status="active"))


async def test_update_bumps_version_and_sets_updated_by():
    rule = _rule(version=1)
    rule_repo = AsyncMock()
    rule_repo.get_by_id.return_value = rule
    rule_repo.find_active_conflict.return_value = None
    rule_repo.update.side_effect = lambda r: r
    service = _service(rule_repo)

    updated = await service.update_rule(rule.id, RuleUpdateRequest(updated_by="jane.doe", priority=50))

    assert updated.version == 2
    assert updated.priority == 50
    assert updated.updated_by == "jane.doe"


async def test_delete_raises_not_found_when_missing():
    rule_repo = AsyncMock()
    rule_repo.get_by_id.return_value = None
    service = _service(rule_repo)

    with pytest.raises(RuleNotFoundError):
        await service.delete_rule(uuid.uuid4())
