import json
from types import SimpleNamespace

import pytest

from agentforge.context.models import ContextItem, ContextItemKind
from agentforge.domain.enums import MultiToolResponsePolicy, ToolRisk, ToolSource
from agentforge.domain.models import ToolSpec
from agentforge.models.base import FinalAnswer, ModelRequest, ToolCall
from agentforge.models.domain import ModelProviderConfig
from agentforge.models.errors import (
    ModelOutputInvalidError,
    ModelProtocolError,
    ProviderContractDeviationError,
)
from agentforge.models.openai_provider import OpenAIModelProvider


class FakeResponses:
    def __init__(self, response: object) -> None:
        self.response = response
        self.requests: list[dict[str, object]] = []

    async def create(self, **kwargs: object) -> object:
        self.requests.append(kwargs)
        return self.response


class FakeClient:
    def __init__(self, response: object) -> None:
        self.responses = FakeResponses(response)


def request() -> ModelRequest:
    return ModelRequest(
        task="inspect repository",
        step_number=1,
        tools=[
            ToolSpec(
                name="read_file",
                description="Read a file.",
                input_schema={
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
                risk_level=ToolRisk.READ,
            )
        ],
    )


def tool_spec(
    name: str,
    *,
    risk: ToolRisk = ToolRisk.READ,
    source: ToolSource = ToolSource.LOCAL,
    requires_approval: bool = False,
) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=f"Test tool {name}.",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
        risk_level=risk,
        source=source,
        requires_approval=requires_approval,
    )


def function_call(name: str, call_id: str, arguments: str) -> object:
    return SimpleNamespace(
        type="function_call",
        call_id=call_id,
        name=name,
        arguments=arguments,
    )


def usage() -> object:
    return SimpleNamespace(
        input_tokens=12,
        output_tokens=5,
        total_tokens=17,
        input_tokens_details=SimpleNamespace(cached_tokens=3),
        output_tokens_details=SimpleNamespace(reasoning_tokens=2),
    )


@pytest.mark.asyncio
async def test_provider_maps_single_function_call_and_request_options() -> None:
    response = SimpleNamespace(
        id="resp_1",
        model="test-model",
        output=[
            SimpleNamespace(
                type="function_call",
                call_id="call_1",
                name="read_file",
                arguments='{"path":"README.md"}',
            )
        ],
        output_text="",
        usage=usage(),
        service_tier="default",
    )
    client = FakeClient(response)
    provider = OpenAIModelProvider(
        ModelProviderConfig(api_key="secret", model="test-model"),
        client=client,
    )

    result = await provider.generate(request())

    assert isinstance(result.action, ToolCall)
    assert result.action.call_id == "call_1"
    assert result.action.arguments == {"path": "README.md"}
    assert result.usage is not None and result.usage.cached_input_tokens == 3
    sent = client.responses.requests[0]
    assert sent["store"] is False
    assert sent["parallel_tool_calls"] is False
    assert sent["tools"][0]["strict"] is True


@pytest.mark.asyncio
async def test_provider_maps_final_text() -> None:
    response = SimpleNamespace(
        id="resp_2",
        model="test-model",
        output=[],
        output_text="final answer",
        usage=None,
        service_tier=None,
    )
    provider = OpenAIModelProvider(
        ModelProviderConfig(api_key="secret", model="test-model"),
        client=FakeClient(response),
    )

    result = await provider.generate(request())

    assert isinstance(result.action, FinalAnswer)
    assert result.action.answer == "final answer"


@pytest.mark.asyncio
async def test_provider_maps_approval_rejection_as_function_output() -> None:
    response = SimpleNamespace(
        id="resp_rejected",
        model="test-model",
        output=[],
        output_text="alternate answer",
        usage=None,
        service_tier=None,
    )
    client = FakeClient(response)
    provider = OpenAIModelProvider(
        ModelProviderConfig(api_key="secret", model="test-model"),
        client=client,
    )
    model_request = request().model_copy(
        update={
            "history": [
                ContextItem(
                    kind=ContextItemKind.APPROVAL_RESULT,
                    call_id="call_rejected",
                    payload={"decision": "REJECTED"},
                ).model_dump(mode="json")
            ]
        }
    )

    await provider.generate(model_request)

    sent_input = client.responses.requests[0]["input"]
    assert isinstance(sent_input, list)
    assert sent_input[1]["type"] == "function_call_output"
    assert sent_input[1]["call_id"] == "call_rejected"


@pytest.mark.asyncio
async def test_provider_serializes_multiple_calls_to_the_first_action() -> None:
    first = function_call("read_file", "call_first", '{"path":"README.md"}')
    second = function_call(
        "read_file",
        "call_second",
        '{"path":"do-not-persist-sensitive-value"}',
    )
    config = ModelProviderConfig(api_key="secret", model="test-model")
    provider = OpenAIModelProvider(
        config,
        client=FakeClient(
            SimpleNamespace(
                id="multiple",
                model="test-model",
                output=[first, second],
                output_text="",
                usage=None,
                service_tier=None,
            )
        ),
    )

    result = await provider.generate(request())

    assert isinstance(result.action, ToolCall)
    assert result.action.call_id == "call_first"
    assert result.action.arguments == {"path": "README.md"}
    assert result.multi_tool_response is not None
    assert result.multi_tool_response.policy is MultiToolResponsePolicy.SEQUENTIAL_READ_ONLY
    assert result.multi_tool_response.discarded_tool_names == ["read_file"]
    assert result.sanitized_metadata["returned_function_call_count"] == 2
    assert result.sanitized_metadata["discarded_function_call_count"] == 1
    assert (
        result.sanitized_metadata["multi_tool_policy"] == "SEQUENTIAL_READ_ONLY"
    )
    assert result.sanitized_metadata["provider_contract_deviation"] is True
    serialized = json.dumps(result.model_dump(mode="json"))
    assert "call_second" not in serialized
    assert "do-not-persist-sensitive-value" not in serialized


