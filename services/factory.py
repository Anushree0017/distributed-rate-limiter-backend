"""Builds a `RateLimiter` implementation from an `EndpointConfig`."""
import logging

from redis.asyncio import Redis

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

    `scope` disambiguates Redis keys between endpoints that share the same
    algorithm + params (including two endpoints that fall back to the same
    default), so their rate-limit state stays isolated the same way separate
    in-memory instances kept it isolated pre-Redis. Every algorithm class
    still only registers its Lua script once for the process lifetime
    regardless of how many `scope`s use it — that dedup lives in
    `script_loader.load_script`, not here, so isolation and "register once"
    aren't in tension (see CLAUDE.md's deviations section for why this
    differs from the plan's original "cache instances per (algorithm,
    config)" suggestion).
    """

    @staticmethod
    def create(config: EndpointConfig, redis_client: Redis, scope: str) -> RateLimiter:
        params = config.config
        limiter_cls = _LIMITER_CLASSES.get(type(params))
        if limiter_cls is None:
            raise ValueError(f"Unknown rate limiter algorithm params: {type(params).__name__}")

        kwargs = params.model_dump(exclude={"algorithm"})
        logger.debug("Instantiating %s scope=%s with params=%s", limiter_cls.__name__, scope, kwargs)
        return limiter_cls(**kwargs, redis_client=redis_client, scope=scope)
