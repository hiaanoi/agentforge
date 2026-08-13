from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Self
from uuid import UUID, uuid4

from pydantic import field_validator, model_validator

from agentforge.application.config import UnsafeConfigurationError
from agentforge.application.dto import PublicDto
from agentforge.application.kernel_errors import (
    IdempotencyConflictError,
    IncompatibleProductSchemaError,
    KernelPersistenceError,
    ProfileTrustMismatchError,
    SourceRevisionConflictError,
    StaleFenceError,
)
from agentforge.domain.errors import ApprovalNotFoundError, RunNotFoundError


class ApplicationErrorCode(StrEnum):
    INVALID_REQUEST = "INVALID_REQUEST"
    NOT_FOUND = "NOT_FOUND"
    COMMAND_CONFLICT = "COMMAND_CONFLICT"
    PROFILE_TRUST_MISMATCH = "PROFILE_TRUST_MISMATCH"
    SOURCE_CONFLICT = "SOURCE_CONFLICT"
    STALE_OWNER = "STALE_OWNER"
    INCOMPATIBLE_SCHEMA = "INCOMPATIBLE_SCHEMA"
    PERSISTENCE_FAILURE = "PERSISTENCE_FAILURE"
    INTERNAL_ERROR = "INTERNAL_ERROR"


@dataclass(frozen=True, slots=True)
class _CatalogEntry:
    safe_message: str
    retryable: bool
    exit_code: int


_CATALOG: dict[ApplicationErrorCode, _CatalogEntry] = {
    ApplicationErrorCode.INVALID_REQUEST: _CatalogEntry(
        "The request is invalid.", False, 2
    ),
    ApplicationErrorCode.NOT_FOUND: _CatalogEntry(
        "The requested AgentForge resource was not found.", False, 4
    ),
    ApplicationErrorCode.COMMAND_CONFLICT: _CatalogEntry(
        "The command conflicts with an accepted request.", False, 5
    ),
    ApplicationErrorCode.PROFILE_TRUST_MISMATCH: _CatalogEntry(
        "The requested profile is not trusted with this exact identity.", False, 4
    ),
    ApplicationErrorCode.SOURCE_CONFLICT: _CatalogEntry(
        "The workspace source no longer matches the durable revision.", False, 5
    ),
    ApplicationErrorCode.STALE_OWNER: _CatalogEntry(
        "Run ownership changed; reconnect to the current owner.", True, 6
    ),
    ApplicationErrorCode.INCOMPATIBLE_SCHEMA: _CatalogEntry(
        "The persisted AgentForge schema is incompatible.", False, 3
    ),
    ApplicationErrorCode.PERSISTENCE_FAILURE: _CatalogEntry(
        "AgentForge could not safely persist the operation.", True, 3
    ),
    ApplicationErrorCode.INTERNAL_ERROR: _CatalogEntry(
        "AgentForge could not complete the request.", False, 1
    ),
}


class ApplicationError(PublicDto):

    code: ApplicationErrorCode
    safe_message: str
    retryable: bool
    exit_code: int
    error_id: UUID
    run_id: UUID | None = None

    @field_validator("exit_code", mode="before")
    @classmethod
    def exact_exit_code(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("exit_code must be an exact integer")
        return value

    @model_validator(mode="after")
    def catalog_bound(self) -> Self:
        entry = _CATALOG[self.code]
        if (
            self.safe_message != entry.safe_message
            or self.retryable is not entry.retryable
            or self.exit_code != entry.exit_code
        ):
            raise ValueError("application error must be created from the catalog")
        return self


class ApplicationFailure(RuntimeError):
    """Exception boundary carrying only a catalog-created public error."""

    def __init__(self, error: ApplicationError) -> None:
        self.error = error
        super().__init__(error.safe_message)


def application_error_from_exception(
    exc: BaseException,
    *,
    error_id: UUID | None = None,
    run_id: UUID | None = None,
) -> ApplicationError:
    """Map by closed exception class only; exception text is never observed."""

    if isinstance(exc, (RunNotFoundError, ApprovalNotFoundError)):
        code = ApplicationErrorCode.NOT_FOUND
    elif isinstance(exc, IdempotencyConflictError):
        code = ApplicationErrorCode.COMMAND_CONFLICT
    elif isinstance(exc, ProfileTrustMismatchError):
        code = ApplicationErrorCode.PROFILE_TRUST_MISMATCH
    elif isinstance(exc, SourceRevisionConflictError):
        code = ApplicationErrorCode.SOURCE_CONFLICT
    elif isinstance(exc, StaleFenceError):
        code = ApplicationErrorCode.STALE_OWNER
    elif isinstance(exc, IncompatibleProductSchemaError):
        code = ApplicationErrorCode.INCOMPATIBLE_SCHEMA
    elif isinstance(exc, KernelPersistenceError):
        code = ApplicationErrorCode.PERSISTENCE_FAILURE
    elif isinstance(exc, (ValueError, UnsafeConfigurationError)):
        code = ApplicationErrorCode.INVALID_REQUEST
    else:
        code = ApplicationErrorCode.INTERNAL_ERROR
    entry = _CATALOG[code]
    return ApplicationError(
        code=code,
        safe_message=entry.safe_message,
        retryable=entry.retryable,
        exit_code=entry.exit_code,
        error_id=error_id or uuid4(),
        run_id=run_id,
    )
