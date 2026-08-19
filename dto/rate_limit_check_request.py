"""Request payload for the rate limit check endpoint."""
from pydantic import BaseModel, Field


class RateLimitCheckRequest(BaseModel):
    """Sent by the Gateway before forwarding a real request.

    `identifier` is a single value whose meaning (client id / api key / ip
    address) is settled by `endpoint`'s configured `identifier_type` — the
    Gateway is expected to already know which value to send, from the same
    shared config.
    """

    endpoint: str = Field(..., min_length=1)
    identifier: str = Field(..., min_length=1)
