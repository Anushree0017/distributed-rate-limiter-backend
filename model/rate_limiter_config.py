"""Pydantic models describing the rate limiter YAML configuration."""
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class AlgorithmName(str, Enum):
    TOKEN_BUCKET = "TokenBucket"
    SLIDING_WINDOW_LOG = "SlidingWindowLog"
    SLIDING_WINDOW_COUNTER = "SlidingWindowCounter"
    FIXED_WINDOW = "FixedWindow"
    LEAKY_BUCKET = "LeakyBucket"


class TokenBucketParams(BaseModel):
    capacity: int = Field(gt=0)
    refill_rate_per_second: float = Field(gt=0)


class FixedWindowParams(BaseModel):
    window_size_ms: int = Field(gt=0)
    max_requests: int = Field(gt=0)


class SlidingWindowLogParams(BaseModel):
    window_size_ms: int = Field(gt=0)
    max_requests: int = Field(gt=0)


class SlidingWindowCounterParams(BaseModel):
    window_size_ms: int = Field(gt=0)
    max_requests: int = Field(gt=0)


class LeakyBucketParams(BaseModel):
    capacity: int = Field(gt=0)
    leak_rate_per_second: float = Field(gt=0)


class EndpointConfig(BaseModel):
    """Raw config entry for one endpoint: algorithm name + its params.

    `params` stays a loosely-typed dict here; `RateLimiterFactory` validates
    it into the algorithm-specific params model at build time, so an unknown
    algorithm or malformed params fails fast at startup.
    """

    algorithm: AlgorithmName
    params: dict[str, Any] = Field(default_factory=dict)


class RateLimiterSettings(BaseModel):
    default: EndpointConfig
    endpoints: dict[str, EndpointConfig] = Field(default_factory=dict)
