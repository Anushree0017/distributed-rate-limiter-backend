"""Redis diagnostics endpoint — heavier than `GET /health`'s single `PING`.
Meant for humans/dashboards checking in during incidents or load testing, not
for automated polling on a tight interval (see `api/health.py`).
"""
from fastapi import APIRouter, Depends
from redis.asyncio import Redis

from core.dependencies import get_redis

router = APIRouter()


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
