from agentforge.domain.enums import ToolErrorCode


class AgentForgeError(Exception):
    """Base exception for expected AgentForge failures."""


class InvalidStateTransitionError(AgentForgeError):
    """Raised when a run attempts an invalid state transition."""


class RunNotFoundError(AgentForgeError):
    """Raised when a run cannot be found in persistent storage."""


class ApprovalNotFoundError(AgentForgeError):
    """Raised when an approval request cannot be found."""


class CheckpointNotFoundError(AgentForgeError):
    """Raised when a checkpoint cannot be found."""


class DuplicateApprovalError(AgentForgeError):
    """Raised when a Run checkpoint already has an approval request."""


class MutationBindingNotFoundError(AgentForgeError):
    """Raised when an approval has no durable mutation binding."""


class MutationExecutionNotFoundError(AgentForgeError):
    """Raised when a mutation execution record cannot be found."""


class DuplicateMutationExecutionError(AgentForgeError):
    """Raised when an approval or digest already has a mutation execution."""


class DuplicateTestProfileError(AgentForgeError):
    """Raised when trusted startup code registers the same profile twice."""


class TestApprovalBindingNotFoundError(AgentForgeError):
    """Raised when an approval has no durable test-profile binding."""


class ProcessExecutionNotFoundError(AgentForgeError):
    """Raised when a process execution record cannot be found."""


class DuplicateProcessExecutionError(AgentForgeError):
    """Raised when a process execution violates its durable identity."""


class ApprovalDecisionConflictError(AgentForgeError):
    """Raised when a resolved approval receives a different decision."""


class ResumeNotAllowedError(AgentForgeError):
    """Raised when a Run cannot enter or continue recovery."""


class ToolRuntimeError(AgentForgeError):
    """Expected tool failure with a stable model-facing error code."""

    def __init__(self, code: ToolErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.safe_message = message


class ToolNotFoundError(ToolRuntimeError):
    """Raised when a requested tool is not registered."""

    def __init__(self, name: str) -> None:
        super().__init__(ToolErrorCode.TOOL_NOT_FOUND, f"Tool {name!r} is not registered")


class DuplicateToolError(ToolRuntimeError):
    """Raised when a registry receives the same tool name twice."""

    def __init__(self, name: str) -> None:
        super().__init__(ToolErrorCode.DUPLICATE_TOOL, f"Tool {name!r} is already registered")


class InvalidToolSpecError(ToolRuntimeError):
    """Raised when a ToolSpec disagrees with its executable binding."""

    def __init__(self, name: str) -> None:
        super().__init__(
            ToolErrorCode.INVALID_TOOL_SPEC,
            f"Tool {name!r} input schema does not match its Pydantic model",
        )


class ToolExecutionError(ToolRuntimeError):
    """Raised when a registered tool cannot complete its action."""


class TestProfileBindingMismatchError(ToolExecutionError):
    """Raised when a registered profile no longer matches its approval binding."""


class WorkspacePathError(ToolRuntimeError):
    """Raised when a requested path violates workspace constraints."""


class SensitivePathError(ToolRuntimeError):
    """Raised when a requested path matches a sensitive-file rule."""


class ModelProviderError(AgentForgeError):
    """Raised when a model provider cannot produce a response."""


class ModelOutputError(AgentForgeError):
    """Raised when model output does not match the supported schema."""


class PersistenceError(AgentForgeError):
    """Raised when durable storage cannot complete an operation."""
