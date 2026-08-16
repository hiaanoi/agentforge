import hashlib
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from agentforge.evaluation.protocol import (
    ContextPolicyBinding,
    EvaluationExecutionMode,
    EvaluationProtocol,
    ModelBudgetBinding,
    PlatformBinding,
    ProviderBinding,
    ReplacementPolicy,
)

SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
SHA_E = "e" * 64
SHA_F = "f" * 64


def executable_digest() -> str:
    return hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest()


def platform_binding() -> PlatformBinding:
    return PlatformBinding(
        os_family="WINDOWS" if sys.platform == "win32" else "POSIX",
        python_implementation=sys.implementation.name,
        python_version=".".join(str(item) for item in sys.version_info[:3]),
        executable_path=sys.executable,
        executable_sha256=executable_digest(),
    )


def provider_binding(
    *,
    provider: str = "mock",
    model_id: str = "deterministic-repair-model",
    response_model_id: str | None = None,
) -> ProviderBinding:
    values: dict[str, object] = {
        "provider": provider,
        "model_id": model_id,
        "timeout_seconds": 30,
        "max_retries": 1,
        "store": False,
        "max_output_tokens": 2_000,
        "multi_tool_response_policy": "SEQUENTIAL_READ_ONLY",
        "max_function_calls_per_response": 8,
    }
    if response_model_id is not None:
        values["response_model_id"] = response_model_id
    return ProviderBinding.model_validate(values)


def protocol_payload() -> dict[str, object]:
    system_prompt = "Repair only through AgentForge tools."
    return {
        "protocol_name": "offline-self-durable-v1",
        "execution_mode": EvaluationExecutionMode.OFFLINE_TEST,
        "task_id": "self-durable-double-consumption",
        "fixture_registry_digest": SHA_F,
        "fixture_asset_digest": SHA_A,
        "expected_baseline_fingerprint_digest": SHA_B,
        "task_policy_digest": SHA_C,
        "test_profile_template_digest": SHA_D,
        "provider_binding": provider_binding(),
        "model_budget": ModelBudgetBinding(
            max_model_requests=10,
            max_retries=1,
            max_total_tokens=50_000,
        ),
        "system_prompt_version": 1,
        "system_prompt": system_prompt,
        "task_prompt": "Repair the duplicate dispatch symptom.",
        "tool_schema_digest": SHA_E,
        "context_policy": ContextPolicyBinding(
            max_items=100,
            max_characters=20_000,
            max_utf8_bytes=40_000,
            version="1",
            system_prompt_version="1",
            system_instructions=system_prompt,
        ),
        "completion_correction_mode": "DEFAULT",
        "repetition_count": 3,
        "replacement_policy": ReplacementPolicy(
            max_replacements_per_slot=1,
            replaceable_failure_categories=(
                "MODEL_TIMEOUT",
                "MODEL_TRANSPORT_ERROR",
            ),
        ),
        "platform_binding": platform_binding(),
        "real_model_authorized": False,
    }


def test_protocol_computes_deterministic_nested_and_top_level_digests() -> None:
    first = EvaluationProtocol.model_validate(protocol_payload())
    second = EvaluationProtocol.model_validate(
        {
            **protocol_payload(),
            "replacement_policy": {
                "mode": "INFRASTRUCTURE_ONLY",
                "max_replacements_per_slot": 1,
                "replaceable_failure_categories": [
                    "MODEL_TRANSPORT_ERROR",
                    "MODEL_TIMEOUT",
                ],
            },
        }
    )

    assert first.protocol_digest == second.protocol_digest
    assert first.provider_binding.configuration_digest
    assert first.system_prompt_digest == hashlib.sha256(
        first.system_prompt.encode()
    ).hexdigest()
    assert first.task_prompt_digest == hashlib.sha256(
        first.task_prompt.encode()
    ).hexdigest()
    assert first.fixture_registry_digest != first.fixture_asset_digest
    assert first.model_budget.to_domain().max_model_requests == 10
    assert first.context_policy.to_domain().system_instructions == first.system_prompt


