"""FastAPI app entry point."""
from contextlib import asynccontextmanager

from fastapi import FastAPI

from api.v1.endpoints import rate_limit
from core.config_loader import load_rate_limiter_settings
from core.settings import get_client_ttl_seconds, get_rate_limit_config_path
from services.rate_limiter_service import RateLimiterService


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_rate_limiter_settings(get_rate_limit_config_path())
    app.state.rate_limiter_service = RateLimiterService(settings, get_client_ttl_seconds())
    yield


app = FastAPI(title="Rate Limiter Service", lifespan=lifespan)
app.include_router(rate_limit.router, prefix="/api/v1")
