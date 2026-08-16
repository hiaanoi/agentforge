import json
from types import SimpleNamespace

import httpx
import pytest
from openai import APIConnectionError, APITimeoutError

from agentforge.context.models import ContextItem, ContextItemKind
from agentforge.domain.enums import MultiToolResponsePolicy, ToolRisk, ToolSource
from agentforge.domain.models import ToolSpec
from agentforge.models.base import FinalAnswer, ModelRequest, ToolCall
from agentforge.models.deepseek_provider import DeepSeekModelProvider
from agentforge.models.domain import ModelErrorCode, ModelProviderConfig
from agentforge.models.errors import (
    ModelOutputInvalidError,
    ModelProtocolError,
    ModelRequestError,
    ProviderContractDeviationError,
)


class FakeCompletions:
    def __init__(self, response: object = None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.requests: list[dict[str, object]] = []

    async def create(self, **kwargs: object) -> object:
        self.requests.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.response


class FakeClient:
    def __init__(self, response: object = None, error: Exception | None = None) -> None:
        self.chat = SimpleNamespace(completions=FakeCompletions(response, error))


def tool_spec(
    name: str = "read_file",
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


def model_request(*, tools: list[ToolSpec] | None = None) -> ModelRequest:
    return ModelRequest(
        task="repair the repository",
        instructions="Use only registered tools.",
        step_number=2,
        history=[
            ContextItem(
                kind=ContextItemKind.TOOL_CALL,
                call_id="call_1",
                payload={"tool": "read_file", "arguments": {"path": "README.md"}},
            ).model_dump(mode="json"),
            ContextItem(
                kind=ContextItemKind.TOOL_RESULT,
                call_id="call_1",
                payload={"content": "hello"},
            ).model_dump(mode="json"),
        ],
        tools=tools if tools is not None else [tool_spec()],
    )


def function_call(
    name: str = "read_file",
    call_id: str = "call_2",
    arguments: str = '{"path":"pyproject.toml"}',
) -> object:
    return SimpleNamespace(
        id=call_id,
        type="function",
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def response(
    *,
    content: str | None = None,
    calls: list[object] | None = None,
    usage: object | None = None,
) -> object:
    return SimpleNamespace(
        id="chat_1",
        model="deepseek-account-model",
        choices=[
            SimpleNamespace(
                finish_reason="tool_calls" if calls else "stop",
                message=SimpleNamespace(content=content, tool_calls=calls or []),
            )
        ],
        usage=usage,
    )


@pytest.mark.asyncio
async def test_provider_maps_messages_tools_usage_and_fixed_options() -> None:
    usage = SimpleNamespace(
        prompt_tokens=20,
        completion_tokens=7,
        total_tokens=27,
        prompt_cache_hit_tokens=4,
        completion_tokens_details=SimpleNamespace(reasoning_tokens=0),
    )
    client = FakeClient(response(calls=[function_call()], usage=usage))
    provider = DeepSeekModelProvider(
        ModelProviderConfig(
            api_key="secret",
            model="deepseek-account-model",
            max_output_tokens=800,
        ),
        client=client,
    )

    result = await provider.generate(model_request())

    assert provider.name == "deepseek"
    assert provider.journal_identity == "deepseek/deepseek-account-model"
    assert isinstance(result.action, ToolCall)
    assert result.action.call_id == "call_2"
    assert result.action.arguments == {"path": "pyproject.toml"}
    assert result.usage is not None
    assert result.usage.model_dump() == {
        "input_tokens": 20,
        "output_tokens": 7,
        "total_tokens": 27,
        "cached_input_tokens": 4,
        "reasoning_tokens": 0,
    }
    sent = client.chat.completions.requests[0]
    assert sent["model"] == "deepseek-account-model"
    assert sent["extra_body"] == {"thinking": {"type": "disabled"}}
    assert sent["stream"] is False
    assert sent["n"] == 1
    assert sent["max_tokens"] == 800
    messages = sent["messages"]
    assert isinstance(messages, list)
    assert [message["role"] for message in messages] == [
        "system",
        "user",
        "assistant",
        "tool",
    ]
    assert messages[2]["tool_calls"][0]["id"] == "call_1"
    assert messages[3]["tool_call_id"] == "call_1"
    tools = sent["tools"]
    assert isinstance(tools, list)
    assert tools[0]["function"]["name"] == "read_file"


@pytest.mark.asyncio
async def test_provider_maps_final_text() -> None:
    provider = DeepSeekModelProvider(
        ModelProviderConfig(api_key="secret", model="deepseek-account-model"),
        client=FakeClient(response(content=" finished ")),
    )

    result = await provider.generate(model_request())

    assert isinstance(result.action, FinalAnswer)
    assert result.action.answer == "finished"


@pytest.mark.asyncio
async def test_provider_serializes_only_first_safe_read_call() -> None:
    provider = DeepSeekModelProvider(
        ModelProviderConfig(api_key="secret", model="deepseek-account-model"),
        client=FakeClient(
            response(
                calls=[
                    function_call(call_id="first", arguments='{"path":"README.md"}'),
                    function_call(
                        call_id="discarded",
                        arguments='{"path":"do-not-persist-sensitive-value"}',
                    ),
                ]
            )
        ),
    )

    result = await provider.generate(model_request())

    assert isinstance(result.action, ToolCall)
    assert result.action.call_id == "first"
    assert result.multi_tool_response is not None
    assert result.multi_tool_response.discarded_call_count == 1
    serialized = json.dumps(result.model_dump(mode="json"))
    assert '"discarded"' not in serialized
    assert "do-not-persist-sensitive-value" not in serialized


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("config", "extra_tool", "name", "reason"),
    [
        (
            ModelProviderConfig(
                api_key="secret",
                model="deepseek-account-model",
                multi_tool_response_policy=MultiToolResponsePolicy.STRICT,
            ),
            None,
            "read_file",
            "strict_policy",
        ),
        (
            ModelProviderConfig(api_key="secret", model="deepseek-account-model"),
            tool_spec("write_file", risk=ToolRisk.WRITE),
            "write_file",
            "non_read_tool",
        ),
        (
            ModelProviderConfig(api_key="secret", model="deepseek-account-model"),
            tool_spec("remote_read", source=ToolSource.MCP),
            "remote_read",
            "non_local_tool",
        ),
        (
            ModelProviderConfig(api_key="secret", model="deepseek-account-model"),
            tool_spec("approved_read", requires_approval=True),
            "approved_read",
            "approval_required_tool",
        ),
        (
            ModelProviderConfig(api_key="secret", model="deepseek-account-model"),
            None,
            "unknown_tool",
            "unknown_tool",
        ),
    ],
)
async def test_provider_rejects_unsafe_multi_tool_responses(
    config: ModelProviderConfig,
    extra_tool: ToolSpec | None,
    name: str,
    reason: str,
) -> None:
    tools = [tool_spec()]
    if extra_tool is not None:
        tools.append(extra_tool)
    provider = DeepSeekModelProvider(
        config,
        client=FakeClient(
            response(calls=[function_call(call_id="first"), function_call(name, "second")])
        ),
    )

    with pytest.raises(ProviderContractDeviationError) as raised:
        await provider.generate(model_request(tools=tools))

    assert raised.value.info.reason == reason
    assert raised.value.info.provider == "deepseek"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider_response",
    [
        SimpleNamespace(id="none", model="deepseek-account-model", choices=[], usage=None),
        response(content="  "),
        response(content="final", calls=[function_call()]),
    ],
)
async def test_provider_rejects_missing_empty_or_ambiguous_choices(
    provider_response: object,
) -> None:
    provider = DeepSeekModelProvider(
        ModelProviderConfig(api_key="secret", model="deepseek-account-model"),
        client=FakeClient(provider_response),
    )

    with pytest.raises(ModelProtocolError):
        await provider.generate(model_request())


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments", ["{invalid", "[]"])
async def test_provider_rejects_invalid_function_arguments(arguments: str) -> None:
    provider = DeepSeekModelProvider(
        ModelProviderConfig(api_key="secret", model="deepseek-account-model"),
        client=FakeClient(response(calls=[function_call(arguments=arguments)])),
    )

    with pytest.raises(ModelOutputInvalidError):
        await provider.generate(model_request())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "code"),
    [
        (
            APIConnectionError(request=httpx.Request("POST", "https://api.deepseek.com")),
            ModelErrorCode.MODEL_TRANSPORT_ERROR,
        ),
        (
            APITimeoutError(request=httpx.Request("POST", "https://api.deepseek.com")),
            ModelErrorCode.MODEL_TIMEOUT,
        ),
    ],
)
async def test_provider_maps_transport_errors(error: Exception, code: ModelErrorCode) -> None:
    provider = DeepSeekModelProvider(
        ModelProviderConfig(api_key="secret", model="deepseek-account-model"),
        client=FakeClient(error=error),
    )

    with pytest.raises(ModelRequestError) as raised:
        await provider.generate(model_request())

    assert raised.value.code is code
    assert raised.value.retryable is True
    assert "secret" not in str(raised.value)
