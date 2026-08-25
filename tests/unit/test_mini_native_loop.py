from uuid import UUID

import pytest

from agentforge.models.base import FinalAnswer, ModelRequest, ToolCall
from agentforge.models.domain import ModelResponse
from agentforge.repair_engines.mini_native.contracts import (
    CandidatePatchResult,
    MiniNativeState,
    RepairAction,
    RepairActionKind,
    RepairActionResult,
)


class _FakeHost:
    def __init__(self) -> None:
        self.requests: list[ModelRequest] = []
        self.actions: list[RepairAction] = []
        self.checkpoints: list[MiniNativeState] = []
        self.publish_calls: list[UUID] = []
        self._responses = [
            ToolCall(type="tool_call", tool="read_file", arguments={"path": "src/widget.py"}),
            ToolCall(
                type="tool_call",
                tool="edit_file",
                arguments={"path": "src/widget.py", "content": "first fix"},
            ),
            ToolCall(
                type="tool_call",
                tool="run_tests",
                arguments={"command": "pytest tests/unit/test_widget.py"},
            ),
            ToolCall(
                type="tool_call",
                tool="edit_file",
                arguments={"path": "src/widget.py", "content": "second fix"},
            ),
            ToolCall(
                type="tool_call",
                tool="run_tests",
                arguments={"command": "pytest tests/unit/test_widget.py"},
            ),
            FinalAnswer(type="final", answer="submit the verified repair"),
        ]

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request.model_copy(deep=True))
        return ModelResponse(
            action=self._responses.pop(0),
            provider="fake",
            model="fake-model",
            duration_ms=1,
            attempt_count=1,
        )

    async def execute(self, action: RepairAction) -> RepairActionResult:
        self.actions.append(action)
        if action.kind is RepairActionKind.TEST and len(
            [item for item in self.actions if item.kind is RepairActionKind.TEST]
        ) == 1:
            return RepairActionResult(returncode=1, stdout="FAILED: expected 2, got 1", duration_ms=2)
        return RepairActionResult(returncode=0, stdout="ok", duration_ms=2)

    async def checkpoint(self, state: MiniNativeState) -> None:
        self.checkpoints.append(state)

    async def publish(self, run_id: UUID) -> CandidatePatchResult:
        self.publish_calls.append(run_id)
        return CandidatePatchResult(run_id=run_id, published=True, patch_digest="a" * 64)


@pytest.mark.asyncio
async def test_mini_native_loop_retries_after_a_failed_test_before_submission() -> None:
    from agentforge.repair_engines.mini_native.loop import MiniNativeRepairEngine

    host = _FakeHost()
    run_id = UUID("00000000-0000-0000-0000-000000000123")

    result = await MiniNativeRepairEngine(host).run(
        run_id=run_id,
        task="Repair widget behavior",
        max_steps=6,
        working_directory=".",
    )

    assert [action.kind for action in host.actions] == [
        RepairActionKind.READ,
        RepairActionKind.WRITE,
        RepairActionKind.TEST,
        RepairActionKind.WRITE,
        RepairActionKind.TEST,
        RepairActionKind.FINAL,
    ]
    assert "FAILED: expected 2, got 1" in str(host.requests[3].history)
    assert result.submitted
    assert result.candidate.published
    assert host.publish_calls == [run_id]
    assert host.actions[-1].kind is RepairActionKind.FINAL
