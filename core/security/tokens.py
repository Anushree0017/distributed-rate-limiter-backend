"""Issues and verifies the short-lived signed JWTs clients use on every
`/check`/admin call. HS256 with a keyring (`AUTH_JWT_SIGNING_KEYS`): issuer
and verifier are the same service, so asymmetric signing buys no extra trust
(every instance already holds the signing key either way). Isolated in this
one module so a later switch to ES256 + JWKS is a one-file change. See
`.claude/plans/phase6/plan.md`'s "Settled design > Tokens".
"""
import time
import uuid

import jwt

from core.settings import settings

_ALGORITHM = "HS256"
_CLOCK_SKEW_LEEWAY_SECONDS = 30
_REQUIRED_CLAIMS = ["exp", "iat", "iss", "aud", "sub", "jti", "scope"]


class TokenError(Exception):
    """Any reason a token couldn't be issued or verified. The auth dependency
    (Step 6) maps every instance of this to a 401 — the reason is for logs,
    never echoed verbatim to the caller (it could otherwise be used to probe
    why a token was rejected).
    """


class TokenClaims:
    """The verified, parsed claims of a bearer token — what
    `require_scope` needs to build an `AuthenticatedClient`."""

    def __init__(self, client_id: str, scopes: list[str], jti: str) -> None:
        self.client_id = client_id
        self.scopes = scopes
        self.jti = jti


class TokenService:
    def __init__(self) -> None:
        self._signing_keys = settings.get_auth_jwt_signing_keys()
        self._active_kid = settings.get_auth_jwt_active_kid()
        self._issuer = settings.get_auth_jwt_issuer()
        self._audience = settings.get_auth_jwt_audience()
        self._ttl_seconds = settings.get_auth_token_ttl_seconds()

    def issue(self, client_id: str, scopes: list[str]) -> tuple[str, int]:
        """Returns `(token, expires_in)`. Always signs with the *active* kid
        — only verification needs to look older keys up, for tokens issued
        before the last rotation.
        """
        now = int(time.time())
        claims = {
            "iss": self._issuer,
            "aud": self._audience,
            "sub": client_id,
            "scope": " ".join(scopes),
            "iat": now,
            "exp": now + self._ttl_seconds,
            "jti": str(uuid.uuid4()),
        }
        token = jwt.encode(
            claims, self._signing_keys[self._active_kid], algorithm=_ALGORITHM, headers={"kid": self._active_kid}
        )
        return token, self._ttl_seconds

    def verify(self, token: str) -> TokenClaims:
        """Pins `algorithms=["HS256"]` (never trusts the token's own `alg`
        header for algorithm selection — that's what makes `alg: none` /
        algorithm-confusion attacks possible), looks the signing key up by
        `kid` (rejecting an unknown one), and requires every claim in
        `_REQUIRED_CLAIMS`. Raises `TokenError` for any failure — expired,
        wrong issuer/audience, unknown kid, tampered payload, missing claim,
        wrong algorithm.
        """
        try:
            unverified_header = jwt.get_unverified_header(token)
        except jwt.InvalidTokenError as exc:
            raise TokenError("malformed token header") from exc

        kid = unverified_header.get("kid")
        key = self._signing_keys.get(kid) if kid is not None else None
        if key is None:
            raise TokenError(f"unknown signing key id: {kid!r}")

        try:
            claims = jwt.decode(
                token,
                key,
                algorithms=[_ALGORITHM],
                issuer=self._issuer,
                audience=self._audience,
                leeway=_CLOCK_SKEW_LEEWAY_SECONDS,
                options={"require": _REQUIRED_CLAIMS},
            )
        except jwt.InvalidTokenError as exc:
            raise TokenError("token verification failed") from exc

        scope_claim = claims["scope"]
        scopes = scope_claim.split(" ") if scope_claim else []
        return TokenClaims(client_id=claims["sub"], scopes=scopes, jti=claims["jti"])
