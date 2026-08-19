"""Pydantic models describing the rate limiter YAML configuration."""
from enum import Enum
from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field

from model.identifier import IdentifierType


class AlgorithmName(str, Enum):
    TOKEN_BUCKET = "TokenBucket"
    SLIDING_WINDOW_LOG = "SlidingWindowLog"
    SLIDING_WINDOW_COUNTER = "SlidingWindowCounter"
    FIXED_WINDOW = "FixedWindow"
    LEAKY_BUCKET = "LeakyBucket"


class TokenBucketParams(BaseModel):
    algorithm: Literal[AlgorithmName.TOKEN_BUCKET]
    capacity: int = Field(gt=0)
    refill_rate_per_second: float = Field(gt=0)


class FixedWindowParams(BaseModel):
    algorithm: Literal[AlgorithmName.FIXED_WINDOW]
    window_size_ms: int = Field(gt=0)
    max_requests: int = Field(gt=0)


class SlidingWindowLogParams(BaseModel):
    algorithm: Literal[AlgorithmName.SLIDING_WINDOW_LOG]
    window_size_ms: int = Field(gt=0)
    max_requests: int = Field(gt=0)


class SlidingWindowCounterParams(BaseModel):
    algorithm: Literal[AlgorithmName.SLIDING_WINDOW_COUNTER]
    window_size_ms: int = Field(gt=0)
    max_requests: int = Field(gt=0)


class LeakyBucketParams(BaseModel):
    algorithm: Literal[AlgorithmName.LEAKY_BUCKET]
    capacity: int = Field(gt=0)
    leak_rate_per_second: float = Field(gt=0)


AlgorithmConfig = Annotated[
    Union[
        TokenBucketParams,
        FixedWindowParams,
        SlidingWindowLogParams,
        SlidingWindowCounterParams,
        LeakyBucketParams,
    ],
    Field(discriminator="algorithm"),
]


class EndpointConfig(BaseModel):
    """Config entry for one endpoint: which identifier it's keyed by, plus its
    algorithm + params, validated up front via the `AlgorithmConfig`
    discriminated union — an endpoint with a missing/invalid algorithm param
    fails Pydantic validation at config-load time, before any request ever
    reaches the factory.
    """

    identifier_type: IdentifierType
    config: AlgorithmConfig


class RateLimiterSettings(BaseModel):
    default: EndpointConfig
    endpoints: dict[str, EndpointConfig] = Field(default_factory=dict)
