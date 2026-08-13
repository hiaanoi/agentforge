from collections.abc import Callable
from copy import deepcopy
from typing import Protocol

from pydantic import SecretStr

from agentforge.evaluation.protocol import (
    EvaluationExecutionMode,
    EvaluationProtocol,
    ProviderBinding,
)
from agentforge.evaluation.real_model_gate import (
    RealModelAuthorizationError,
    RealModelExecutionGate,
)
from agentforge.models.base import ModelProvider, ModelRequest
from agentforge.models.domain import (
    ModelErrorCode,
    ModelProviderConfig,
    ModelResponse,
)
from agentforge.models.errors import ModelRequestError
from agentforge.models.mock import MockModelProvider
from agentforge.models.openai_provider import OpenAIModelProvider


class EvaluationProviderBindingError(RuntimeError):
    pass


class EvaluationProviderFactory(Protocol):
    def create(self, protocol: EvaluationProtocol) -> "ProtocolBoundModelProvider": ...


class ProtocolBoundModelProvider:
    def __init__(
        self,
        protocol: EvaluationProtocol,
        delegate: ModelProvider,
    ) -> None:
        self._protocol_digest = protocol.protocol_digest
        self._execution_mode = protocol.execution_mode
        self._binding = protocol.provider_binding.model_copy(deep=True)
        self._delegate = delegate
        self._validate_delegate()

    @property
    def name(self) -> str:
        return self._binding.provider

    @property
    def journal_identity(self) -> str:
        return f"{self.name}/{self._binding.response_model_id}"

    @property
    def configuration_digest(self) -> str:
        return self._binding.configuration_digest

    def validate_protocol(self, protocol: EvaluationProtocol) -> None:
        if (
            protocol.protocol_digest != self._protocol_digest
            or protocol.execution_mode is not self._execution_mode
            or protocol.provider_binding != self._binding
        ):
            raise EvaluationProviderBindingError(
                "Provider does not match the frozen evaluation protocol"
            )
        self._validate_binding()
        self._validate_delegate()

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self._validate_binding()
        self._validate_delegate()
        response = await self._delegate.generate(request)
        if (
            response.provider != self._binding.provider
            or response.model != self._binding.response_model_id
        ):
            raise ModelRequestError(
                ModelErrorCode.MODEL_BAD_REQUEST,
                "Provider response identity does not match the frozen binding",
                retryable=False,
            )
        return response

    def __repr__(self) -> str:
        return (
            "ProtocolBoundModelProvider("
            f"provider={self.name!r},"
            f"configuration_digest={self.configuration_digest!r})"
        )

    def _validate_binding(self) -> None:
        try:
            validated = ProviderBinding.model_validate(
                self._binding.model_dump(mode="json")
            )
        except ValueError as exc:
            raise EvaluationProviderBindingError(
                "Frozen provider binding is invalid"
            ) from exc
        if validated != self._binding:
            raise EvaluationProviderBindingError(
                "Frozen provider binding digest has drifted"
            )

    def _validate_delegate(self) -> None:
        if self._delegate.name != self._binding.provider:
            raise EvaluationProviderBindingError(
                "Provider implementation does not match the frozen binding"
            )


class MockEvaluationProviderFactory:
    def __init__(self, responses: list[object]) -> None:
        self._responses = deepcopy(responses)

    def create(self, protocol: EvaluationProtocol) -> ProtocolBoundModelProvider:
        if (
            protocol.execution_mode is not EvaluationExecutionMode.OFFLINE_TEST
            or protocol.provider_binding.provider != "mock"
            or protocol.real_model_authorized
        ):
            raise EvaluationProviderBindingError(
                "Mock provider requires an exact OFFLINE_TEST protocol"
            )
        return ProtocolBoundModelProvider(
            protocol,
            MockModelProvider(
                self._responses,
                model_id=protocol.provider_binding.model_id,
            ),
        )


class OpenAIEvaluationProviderFactory:
    def __init__(
        self,
        api_key: SecretStr,
        *,
        execution_gate: RealModelExecutionGate | None = None,
        provider_builder: Callable[[ModelProviderConfig], ModelProvider]
        | None = None,
    ) -> None:
        self._api_key = api_key
        self._execution_gate = execution_gate
        self._provider_builder = provider_builder or OpenAIModelProvider

    def create(self, protocol: EvaluationProtocol) -> ProtocolBoundModelProvider:
        if (
            protocol.execution_mode is not EvaluationExecutionMode.REAL_MODEL
            or protocol.provider_binding.provider != "openai"
            or not protocol.real_model_authorized
        ):
            raise EvaluationProviderBindingError(
                "OpenAI provider requires an authorized REAL_MODEL protocol"
            )
        if self._execution_gate is None:
            raise EvaluationProviderBindingError(
                "A validated real-model execution gate is required"
            )
        if not self._api_key.get_secret_value().strip():
            raise EvaluationProviderBindingError(
                "OpenAI runtime secret is missing"
            )
        try:
            self._execution_gate.require_protocol(protocol)
        except RealModelAuthorizationError as exc:
            raise EvaluationProviderBindingError(
                "Real-model execution gate rejected the Protocol"
            ) from exc
        binding = protocol.provider_binding
        config = ModelProviderConfig(
            api_key=self._api_key,
            model=binding.model_id,
            timeout_seconds=binding.timeout_seconds,
            max_retries=binding.max_retries,
            store=binding.store,
            max_output_tokens=binding.max_output_tokens,
            multi_tool_response_policy=binding.multi_tool_response_policy,
            max_function_calls_per_response=(
                binding.max_function_calls_per_response
            ),
        )
        return ProtocolBoundModelProvider(
            protocol,
            self._provider_builder(config),
        )
