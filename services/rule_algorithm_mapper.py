"""Translates a cached DB rule's algorithm name + params (the rules-CRUD
service's own param vocabulary — `limit`, `window_seconds`, `capacity`,
`refill_rate`, `leak_rate`, seeded in `alembic/versions/0002_seed_algorithms.py`)
into the runtime engine's `AlgorithmConfig` (`model/rate_limiter_config.py`'s
field names — `max_requests`, `window_size_ms`, `refill_rate_per_second`,
`leak_rate_per_second`).

The two vocabularies were deliberately allowed to diverge (see that
migration's docstring — nothing ties `rules.params` to the engine's config
shape at the DB layer), so this is the one place that bridges them for the
`/check` request path.

`initial_tokens` (a `TokenBucket` rule param) has no engine equivalent and is
silently ignored here: `TokenBucketLimiter`/`token_bucket.lua` have no notion
of a custom initial fill — every bucket starts full at `capacity` the first
time its key is touched. Accepting the param without applying it is a known
gap in the engine, not a bug in this mapping; revisit if that engine
limitation is ever lifted.
"""
from model.rate_limiter_config import (
    AlgorithmConfig,
    AlgorithmName,
    FixedWindowParams,
    LeakyBucketParams,
    SlidingWindowCounterParams,
    SlidingWindowLogParams,
    TokenBucketParams,
)

_WINDOWED_PARAM_CLASSES: dict[str, type] = {
    AlgorithmName.FIXED_WINDOW.value: FixedWindowParams,
    AlgorithmName.SLIDING_WINDOW_LOG.value: SlidingWindowLogParams,
    AlgorithmName.SLIDING_WINDOW_COUNTER.value: SlidingWindowCounterParams,
}


class UnsupportedRuleAlgorithmError(Exception):
    """Raised when a cached rule's `algorithm_name` has no runtime mapping."""

    def __init__(self, algorithm_name: str):
        self.algorithm_name = algorithm_name
        super().__init__(f"No runtime algorithm mapping for {algorithm_name!r}")


def build_algorithm_config(algorithm_name: str, params: dict) -> AlgorithmConfig:
    """Raises `UnsupportedRuleAlgorithmError` for an unrecognized algorithm
    name, or `KeyError`/pydantic `ValidationError` for missing/malformed
    params — callers on the request path should treat any of these as "this
    rule can't be used" and fall back, not propagate to the caller, since
    `rules.params` isn't schema-validated against `algorithms.params` yet
    (see plan.md's open questions).
    """
    if algorithm_name in _WINDOWED_PARAM_CLASSES:
        return _WINDOWED_PARAM_CLASSES[algorithm_name](
            algorithm=algorithm_name,
            max_requests=params["limit"],
            window_size_ms=int(params["window_seconds"] * 1000),
        )
    if algorithm_name == AlgorithmName.TOKEN_BUCKET.value:
        return TokenBucketParams(
            algorithm=algorithm_name,
            capacity=params["capacity"],
            refill_rate_per_second=params["refill_rate"],
        )
    if algorithm_name == AlgorithmName.LEAKY_BUCKET.value:
        return LeakyBucketParams(
            algorithm=algorithm_name,
            capacity=params["capacity"],
            leak_rate_per_second=params["leak_rate"],
        )
    raise UnsupportedRuleAlgorithmError(algorithm_name)
