from enum import StrEnum


class ReceiptStatus(StrEnum):
    """Closed lifecycle for an idempotent application command."""

    ACCEPTED = "ACCEPTED"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    INDETERMINATE = "INDETERMINATE"


class LifecycleStatus(StrEnum):
    """Coarse product lifecycle, separate from outcome."""

    CREATED = "CREATED"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    TERMINAL = "TERMINAL"


class OutcomeStatus(StrEnum):
    """Closed result vocabulary for product operations."""

    VERIFIED = "VERIFIED"
    UNVERIFIED = "UNVERIFIED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class ProfilePurpose(StrEnum):
    DEVELOPMENT = "development"
    VERIFICATION = "verification"
    UTILITY = "utility"


class VerificationRuntimeMode(StrEnum):
    SYSTEM_RUNTIME = "SYSTEM_RUNTIME"


class RuntimeTrustClass(StrEnum):
    NON_HERMETIC = "NON_HERMETIC"


class VerificationCapsuleState(StrEnum):
    STAGING = "STAGING"
    SEALED = "SEALED"


class RunControlRequestType(StrEnum):
    """Closed control operations accepted by the cooperative Run owner."""

    CANCEL = "CANCEL"


class RunControlRequestStatus(StrEnum):
    """Durable cancellation request states."""

    REQUESTED = "REQUESTED"
    CANCELLED = "CANCELLED"
    INDETERMINATE = "INDETERMINATE"
