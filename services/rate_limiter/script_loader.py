"""Registers every algorithm's Lua script once, at app startup.

`register_all_scripts()` must run exactly once, in `main.py`'s lifespan,
before any `RateLimiter` is constructed. It uploads each script under
`scripts/` to Redis via `SCRIPT LOAD` and caches the resulting `Script`
object so later `RateLimiter.__init__` calls just look it up via
`get_script()` — no algorithm ever registers its own script (redis_guidelines.md
§4, §10). This is deliberately dumb: a few functions, not a class hierarchy.
"""
import logging
from pathlib import Path

from redis.asyncio import Redis
from redis.commands.core import AsyncScript
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)

_SCRIPTS_DIR = Path(__file__).parent / "scripts"
_script_cache: dict[str, AsyncScript] = {}


class ScriptRegistrationError(Exception):
    """Raised when a script fails to load onto Redis at startup."""


async def register_all_scripts(redis_client: Redis) -> list[str]:
    """Upload every script under scripts/ to Redis's script cache via SCRIPT
    LOAD and store the resulting AsyncScript objects for get_script() to hand
    out. Must run exactly once at app startup, before any RateLimiter is
    constructed. Returns the registered script names, in the order they were
    registered.

    Raises ScriptRegistrationError (chained from the original exception) on
    the first failure — a script that fails to load is a boot-blocking
    problem, not something to skip and continue past.
    """
    names = []
    for path in sorted(_SCRIPTS_DIR.glob("*.lua")):
        try:
            script = redis_client.register_script(path.read_text())
            await redis_client.script_load(script.script)
        except (RedisError, OSError) as exc:
            logger.error("Failed to register script %r", path.stem, exc_info=True)
            raise ScriptRegistrationError(f"Failed to register script {path.stem!r}") from exc
        _script_cache[path.stem] = script
        names.append(path.stem)
    return names


async def flush_and_reregister_scripts(redis_client: Redis) -> list[str]:
    """Clear Redis's script cache instance-wide (SCRIPT FLUSH) and this
    process's local cache, then re-upload every script from disk. Callers
    should expect this to affect any other process sharing this Redis
    instance's script cache, not just this app's own scripts."""
    await redis_client.script_flush()
    _script_cache.clear()
    return await register_all_scripts(redis_client)


def get_script(script_name: str) -> AsyncScript:
    """Look up an already-registered script. Raises if register_all_scripts()
    hasn't run yet — a RateLimiter must never be constructed before startup
    registration completes."""
    try:
        return _script_cache[script_name]
    except KeyError:
        logger.error("Script %r was requested before registration", script_name)
        raise RuntimeError(
            f"Script {script_name!r} was requested before registration — "
            "register_all_scripts() must run at app startup first"
        ) from None


async def run_script(script: AsyncScript, keys: list, args: list):
    """Invoke an already-registered script, logging and re-raising on any
    failure — a Lua runtime error, a Redis connection drop mid-call, or
    (defensively) a script object that somehow never got registered.
    Re-raises unchanged so callers up the stack
    (`RateLimiterService.check_rate_limit`'s fail-open/fail-closed policy) see
    the exact same exception types as before; this only adds a diagnostic log
    line at the point of failure."""
    try:
        return await script(keys=keys, args=args)
    except Exception:
        name = next((n for n, s in _script_cache.items() if s is script), "<unregistered>")
        logger.error("Error running script %r", name, exc_info=True)
        raise
