"""Redis diagnostics endpoint — heavier than `GET /health`'s single `PING`.
Meant for humans/dashboards checking in during incidents or load testing, not
for automated polling on a tight interval (see `api/health.py`).
"""
from fastapi import APIRouter, Depends
from redis.asyncio import Redis

from core.dependencies import get_redis
from core.security.auth_dependency import require_scope

# Not explicitly listed in the plan's endpoint table — treated as admin
# plane: it exposes Redis internals (memory, connected clients, replication
# role), diagnostic/operational like the rules/groups/clients API, not data
# plane. See CLAUDE.md's Phase 6 deviations.
router = APIRouter(dependencies=[Depends(require_scope("admin"))])


@router.get("/redis/health")
async def redis_health(redis_client: Redis = Depends(get_redis)) -> dict:
    memory = await redis_client.info(section="memory")
    clients = await redis_client.info(section="clients")
    stats = await redis_client.info(section="stats")
    replication = await redis_client.info(section="replication")

    return {
        "used_memory": memory.get("used_memory"),
        "used_memory_peak": memory.get("used_memory_peak"),
        "maxmemory_policy": memory.get("maxmemory_policy"),
        "connected_clients": clients.get("connected_clients"),
        "evicted_keys": stats.get("evicted_keys"),
        "expired_keys": stats.get("expired_keys"),
        "role": replication.get("role"),
    }
