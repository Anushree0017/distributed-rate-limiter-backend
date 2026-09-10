"""Thin pass-through to `script_loader` — the admin script-reload endpoint has
no business logic beyond triggering a flush + re-registration."""
from redis.asyncio import Redis

from services.rate_limiter.script_loader import flush_and_reregister_scripts


class ScriptService:
    def __init__(self, redis_client: Redis):
        self._redis_client = redis_client

    async def reload_scripts(self) -> list[str]:
        return await flush_and_reregister_scripts(self._redis_client)
