"""Loads clients from Postgres into a `ClientsCache`, both once at startup and
repeatedly on a schedule owned by `core/scheduler.py`. Mirrors
`services/rules_loader.py` exactly.
"""
import logging

from core.db import get_session_factory
from model.client import Client
from repositories.client_repository import ClientRepository
from services.clients_cache import ClientsCache

logger = logging.getLogger(__name__)


def _serialize_client(client: Client) -> dict:
    return {
        "pk": str(client.id),
        "client_id": client.client_id,
        "status": client.status,
        "scopes": list(client.scopes),
    }


async def fetch_all_clients_from_db() -> list[dict]:
    async with get_session_factory()() as session:
        clients = await ClientRepository(session).list_all()
        return [_serialize_client(client) for client in clients]


async def load_clients_into_cache(cache: ClientsCache) -> list[dict]:
    """Fetch every client from Postgres and fully replace `cache`'s contents.
    Raises on failure — used both by the startup hard-fail path (`main.py`'s
    lifespan, uncaught) and the scheduled poll (`core/scheduler.py`, which
    catches around this call so a failed cycle logs and keeps the last good
    cache instead of crashing the app).
    """
    clients = await fetch_all_clients_from_db()
    cache.load_all(clients)
    logger.debug("Clients poll succeeded: %d clients loaded", len(clients))
    return clients
