import pytest
from pydantic import ValidationError

from agentforge.domain import enums as domain_enums
from agentforge.models import domain as model_domain
from agentforge.models.base import FinalAnswer, ToolCall
from agentforge.models.domain import (
    ModelProviderConfig,
    ModelResponse,
    ModelUsage,
)


def test_model_usage_preserves_unknown_provider_fields_as_none() -> None:
    usage = ModelUsage(input_tokens=10, output_tokens=4, total_tokens=14)

    assert usage.cached_input_tokens is None
    assert usage.reasoning_tokens is None


def test_model_response_contains_provider_neutral_action_and_metadata() -> None:
    response = ModelResponse(
        action=ToolCall(
            type="tool_call",
            call_id="call_123",
            tool="read_file",
            arguments={"path": "README.md"},
        ),
        usage=ModelUsage(input_tokens=10, output_tokens=4, total_tokens=14),
        provider="openai",
        model="test-model",
        provider_request_id="resp_123",
        duration_ms=12,
        attempt_count=1,
        sanitized_metadata={"service_tier": "default"},
    )

    assert response.action.call_id == "call_123"
    assert response.model_dump(mode="json")["provider"] == "openai"


def test_provider_config_does_not_serialize_or_repr_api_key() -> None:
    config = ModelProviderConfig(
        api_key="super-secret",
        model="test-model",
        timeout_seconds=5,
        max_retries=2,
    )

    assert "super-secret" not in repr(config)
    assert "api_key" not in config.model_dump(mode="json")


def test_provider_config_cannot_enable_provider_side_storage() -> None:
    with pytest.raises(ValidationError):
        ModelProviderConfig(
            api_key="super-secret",
            model="test-model",
            store=True,
        )


def test_provider_config_defaults_to_bounded_sequential_read_only_policy() -> None:
    config = ModelProviderConfig(
        api_key="super-secret",
        model="test-model",
    )

    assert config.multi_tool_response_policy.value == "SEQUENTIAL_READ_ONLY"
    assert config.max_function_calls_per_response == 8


def test_multi_tool_domain_types_are_explicit_and_secret_free() -> None:
    policy_type = getattr(domain_enums, "MultiToolResponsePolicy", None)
    info_type = getattr(model_domain, "MultiToolResponseInfo", None)

    assert policy_type is not None
    assert info_type is not None
    info = info_type(
        provider="openai",
        model="test-model",
        provider_request_id="resp_123",
        returned_call_count=3,
        selected_call_count=1,
        discarded_call_count=2,
        selected_tool_name="read_file",
        discarded_tool_names=["search_text", "list_files"],
        policy=policy_type.SEQUENTIAL_READ_ONLY,
        reason="multiple_local_read_calls",
    )

    dumped = info.model_dump(mode="json")
    assert dumped["provider_contract_deviation"] is True
    assert "arguments" not in dumped
    assert "output" not in dumped


def test_model_response_requires_normalized_info_to_match_selected_tool() -> None:
    info = model_domain.MultiToolResponseInfo(
        provider="openai",
        model="test-model",
        returned_call_count=2,
        selected_call_count=1,
        discarded_call_count=1,
        selected_tool_name="read_file",
        discarded_tool_names=["search_text"],
        policy=domain_enums.MultiToolResponsePolicy.SEQUENTIAL_READ_ONLY,
        reason="multiple_local_read_calls",
    )

    with pytest.raises(ValidationError):
        ModelResponse(
            action=ToolCall(
                type="tool_call",
                call_id="call_wrong",
                tool="search_text",
                arguments={"query": "x"},
            ),
            provider="openai",
            model="test-model",
            duration_ms=1,
            attempt_count=1,
            multi_tool_response=info,
        )


@pytest.mark.parametrize("limit", [0, 33])
def test_multi_tool_call_limit_is_bounded(limit: int) -> None:
    with pytest.raises(ValidationError):
        ModelProviderConfig(
            api_key="super-secret",
            model="test-model",
            max_function_calls_per_response=limit,
        )


def test_final_answer_remains_a_valid_model_response_action() -> None:
    response = ModelResponse(
        action=FinalAnswer(type="final", answer="done"),
        provider="mock",
        model="mock",
        duration_ms=0,
        attempt_count=1,
    )

    assert response.action.answer == "done"
