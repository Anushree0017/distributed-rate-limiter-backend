"""FastAPI dependency enforcing bearer-token auth + scope on protected
endpoints, per the table in `.claude/plans/phase6/plan.md`'s "Settled design
> Endpoints and scopes". Purely local: token signature/claims verification
(`core/security/tokens.py`) plus an in-memory `ClientsCache` lookup — no DB
or Redis access here, so this dependency never adds request-path I/O (Phase 6
invariant: `/check` does zero DB/Redis calls for authentication, and the same
holds for every other protected endpoint's auth step).

`bearer_scheme` is declared via `fastapi.security.HTTPBearer` (not a plain
`Header`) purely so FastAPI registers an `HTTPBearer` security scheme in the
generated OpenAPI document — this is what makes Swagger UI (`/docs`) show an
"Authorize" padlock: paste a token obtained from `POST /auth/token` once, and
every subsequent "Try it out" call on a protected endpoint carries it
automatically. `auto_error=False` is required because this module raises its
own `AuthenticationError` (the `{"error": {...}}` envelope + `WWW-Authenticate:
Bearer`) for a missing/malformed header; the default `HTTPBearer` would
otherwise short-circuit with FastAPI's generic `{"detail": "Not authenticated"}`
403 before this dependency body ever runs.
"""
from dataclasses import dataclass

from fastapi import Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from core.exceptions import AuthenticationError, AuthorizationError
from core.security.tokens import TokenError, TokenService
from services.clients_cache import ClientsCache

bearer_scheme = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class AuthenticatedClient:
    """What a protected endpoint gets once auth succeeds. `scopes` is already
    the *intersection* of the token's own scopes and the client's current
    scopes in `ClientsCache` — so a scope revoked after the token was issued
    is never visible here even though it's still present in the token's own
    claims.
    """

    pk: str
    client_id: str
    scopes: frozenset[str]


def require_scope(scope: str):
    """Returns a FastAPI dependency requiring a valid bearer token whose
    (cache-intersected) scopes include `scope`. Missing/malformed/expired/
    tampered/wrong-audience token -> `AuthenticationError` (401,
    `WWW-Authenticate: Bearer`). Valid token but the client is unknown to
    `ClientsCache`, disabled, or lacks `scope` -> `AuthorizationError` (403).
    """

    async def _dependency(
        request: Request,
        credentials: HTTPAuthorizationCredentials | None = Security(bearer_scheme),
    ) -> AuthenticatedClient:
        if credentials is None or credentials.scheme.lower() != "bearer":
            raise AuthenticationError()
        token = credentials.credentials

        token_service: TokenService = request.app.state.token_service
        try:
            claims = token_service.verify(token)
        except TokenError:
            raise AuthenticationError()

        clients_cache: ClientsCache = request.app.state.clients_cache
        record = clients_cache.get_by_client_id(claims.client_id)
        if record is None or not record.is_active():
            # A structurally valid, correctly-signed token referring to a
            # client that's since been disabled or no longer exists — the
            # token itself isn't the problem, so this is a 403, not a 401.
            raise AuthorizationError()

        effective_scopes = frozenset(claims.scopes) & frozenset(record.scopes)
        if scope not in effective_scopes:
            raise AuthorizationError()

        return AuthenticatedClient(pk=record.pk, client_id=record.client_id, scopes=effective_scopes)

    return _dependency
