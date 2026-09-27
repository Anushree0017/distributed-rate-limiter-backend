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
