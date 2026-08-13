class ProductKernelError(RuntimeError):
    """Base class for safe product-kernel failures."""


class IncompatibleProductSchemaError(ProductKernelError):
    """Raised when persisted state cannot be used by this product version."""

    def __init__(self) -> None:
        super().__init__("incompatible product schema")


class EventAuthorityError(ProductKernelError):
    """Raised when an event append does not carry valid scope authority."""

    def __init__(self) -> None:
        super().__init__("event authority rejected")


class StaleFenceError(ProductKernelError):
    """Raised when a former Run owner attempts a fenced write."""

    def __init__(self) -> None:
        super().__init__("stale event authority")


class EventPayloadError(ProductKernelError):
    """Raised when an event payload is not strict JSON data."""

    def __init__(self) -> None:
        super().__init__("event payload rejected")


class EventPersistenceError(ProductKernelError):
    """Raised when an atomic event append cannot be persisted."""

    def __init__(self) -> None:
        super().__init__("event persistence failed")


class IdempotencyConflictError(ProductKernelError):
    """Raised when one command identity is reused for different semantics."""

    def __init__(self) -> None:
        super().__init__("command identity conflicts with the accepted request")


class InvalidReceiptTransitionError(ProductKernelError):
    """Raised when a durable command receipt cannot make the requested transition."""

    def __init__(self) -> None:
        super().__init__("command receipt transition rejected")


class UnitOfWorkStateError(ProductKernelError):
    """Raised when a transaction scope is reused after its terminal state."""

    def __init__(self) -> None:
        super().__init__("application unit of work is not active")


class IncompleteRunBundleError(ProductKernelError):
    """Raised when recovery observes a Run without all required creation facts."""

    def __init__(self) -> None:
        super().__init__("persisted Run bundle is incomplete")


class KernelPersistenceError(ProductKernelError):
    """Stable safe error for a failed durable kernel operation."""

    def __init__(self) -> None:
        super().__init__("kernel persistence operation failed")


class PersistenceBoundaryError(KernelPersistenceError):
    """Stable safe error for transaction begin/commit/cleanup failures."""

    def __init__(self) -> None:
        super().__init__()


class WorkspaceDigestError(ProductKernelError):
    """Raised when a workspace cannot be safely and deterministically digested."""

    def __init__(self) -> None:
        super().__init__("workspace digest rejected")


class SourceRevisionConflictError(ProductKernelError):
    """Raised when actual source or a revision CAS disagrees with durable facts."""

    def __init__(self) -> None:
        super().__init__("source revision conflict")


class MutationConflictError(ProductKernelError):
    """Raised when one approval identity is reused for different mutation facts."""

    def __init__(self) -> None:
        super().__init__("mutation execution conflict")


class ProfileTrustMismatchError(ProductKernelError):
    """Raised when requested test-profile identity is not exactly trusted."""

    def __init__(self) -> None:
        super().__init__("trusted profile identity mismatch")
