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

from agentforge.domain.enums import (
    MultiToolResponsePolicy,
    ToolRisk,
    ToolSource,
)
from agentforge.models.base import FinalAnswer, ModelRequest, ToolCall
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
from agentforge.models.openai_schema import convert_tool_spec


class ResponsesResource(Protocol):
    async def create(self, **kwargs: Any) -> object: ...


class OpenAIClient(Protocol):
    responses: ResponsesResource


class OpenAIModelProvider:
    def __init__(
        self,
        config: ModelProviderConfig,
        *,
        client: OpenAIClient | None = None,
    ) -> None:
        self._config = config
        if client is not None:
            self._client = client
        elif config.base_url is not None:
            self._client = cast(
                OpenAIClient,
                AsyncOpenAI(
                    api_key=config.api_key.get_secret_value(),
                    timeout=config.timeout_seconds,
                    max_retries=0,
                    base_url=config.base_url,
                ),
            )
        else:
            self._client = cast(
                OpenAIClient,
                AsyncOpenAI(
                    api_key=config.api_key.get_secret_value(),
                    timeout=config.timeout_seconds,
                    max_retries=0,
                ),
            )

    @property
    def name(self) -> str:
        return "openai"

    @property
    def journal_identity(self) -> str:
        """Stable non-secret identity persisted before a network dispatch."""
        return f"{self.name}/{self._config.model}"

    async def generate(self, request: ModelRequest) -> ModelResponse:
        started = perf_counter()
        input_items: list[object] = [{"role": "user", "content": request.task}]
        input_items.extend(self._map_history_item(item) for item in request.history)
        kwargs: dict[str, object] = {
            "model": self._config.model,
            "input": input_items,
            "tools": [convert_tool_spec(spec) for spec in request.tools],
            "store": self._config.store,
            "parallel_tool_calls": False,
        }
        if self._config.max_output_tokens is not None:
            kwargs["max_output_tokens"] = self._config.max_output_tokens
        if request.instructions:
            kwargs["instructions"] = request.instructions
        try:
            response = await self._client.responses.create(**kwargs)
        except AuthenticationError as exc:
            raise ModelRequestError(
                ModelErrorCode.MODEL_AUTH_ERROR,
                "OpenAI authentication failed",
                retryable=False,
            ) from exc
        except PermissionDeniedError as exc:
            raise ModelRequestError(
                ModelErrorCode.MODEL_AUTH_ERROR,
                "OpenAI permission was denied",
                retryable=False,
            ) from exc
        except RateLimitError as exc:
            raise ModelRequestError(
                ModelErrorCode.MODEL_RATE_LIMITED,
                "OpenAI rate limit was reached",
                retryable=True,
            ) from exc
        except APITimeoutError as exc:
            raise ModelRequestError(
                ModelErrorCode.MODEL_TIMEOUT,
                "OpenAI request timed out",
                retryable=True,
            ) from exc
        except APIConnectionError as exc:
            raise ModelRequestError(
                ModelErrorCode.MODEL_TRANSPORT_ERROR,
                "OpenAI transport failed",
                retryable=True,
            ) from exc
        except BadRequestError as exc:
            raise ModelRequestError(
                ModelErrorCode.MODEL_BAD_REQUEST,
                "OpenAI rejected the request",
                retryable=False,
            ) from exc
        except APIStatusError as exc:
            raise ModelRequestError(
                ModelErrorCode.MODEL_PROVIDER_ERROR,
                "OpenAI service returned an error",
                retryable=exc.status_code >= 500,
            ) from exc
        duration_ms = max(0, int((perf_counter() - started) * 1000))
        action, function_call_count, multi_tool_response = self._parse_action(
            response,
            request,
        )
        return ModelResponse(
            action=action,
            usage=self._parse_usage(getattr(response, "usage", None)),
            provider=self.name,
            model=str(getattr(response, "model", self._config.model)),
            provider_request_id=self._optional_string(getattr(response, "id", None)),
            duration_ms=duration_ms,
            attempt_count=1,
            sanitized_metadata={
                "service_tier": self._optional_string(
                    getattr(response, "service_tier", None)
                ),
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
    ) -> tuple[ToolCall | FinalAnswer, int, MultiToolResponseInfo | None]:
        output = getattr(response, "output", [])
        if not isinstance(output, list):
            raise ModelProtocolError("Provider output must be a list")
        calls = [item for item in output if getattr(item, "type", None) == "function_call"]
        text = getattr(response, "output_text", "")
        final_text = text.strip() if isinstance(text, str) else ""
        if calls and final_text:
            raise ModelProtocolError("Provider returned both a function call and final text")
        multi_tool_response: MultiToolResponseInfo | None = None
        if len(calls) > 1:
            multi_tool_response = self._classify_multi_tool_response(
                response,
                request,
                calls,
            )
        if calls:
            call = calls[0]
            raw_arguments = getattr(call, "arguments", "")
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
            name = getattr(call, "name", None)
            call_id = getattr(call, "call_id", None)
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
            )
        if final_text:
            return FinalAnswer(type="final", answer=final_text), 0, None
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
            self._raise_contract_deviation(
                response,
                names,
                reason="strict_policy",
            )

        specs = {spec.name: spec for spec in request.tools}
        for name in names:
            spec = specs.get(name)
            if spec is None:
                self._raise_contract_deviation(
                    response,
                    names,
                    reason="unknown_tool",
                )
            if spec.source is not ToolSource.LOCAL:
                self._raise_contract_deviation(
                    response,
                    names,
                    reason="non_local_tool",
                )
            if spec.risk_level is not ToolRisk.READ:
                self._raise_contract_deviation(
                    response,
                    names,
                    reason="non_read_tool",
                )
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
        value = getattr(call, "name", None)
        if not isinstance(value, str) or not value:
            return "<invalid>"
        return value[:100]

    @classmethod
    def _parse_usage(cls, usage: object | None) -> ModelUsage | None:
        if usage is None:
            return None
        input_details = getattr(usage, "input_tokens_details", None)
        output_details = getattr(usage, "output_tokens_details", None)
        return ModelUsage(
            input_tokens=cls._optional_int(getattr(usage, "input_tokens", None)),
            output_tokens=cls._optional_int(getattr(usage, "output_tokens", None)),
            total_tokens=cls._optional_int(getattr(usage, "total_tokens", None)),
            cached_input_tokens=cls._optional_int(
                getattr(input_details, "cached_tokens", None)
            ),
            reasoning_tokens=cls._optional_int(
                getattr(output_details, "reasoning_tokens", None)
            ),
        )

    @staticmethod
    def _optional_int(value: object) -> int | None:
        return value if isinstance(value, int) else None

    @staticmethod
    def _optional_string(value: object) -> str | None:
        return value if isinstance(value, str) else None

    @staticmethod
    def _map_history_item(item: object) -> object:
        if not isinstance(item, dict):
            return {"role": "user", "content": json.dumps(item, ensure_ascii=False)}
        kind = item.get("kind")
        payload = item.get("payload")
        call_id = item.get("call_id")
        if item.get("type") == "tool_call":
            kind = "TOOL_CALL"
            payload = item.get("payload")
        elif "tool_result" in item:
            kind = "TOOL_RESULT"
            payload = item["tool_result"]
        if kind == "TOOL_CALL" and isinstance(payload, dict):
            payload = {**payload, "tool": payload.get("tool", payload.get("tool_name"))}
        if kind == "TOOL_CALL" and isinstance(payload, dict):
            return {
                "type": "function_call",
                "call_id": call_id,
                "name": payload.get("tool"),
                "arguments": json.dumps(
                    payload.get("arguments", {}),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            }
        if kind in {"TOOL_RESULT", "APPROVAL_RESULT"} and isinstance(call_id, str):
            return {
                "type": "function_call_output",
                "call_id": call_id,
                "output": json.dumps(
                    payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            }
        return {
            "role": "user",
            "content": json.dumps(payload, ensure_ascii=False, sort_keys=True),
        }
