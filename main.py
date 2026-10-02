"""FastAPI app entry point."""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from api.health import router as health_router
from api.v1.endpoints import algorithms, auth, clients, groups, rate_limit, redis_health, rules, scripts
from core.config_loader import load_rate_limiter_settings
from core.db import dispose_engine
from core.exceptions import register_exception_handlers
from core.key_hasher import KeyHasher
from core.logging import setup_logging
from core.redis_client import create_redis_pool, get_redis_client, ping
from core.scheduler import shutdown_scheduler, start_scheduler
from core.security.tokens import TokenService
from core.settings import settings as env_settings
from services.clients_cache import ClientsCache
from services.clients_loader import load_clients_into_cache
from services.rate_limiter.script_loader import register_all_scripts
from services.rate_limiter_service import RateLimiterService
from services.rules_cache import RulesCache
from services.rules_loader import load_rules_into_cache

setup_logging()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Pure-settings construction (reads the signing keyring, no I/O) — built
    # once up front so `require_scope`/the token endpoint never rebuild it
    # per request. See core/security/tokens.py.
    app.state.token_service = TokenService()

    redis_pool = create_redis_pool()
    redis_client = get_redis_client(redis_pool)

    # Hard-fail at boot if Redis is unreachable — distinct from the
    # steady-state fail-open policy in RateLimiterService, which is meant for
    # *transient* outages, not "Redis was never configured correctly."
    if not await ping(redis_client):
        raise RuntimeError(
            "Cannot start: Redis is unreachable at boot (PING failed). "
            "Check REDIS_URL and that Redis is running."
        )

    app.state.redis_client = redis_client

    # Registers every algorithm's Lua script with Redis exactly once, up
    # front, so no request pays the first-use EVALSHA->NOSCRIPT->SCRIPT LOAD
    # round trip. Must happen before any RateLimiter is constructed below.
    registered_scripts = await register_all_scripts(redis_client)
    logger.info("Registered Lua scripts: %s", ", ".join(registered_scripts))

    settings = load_rate_limiter_settings(env_settings.get_rate_limit_config_path())

    # Rules cache must be fully loaded — and this must succeed — *before* the
    # app starts serving traffic. An exception here is deliberately left to
    # propagate: it fails the boot rather than starting with an empty/unready
    # cache. See .claude/plans/phase3/plan-part2.md.
    rules_cache = RulesCache()
    loaded_rules = await load_rules_into_cache(rules_cache)
    app.state.rules_cache = rules_cache

    logger.info("Loaded %d rate-limiting rule(s) from the database:", len(loaded_rules))
    for rule in sorted(loaded_rules, key=lambda r: (r["endpoint"], r["identifier_signature"], r["priority"])):
        logger.info(
            "  rule %s: endpoint=%s identifier_signature=%s algorithm=%s params=%s status=%s priority=%d version=%d",
            rule["id"],
            rule["endpoint"],
            rule["identifier_signature"],
            rule["algorithm_name"],
            rule["params"],
            rule["status"],
            rule["priority"],
            rule["version"],
        )

    # Clients cache must also be fully loaded before traffic is served — same
    # hard-fail-at-boot stance as the rules cache. `/check`'s auth dependency
    # (core/dependencies.require_scope) only ever consults this in-memory
    # cache, never Postgres directly (Phase 6 invariant: zero DB/Redis calls
    # for authentication on the request path).
    clients_cache = ClientsCache()
    loaded_clients = await load_clients_into_cache(clients_cache)
    app.state.clients_cache = clients_cache
    logger.info("Loaded %d client(s) from the database", len(loaded_clients))

    hasher = KeyHasher(env_settings.get_identifier_hash_secret())
    app.state.rate_limiter_service = RateLimiterService(settings, redis_client, hasher, rules_cache=rules_cache)
    logger.info(
        "Rate limiter service ready: fallback default=%s, %d DB rule(s) loaded",
        settings.default.config.algorithm,
        rules_cache.stats()["rule_count"],
    )

    start_scheduler(rules_cache, clients_cache)

    yield

    # wait=False: an in-flight poll cycle hasn't mutated the cache yet
    # (load_all only runs after a successful fetch), so there's nothing to
    # finish cleanly — don't block shutdown on a DB call whose result would
    # be discarded anyway.
    await shutdown_scheduler()

    await redis_client.aclose()
    await redis_pool.disconnect()
    await dispose_engine()


app = FastAPI(title="Rate Limiter Service", lifespan=lifespan)
# Allows the frontend (a separate origin once deployed) to call this API
# directly from the browser. `allow_origins=["*"]` is safe here specifically
# because auth is bearer-token-based, not cookie-based — there's no session
# cookie for a malicious origin to ride along via CORS-permitted credentialed
# requests. `allow_credentials` is deliberately left `False` (the default):
# browsers reject `allow_origins=["*"]` combined with `allow_credentials=True`
# outright, and nothing here uses cookies anyway. Narrow via
# `CORS_ALLOWED_ORIGINS` (comma-separated) in a deployed env that wants to
# restrict this to the frontend's real origin(s) — see core/settings.py.
app.add_middleware(
    CORSMiddleware,
    allow_origins=env_settings.get_cors_allowed_origins(),
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(auth.router, prefix="/api/v1")
app.include_router(rate_limit.router, prefix="/api/v1")
app.include_router(redis_health.router, prefix="/api/v1")
app.include_router(rules.router, prefix="/api/v1")
app.include_router(groups.router, prefix="/api/v1")
app.include_router(clients.router, prefix="/api/v1")
app.include_router(algorithms.router, prefix="/api/v1")
app.include_router(scripts.router, prefix="/api/v1")
app.include_router(health_router)
register_exception_handlers(app)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Last-resort handler: only reached when a request path raises something
    no more specific FastAPI/Starlette handler (422 validation, HTTPException,
    etc.) already caught. Never leak internals — the caller just gets a 500.
    """
    logger.error("Unhandled exception on %s %s", request.method, request.url.path, exc_info=exc)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})
