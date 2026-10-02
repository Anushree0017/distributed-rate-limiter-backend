"""Domain exceptions for the rules-CRUD service, and the FastAPI handlers that
map them onto the standard error envelope from `api-endpoints.md`:
`{ "error": { "code": "...", "message": "...", "details": {...} } }`.
"""
from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError

from model.identifier_validation import InvalidIdentifierValue
from model.rule_identifier_type import InvalidIdentifierTypesError
from services.rate_limiter.script_loader import ScriptRegistrationError


class RuleNotFoundError(Exception):
    def __init__(self, rule_id):
        self.rule_id = rule_id
        super().__init__(f"Rule {rule_id} not found")


class AlgorithmNotFoundError(Exception):
    def __init__(self, algorithm_id):
        self.algorithm_id = algorithm_id
        super().__init__(f"Algorithm {algorithm_id} not found")


class AlgorithmNameNotFoundError(Exception):
    """Like `AlgorithmNotFoundError`, but for the name-based lookup
    `PATCH /rules/{id}/detach` uses (the caller picks an algorithm by name,
    not id, since a group member has no `algorithm_id` of its own to default
    to once detached).
    """

    def __init__(self, name: str):
        self.name = name
        super().__init__(f"Algorithm named {name!r} not found")


class VersionConflictError(Exception):
    def __init__(self, rule_id, expected_version, actual_version):
        self.rule_id = rule_id
        self.expected_version = expected_version
        self.actual_version = actual_version
        super().__init__(
            f"Rule {rule_id} version mismatch: expected {expected_version}, actual {actual_version}"
        )


class ScopeConflictError(Exception):
    def __init__(self, endpoint, identifier_signature):
        self.endpoint = endpoint
        self.identifier_signature = identifier_signature
        super().__init__(
            f"An active rule already exists for endpoint={endpoint!r}, "
            f"identifier_signature={identifier_signature!r}"
        )


class CrossClientOperationError(Exception):
    """Phase 6: a rule and the group it's being moved into/added to belong to
    different clients. The group invariant ("a rule's client_id equals its
    group's client_id") makes this illegal regardless of any other state —
    see `.claude/plans/phase6/plan.md`'s Step 9.
    """

    def __init__(self, rule_id, group_id):
        self.rule_id = rule_id
        self.group_id = group_id
        super().__init__(f"Rule {rule_id} and group {group_id} belong to different clients")


class RuleGroupNotFoundError(Exception):
    def __init__(self, group_id):
        self.group_id = group_id
        super().__init__(f"Rule group {group_id} not found")


class GroupNameConflictError(Exception):
    def __init__(self, name: str):
        self.name = name
        super().__init__(f"A rule group named {name!r} already exists (case-insensitive)")


class RuleManagedByGroupError(Exception):
    """A grouped rule's `algorithm`, `params`, or `priority` were targeted by
    a direct `PATCH /rules/{id}` — those are governed by the group; the
    caller must use `overrides`, `move-to-group`, or `detach` instead.
    """

    def __init__(self, rule_id):
        self.rule_id = rule_id
        super().__init__(
            f"Rule {rule_id} is managed by a group; use `overrides`, `move-to-group`, or detach"
        )


class OverridesRequireGroupError(Exception):
    def __init__(self, rule_id):
        self.rule_id = rule_id
        super().__init__(f"Rule {rule_id} is standalone; `overrides` only apply to grouped rules")


class RuleNotInGroupError(Exception):
    def __init__(self, rule_id):
        self.rule_id = rule_id
        super().__init__(f"Rule {rule_id} is not a member of any group")


class InvalidOverrideKeysError(Exception):
    """An override key doesn't exist in the group's base params (catches
    typos rather than silently accepting a param the algorithm never sees).
    """

    def __init__(self, unknown_keys: list[str]):
        self.unknown_keys = unknown_keys
        super().__init__(f"Override key(s) not present in base params: {unknown_keys}")


class DuplicateMemberEndpointError(Exception):
    def __init__(self, endpoint: str):
        self.endpoint = endpoint
        super().__init__(f"Duplicate endpoint in members payload: {endpoint!r}")


class GroupMemberConflictError(Exception):
    """Raised by group creation (with initial members) when one or more
    requested endpoints already have an active rule outside this group.
    All-or-nothing: nothing was written.
    """

    def __init__(self, conflicts: list[dict]):
        self.conflicts = conflicts
        super().__init__("One or more member endpoints conflict with an existing rule")


