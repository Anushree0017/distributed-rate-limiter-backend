"""FastAPI DI providers."""
from fastapi import Request

from services.rate_limiter_service import RateLimiterService


def get_rate_limiter_service(request: Request) -> RateLimiterService:
    return request.app.state.rate_limiter_service
