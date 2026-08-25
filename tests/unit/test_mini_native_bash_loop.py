from uuid import UUID

import pytest

from agentforge.models.base import ModelRequest, ToolCall
from agentforge.models.deepseek_provider import DeepSeekModelProvider
from agentforge.models.domain import ModelResponse
from agentforge.models.openai_provider import OpenAIModelProvider
from agentforge.repair_engines.mini_native.contracts import (
    CandidatePatchResult,
    MiniNativeState,
    RepairAction,
    RepairActionKind,
    RepairActionResult,
)
from agentforge.repair_engines.mini_native.vendor.bash_protocol import (
    BASH_INSTANCE_TEMPLATE,
    BASH_SYSTEM_TEMPLATE,
    bash_tool_schema,
    format_observation,
)


class _FakeBashHost:
    def __init__(self) -> None:
        self.requests: list[ModelRequest] = []
        self.actions: list[RepairAction] = []
        self.checkpoints: list[MiniNativeState] = []
        self.publish_calls: list[UUID] = []
        self._commands = iter(
            (
                "ls",
                "sed -i 's/before/after/' src/widget.py",
                "pytest -q",
                "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT",
            )
        )

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request.model_copy(deep=True))
        return ModelResponse(
            action=ToolCall(
                type="tool_call",
                tool="bash",
                arguments={"command": next(self._commands)},
            ),
            provider="fake",
            model="fake-model",
            duration_ms=1,
            attempt_count=1,
        )

    async def execute(self, action: RepairAction) -> RepairActionResult:
        self.actions.append(action)
        output = (
            "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\ncandidate complete"
            if action.arguments["command"] == "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"
            else f"observed {action.arguments['command']}"
        )
        return RepairActionResult(returncode=0, stdout=output, duration_ms=2)

    async def checkpoint(self, state: MiniNativeState) -> None:
        self.checkpoints.append(state)

    async def publish(self, run_id: UUID) -> CandidatePatchResult:
        self.publish_calls.append(run_id)
        return CandidatePatchResult(run_id=run_id, published=True, patch_digest="a" * 64)


@pytest.mark.asyncio
async def test_mini_native_loop_runs_frozen_bash_protocol_and_publishes_submission() -> None:
    from agentforge.repair_engines.mini_native.loop import MiniNativeRepairEngine

    host = _FakeBashHost()
    run_id = UUID("00000000-0000-0000-0000-000000000128")

    result = await MiniNativeRepairEngine(host).run(
        run_id=run_id,
        task="Repair widget behavior",
        max_steps=4,
        working_directory=".",
    )

    assert [action.tool_name for action in host.actions] == ["bash"] * 4
    assert [action.kind for action in host.actions] == [RepairActionKind.BASH] * 4
    assert [action.arguments["command"] for action in host.actions] == [
        "ls",
        "sed -i 's/before/after/' src/widget.py",
        "pytest -q",
        "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT",
    ]
    assert len(host.requests[0].tools) == 1
    assert host.requests[0].tools[0].name == "bash"
    assert host.requests[0].tools[0].description == bash_tool_schema()["description"]
    assert host.requests[0].tools[0].input_schema == bash_tool_schema()["parameters"]
    assert host.requests[0].instructions == BASH_SYSTEM_TEMPLATE
    assert host.requests[0].task == BASH_INSTANCE_TEMPLATE.replace(
        "{{task}}", "Repair widget behavior"
    )
    assert host.requests[1].history == [
        {
            "kind": "TOOL_CALL",
            "payload": {
                "tool_name": "bash",
                "kind": "BASH",
                "arguments": {"command": "ls"},
            },
            "call_id": str(host.actions[0].action_id),
        },
        {
            "kind": "TOOL_RESULT",
            "payload": {"output": format_observation({"returncode": 0, "output": "observed ls"})},
            "call_id": str(host.actions[0].action_id),
        },
    ]
    assert result.submitted
    assert result.candidate is not None and result.candidate.published
    assert host.publish_calls == [run_id]


def test_bash_history_maps_to_provider_function_call_pairs() -> None:
    call = {
        "kind": "TOOL_CALL",
        "payload": {"tool_name": "bash", "arguments": {"command": "ls"}},
        "call_id": "bash-call-id",
    }
    result = {
        "kind": "TOOL_RESULT",
        "payload": {"output": format_observation({"returncode": 0, "output": "ok"})},
        "call_id": "bash-call-id",
    }

    openai_call = OpenAIModelProvider._map_history_item(call)
    openai_result = OpenAIModelProvider._map_history_item(result)
    deepseek_call = DeepSeekModelProvider._map_history_item(call)
    deepseek_result = DeepSeekModelProvider._map_history_item(result)

    assert openai_call["type"] == "function_call"
    assert openai_call["name"] == "bash"
    assert openai_result["type"] == "function_call_output"
    assert openai_result["call_id"] == "bash-call-id"
    assert deepseek_call["tool_calls"][0]["function"]["name"] == "bash"
    assert deepseek_result["tool_call_id"] == "bash-call-id"


@pytest.mark.asyncio
async def test_mini_native_loop_rejects_non_bash_actions() -> None:
    from agentforge.repair_engines.mini_native.loop import MiniNativeRepairEngine

    host = _FakeBashHost()
    host._commands = iter(())

    async def generate_non_bash(request: ModelRequest) -> ModelResponse:
        del request
        return ModelResponse(
            action=ToolCall(type="tool_call", tool="read_file", arguments={"path": "x"}),
            provider="fake",
            model="fake-model",
            duration_ms=1,
            attempt_count=1,
        )

    host.generate = generate_non_bash  # type: ignore[method-assign]
    with pytest.raises(ValueError, match="bash"):
        await MiniNativeRepairEngine(host).run(
            run_id=UUID("00000000-0000-0000-0000-000000000129"),
            task="Repair widget behavior",
            max_steps=1,
            working_directory=".",
        )
