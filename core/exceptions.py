"""Domain exceptions for the rules-CRUD service, and the FastAPI handlers that
map them onto the standard error envelope from `api-endpoints.md`:
`{ "error": { "code": "...", "message": "...", "details": {...} } }`.
"""
from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError


class RuleNotFoundError(Exception):
    def __init__(self, rule_id):
        self.rule_id = rule_id
        super().__init__(f"Rule {rule_id} not found")


class AlgorithmNotFoundError(Exception):
    def __init__(self, algorithm_id):
        self.algorithm_id = algorithm_id
        super().__init__(f"Algorithm {algorithm_id} not found")


class VersionConflictError(Exception):
    def __init__(self, rule_id, expected_version, actual_version):
        self.rule_id = rule_id
        self.expected_version = expected_version
        self.actual_version = actual_version
        super().__init__(
            f"Rule {rule_id} version mismatch: expected {expected_version}, actual {actual_version}"
        )


class ScopeConflictError(Exception):
    def __init__(self, endpoint, identifier_type, identifier_value):
        self.endpoint = endpoint
        self.identifier_type = identifier_type
        self.identifier_value = identifier_value
        super().__init__(
            f"An active rule already exists for endpoint={endpoint!r}, "
            f"identifier_type={identifier_type!r}, identifier_value={identifier_value!r}"
        )


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
                "identifier_type": exc.identifier_type,
                "identifier_value": exc.identifier_value,
            },
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

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return _error_response(
            422, "VALIDATION_ERROR", "Request validation failed", {"errors": jsonable_encoder(exc.errors())}
        )
