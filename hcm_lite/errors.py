"""Domain errors. Services raise these; the API layer maps them to HTTP status codes."""

from __future__ import annotations

from typing import Any


class DomainError(Exception):
    status_code: int = 400
    code: str = "bad_request"

    def __init__(self, message: str, *, details: Any | None = None, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details
        if code:
            self.code = code

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"error": self.code, "message": self.message}
        if self.details is not None:
            payload["details"] = self.details
        return payload


class Unauthorized(DomainError):
    status_code, code = 401, "unauthorized"


class Forbidden(DomainError):
    status_code, code = 403, "forbidden"


class NotFound(DomainError):
    status_code, code = 404, "not_found"


class Conflict(DomainError):
    status_code, code = 409, "conflict"


class ValidationFailed(DomainError):
    status_code, code = 422, "validation_failed"


class RateLimited(DomainError):
    status_code, code = 429, "rate_limited"
