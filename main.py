"""FastAPI app entry point."""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from api.health import router as health_router
from api.v1.endpoints import algorithms, rate_limit, redis_health, rules
from core.config_loader import load_rate_limiter_settings
from core.db import dispose_engine
from core.exceptions import register_exception_handlers
from core.logging import setup_logging
from core.redis_client import create_redis_pool, get_redis_client, ping
from core.settings import get_rate_limit_config_path
from services.rate_limiter_service import RateLimiterService

setup_logging()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
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
    settings = load_rate_limiter_settings(get_rate_limit_config_path())
    app.state.rate_limiter_service = RateLimiterService(settings, redis_client)
    logger.info(
        "Rate limiter service ready: default=%s, %d endpoint(s) configured",
        settings.default.config.algorithm,
        len(settings.endpoints),
    )
    yield

    await redis_client.aclose()
    await redis_pool.disconnect()
    await dispose_engine()


app = FastAPI(title="Rate Limiter Service", lifespan=lifespan)
app.include_router(rate_limit.router, prefix="/api/v1")
app.include_router(redis_health.router, prefix="/api/v1")
app.include_router(rules.router, prefix="/api/v1")
app.include_router(algorithms.router, prefix="/api/v1")
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
