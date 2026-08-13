from agentforge.models.domain import ModelErrorCode, MultiToolResponseInfo


class ModelAdapterError(Exception):
    """Base exception for safe provider-adapter failures."""


class ModelProtocolError(ModelAdapterError):
    """Raised when a provider response has an unsupported action shape."""


class ProviderContractDeviationError(ModelProtocolError):
    """Raised when provider output violates the configured multi-tool contract."""

    def __init__(self, message: str, info: MultiToolResponseInfo) -> None:
        super().__init__(message)
        self.info = info


class ModelOutputInvalidError(ModelAdapterError):
    """Raised when provider action data cannot be decoded."""


class ModelAttemptConflictError(ModelAdapterError):
    """Raised when an attempt identity is replayed with different semantics."""


class ModelRequestError(ModelAdapterError):
    """Safe, provider-neutral request failure."""

    def __init__(
        self,
        code: ModelErrorCode,
        message: str,
        *,
        retryable: bool,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.safe_message = message
        self.retryable = retryable
