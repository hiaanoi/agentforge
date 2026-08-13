import hashlib
import sys
from pathlib import Path

import pytest
from pydantic import SecretStr

from agentforge.evaluation.protocol import (
    ContextPolicyBinding,
    EvaluationProtocol,
    ModelBudgetBinding,
    PlatformBinding,
    ProviderBinding,
    ReplacementPolicy,
)
from agentforge.evaluation.provider_factory import (
    EvaluationProviderBindingError,
    MockEvaluationProviderFactory,
    OpenAIEvaluationProviderFactory,
)
from agentforge.models.base import ModelRequest
from agentforge.models.domain import (
    ModelErrorCode,
    ModelProviderConfig,
    ModelResponse,
)
from agentforge.models.errors import ModelRequestError
from agentforge.models.mock import MockModelProvider

SHA = "a" * 64


def protocol(
    *,
    real: bool,
    model_id: str = "bound-model",
    response_model_id: str | None = None,
    task_id: str = "self-durable-double-consumption",
) -> EvaluationProtocol:
    executable = Path(sys.executable).resolve()
    system_prompt = "Use only the constrained repair tools."
    return EvaluationProtocol(
        protocol_name=f"{'real' if real else 'offline'}-{model_id}",
        execution_mode="REAL_MODEL" if real else "OFFLINE_TEST",
        task_id=task_id,
        fixture_registry_digest=SHA,
        fixture_asset_digest=SHA,
        expected_baseline_fingerprint_digest=SHA,
        task_policy_digest=SHA,
        test_profile_template_digest=SHA,
        provider_binding=ProviderBinding(
            provider="openai" if real else "mock",
            model_id=model_id,
            response_model_id=response_model_id or model_id,
            timeout_seconds=45,
            max_retries=1,
            store=False,
            max_output_tokens=800,
            multi_tool_response_policy="STRICT",
            max_function_calls_per_response=4,
        ),
        model_budget=ModelBudgetBinding(
            max_model_requests=10,
            max_retries=1,
            max_total_tokens=50_000,
        ),
        system_prompt_version=1,
        system_prompt=system_prompt,
        task_prompt="Repair it.",
        tool_schema_digest=SHA,
        context_policy=ContextPolicyBinding(
            version="1",
            system_prompt_version="1",
            system_instructions=system_prompt,
        ),
        completion_correction_mode="DEFAULT",
        repetition_count=3,
        replacement_policy=ReplacementPolicy(
            max_replacements_per_slot=1,
            replaceable_failure_categories=("MODEL_TIMEOUT",),
        ),
        platform_binding=PlatformBinding(
            os_family="WINDOWS" if sys.platform == "win32" else "POSIX",
            python_implementation=sys.implementation.name,
            python_version=".".join(str(item) for item in sys.version_info[:3]),
            executable_path=str(executable),
            executable_sha256=hashlib.sha256(executable.read_bytes()).hexdigest(),
        ),
        real_model_authorized=real,
    )


@pytest.mark.asyncio
async def test_offline_factory_accepts_only_exact_mock_binding() -> None:
    value = protocol(real=False)
    provider = MockEvaluationProviderFactory(
        [{"type": "final", "answer": "done"}]
    ).create(value)

    provider.validate_protocol(value)
    response = await provider.generate(
        ModelRequest(task="repair", step_number=1)
    )

    assert provider.name == "mock"
    assert provider.configuration_digest == (
        value.provider_binding.configuration_digest
    )
    assert provider.journal_identity == (
        f"{value.provider_binding.provider}/"
        f"{value.provider_binding.response_model_id}"
    )
    assert response.provider == "mock"
    assert response.model == "bound-model"

    with pytest.raises(EvaluationProviderBindingError, match="protocol"):
        provider.validate_protocol(protocol(real=False, model_id="other-model"))
    with pytest.raises(EvaluationProviderBindingError, match="OFFLINE_TEST"):
        MockEvaluationProviderFactory([]).create(protocol(real=True))


