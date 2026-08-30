"""Shared async Redis connection pool — built once at process startup
(`main.py`'s lifespan), never per-request. See `redis_guidelines.md` §1.
"""
import logging

from redis.asyncio import ConnectionPool, Redis
from redis.exceptions import RedisError

from core.settings import (
    get_redis_max_connections,
    get_redis_socket_connect_timeout_seconds,
    get_redis_socket_timeout_seconds,
    get_redis_url,
)

logger = logging.getLogger(__name__)


def create_redis_pool() -> ConnectionPool:
    return ConnectionPool.from_url(
        get_redis_url(),
        max_connections=get_redis_max_connections(),
        socket_timeout=get_redis_socket_timeout_seconds(),
        socket_connect_timeout=get_redis_socket_connect_timeout_seconds(),
    )


def get_redis_client(pool: ConnectionPool) -> Redis:
    return Redis(connection_pool=pool)


async def ping(redis_client: Redis) -> bool:
    """Cheap, sub-millisecond connectivity check — used by `/health`. Never
    raises: an unreachable Redis is a `False`, not an exception, since the
    liveness endpoint just reports connectivity, it doesn't fail the process.
    """
    try:
        return bool(await redis_client.ping())
    except RedisError:
        logger.error("Redis PING failed", exc_info=True)
        return False
