import pytest

from model.rate_limiter_config import AlgorithmName, EndpointConfig
from services.factory import RateLimiterFactory
from services.rate_limiter.fixed_window import FixedWindowLimiter
from services.rate_limiter.token_bucket import TokenBucketLimiter


def test_creates_token_bucket_from_config():
    config = EndpointConfig(
        algorithm=AlgorithmName.TOKEN_BUCKET,
        params={"capacity": 10, "refill_rate_per_second": 2},
    )
    limiter = RateLimiterFactory.create(config)
    assert isinstance(limiter, TokenBucketLimiter)


def test_creates_fixed_window_from_config():
    config = EndpointConfig(
        algorithm=AlgorithmName.FIXED_WINDOW,
        params={"window_size_ms": 1000, "max_requests": 5},
    )
    limiter = RateLimiterFactory.create(config)
    assert isinstance(limiter, FixedWindowLimiter)


def test_raises_on_invalid_params():
    config = EndpointConfig(
        algorithm=AlgorithmName.TOKEN_BUCKET,
        params={"capacity": "not-a-number", "refill_rate_per_second": 2},
    )
    with pytest.raises(Exception):
        RateLimiterFactory.create(config)
