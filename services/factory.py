"""Builds a `RateLimiter` implementation from an `EndpointConfig`."""
from interfaces.base import RateLimiter
from model.rate_limiter_config import (
    AlgorithmName,
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

_PARAM_MODELS = {
    AlgorithmName.TOKEN_BUCKET: TokenBucketParams,
    AlgorithmName.SLIDING_WINDOW_LOG: SlidingWindowLogParams,
    AlgorithmName.SLIDING_WINDOW_COUNTER: SlidingWindowCounterParams,
    AlgorithmName.FIXED_WINDOW: FixedWindowParams,
    AlgorithmName.LEAKY_BUCKET: LeakyBucketParams,
}

_LIMITER_CLASSES = {
    AlgorithmName.TOKEN_BUCKET: TokenBucketLimiter,
    AlgorithmName.SLIDING_WINDOW_LOG: SlidingWindowLogLimiter,
    AlgorithmName.SLIDING_WINDOW_COUNTER: SlidingWindowCounterLimiter,
    AlgorithmName.FIXED_WINDOW: FixedWindowLimiter,
    AlgorithmName.LEAKY_BUCKET: LeakyBucketLimiter,
}


class RateLimiterFactory:
    """Instantiates the `RateLimiter` for a given algorithm + params config."""

    @staticmethod
    def create(config: EndpointConfig) -> RateLimiter:
        params_model = _PARAM_MODELS.get(config.algorithm)
        limiter_cls = _LIMITER_CLASSES.get(config.algorithm)
        if params_model is None or limiter_cls is None:
            raise ValueError(f"Unknown rate limiter algorithm: {config.algorithm}")

        params = params_model.model_validate(config.params)
        return limiter_cls(**params.model_dump())
