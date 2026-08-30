import pytest
from pydantic import ValidationError

from model.identifier import ClientIdentifier, IdentifierType
from model.rate_limiter_config import EndpointConfig
from services.factory import RateLimiterFactory
from services.rate_limiter.fixed_window import FixedWindowLimiter
from services.rate_limiter.token_bucket import TokenBucketLimiter


def test_creates_token_bucket_from_config(redis_client):
    config = EndpointConfig(
        identifier_type=IdentifierType.CLIENT_ID,
        config={"algorithm": "TokenBucket", "capacity": 10, "refill_rate_per_second": 2},
    )
    limiter = RateLimiterFactory.create(config, redis_client, scope="test")
    assert isinstance(limiter, TokenBucketLimiter)


def test_creates_fixed_window_from_config(redis_client):
    config = EndpointConfig(
        identifier_type=IdentifierType.CLIENT_ID,
        config={"algorithm": "FixedWindow", "window_size_ms": 1000, "max_requests": 5},
    )
    limiter = RateLimiterFactory.create(config, redis_client, scope="test")
    assert isinstance(limiter, FixedWindowLimiter)


def test_raises_on_invalid_params_at_config_construction():
    with pytest.raises(ValidationError):
        EndpointConfig(
            identifier_type=IdentifierType.CLIENT_ID,
            config={"algorithm": "TokenBucket", "capacity": "not-a-number", "refill_rate_per_second": 2},
        )


def test_raises_on_unknown_algorithm_at_config_construction():
    with pytest.raises(ValidationError):
        EndpointConfig(
            identifier_type=IdentifierType.CLIENT_ID,
            config={"algorithm": "NotARealAlgorithm"},
        )


def test_two_scopes_with_identical_config_get_isolated_keys(redis_client):
    """Different endpoints sharing an algorithm+config must not share Redis
    state — mirrors the pre-Redis behavior where each endpoint held its own
    in-memory instance. Isolation comes from `scope`, not from the factory
    refusing to build a second instance.
    """
    config = EndpointConfig(
        identifier_type=IdentifierType.CLIENT_ID,
        config={"algorithm": "FixedWindow", "window_size_ms": 1000, "max_requests": 5},
    )
    limiter_a = RateLimiterFactory.create(config, redis_client, scope="/endpoint-a")
    limiter_b = RateLimiterFactory.create(config, redis_client, scope="/endpoint-b")

    assert limiter_a is not limiter_b
    identifier = ClientIdentifier(value="same-client")
    assert limiter_a._key(identifier) != limiter_b._key(identifier)


def test_same_script_is_registered_once_across_scopes(redis_client):
    """Per redis_guidelines.md §4/§10: `register_script()` must run once per
    script for the process lifetime, not once per endpoint. Two limiters for
    the same algorithm (different scopes) must share the identical `Script`
    object/SHA rather than each re-registering it.
    """
    config = EndpointConfig(
        identifier_type=IdentifierType.CLIENT_ID,
        config={"algorithm": "FixedWindow", "window_size_ms": 1000, "max_requests": 5},
    )
    limiter_a = RateLimiterFactory.create(config, redis_client, scope="/endpoint-a")
    limiter_b = RateLimiterFactory.create(config, redis_client, scope="/endpoint-b")

    assert limiter_a._script is limiter_b._script
