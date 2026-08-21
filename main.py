"""FastAPI app entry point."""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from api.health import router as health_router
from api.v1.endpoints import rate_limit
from core.config_loader import load_rate_limiter_settings
from core.logging import setup_logging
from core.settings import get_client_ttl_seconds, get_rate_limit_config_path
from services.rate_limiter_service import RateLimiterService

setup_logging()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_rate_limiter_settings(get_rate_limit_config_path())
    app.state.rate_limiter_service = RateLimiterService(settings, get_client_ttl_seconds())
    logger.info(
        "Rate limiter service ready: default=%s, %d endpoint(s) configured",
        settings.default.config.algorithm,
        len(settings.endpoints),
    )
    yield


app = FastAPI(title="Rate Limiter Service", lifespan=lifespan)
app.include_router(rate_limit.router, prefix="/api/v1")
app.include_router(health_router)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Last-resort handler: only reached when a request path raises something
    no more specific FastAPI/Starlette handler (422 validation, HTTPException,
    etc.) already caught. Never leak internals — the caller just gets a 500.
    """
    logger.error("Unhandled exception on %s %s", request.method, request.url.path, exc_info=exc)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})
