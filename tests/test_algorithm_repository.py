from repositories.algorithm_repository import AlgorithmRepository


async def test_list_all_returns_seeded_algorithms(db_session):
    repo = AlgorithmRepository(db_session)
    algorithms = await repo.list_all()
    names = {algorithm.name for algorithm in algorithms}
    assert {"TokenBucket", "FixedWindow", "SlidingWindowLog", "SlidingWindowCounter", "LeakyBucket"} <= names


async def test_get_by_id_returns_none_when_missing(db_session):
    import uuid

    repo = AlgorithmRepository(db_session)
    assert await repo.get_by_id(uuid.uuid4()) is None


async def test_get_by_id_returns_matching_algorithm(db_session):
    repo = AlgorithmRepository(db_session)
    seeded = (await repo.list_all())[0]
    fetched = await repo.get_by_id(seeded.id)
    assert fetched is not None
    assert fetched.name == seeded.name
