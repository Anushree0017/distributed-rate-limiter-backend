"""Response schema for `POST /api/v1/auth/token` — RFC 6749 §5.1 success
shape.
"""
from pydantic import BaseModel


class TokenResponseDTO(BaseModel):
    access_token: str
    token_type: str = "Bearer"
    expires_in: int
    scope: str
