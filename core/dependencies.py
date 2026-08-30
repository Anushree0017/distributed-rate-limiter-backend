"""FastAPI DI providers."""
from fastapi import Request
from redis.asyncio import Redis

from services.rate_limiter_service import RateLimiterService


def get_rate_limiter_service(request: Request) -> RateLimiterService:
    return request.app.state.rate_limiter_service


def get_redis(request: Request) -> Redis:
    return request.app.state.redis_client
