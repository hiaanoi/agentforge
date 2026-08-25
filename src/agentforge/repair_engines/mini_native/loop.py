from __future__ import annotations

from dataclasses import dataclass
from typing import cast
from uuid import UUID

from pydantic import JsonValue

from agentforge.domain.enums import ToolRisk
from agentforge.domain.models import ToolSpec
from agentforge.models.base import ModelRequest
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
from agentforge.repair_engines.mini_native.host import (
    MiniNativeHost,
)
from agentforge.repair_engines.mini_native.vendor.loop import VendorRepairLoop
from agentforge.tools.mutation.edit_file import EditFileArguments
from agentforge.tools.repository.read_file import ReadFileArguments
from agentforge.tools.testing.run_tests import RunTestsArguments

_SYSTEM_PROMPT = "You are a helpful assistant that can interact with a computer to repair code."
_TOOLS = [
    ToolSpec(
        name="read_file",
        description="Read a file from the candidate workspace",
        input_schema=ReadFileArguments.model_json_schema(),
        risk_level=ToolRisk.READ,
        requires_approval=False,
    ),
    ToolSpec(
        name="edit_file",
        description="Edit a file in the candidate workspace",
        input_schema=EditFileArguments.model_json_schema(),
        risk_level=ToolRisk.WRITE,
        requires_approval=True,
    ),
    ToolSpec(
        name="run_tests",
        description="Run focused tests in the candidate workspace",
        input_schema=RunTestsArguments.model_json_schema(),
        risk_level=ToolRisk.DANGEROUS,
        requires_approval=True,
    ),
]


@dataclass(frozen=True, slots=True)
class MiniNativeResult:
    submitted: bool
    candidate: CandidatePatchResult | None
    history: list[JsonValue]
    model_calls: int
    last_test_passed: bool = False


class MiniNativeRepairEngine:
    """Adapt the pinned mini-SWE-agent loop to AgentForge's host protocol."""

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
                task=task,
                step_number=step,
                instructions=_SYSTEM_PROMPT,
                preserve_tool_call_text=True,
                history=compacted_history,
                tools=_TOOLS,
            )

        async def execute(response: ModelResponse, step: int) -> bool:
            nonlocal candidate, last_test_passed
            action = to_repair_action(
                response.action,
                run_id=run_id,
                step=step,
                working_directory=working_directory,
                parent_model_call_id=response.model_call_id,
            )
            history.append(action_history_item(action))
            if action.kind is RepairActionKind.FINAL and not last_test_passed:
                history.append(
                    _result_history_item(
                        action,
                        RepairActionResult(
                            returncode=1,
                            stdout="A passing test is required before submission",
                            duration_ms=0,
                        ),
                    )
                )
                return False
            result = await self._host.execute(action)
            history.append(_result_history_item(action, result))
            if action.kind is RepairActionKind.TEST:
                last_test_passed = result.returncode == 0
            elif action.kind is RepairActionKind.WRITE:
                last_test_passed = False
            if action.kind is RepairActionKind.FINAL and result.returncode in {None, 0}:
                candidate = await self._host.publish(run_id)
                return candidate.published
            return False

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
        """Finish a response checkpoint without requesting the model again."""
        candidate: CandidatePatchResult | None = None
        if action.kind is RepairActionKind.FINAL and not last_test_passed:
            result = RepairActionResult(
                returncode=1,
                stdout="A passing test is required before submission",
                duration_ms=0,
            )
            history.append(_result_history_item(action, result))
            await self._host.checkpoint(
                MiniNativeState(
                    run_id=action.run_id,
                    step_number=1,
                    history=tuple(history),
                    last_test_passed=False,
                )
            )
            return MiniNativeResult(
                submitted=False,
                candidate=None,
                history=history,
                model_calls=0,
                last_test_passed=False,
            )
        result = saved_result or await self._host.execute(action)
        history.append(_result_history_item(action, result))
        if action.kind is RepairActionKind.TEST:
            last_test_passed = result.returncode == 0
        elif action.kind is RepairActionKind.WRITE:
            last_test_passed = False
        if action.kind is RepairActionKind.FINAL and result.returncode in {None, 0}:
            candidate = await self._host.publish(action.run_id)
        await self._host.checkpoint(
            MiniNativeState(
                run_id=action.run_id,
                step_number=1,
                history=tuple(history),
                last_test_passed=last_test_passed,
            )
        )
        return MiniNativeResult(
            submitted=bool(candidate and candidate.published),
            candidate=candidate,
            history=history,
            model_calls=0,
            last_test_passed=last_test_passed,
        )


def _result_history_item(action: RepairAction, result: RepairActionResult) -> JsonValue:
    return cast(
        JsonValue,
        {
            "kind": "TOOL_RESULT",
            "payload": result.model_dump(mode="json"),
            "call_id": str(action.action_id),
        },
    )


__all__ = ["MiniNativeRepairEngine", "MiniNativeResult"]
