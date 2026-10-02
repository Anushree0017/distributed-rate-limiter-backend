"""`POST /api/v1/auth/token` — OAuth2 client-credentials grant. Accepts
credentials as form fields or HTTP Basic (RFC 6749 §2.3.1 both ways); no
bearer token is required to call this endpoint (it's how one is obtained).
See `.claude/plans/phase6/plan.md`'s "Settled design > Endpoints and scopes".
"""
import base64
import binascii

from fastapi import APIRouter, Depends, Form, Header

from core.dependencies import get_auth_service
from core.exceptions import InvalidRequestError
from dto.auth_dto import TokenResponseDTO
from services.auth_service import AuthService

router = APIRouter(prefix="/auth")


def _parse_basic_auth(authorization: str | None) -> tuple[str, str] | None:
    if authorization is None or not authorization.startswith("Basic "):
        return None
    try:
        decoded = base64.b64decode(authorization[len("Basic ") :]).decode("utf-8")
        client_id, _, client_secret = decoded.partition(":")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        raise InvalidRequestError("Malformed HTTP Basic Authorization header")
    if not client_id or not client_secret:
        raise InvalidRequestError("Malformed HTTP Basic Authorization header")
    return client_id, client_secret


@router.post("/token", response_model=TokenResponseDTO)
async def issue_token(
    grant_type: str = Form(...),
    client_id: str | None = Form(default=None),
    client_secret: str | None = Form(default=None),
    scope: str | None = Form(default=None),
    authorization: str | None = Header(default=None),
    service: AuthService = Depends(get_auth_service),
) -> TokenResponseDTO:
    if grant_type != "client_credentials":
        raise InvalidRequestError(f"Unsupported grant_type: {grant_type!r}")

    basic = _parse_basic_auth(authorization)
    if basic is not None:
        if client_id is not None or client_secret is not None:
            raise InvalidRequestError("Supply credentials via either HTTP Basic or form fields, not both")
        resolved_client_id, resolved_client_secret = basic
    elif client_id is not None and client_secret is not None:
        resolved_client_id, resolved_client_secret = client_id, client_secret
    else:
        raise InvalidRequestError("client_id and client_secret are required (form fields or HTTP Basic)")

    requested_scopes = scope.split(" ") if scope else None
    token, expires_in, granted_scopes = await service.issue_token(
        resolved_client_id, resolved_client_secret, requested_scopes
    )
    return TokenResponseDTO(access_token=token, expires_in=expires_in, scope=" ".join(granted_scopes))
