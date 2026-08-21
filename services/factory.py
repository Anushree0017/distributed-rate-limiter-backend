"""Builds a `RateLimiter` implementation from an `EndpointConfig`."""
import logging

from interfaces.base import RateLimiter
from model.rate_limiter_config import (
    EndpointConfig,
    FixedWindowParams,
    LeakyBucketParams,
    SlidingWindowCounterParams,
    SlidingWindowLogParams,
    TokenBucketParams,
)
from services.rate_limiter.fixed_window import FixedWindowLimiter
from services.rate_limiter.leaky_bucket import LeakyBucketLimiter
from services.rate_limiter.sliding_window_counter import SlidingWindowCounterLimiter
from services.rate_limiter.sliding_window_log import SlidingWindowLogLimiter
from services.rate_limiter.token_bucket import TokenBucketLimiter

_LIMITER_CLASSES: dict[type, type[RateLimiter]] = {
    TokenBucketParams: TokenBucketLimiter,
    SlidingWindowLogParams: SlidingWindowLogLimiter,
    SlidingWindowCounterParams: SlidingWindowCounterLimiter,
    FixedWindowParams: FixedWindowLimiter,
    LeakyBucketParams: LeakyBucketLimiter,
}

logger = logging.getLogger(__name__)


class RateLimiterFactory:
    """Instantiates the `RateLimiter` for a given endpoint's algorithm config.

    `EndpointConfig.config` is already a validated, algorithm-specific params
    model by the time it reaches here (Pydantic's discriminated union in
    `model/rate_limiter_config.py` guarantees the shape at config-load time),
    so this only needs to dispatch on its type — no defensive key checks.
    """

    @staticmethod
    def create(config: EndpointConfig, ttl_seconds: float) -> RateLimiter:
        params = config.config
        limiter_cls = _LIMITER_CLASSES.get(type(params))
        if limiter_cls is None:
            raise ValueError(f"Unknown rate limiter algorithm params: {type(params).__name__}")

        kwargs = params.model_dump(exclude={"algorithm"})
        logger.debug("Instantiating %s with params=%s ttl_seconds=%s", limiter_cls.__name__, kwargs, ttl_seconds)
        return limiter_cls(**kwargs, ttl_seconds=ttl_seconds)