class MembersWriteRaceError(Exception):
    """A racing unique-constraint violation slipped past the service-layer
    pre-check during `POST /groups/{id}/members`'s (or `POST /groups`'s
    initial-members) write phase. Backstop only — the pre-check conflict
    list is the normal path.
    """


class InvalidRuleParamsError(Exception):
    """Raised when a rule's `params` can't build a valid runtime algorithm
    config for its algorithm — validated at write time (create/update) so a
    bad/missing param fails the request instead of the rule silently falling
    back to the static default at `/check` time.
    """

    def __init__(self, algorithm_name: str, reason: str):
        self.algorithm_name = algorithm_name
        self.reason = reason
        super().__init__(f"params are invalid for algorithm {algorithm_name!r}: {reason}")


class ClientNotFoundError(Exception):
    def __init__(self, client_id):
        self.client_id = client_id
        super().__init__(f"Client {client_id!r} not found")


class ClientIdConflictError(Exception):
    def __init__(self, client_id: str):
        self.client_id = client_id
        super().__init__(f"A client with client_id {client_id!r} already exists")


class TooManyActiveSecretsError(Exception):
    """At most two active secrets per client, so rotation has no downtime —
    a third requires revoking one first."""

    def __init__(self, client_id: str):
        self.client_id = client_id
        super().__init__(f"Client {client_id!r} already has two active secrets; revoke one before adding another")


class LastActiveSecretError(Exception):
    """Refuses to revoke a client's only remaining active secret unless the
    client is itself disabled (which already blocks new token issuance)."""

    def __init__(self, client_id: str):
        self.client_id = client_id
        super().__init__(f"Cannot revoke the last active secret of client {client_id!r} while it is active")


class ClientSecretNotFoundError(Exception):
    def __init__(self, secret_id):
        self.secret_id = secret_id
        super().__init__(f"Client secret {secret_id} not found")


class InvalidRequestError(Exception):
    """RFC 6749 `invalid_request` — the token request itself is malformed
    (missing `grant_type`, unsupported `grant_type`, no credentials
    supplied by either form fields or HTTP Basic).
    """

    def __init__(self, description: str):
        self.description = description
        super().__init__(description)


class InvalidClientError(Exception):
    """RFC 6749 `invalid_client` — deliberately the *same* exception for
    unknown client, wrong secret, expired/revoked secret, and a disabled
    client (see `services/auth_service.py`), so none of those cases is
    distinguishable from the outside.
    """


class InvalidScopeError(Exception):
    """RFC 6749 `invalid_scope` — a requested scope isn't a subset of the
    client's registered scopes.
    """

    def __init__(self, unknown_scopes: list[str]):
        self.unknown_scopes = unknown_scopes
        super().__init__(f"Requested scope(s) not granted to this client: {unknown_scopes}")


class AuthenticationError(Exception):
    """No bearer token, a malformed one, or one that fails verification
    (expired, wrong signature, unknown kid, ...) — 401, per
    `.claude/plans/phase6/plan.md`'s endpoint/scope table. Never echoes the
    token or the underlying `TokenError` reason to the caller.
    """


class AuthorizationError(Exception):
    """A verified, valid token whose client lacks the required scope, or
    whose client is disabled per `ClientsCache` — 403, distinct from
    `AuthenticationError`'s 401 (the token itself is fine; the caller just
    isn't allowed to do this).
    """


def _oauth_error_response(status_code: int, error: str, description: str) -> JSONResponse:
    """RFC 6749 §5.2 error shape — distinct from this service's own
    `{"error": {"code", "message", "details"}}` envelope, since the token
    endpoint's error contract is the OAuth2 spec's, not ours.
    """
    return JSONResponse(status_code=status_code, content={"error": error, "error_description": description})


