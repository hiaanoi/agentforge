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


def test_provider_binds_configured_client_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def build_client(**kwargs: object) -> FakeClient:
        captured.update(kwargs)
        return FakeClient()

    monkeypatch.setattr("agentforge.models.deepseek_provider.AsyncOpenAI", build_client)
    DeepSeekModelProvider(
        ModelProviderConfig(
            api_key="secret",
            model="deepseek-account-model",
            timeout_seconds=600.0,
            temperature=0.0,
        )
    )

    assert captured["timeout"] == 600.0
    assert captured["max_retries"] == 0


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


def model_request(
    *, tools: list[ToolSpec] | None = None, preserve_tool_call_text: bool = False
) -> ModelRequest:
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
        preserve_tool_call_text=preserve_tool_call_text,
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


def test_history_mapper_accepts_generic_runtime_tool_items() -> None:
    call = DeepSeekModelProvider._map_history_item(
        {
            "type": "tool_call",
            "call_id": "call_generic",
            "payload": {"tool_name": "read_file", "arguments": {"path": "README.md"}},
        }
    )
    result = DeepSeekModelProvider._map_history_item(
        {"tool_result": {"output": "hello"}, "call_id": "call_generic"}
    )
    assert call["tool_calls"][0]["function"]["name"] == "read_file"
    assert call["tool_calls"][0]["function"]["arguments"] == '{"path":"README.md"}'
    assert result == {
        "role": "tool",
        "tool_call_id": "call_generic",
        "content": '{"output":"hello"}',
    }
    top_level_call = DeepSeekModelProvider._map_history_item(
        {
            "type": "tool_call",
            "call_id": "call_top_level",
            "tool": "read_file",
            "arguments": {"path": "README.md"},
        }
    )
    assert top_level_call["tool_calls"][0]["function"]["name"] == "read_file"
    assert top_level_call["tool_calls"][0]["id"] == "call_top_level"


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
            temperature=0.0,
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
    assert sent["temperature"] == 0.0


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
    ],
)
async def test_provider_rejects_missing_or_empty_choices(
    provider_response: object,
) -> None:
    provider = DeepSeekModelProvider(
        ModelProviderConfig(api_key="secret", model="deepseek-account-model"),
        client=FakeClient(provider_response),
    )

    with pytest.raises(ModelProtocolError):
        await provider.generate(model_request())


@pytest.mark.asyncio
async def test_provider_prefers_tool_call_and_audits_discarded_text() -> None:
    provider = DeepSeekModelProvider(
        ModelProviderConfig(api_key="secret", model="deepseek-account-model"),
        client=FakeClient(
            response(
                content="Sensitive explanatory text must not be persisted.",
                calls=[function_call()],
            )
        ),
    )

    result = await provider.generate(model_request())

    assert isinstance(result.action, ToolCall)
    assert result.action.call_id == "call_2"
    assert result.sanitized_metadata["discarded_text_with_tool_calls"] is True
    assert result.sanitized_metadata["provider_contract_deviation"] is True
    serialized = json.dumps(result.model_dump(mode="json"))
    assert "Sensitive explanatory text" not in serialized


@pytest.mark.asyncio
async def test_openai_chat_relay_provider_uses_openai_identity() -> None:
    from agentforge.models.openai_chat_provider import OpenAIChatCompletionsProvider

    provider = OpenAIChatCompletionsProvider(
        ModelProviderConfig(
            api_key="secret",
            model="gpt-5.4-mini",
            base_url="https://relay.example/v1",
        ),
        client=FakeClient(response(calls=[function_call()])),
    )

    result = await provider.generate(model_request())

    assert provider.name == "openai"
    assert provider.journal_identity == "openai/gpt-5.4-mini"
    assert isinstance(result.action, ToolCall)


@pytest.mark.asyncio
async def test_openai_chat_relay_omits_unsupported_token_limit_parameter() -> None:
    from agentforge.models.openai_chat_provider import OpenAIChatCompletionsProvider

    client = FakeClient(response(calls=[function_call()]))
    provider = OpenAIChatCompletionsProvider(
        ModelProviderConfig(
            api_key="secret",
            model="gpt-5.4-mini",
            base_url="https://relay.example/v1",
            max_output_tokens=800,
        ),
        client=client,
    )

    await provider.generate(model_request())

    sent = client.chat.completions.requests[0]
    assert "max_completion_tokens" not in sent
    assert "max_tokens" not in sent


@pytest.mark.asyncio
async def test_provider_can_preserve_tool_call_text_for_linear_agent_history() -> None:
    provider = DeepSeekModelProvider(
        ModelProviderConfig(api_key="secret", model="deepseek-account-model"),
        client=FakeClient(
            response(content="THOUGHT inspect source", calls=[function_call()])
        ),
    )

    result = await provider.generate(model_request(preserve_tool_call_text=True))

    assert isinstance(result.action, ToolCall)
    assert result.action.reason == "THOUGHT inspect source"


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
