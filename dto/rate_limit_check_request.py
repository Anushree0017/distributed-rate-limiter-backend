"""Request payload for the rate limit check endpoint."""
from pydantic import BaseModel, Field


class RateLimitCheckRequest(BaseModel):
    client_id: str = Field(..., min_length=1)
    endpoint: str = Field(..., min_length=1)
