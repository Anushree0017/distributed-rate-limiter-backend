"""Result contract returned by every rate limiter check."""
from pydantic import BaseModel


class RateLimitResult(BaseModel):
    allowed: bool
    remaining: int
    retry_after_ms: int | None = None