@pytest.mark.asyncio
async def test_provider_strict_mode_rejects_two_read_calls_without_selection() -> None:
    provider = OpenAIModelProvider(
        ModelProviderConfig(
            api_key="secret",
            model="test-model",
            multi_tool_response_policy=MultiToolResponsePolicy.STRICT,
        ),
        client=FakeClient(
            SimpleNamespace(
                id="strict",
                model="test-model",
                output=[
                    function_call("read_file", "call_1", '{"path":"README.md"}'),
                    function_call("read_file", "call_2", '{"path":"secret"}'),
                ],
                output_text="",
                usage=None,
                service_tier=None,
            )
        ),
    )

    with pytest.raises(ProviderContractDeviationError) as error:
        await provider.generate(request())

    assert error.value.info.selected_call_count == 0
    assert error.value.info.discarded_call_count == 2
    assert error.value.info.policy is MultiToolResponsePolicy.STRICT
    assert "secret" not in repr(error.value.info)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("spec", "call_name", "reason"),
    [
        (tool_spec("write_file", risk=ToolRisk.WRITE), "write_file", "non_read_tool"),
        (
            tool_spec("dangerous_tool", risk=ToolRisk.DANGEROUS),
            "dangerous_tool",
            "non_read_tool",
        ),
        (
            tool_spec("approved_read", requires_approval=True),
            "approved_read",
            "approval_required_tool",
        ),
        (
            tool_spec("remote_read", source=ToolSource.MCP),
            "remote_read",
            "non_local_tool",
        ),
        (None, "unknown_tool", "unknown_tool"),
    ],
)
async def test_provider_rejects_mixed_or_unknown_multi_tool_responses(
    spec: ToolSpec | None,
    call_name: str,
    reason: str,
) -> None:
    model_request = request()
    if spec is not None:
        model_request = model_request.model_copy(
            update={"tools": [*model_request.tools, spec]}
        )
    provider = OpenAIModelProvider(
        ModelProviderConfig(api_key="secret", model="test-model"),
        client=FakeClient(
            SimpleNamespace(
                id=f"mixed-{call_name}",
                model="test-model",
                output=[
                    function_call("read_file", "call_read", '{"path":"README.md"}'),
                    function_call(call_name, "call_blocked", '{"path":"private"}'),
                ],
                output_text="",
                usage=None,
                service_tier=None,
            )
        ),
    )

    with pytest.raises(ProviderContractDeviationError) as error:
        await provider.generate(model_request)

    assert error.value.info.reason == reason
    assert error.value.info.selected_call_count == 0
    assert error.value.info.discarded_call_count == 2


@pytest.mark.asyncio
async def test_provider_rejects_multi_tool_response_above_configured_limit() -> None:
    provider = OpenAIModelProvider(
        ModelProviderConfig(
            api_key="secret",
            model="test-model",
            max_function_calls_per_response=2,
        ),
        client=FakeClient(
            SimpleNamespace(
                id="over-limit",
                model="test-model",
                output=[
                    function_call("read_file", "call_1", '{"path":"one"}'),
                    function_call("read_file", "call_2", '{"path":"two"}'),
                    function_call("read_file", "call_3", '{"path":"three"}'),
                ],
                output_text="",
                usage=None,
                service_tier=None,
            )
        ),
    )

    with pytest.raises(ProviderContractDeviationError) as error:
        await provider.generate(request())

    assert error.value.info.reason == "function_call_limit_exceeded"
    assert error.value.info.returned_call_count == 3


@pytest.mark.asyncio
async def test_provider_rejects_ambiguous_and_empty_responses() -> None:
    call = SimpleNamespace(
        type="function_call",
        call_id="call",
        name="read_file",
        arguments="{}",
    )
    config = ModelProviderConfig(api_key="secret", model="test-model")

    with pytest.raises(ModelProtocolError):
        await OpenAIModelProvider(
            config,
            client=FakeClient(
                SimpleNamespace(
                    id="ambiguous",
                    model="test-model",
                    output=[call],
                    output_text="also final",
                    usage=None,
                    service_tier=None,
                )
            ),
        ).generate(request())
    with pytest.raises(ModelProtocolError):
        await OpenAIModelProvider(
            config,
            client=FakeClient(
                SimpleNamespace(
                    id="empty",
                    model="test-model",
                    output=[],
                    output_text="   ",
                    usage=None,
                    service_tier=None,
                )
            ),
        ).generate(request())


@pytest.mark.asyncio
async def test_provider_rejects_invalid_function_arguments() -> None:
    response = SimpleNamespace(
        id="invalid",
        model="test-model",
        output=[
            SimpleNamespace(
                type="function_call",
                call_id="call",
                name="read_file",
                arguments="{invalid",
            )
        ],
        output_text="",
        usage=None,
        service_tier=None,
    )
    provider = OpenAIModelProvider(
        ModelProviderConfig(api_key="secret", model="test-model"),
        client=FakeClient(response),
    )

    with pytest.raises(ModelOutputInvalidError):
        await provider.generate(request())
