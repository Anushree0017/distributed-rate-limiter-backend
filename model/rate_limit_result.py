"""Result contract returned by every rate limiter check.

Field names/units are aligned 1:1 with conventional `X-RateLimit-*` /
`Retry-After` headers so the Gateway's mapping from this JSON body to
headers is a trivial rename, not a re-derivation (see README for the table).
"""
from pydantic import BaseModel


class RateLimitResult(BaseModel):
    allowed: bool
    limit: int
    remaining: int
    retry_after_ms: int | None = None
    reset_at_ms: int | None = None
