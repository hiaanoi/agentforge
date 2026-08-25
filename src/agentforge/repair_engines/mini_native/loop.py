from __future__ import annotations

from dataclasses import dataclass
from typing import cast
from uuid import UUID

from pydantic import JsonValue

from agentforge.domain.enums import ToolRisk
from agentforge.domain.models import ToolSpec
from agentforge.models.base import ModelRequest, ToolCall
from agentforge.models.domain import ModelResponse
from agentforge.repair_engines.mini_native.contracts import (
    CandidatePatchResult,
    MiniNativeState,
    RepairAction,
    RepairActionKind,
    RepairActionResult,
    action_history_item,
    to_repair_action,
)
from agentforge.repair_engines.mini_native.host import MiniNativeHost
from agentforge.repair_engines.mini_native.vendor.bash_protocol import (
    BASH_INSTANCE_TEMPLATE,
    BASH_SYSTEM_TEMPLATE,
    bash_tool_schema,
    format_observation,
    parse_submit_output,
)
from agentforge.repair_engines.mini_native.vendor.loop import VendorRepairLoop


def _bash_tool_spec() -> ToolSpec:
    schema = bash_tool_schema()
    return ToolSpec(
        name=cast(str, schema["name"]),
        description=cast(str, schema["description"]),
        input_schema=cast(dict[str, JsonValue], schema["parameters"]),
        risk_level=ToolRisk.DANGEROUS,
        requires_approval=False,
    )


@dataclass(frozen=True, slots=True)
class MiniNativeResult:
    submitted: bool
    candidate: CandidatePatchResult | None
    history: list[JsonValue]
    model_calls: int
    last_test_passed: bool = False


class MiniNativeRepairEngine:
    """Adapt mini-SWE-agent's one-command-at-a-time bash loop to the host protocol."""

    def __init__(self, host: MiniNativeHost) -> None:
        self._host = host

    async def run(
        self,
        *,
        run_id: UUID,
        task: str,
        max_steps: int,
        working_directory: str,
        history: list[JsonValue] | None = None,
        last_test_passed: bool = False,
    ) -> MiniNativeResult:
        history = list(history or [])
        candidate: CandidatePatchResult | None = None

        def build_request(step: int, compacted_history: list[JsonValue]) -> ModelRequest:
            return ModelRequest(
                run_id=run_id,
                task=BASH_INSTANCE_TEMPLATE.replace("{{task}}", task),
                step_number=step,
                instructions=BASH_SYSTEM_TEMPLATE,
                preserve_tool_call_text=True,
                history=compacted_history,
                tools=[_bash_tool_spec()],
            )

        async def execute(response: ModelResponse, step: int) -> bool:
            nonlocal candidate
            action = _to_bash_action(
                response,
                run_id=run_id,
                step=step,
                working_directory=working_directory,
            )
            history.append(action_history_item(action))
            result = await self._host.execute(action)
            history.append(_observation_history_item(action, result))
            if parse_submit_output(result.stdout) is None:
                return False
            candidate = await self._host.publish(run_id)
            return True

        async def checkpoint(step: int, compacted_history: list[JsonValue]) -> None:
            await self._host.checkpoint(
                MiniNativeState(
                    run_id=run_id,
                    step_number=step,
                    history=tuple(compacted_history),
                    last_test_passed=last_test_passed,
                )
            )

        submitted, model_calls, final_history = await VendorRepairLoop(
            generate=self._host.generate,
            build_request=build_request,
            execute=execute,
            checkpoint=checkpoint,
        ).run(max_steps=max_steps, history=history)
        return MiniNativeResult(
            submitted=submitted,
            candidate=candidate,
            history=final_history,
            model_calls=model_calls,
            last_test_passed=last_test_passed,
        )

    async def resume_pending(
        self,
        *,
        action: RepairAction,
        history: list[JsonValue],
        last_test_passed: bool,
        saved_result: RepairActionResult | None = None,
    ) -> MiniNativeResult:
        """Finish an already-dispatched bash command without another model request."""
        if action.kind is not RepairActionKind.BASH:
            raise ValueError("Only pending bash actions can resume the mini-native loop")
        result = saved_result or await self._host.execute(action)
        history.append(_observation_history_item(action, result))
        candidate = (
            await self._host.publish(action.run_id)
            if parse_submit_output(result.stdout) is not None
            else None
        )
        await self._host.checkpoint(
            MiniNativeState(
                run_id=action.run_id,
                step_number=1,
                history=tuple(history),
                last_test_passed=last_test_passed,
            )
        )
        return MiniNativeResult(
            submitted=candidate is not None,
            candidate=candidate,
            history=history,
            model_calls=0,
            last_test_passed=last_test_passed,
        )


def _to_bash_action(
    response: ModelResponse,
    *,
    run_id: UUID,
    step: int,
    working_directory: str,
) -> RepairAction:
    if not isinstance(response.action, ToolCall) or response.action.tool != "bash":
        raise ValueError("Mini-native models must return a bash tool call")
    command = response.action.arguments.get("command")
    if not isinstance(command, str) or not command.strip():
        raise ValueError("Mini-native bash tool calls require a non-empty command")
    return to_repair_action(
        response.action,
        run_id=run_id,
        step=step,
        working_directory=working_directory,
        parent_model_call_id=response.model_call_id,
    )


def _observation_history_item(action: RepairAction, result: RepairActionResult) -> JsonValue:
    output = result.stdout + result.stderr
    return cast(
        JsonValue,
        {
            "kind": "TOOL_RESULT",
            "payload": {
                "output": format_observation(
                    {"returncode": result.returncode, "output": output}
                )
            },
            "call_id": str(action.action_id),
        },
    )


__all__ = ["MiniNativeRepairEngine", "MiniNativeResult"]
