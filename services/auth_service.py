"""Business rules behind `POST /api/v1/auth/token`: client-credentials grant,
RFC 6749 error shapes. See `.claude/plans/phase6/plan.md`'s "Settled design >
Tokens" and "> Clients and secrets".
"""
import hmac

from core.exceptions import InvalidClientError, InvalidScopeError
from core.security.secrets import hash_secret, verify_secret
from core.security.tokens import TokenService
from model.client import Client
from model.client_status import ClientStatus
from repositories.client_repository import ClientRepository

# A fixed, never-matching hash compared against on an unknown-client lookup,
# so "client not found" and "wrong secret" take the same amount of time —
# no timing oracle revealing whether a client_id exists. Not a real secret;
# never actually verifies anything.
_DUMMY_SECRET_HASH = hash_secret("dummy-secret-used-only-to-equalize-timing-no-real-value-here")


class AuthService:
    def __init__(self, client_repository: ClientRepository, token_service: TokenService):
        self._clients = client_repository
        self._tokens = token_service

    async def issue_token(
        self, client_id: str, client_secret: str, requested_scopes: list[str] | None
    ) -> tuple[str, int, list[str]]:
        """Returns `(access_token, expires_in, granted_scopes)`. Raises
        `InvalidClientError` for unknown client, wrong secret,
        expired/revoked secret (not in `list_active_secrets`), or a disabled
        client — all the *same* error, deliberately, so none of those cases
        is distinguishable from the outside. Raises `InvalidScopeError` if a
        requested scope isn't a subset of the client's registered scopes.
        """
        client = await self._clients.get_by_client_id(client_id)
        if client is None:
            # Still spend a hash-compare's worth of time, against a fixed
            # dummy hash, so "no such client" takes as long as "wrong secret".
            hmac.compare_digest(hash_secret(client_secret), _DUMMY_SECRET_HASH)
            raise InvalidClientError()

        if client.status != ClientStatus.ACTIVE.value:
            raise InvalidClientError()

        active_secrets = await self._clients.list_active_secrets(client.id)
        if not any(verify_secret(client_secret, secret.secret_hash) for secret in active_secrets):
            raise InvalidClientError()

        granted_scopes = self._resolve_scopes(client, requested_scopes)
        token, expires_in = self._tokens.issue(client.client_id, granted_scopes)
        return token, expires_in, granted_scopes

    @staticmethod
    def _resolve_scopes(client: Client, requested_scopes: list[str] | None) -> list[str]:
        registered = set(client.scopes)
        if requested_scopes is None:
            return sorted(registered)
        requested = set(requested_scopes)
        if not requested <= registered:
            raise InvalidScopeError(sorted(requested - registered))
        return sorted(requested)
