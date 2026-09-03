"""Liveness endpoint — confirms the process booted and its config loaded.

Kept cheap: `redis_connected` is a single `PING` (sub-millisecond), not a
full `INFO` call — this is likely polled frequently by orchestration tooling
(load balancer / k8s probes), so it stays fast rather than pulling full Redis
diagnostics on every hit. For that, see `GET /api/v1/redis/health`.
"""
from fastapi import APIRouter, Depends
from redis.asyncio import Redis

from core.dependencies import get_redis
from core.redis_client import ping

router = APIRouter()


@router.get("/health")
async def health(redis_client: Redis = Depends(get_redis)) -> dict:
    return {"status": "ok", "redis_connected": await ping(redis_client)}
