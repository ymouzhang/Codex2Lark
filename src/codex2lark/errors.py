from __future__ import annotations

from enum import StrEnum
from typing import Any


class ErrorCategory(StrEnum):
    VALIDATION = "validation_error"
    AUTHENTICATION = "authentication_error"
    PERMISSION = "permission_error"
    CONFLICT = "conflict_error"
    AMBIGUITY = "ambiguity_error"
    UPSTREAM = "upstream_error"
    TIMEOUT = "timeout_error"
    VERIFICATION = "verification_error"
    INTERNAL = "internal_error"


class Codex2LarkError(Exception):
    """Base error whose public representation is safe for an MCP response."""

    def __init__(
        self,
        category: ErrorCategory,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.message = message
        self.details = details or {}

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "category": self.category.value,
                "message": self.message,
                "details": self.details,
            },
        }


class LarkCliError(Codex2LarkError):
    pass


class ConflictError(Codex2LarkError):
    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCategory.CONFLICT, message, details=details)


class AmbiguityError(Codex2LarkError):
    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCategory.AMBIGUITY, message, details=details)


class VerificationError(Codex2LarkError):
    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCategory.VERIFICATION, message, details=details)
