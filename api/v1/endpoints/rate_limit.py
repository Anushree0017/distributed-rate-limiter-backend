"""Rate limit check endpoint.

This service runs independently of the API gateway: the gateway calls
`POST /check` with an `identifier` (whichever `client_id` / `api_key` /
`ip_address` value the target `endpoint` is configured to key on) *before*
forwarding the real request, and enforces the result itself. This service
never sees the gateway's actual traffic, so the check is a plain JSON
payload rather than something derived from the incoming request/headers.
"""
from fastapi import APIRouter, Depends

from core.dependencies import get_rate_limiter_service
from dto.rate_limit_check_request import RateLimitCheckRequest
from model.rate_limit_result import RateLimitResult
from services.rate_limiter_service import RateLimiterService

router = APIRouter()


@router.post("/check", response_model=RateLimitResult)
async def check_rate_limit(
    payload: RateLimitCheckRequest,
    service: RateLimiterService = Depends(get_rate_limiter_service),
) -> RateLimitResult:
    return await service.check_rate_limit(payload.endpoint, payload.identifier)