def _error_response(status_code: int, code: str, message: str, details: dict | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message, "details": details or {}}},
    )


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(RuleNotFoundError)
    async def _rule_not_found(request: Request, exc: RuleNotFoundError) -> JSONResponse:
        return _error_response(404, "RULE_NOT_FOUND", str(exc), {"rule_id": str(exc.rule_id)})

    @app.exception_handler(AlgorithmNotFoundError)
    async def _algorithm_not_found(request: Request, exc: AlgorithmNotFoundError) -> JSONResponse:
        return _error_response(
            422, "ALGORITHM_NOT_FOUND", str(exc), {"algorithm_id": str(exc.algorithm_id)}
        )

    @app.exception_handler(AlgorithmNameNotFoundError)
    async def _algorithm_name_not_found(request: Request, exc: AlgorithmNameNotFoundError) -> JSONResponse:
        return _error_response(422, "ALGORITHM_NOT_FOUND", str(exc), {"algorithm_name": exc.name})

    @app.exception_handler(VersionConflictError)
    async def _version_conflict(request: Request, exc: VersionConflictError) -> JSONResponse:
        return _error_response(
            409,
            "VERSION_CONFLICT",
            str(exc),
            {
                "rule_id": str(exc.rule_id),
                "expected_version": exc.expected_version,
                "actual_version": exc.actual_version,
            },
        )

    @app.exception_handler(ScopeConflictError)
    async def _scope_conflict(request: Request, exc: ScopeConflictError) -> JSONResponse:
        return _error_response(
            409,
            "SCOPE_CONFLICT",
            "An active rule already exists for this endpoint/identifier scope",
            {
                "endpoint": exc.endpoint,
                "identifier_signature": exc.identifier_signature,
            },
        )

    @app.exception_handler(InvalidRuleParamsError)
    async def _invalid_rule_params(request: Request, exc: InvalidRuleParamsError) -> JSONResponse:
        return _error_response(
            422, "INVALID_RULE_PARAMS", str(exc), {"algorithm_name": exc.algorithm_name}
        )

    @app.exception_handler(CrossClientOperationError)
    async def _cross_client_operation(request: Request, exc: CrossClientOperationError) -> JSONResponse:
        return _error_response(
            409, "CROSS_CLIENT_OPERATION", str(exc), {"rule_id": str(exc.rule_id), "group_id": str(exc.group_id)}
        )

    @app.exception_handler(RuleGroupNotFoundError)
    async def _rule_group_not_found(request: Request, exc: RuleGroupNotFoundError) -> JSONResponse:
        return _error_response(404, "RULE_GROUP_NOT_FOUND", str(exc), {"group_id": str(exc.group_id)})

    @app.exception_handler(GroupNameConflictError)
    async def _group_name_conflict(request: Request, exc: GroupNameConflictError) -> JSONResponse:
        return _error_response(409, "GROUP_NAME_CONFLICT", str(exc), {"name": exc.name})

    @app.exception_handler(RuleManagedByGroupError)
    async def _rule_managed_by_group(request: Request, exc: RuleManagedByGroupError) -> JSONResponse:
        return _error_response(409, "RULE_MANAGED_BY_GROUP", str(exc), {"rule_id": str(exc.rule_id)})

    @app.exception_handler(OverridesRequireGroupError)
    async def _overrides_require_group(request: Request, exc: OverridesRequireGroupError) -> JSONResponse:
        return _error_response(422, "OVERRIDES_REQUIRE_GROUP", str(exc), {"rule_id": str(exc.rule_id)})

    @app.exception_handler(RuleNotInGroupError)
    async def _rule_not_in_group(request: Request, exc: RuleNotInGroupError) -> JSONResponse:
        return _error_response(409, "RULE_NOT_IN_GROUP", str(exc), {"rule_id": str(exc.rule_id)})

    @app.exception_handler(InvalidOverrideKeysError)
    async def _invalid_override_keys(request: Request, exc: InvalidOverrideKeysError) -> JSONResponse:
        return _error_response(422, "INVALID_OVERRIDE_KEYS", str(exc), {"unknown_keys": exc.unknown_keys})

    @app.exception_handler(DuplicateMemberEndpointError)
    async def _duplicate_member_endpoint(request: Request, exc: DuplicateMemberEndpointError) -> JSONResponse:
        return _error_response(422, "DUPLICATE_MEMBER_ENDPOINT", str(exc), {"endpoint": exc.endpoint})

    @app.exception_handler(GroupMemberConflictError)
    async def _group_member_conflict(request: Request, exc: GroupMemberConflictError) -> JSONResponse:
        return _error_response(409, "GROUP_MEMBER_CONFLICT", str(exc), {"conflicts": exc.conflicts})

    @app.exception_handler(MembersWriteRaceError)
    async def _members_write_race(request: Request, exc: MembersWriteRaceError) -> JSONResponse:
        return _error_response(409, "GROUP_MEMBER_CONFLICT", "A concurrent write conflicted with this request; retry")

    @app.exception_handler(InvalidIdentifierTypesError)
    async def _invalid_identifier_types(request: Request, exc: InvalidIdentifierTypesError) -> JSONResponse:
        return _error_response(422, "INVALID_IDENTIFIER_TYPES", str(exc))

    @app.exception_handler(InvalidIdentifierValue)
    async def _invalid_identifier_value(request: Request, exc: InvalidIdentifierValue) -> JSONResponse:
        # Never echo the raw value — only the type and reason (exc's own
        # message already omits it).
        return _error_response(
            422,
            "INVALID_IDENTIFIER_VALUE",
            str(exc),
            {"identifier_type": exc.identifier_type.value, "reason": exc.reason, "index": exc.index},
        )

    @app.exception_handler(IntegrityError)
    async def _integrity_error(request: Request, exc: IntegrityError) -> JSONResponse:
        # Final backstop for the race-condition case the service-layer
        # pre-check can't fully close: the unique-violation on
        # `ux_rules_active_scope` (see db_schema.sql) surfaces here as a raw
        # IntegrityError if two concurrent requests both pass the pre-check.
        return _error_response(
            409,
            "SCOPE_CONFLICT",
            "An active rule already exists for this endpoint/identifier scope",
        )

    @app.exception_handler(ScriptRegistrationError)
    async def _script_registration_failed(request: Request, exc: ScriptRegistrationError) -> JSONResponse:
        return _error_response(503, "SCRIPT_REGISTRATION_FAILED", str(exc))

    @app.exception_handler(ClientNotFoundError)
    async def _client_not_found(request: Request, exc: ClientNotFoundError) -> JSONResponse:
        return _error_response(404, "CLIENT_NOT_FOUND", str(exc), {"client_id": str(exc.client_id)})

    @app.exception_handler(ClientIdConflictError)
    async def _client_id_conflict(request: Request, exc: ClientIdConflictError) -> JSONResponse:
        return _error_response(409, "CLIENT_ID_CONFLICT", str(exc), {"client_id": exc.client_id})

    @app.exception_handler(TooManyActiveSecretsError)
    async def _too_many_active_secrets(request: Request, exc: TooManyActiveSecretsError) -> JSONResponse:
        return _error_response(409, "TOO_MANY_ACTIVE_SECRETS", str(exc), {"client_id": exc.client_id})

    @app.exception_handler(LastActiveSecretError)
    async def _last_active_secret(request: Request, exc: LastActiveSecretError) -> JSONResponse:
        return _error_response(409, "LAST_ACTIVE_SECRET", str(exc), {"client_id": exc.client_id})

    @app.exception_handler(ClientSecretNotFoundError)
    async def _client_secret_not_found(request: Request, exc: ClientSecretNotFoundError) -> JSONResponse:
        return _error_response(404, "CLIENT_SECRET_NOT_FOUND", str(exc), {"secret_id": str(exc.secret_id)})

    @app.exception_handler(InvalidRequestError)
    async def _invalid_request(request: Request, exc: InvalidRequestError) -> JSONResponse:
        return _oauth_error_response(400, "invalid_request", exc.description)

    @app.exception_handler(InvalidClientError)
    async def _invalid_client(request: Request, exc: InvalidClientError) -> JSONResponse:
        return JSONResponse(
            status_code=401,
            content={"error": "invalid_client", "error_description": "Client authentication failed"},
            headers={"WWW-Authenticate": "Basic"},
        )

    @app.exception_handler(InvalidScopeError)
    async def _invalid_scope(request: Request, exc: InvalidScopeError) -> JSONResponse:
        return _oauth_error_response(400, "invalid_scope", f"Scope(s) not granted to this client: {exc.unknown_scopes}")

    @app.exception_handler(AuthenticationError)
    async def _authentication_error(request: Request, exc: AuthenticationError) -> JSONResponse:
        return JSONResponse(
            status_code=401,
            content={"error": {"code": "UNAUTHORIZED", "message": "Missing or invalid bearer token", "details": {}}},
            headers={"WWW-Authenticate": "Bearer"},
        )

    @app.exception_handler(AuthorizationError)
    async def _authorization_error(request: Request, exc: AuthorizationError) -> JSONResponse:
        return _error_response(403, "FORBIDDEN", "Insufficient scope, or the client is disabled")

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        # FastAPI's default error shape includes the raw request `input` per
        # error (e.g. a model_validator failure on RateLimitCheckRequestDTO
        # echoes the whole payload, identifier values included) — strip it.
        # `loc`/`msg`/`type` already say what and where without needing it.
        errors = [
            {key: value for key, value in error.items() if key != "input"} for error in jsonable_encoder(exc.errors())
        ]
        return _error_response(422, "VALIDATION_ERROR", "Request validation failed", {"errors": errors})