def test_protocol_rejects_drifted_nested_and_top_level_digests() -> None:
    valid = EvaluationProtocol.model_validate(protocol_payload())

    with pytest.raises(ValidationError, match="configuration_digest"):
        ProviderBinding.model_validate(
            {
                **valid.provider_binding.model_dump(mode="json"),
                "configuration_digest": SHA_A,
            }
        )
    with pytest.raises(ValidationError, match="system_prompt_digest"):
        EvaluationProtocol.model_validate(
            {**protocol_payload(), "system_prompt_digest": SHA_A}
        )
    with pytest.raises(ValidationError, match="protocol_digest"):
        EvaluationProtocol.model_validate(
            {**protocol_payload(), "protocol_digest": SHA_A}
        )


@pytest.mark.parametrize(
    "field",
    ["api_key", "token", "secret", "allowed_env", "model_kwargs"],
)
def test_protocol_rejects_secret_or_open_ended_configuration_fields(field: str) -> None:
    with pytest.raises(ValidationError):
        EvaluationProtocol.model_validate(
            {
                **protocol_payload(),
                field: {"OPENAI_API_KEY": "must-not-enter-protocol"},
            }
        )


def test_real_model_protocol_requires_openai_and_explicit_authorization() -> None:
    openai = provider_binding(provider="openai", model_id="pinned-model")
    payload = {
        **protocol_payload(),
        "execution_mode": "REAL_MODEL",
        "provider_binding": openai,
    }

    with pytest.raises(ValidationError, match="authorized"):
        EvaluationProtocol.model_validate(payload)

    real = EvaluationProtocol.model_validate(
        {**payload, "real_model_authorized": True}
    )
    assert real.execution_mode is EvaluationExecutionMode.REAL_MODEL

    with pytest.raises(ValidationError, match="OFFLINE_TEST"):
        EvaluationProtocol.model_validate(
            {**protocol_payload(), "real_model_authorized": True}
        )


def test_openai_binding_freezes_only_exact_alias_or_dated_snapshot() -> None:
    binding = provider_binding(
        provider="openai",
        model_id="gpt-5.4-mini",
        response_model_id="gpt-5.4-mini-2026-03-17",
    )

    assert binding.model_id == "gpt-5.4-mini"
    assert binding.response_model_id == "gpt-5.4-mini-2026-03-17"

    with pytest.raises(ValidationError, match="response model"):
        provider_binding(
            provider="openai",
            model_id="gpt-5.4-mini",
            response_model_id="gpt-5.6-luna",
        )
    with pytest.raises(ValidationError, match="response model"):
        provider_binding(
            provider="openai",
            model_id="gpt-5.4-mini",
            response_model_id="gpt-5.4-mini-latest",
        )


def test_deepseek_binding_requires_exact_response_identity() -> None:
    binding = provider_binding(
        provider="deepseek",
        model_id="deepseek-account-model",
        response_model_id="deepseek-account-model",
    )

    assert binding.response_model_id == "deepseek-account-model"

    with pytest.raises(ValidationError, match="response model"):
        provider_binding(
            provider="deepseek",
            model_id="deepseek-account-model",
            response_model_id="different-model",
        )


def test_real_model_protocol_accepts_deepseek_with_explicit_authorization() -> None:
    deepseek = provider_binding(
        provider="deepseek",
        model_id="deepseek-account-model",
    )

    real = EvaluationProtocol.model_validate(
        {
            **protocol_payload(),
            "execution_mode": "REAL_MODEL",
            "provider_binding": deepseek,
            "real_model_authorized": True,
        }
    )

    assert real.provider_binding.provider == "deepseek"


def test_replacement_policy_rejects_model_quality_failures() -> None:
    with pytest.raises(ValidationError, match="infrastructure"):
        ReplacementPolicy(
            max_replacements_per_slot=1,
            replaceable_failure_categories=("TESTS_FAILED",),
        )


def test_platform_binding_requires_current_absolute_executable_and_digest() -> None:
    with pytest.raises(ValidationError, match="digest"):
        PlatformBinding(
            os_family="WINDOWS" if sys.platform == "win32" else "POSIX",
            python_implementation=sys.implementation.name,
            python_version=".".join(str(item) for item in sys.version_info[:3]),
            executable_path=sys.executable,
            executable_sha256=SHA_A,
        )
    with pytest.raises(ValidationError, match="absolute"):
        PlatformBinding(
            os_family="WINDOWS" if sys.platform == "win32" else "POSIX",
            python_implementation=sys.implementation.name,
            python_version=".".join(str(item) for item in sys.version_info[:3]),
            executable_path="python",
            executable_sha256=SHA_A,
        )
