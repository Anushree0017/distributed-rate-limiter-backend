import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import ResponseError as RedisResponseError

from core.key_hasher import KeyHasher
from dto.rate_limit_check_request import IdentifierValueDTO, RateLimitCheckRequestDTO
from interfaces.base import RateLimiter
from model.identifier import ClientIdentifier, IdentifierType
from model.rate_limit_result import RateLimitResult
from model.rate_limiter_config import EndpointConfig, RateLimiterSettings
from model.rule_identifier_type import RULE_TO_ENGINE_IDENTIFIER_TYPE
from services.rate_limiter_service import RateLimiterService
from services.rules_cache import RulesCache

_HASHER = KeyHasher("test-secret-at-least-32-characters-long")


def _settings() -> RateLimiterSettings:
    return RateLimiterSettings(
        default=EndpointConfig(
            identifier_type=IdentifierType.ENDPOINT,
            config={"algorithm": "FixedWindow", "window_size_ms": 1000, "max_requests": 1},
        ),
    )


def _token_bucket_rule(**overrides) -> dict:
    identifier_types = overrides.pop("identifier_types", ["global"])
    defaults = dict(
        id="rule-orders",
        endpoint="/api/v1/orders",
        identifier_types=identifier_types,
        identifier_signature="+".join(sorted(identifier_types)),
        is_global=identifier_types == ["global"],
        engine_identifier_types=frozenset(RULE_TO_ENGINE_IDENTIFIER_TYPE[t] for t in identifier_types),
        algorithm_id="algo-1",
        algorithm_name="TokenBucket",
        params={"capacity": 1, "refill_rate": 1},
        status="active",
        priority=100,
        version=1,
    )
    defaults.update(overrides)
    return defaults


def _cache(*rules: dict) -> RulesCache:
    cache = RulesCache()
    cache.load_all(list(rules))
    return cache


def _check(endpoint: str, identifier_type: str, value: str) -> RateLimitCheckRequestDTO:
    return RateLimitCheckRequestDTO(
        endpoint=endpoint, identifiers=[IdentifierValueDTO(type=IdentifierType(identifier_type), value=value)]
    )


async def test_uses_matching_db_rule_for_known_endpoint(redis_client):
    service = RateLimiterService(_settings(), redis_client, _HASHER, rules_cache=_cache(_token_bucket_rule()))

    result = await service.check_rate_limit(_check("/api/v1/orders", "client_id", "client-1"))
    assert result.allowed is True
    # TokenBucket capacity=1, so a second immediate call is blocked.
    result = await service.check_rate_limit(_check("/api/v1/orders", "client_id", "client-1"))
    assert result.allowed is False


async def test_falls_back_to_default_for_unknown_endpoint(redis_client):
    service = RateLimiterService(_settings(), redis_client, _HASHER, rules_cache=_cache())

    result = await service.check_rate_limit(_check("/api/v1/unknown", "client_id", "client-1"))
    assert result.allowed is True
    result = await service.check_rate_limit(_check("/api/v1/unknown", "client_id", "client-1"))
    assert result.allowed is False


async def test_clients_are_isolated_within_an_endpoint(redis_client):
    service = RateLimiterService(_settings(), redis_client, _HASHER, rules_cache=_cache(_token_bucket_rule()))

    result_a = await service.check_rate_limit(_check("/api/v1/orders", "client_id", "a"))
    result_b = await service.check_rate_limit(_check("/api/v1/orders", "client_id", "b"))
    assert result_a.allowed is True
    assert result_b.allowed is True


class _ExplodingLimiter(RateLimiter):
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def check(self, identifier: ClientIdentifier) -> RateLimitResult:
        raise self._exc


async def test_fails_open_with_degraded_flag_on_redis_connection_error(redis_client):
    service = RateLimiterService(_settings(), redis_client, _HASHER)
    service._default_limiter = _ExplodingLimiter(RedisConnectionError("backend unavailable"))

    result = await service.check_rate_limit(_check("/api/v1/unknown", "client_id", "client-1"))

    assert result.allowed is True
    assert result.degraded is True


async def test_response_error_propagates_instead_of_failing_open(redis_client):
    service = RateLimiterService(_settings(), redis_client, _HASHER)
    service._default_limiter = _ExplodingLimiter(RedisResponseError("wrong number of KEYS"))

    with pytest.raises(RedisResponseError):
        await service.check_rate_limit(_check("/api/v1/unknown", "client_id", "client-1"))
