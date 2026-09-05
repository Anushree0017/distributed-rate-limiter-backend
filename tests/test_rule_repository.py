import pytest
from sqlalchemy.exc import IntegrityError

from dto.rule_dto import RuleFilter
from model.rule import Rule
from model.rule_status import RuleStatus
from repositories.algorithm_repository import AlgorithmRepository
from repositories.rule_repository import RuleRepository


async def _an_algorithm_id(db_session):
    algorithms = await AlgorithmRepository(db_session).list_all()
    return algorithms[0].id


def _make_rule(algorithm_id, **overrides) -> Rule:
    defaults = dict(
        endpoint="/checkout",
        identifier_type="user_id",
        identifier_value="user-1",
        algorithm_id=algorithm_id,
        params={"limit": 100},
        status=RuleStatus.ACTIVE.value,
        priority=100,
        version=1,
        created_by="jane.doe",
    )
    defaults.update(overrides)
    return Rule(**defaults)


async def test_create_and_get_by_id(db_session):
    algorithm_id = await _an_algorithm_id(db_session)
    repo = RuleRepository(db_session)

    created = await repo.create(_make_rule(algorithm_id))
    assert created.id is not None
    assert created.algorithm.id == algorithm_id

    fetched = await repo.get_by_id(created.id)
    assert fetched is not None
    assert fetched.endpoint == "/checkout"


async def test_get_by_id_returns_none_when_missing(db_session):
    import uuid

    repo = RuleRepository(db_session)
    assert await repo.get_by_id(uuid.uuid4()) is None


async def test_active_scope_uniqueness_is_enforced_by_the_db(db_session):
    algorithm_id = await _an_algorithm_id(db_session)
    repo = RuleRepository(db_session)
    await repo.create(_make_rule(algorithm_id))

    with pytest.raises(IntegrityError):
        await repo.create(_make_rule(algorithm_id))


async def test_inactive_rule_does_not_block_a_new_active_rule_in_same_scope(db_session):
    algorithm_id = await _an_algorithm_id(db_session)
    repo = RuleRepository(db_session)
    first = await repo.create(_make_rule(algorithm_id))
    first.status = RuleStatus.INACTIVE.value
    await repo.update(first)

    second = await repo.create(_make_rule(algorithm_id))
    assert second.id != first.id


async def test_global_scope_treats_null_identifier_value_as_a_single_slot(db_session):
    algorithm_id = await _an_algorithm_id(db_session)
    repo = RuleRepository(db_session)
    await repo.create(_make_rule(algorithm_id, identifier_type="global", identifier_value=None))

    with pytest.raises(IntegrityError):
        await repo.create(_make_rule(algorithm_id, identifier_type="global", identifier_value=None))


async def test_find_active_conflict_excludes_given_id(db_session):
    algorithm_id = await _an_algorithm_id(db_session)
    repo = RuleRepository(db_session)
    rule = await repo.create(_make_rule(algorithm_id))

    assert await repo.find_active_conflict("/checkout", "user_id", "user-1") is not None
    assert await repo.find_active_conflict("/checkout", "user_id", "user-1", exclude_id=rule.id) is None


async def test_delete_removes_the_row(db_session):
    algorithm_id = await _an_algorithm_id(db_session)
    repo = RuleRepository(db_session)
    rule = await repo.create(_make_rule(algorithm_id))

    await repo.delete(rule)

    assert await repo.get_by_id(rule.id) is None


async def test_list_filters_and_paginates(db_session):
    algorithm_id = await _an_algorithm_id(db_session)
    repo = RuleRepository(db_session)
    for i in range(3):
        await repo.create(
            _make_rule(algorithm_id, endpoint=f"/endpoint-{i}", identifier_value=f"user-{i}")
        )

    items, total = await repo.list(RuleFilter(page=1, page_size=2))
    assert total == 3
    assert len(items) == 2

    items, total = await repo.list(RuleFilter(endpoint="/endpoint-1"))
    assert total == 1
    assert items[0].endpoint == "/endpoint-1"
