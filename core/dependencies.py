"""FastAPI DI providers."""
from fastapi import Depends, Request
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from core.db import get_db
from repositories.algorithm_repository import AlgorithmRepository
from repositories.rule_repository import RuleRepository
from services.algorithm_service import AlgorithmService
from services.rate_limiter_service import RateLimiterService
from services.rule_service import RuleService


def get_rate_limiter_service(request: Request) -> RateLimiterService:
    return request.app.state.rate_limiter_service


def get_redis(request: Request) -> Redis:
    return request.app.state.redis_client


def get_rule_service(session: AsyncSession = Depends(get_db)) -> RuleService:
    return RuleService(RuleRepository(session), AlgorithmRepository(session))


def get_algorithm_service(session: AsyncSession = Depends(get_db)) -> AlgorithmService:
    return AlgorithmService(AlgorithmRepository(session))
