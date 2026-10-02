"""FastAPI DI providers."""
from fastapi import Depends, Request
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from core.db import get_db
from core.security.tokens import TokenService
from repositories.algorithm_repository import AlgorithmRepository
from repositories.client_repository import ClientRepository
from repositories.rule_group_repository import RuleGroupRepository
from repositories.rule_repository import RuleRepository
from services.algorithm_service import AlgorithmService
from services.auth_service import AuthService
from services.client_service import ClientService
from services.clients_cache import ClientsCache
from services.rate_limiter_service import RateLimiterService
from services.rule_group_service import RuleGroupService
from services.rule_service import RuleService
from services.rules_cache import RulesCache
from services.script_service import ScriptService


def get_rate_limiter_service(request: Request) -> RateLimiterService:
    return request.app.state.rate_limiter_service


def get_redis(request: Request) -> Redis:
    return request.app.state.redis_client


def get_rules_cache(request: Request) -> RulesCache:
    return request.app.state.rules_cache


def get_clients_cache(request: Request) -> ClientsCache:
    return request.app.state.clients_cache


def get_token_service(request: Request) -> TokenService:
    return request.app.state.token_service


def get_client_repository(session: AsyncSession = Depends(get_db)) -> ClientRepository:
    return ClientRepository(session)


def get_auth_service(
    client_repository: ClientRepository = Depends(get_client_repository),
    token_service: TokenService = Depends(get_token_service),
) -> AuthService:
    return AuthService(client_repository, token_service)


def get_client_service(client_repository: ClientRepository = Depends(get_client_repository)) -> ClientService:
    return ClientService(client_repository)


def get_rule_service(session: AsyncSession = Depends(get_db)) -> RuleService:
    return RuleService(
        RuleRepository(session), AlgorithmRepository(session), RuleGroupRepository(session), ClientRepository(session)
    )


def get_rule_group_service(session: AsyncSession = Depends(get_db)) -> RuleGroupService:
    return RuleGroupService(
        RuleGroupRepository(session), RuleRepository(session), AlgorithmRepository(session), ClientRepository(session)
    )


def get_algorithm_service(session: AsyncSession = Depends(get_db)) -> AlgorithmService:
    return AlgorithmService(AlgorithmRepository(session))


def get_script_service(redis_client: Redis = Depends(get_redis)) -> ScriptService:
    return ScriptService(redis_client)
