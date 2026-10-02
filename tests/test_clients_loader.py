"""`services/clients_loader.py` against a real Postgres (Phase 6, Step 4's
exit check continued): boot load failure propagates; a successful load
populates the cache; a poll failure (simulated by the scheduler's own
log-and-continue wrapper, tested in test_scheduler.py) keeps the last good
cache.
"""
import pytest

from model.client import Client
from repositories.client_repository import ClientRepository
from services.clients_cache import ClientsCache
from services.clients_loader import load_clients_into_cache


@pytest.mark.asyncio
async def test_load_clients_into_cache_reflects_db_state(db_session):
    repo = ClientRepository(db_session)
    repo.add(Client(client_id="loader-test-client", name="Loader Test", status="active", scopes=["check"]))
    await repo.commit()

    cache = ClientsCache()
    loaded = await load_clients_into_cache(cache)

    assert any(c["client_id"] == "loader-test-client" for c in loaded)
    assert cache.get_by_client_id("loader-test-client") is not None
    assert cache.get_by_client_id("default") is not None
