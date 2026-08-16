import json
from time import perf_counter
from typing import Any, NoReturn, Protocol, cast

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    AuthenticationError,
    BadRequestError,
    PermissionDeniedError,
    RateLimitError,
)

from agentforge.domain.enums import MultiToolResponsePolicy, ToolRisk, ToolSource
from agentforge.models.base import FinalAnswer, ModelRequest, ToolCall
from agentforge.models.deepseek_schema import convert_tool_spec
from agentforge.models.domain import (
    ModelErrorCode,
    ModelProviderConfig,
    ModelResponse,
    ModelUsage,
    MultiToolResponseInfo,
)
from agentforge.models.errors import (
    ModelOutputInvalidError,
    ModelProtocolError,
    ModelRequestError,
    ProviderContractDeviationError,
)

DEEPSEEK_BASE_URL = "https://api.deepseek.com"


class ChatCompletionsResource(Protocol):
    async def create(self, **kwargs: Any) -> object: ...


class ChatResource(Protocol):
    completions: ChatCompletionsResource


class DeepSeekClient(Protocol):
    chat: ChatResource


class DeepSeekModelProvider:
    def __init__(
        self,
        config: ModelProviderConfig,
        *,
        client: DeepSeekClient | None = None,
    ) -> None:
        self._config = config
        self._client = client or cast(
            DeepSeekClient,
            AsyncOpenAI(
                api_key=config.api_key.get_secret_value(),
                base_url=DEEPSEEK_BASE_URL,
                timeout=config.timeout_seconds,
                max_retries=0,
            ),
        )

    @property
    def name(self) -> str:
        return "deepseek"

    @property
    def journal_identity(self) -> str:
        return f"{self.name}/{self._config.model}"

    async def generate(self, request: ModelRequest) -> ModelResponse:
        messages: list[object] = []
        if request.instructions:
            messages.append({"role": "system", "content": request.instructions})
        messages.append({"role": "user", "content": request.task})
        messages.extend(self._map_history_item(item) for item in request.history)
        kwargs: dict[str, object] = {
            "model": self._config.model,
            "messages": messages,
            "stream": False,
            "n": 1,
            "extra_body": {"thinking": {"type": "disabled"}},
        }
        if request.tools:
            kwargs["tools"] = [convert_tool_spec(spec) for spec in request.tools]
        if self._config.max_output_tokens is not None:
            kwargs["max_tokens"] = self._config.max_output_tokens

        started = perf_counter()
        try:
            response = await self._client.chat.completions.create(**kwargs)
        except AuthenticationError as exc:
            raise ModelRequestError(
                ModelErrorCode.MODEL_AUTH_ERROR,
                "DeepSeek authentication failed",
                retryable=False,
            ) from exc
        except PermissionDeniedError as exc:
            raise ModelRequestError(
                ModelErrorCode.MODEL_AUTH_ERROR,
                "DeepSeek permission was denied",
                retryable=False,
            ) from exc
        except RateLimitError as exc:
            raise ModelRequestError(
                ModelErrorCode.MODEL_RATE_LIMITED,
                "DeepSeek rate limit was reached",
                retryable=True,
            ) from exc
        except APITimeoutError as exc:
            raise ModelRequestError(
                ModelErrorCode.MODEL_TIMEOUT,
                "DeepSeek request timed out",
                retryable=True,
            ) from exc
        except APIConnectionError as exc:
            raise ModelRequestError(
                ModelErrorCode.MODEL_TRANSPORT_ERROR,
                "DeepSeek transport failed",
                retryable=True,
            ) from exc
        except BadRequestError as exc:
            raise ModelRequestError(
                ModelErrorCode.MODEL_BAD_REQUEST,
                "DeepSeek rejected the request",
                retryable=False,
            ) from exc
        except APIStatusError as exc:
            raise ModelRequestError(
                ModelErrorCode.MODEL_PROVIDER_ERROR,
                "DeepSeek service returned an error",
                retryable=exc.status_code >= 500,
            ) from exc

        duration_ms = max(0, int((perf_counter() - started) * 1000))
        action, function_call_count, multi_tool_response, finish_reason = (
            self._parse_action(response, request)
        )
        return ModelResponse(
            action=action,
            usage=self._parse_usage(getattr(response, "usage", None)),
            provider=self.name,
            model=self._response_model(response),
            provider_request_id=self._optional_string(getattr(response, "id", None)),
            duration_ms=duration_ms,
            attempt_count=1,
            sanitized_metadata={
                "finish_reason": finish_reason,
                "returned_function_call_count": function_call_count,
                "discarded_function_call_count": (
                    multi_tool_response.discarded_call_count
                    if multi_tool_response is not None
                    else 0
                ),
                "multi_tool_policy": (
                    multi_tool_response.policy.value
                    if multi_tool_response is not None
                    else None
                ),
                "provider_contract_deviation": multi_tool_response is not None,
            },
            multi_tool_response=multi_tool_response,
        )

    def _parse_action(
        self,
        response: object,
        request: ModelRequest,
    ) -> tuple[ToolCall | FinalAnswer, int, MultiToolResponseInfo | None, str | None]:
        choices = getattr(response, "choices", None)
        if not isinstance(choices, list) or not choices:
            raise ModelProtocolError("Provider returned no choices")
        choice = choices[0]
        message = getattr(choice, "message", None)
        if message is None:
            raise ModelProtocolError("Provider choice has no message")
        raw_calls = getattr(message, "tool_calls", None)
        if raw_calls is None:
            calls: list[object] = []
        elif isinstance(raw_calls, list):
            calls = raw_calls
        else:
            raise ModelProtocolError("Provider tool calls must be a list")
        content = getattr(message, "content", None)
        final_text = content.strip() if isinstance(content, str) else ""
        if calls and final_text:
            raise ModelProtocolError("Provider returned both a function call and final text")
        multi_tool_response: MultiToolResponseInfo | None = None
        if len(calls) > 1:
            multi_tool_response = self._classify_multi_tool_response(
                response,
                request,
                calls,
            )
        finish_reason = self._optional_string(getattr(choice, "finish_reason", None))
        if calls:
            call = calls[0]
            function = getattr(call, "function", None)
            if function is None:
                raise ModelProtocolError("Provider function call has no function")
            raw_arguments = getattr(function, "arguments", "")
            try:
                arguments = json.loads(raw_arguments)
            except (TypeError, json.JSONDecodeError) as exc:
                raise ModelOutputInvalidError(
                    "Provider function arguments are not valid JSON"
                ) from exc
            if not isinstance(arguments, dict):
                raise ModelOutputInvalidError(
                    "Provider function arguments must decode to an object"
                )
            name = getattr(function, "name", None)
            call_id = getattr(call, "id", None)
            if not isinstance(name, str) or not name:
                raise ModelProtocolError("Provider function call has no valid name")
            if not isinstance(call_id, str) or not call_id:
                raise ModelProtocolError("Provider function call has no valid call_id")
            return (
                ToolCall(
                    type="tool_call",
                    call_id=call_id,
                    tool=name,
                    arguments=arguments,
                ),
                len(calls),
                multi_tool_response,
                finish_reason,
            )
        if final_text:
            return FinalAnswer(type="final", answer=final_text), 0, None, finish_reason
        raise ModelProtocolError("Provider returned neither a function call nor final text")

    def _classify_multi_tool_response(
        self,
        response: object,
        request: ModelRequest,
        calls: list[object],
    ) -> MultiToolResponseInfo:
        names = [self._bounded_tool_name(call) for call in calls]
        if len(calls) > self._config.max_function_calls_per_response:
            self._raise_contract_deviation(
                response,
                names,
                reason="function_call_limit_exceeded",
            )
        if self._config.multi_tool_response_policy is MultiToolResponsePolicy.STRICT:
            self._raise_contract_deviation(response, names, reason="strict_policy")

        specs = {spec.name: spec for spec in request.tools}
        for name in names:
            spec = specs.get(name)
            if spec is None:
                self._raise_contract_deviation(response, names, reason="unknown_tool")
            if spec.source is not ToolSource.LOCAL:
                self._raise_contract_deviation(response, names, reason="non_local_tool")
            if spec.risk_level is not ToolRisk.READ:
                self._raise_contract_deviation(response, names, reason="non_read_tool")
            if spec.requires_approval:
                self._raise_contract_deviation(
                    response,
                    names,
                    reason="approval_required_tool",
                )

        return MultiToolResponseInfo(
            provider=self.name,
            model=self._response_model(response),
            provider_request_id=self._optional_string(getattr(response, "id", None)),
            returned_call_count=len(calls),
            selected_call_count=1,
            discarded_call_count=len(calls) - 1,
            selected_tool_name=names[0],
            discarded_tool_names=names[1:11],
            policy=MultiToolResponsePolicy.SEQUENTIAL_READ_ONLY,
            reason="multiple_local_read_calls",
        )

    def _raise_contract_deviation(
        self,
        response: object,
        names: list[str],
        *,
        reason: str,
    ) -> NoReturn:
        info = MultiToolResponseInfo(
            provider=self.name,
            model=self._response_model(response),
            provider_request_id=self._optional_string(getattr(response, "id", None)),
            returned_call_count=len(names),
            selected_call_count=0,
            discarded_call_count=len(names),
            selected_tool_name=None,
            discarded_tool_names=names[:10],
            policy=MultiToolResponsePolicy.STRICT,
            reason=reason,
        )
        raise ProviderContractDeviationError(
            "Provider returned a multi-tool response that requires STRICT handling",
            info,
        )

    def _response_model(self, response: object) -> str:
        value = getattr(response, "model", self._config.model)
        return value if isinstance(value, str) and value else self._config.model

    @staticmethod
    def _bounded_tool_name(call: object) -> str:
        function = getattr(call, "function", None)
        value = getattr(function, "name", None)
        if not isinstance(value, str) or not value:
            return "<invalid>"
        return value[:100]

    @classmethod
    def _parse_usage(cls, usage: object | None) -> ModelUsage | None:
        if usage is None:
            return None
        prompt_details = getattr(usage, "prompt_tokens_details", None)
        completion_details = getattr(usage, "completion_tokens_details", None)
        return ModelUsage(
            input_tokens=cls._optional_int(getattr(usage, "prompt_tokens", None)),
            output_tokens=cls._optional_int(getattr(usage, "completion_tokens", None)),
            total_tokens=cls._optional_int(getattr(usage, "total_tokens", None)),
            cached_input_tokens=cls._first_optional_int(
                getattr(usage, "prompt_cache_hit_tokens", None),
                getattr(prompt_details, "cached_tokens", None),
            ),
            reasoning_tokens=cls._first_optional_int(
                getattr(usage, "reasoning_tokens", None),
                getattr(completion_details, "reasoning_tokens", None),
            ),
        )

    @staticmethod
    def _first_optional_int(*values: object) -> int | None:
        return next((value for value in values if isinstance(value, int)), None)

    @staticmethod
    def _optional_int(value: object) -> int | None:
        return value if isinstance(value, int) else None

    @staticmethod
    def _optional_string(value: object) -> str | None:
        return value if isinstance(value, str) else None

    @classmethod
    def _map_history_item(cls, item: object) -> object:
        if not isinstance(item, dict):
            return {"role": "user", "content": cls._canonical_json(item)}
        kind = item.get("kind")
        payload = item.get("payload")
        call_id = item.get("call_id")
        if (
            kind == "TOOL_CALL"
            and isinstance(payload, dict)
            and isinstance(call_id, str)
        ):
            return {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": payload.get("tool"),
                            "arguments": cls._canonical_json(
                                payload.get("arguments", {})
                            ),
                        },
                    }
                ],
            }
        if kind in {"TOOL_RESULT", "APPROVAL_RESULT"} and isinstance(call_id, str):
            return {
                "role": "tool",
                "tool_call_id": call_id,
                "content": cls._canonical_json(payload),
            }
        return {"role": "user", "content": cls._canonical_json(payload)}

    @staticmethod
    def _canonical_json(value: object) -> str:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