def test_openai_factory_requires_validated_gate() -> None:
    with pytest.raises(EvaluationProviderBindingError, match="gate"):
        OpenAIEvaluationProviderFactory(
            SecretStr("secret"),
            provider_builder=MockModelProvider,
        ).create(protocol(real=True))


def test_openai_factory_maps_every_frozen_field_without_serializing_secret() -> None:
    captured: list[ModelProviderConfig] = []

    def build(config: ModelProviderConfig) -> MockModelProvider:
        captured.append(config)
        return MockModelProvider(
            [{"type": "final", "answer": "unused"}],
            model_id=config.model,
            provider_name="openai",
        )

    value = protocol(real=True)
    gate = _ValidatedGate(value)
    provider = OpenAIEvaluationProviderFactory(
        SecretStr("must-not-be-persisted"),
        execution_gate=gate,
        provider_builder=build,
    ).create(value)

    assert provider.name == "openai"
    assert len(captured) == 1
    config = captured[0]
    assert config.model == "bound-model"
    assert config.timeout_seconds == 45
    assert config.max_retries == 1
    assert config.store is False
    assert config.max_output_tokens == 800
    assert config.multi_tool_response_policy.value == "STRICT"
    assert config.max_function_calls_per_response == 4

    serialized = " ".join(
        (
            value.model_dump_json(),
            config.model_dump_json(),
            repr(provider),
        )
    )
    assert "must-not-be-persisted" not in serialized
    assert "api_key" not in serialized
    assert gate.checked == [value.protocol_digest]

    with pytest.raises(EvaluationProviderBindingError, match="REAL_MODEL"):
        OpenAIEvaluationProviderFactory(
            SecretStr("secret"),
            execution_gate=gate,
            provider_builder=build,
        ).create(protocol(real=False))


@pytest.mark.asyncio
async def test_response_identity_mismatch_is_non_retryable_configuration_error() -> None:
    class WrongIdentityProvider:
        name = "openai"

        async def generate(self, request: ModelRequest) -> ModelResponse:
            del request
            return ModelResponse(
                action={"type": "final", "answer": "unused"},
                provider="openai",
                model="different-model",
                duration_ms=1,
                attempt_count=1,
            )

    value = protocol(real=True)
    provider = OpenAIEvaluationProviderFactory(
        SecretStr("secret"),
        execution_gate=_ValidatedGate(value),
        provider_builder=lambda _: WrongIdentityProvider(),
    ).create(value)

    with pytest.raises(ModelRequestError) as raised:
        await provider.generate(ModelRequest(task="repair", step_number=1))

    assert raised.value.code is ModelErrorCode.MODEL_BAD_REQUEST
    assert raised.value.retryable is False
    assert "different-model" not in str(raised.value)


@pytest.mark.asyncio
async def test_response_identity_accepts_only_the_frozen_snapshot() -> None:
    class SnapshotProvider:
        name = "openai"

        async def generate(self, request: ModelRequest) -> ModelResponse:
            del request
            return ModelResponse(
                action={"type": "final", "answer": "done"},
                provider="openai",
                model="gpt-5.4-mini-2026-03-17",
                duration_ms=1,
                attempt_count=1,
            )

    value = protocol(
        real=True,
        model_id="gpt-5.4-mini",
        response_model_id="gpt-5.4-mini-2026-03-17",
    )
    provider = OpenAIEvaluationProviderFactory(
        SecretStr("secret"),
        execution_gate=_ValidatedGate(value),
        provider_builder=lambda _: SnapshotProvider(),
    ).create(value)

    response = await provider.generate(ModelRequest(task="repair", step_number=1))

    assert response.model == "gpt-5.4-mini-2026-03-17"


class _ValidatedGate:
    def __init__(self, value: EvaluationProtocol) -> None:
        self._digest = value.protocol_digest
        self.checked: list[str] = []

    def require_protocol(self, value: EvaluationProtocol) -> None:
        if value.protocol_digest != self._digest:
            raise RuntimeError("Protocol is not authorized")
        self.checked.append(value.protocol_digest)
