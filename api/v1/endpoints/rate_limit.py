"""Rate limit check endpoint.

This service runs independently of the API gateway: the gateway calls
`POST /check` with `endpoint` and `identifiers` (1-3 `{type, value}` pairs
identifying the caller) *before* forwarding the real request, and enforces
the result itself. This service never sees the gateway's actual traffic, so
the check is a plain JSON payload rather than something derived from the
incoming request/headers.
"""
from fastapi import APIRouter, Depends

from core.dependencies import get_rate_limiter_service
from core.security.auth_dependency import AuthenticatedClient, require_scope
from dto.rate_limit_check_request import RateLimitCheckRequestDTO
from model.rate_limit_result import RateLimitResult
from services.rate_limiter_service import RateLimiterService

router = APIRouter()


@router.post("/check", response_model=RateLimitResult)
async def check_rate_limit(
    payload: RateLimitCheckRequestDTO,
    service: RateLimiterService = Depends(get_rate_limiter_service),
    client: AuthenticatedClient = Depends(require_scope("check")),
) -> RateLimitResult:
    return await service.check_rate_limit(client.pk, payload)
