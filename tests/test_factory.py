import pytest
from pydantic import ValidationError

from model.identifier import IdentifierType
from model.rate_limiter_config import EndpointConfig
from services.factory import RateLimiterFactory
from services.rate_limiter.fixed_window import FixedWindowLimiter
from services.rate_limiter.token_bucket import TokenBucketLimiter


def test_creates_token_bucket_from_config():
    config = EndpointConfig(
        identifier_type=IdentifierType.CLIENT_ID,
        config={"algorithm": "TokenBucket", "capacity": 10, "refill_rate_per_second": 2},
    )
    limiter = RateLimiterFactory.create(config, ttl_seconds=3600)
    assert isinstance(limiter, TokenBucketLimiter)


def test_creates_fixed_window_from_config():
    config = EndpointConfig(
        identifier_type=IdentifierType.CLIENT_ID,
        config={"algorithm": "FixedWindow", "window_size_ms": 1000, "max_requests": 5},
    )
    limiter = RateLimiterFactory.create(config, ttl_seconds=3600)
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
