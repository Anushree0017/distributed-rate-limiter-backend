"""Loads and registers each algorithm's Lua script.

Caches the returned `Script` object per `(redis client, script name)` so
`register_script()` — which uploads the script and caches its SHA for
`EVALSHA` — runs at most once per script for the process lifetime, no matter
how many endpoints end up using the same algorithm (redis_guidelines.md §4,
§10). This is deliberately dumb: one function, not a class hierarchy.
"""
from pathlib import Path

from redis.asyncio import Redis
from redis.commands.core import AsyncScript

_SCRIPTS_DIR = Path(__file__).parent / "scripts"
_script_cache: dict[tuple[int, str], AsyncScript] = {}


def load_script(redis_client: Redis, script_name: str) -> AsyncScript:
    cache_key = (id(redis_client), script_name)
    cached = _script_cache.get(cache_key)
    if cached is not None:
        return cached

    path = _SCRIPTS_DIR / f"{script_name}.lua"
    script = redis_client.register_script(path.read_text())
    _script_cache[cache_key] = script
    return script
