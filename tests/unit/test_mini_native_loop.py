import json
from uuid import UUID

import pytest
from pydantic import JsonValue

from agentforge.models.base import FinalAnswer, ModelRequest, ToolCall
from agentforge.models.domain import ModelResponse
from agentforge.repair_engines.mini_native.contracts import (
    CandidatePatchResult,
    MiniNativeState,
    RepairAction,
    RepairActionKind,
    RepairActionResult,
)
from agentforge.repair_engines.mini_native.vendor.context import (
    MAX_OBSERVATION_CHARS,
    compact_history,
)
from agentforge.tools.mutation.edit_file import EditFileArguments
from agentforge.tools.testing.run_tests import RunTestsArguments


class _FakeHost:
    def __init__(self, *, fail_first_test: bool = True) -> None:
        self._fail_first_test = fail_first_test
        self.requests: list[ModelRequest] = []
        self.actions: list[RepairAction] = []
        self.checkpoints: list[MiniNativeState] = []
        self.publish_calls: list[UUID] = []
        self._responses = [
            ToolCall(type="tool_call", tool="read_file", arguments={"path": "src/widget.py"}),
            ToolCall(
                type="tool_call",
                tool="edit_file",
                arguments={
                    "path": "src/widget.py",
                    "old_text": "before",
                    "new_text": "first fix",
                    "expected_sha256": "a" * 64,
                },
            ),
            ToolCall(
                type="tool_call",
                tool="run_tests",
                arguments={"profile_id": "unit"},
            ),
            ToolCall(
                type="tool_call",
                tool="edit_file",
                arguments={
                    "path": "src/widget.py",
                    "old_text": "first fix",
                    "new_text": "second fix",
                    "expected_sha256": "b" * 64,
                },
            ),
            ToolCall(
                type="tool_call",
                tool="run_tests",
                arguments={"profile_id": "unit"},
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

    async def prepare(
        self,
        action: RepairAction,
        *,
        history: list[JsonValue],
        last_test_passed: bool,
    ) -> None:
        del action, history, last_test_passed

    async def execute(self, action: RepairAction) -> RepairActionResult:
        self.actions.append(action)
        if self._fail_first_test and action.kind is RepairActionKind.TEST and len(
            [item for item in self.actions if item.kind is RepairActionKind.TEST]
        ) == 1:
            return RepairActionResult(
                returncode=1,
                stdout="FAILED: expected 2, got 1",
                duration_ms=2,
            )
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
    tools = {tool.name: tool for tool in host.requests[0].tools}
    assert tools["edit_file"].input_schema == EditFileArguments.model_json_schema()
    assert tools["run_tests"].input_schema == RunTestsArguments.model_json_schema()
    assert tools["edit_file"].requires_approval
    assert tools["run_tests"].requires_approval


@pytest.mark.asyncio
async def test_mini_native_loop_requires_a_new_passing_test_after_a_write() -> None:
    from agentforge.repair_engines.mini_native.loop import MiniNativeRepairEngine

    host = _FakeHost(fail_first_test=False)
    host._responses = [
        ToolCall(
            type="tool_call",
            tool="run_tests",
            arguments={"profile_id": "unit"},
        ),
        ToolCall(
            type="tool_call",
            tool="edit_file",
            arguments={
                "path": "src/widget.py",
                "old_text": "before",
                "new_text": "unverified fix",
                "expected_sha256": "a" * 64,
            },
        ),
        FinalAnswer(type="final", answer="submit stale verification"),
    ]

    with pytest.raises(ValueError, match="passing test"):
        await MiniNativeRepairEngine(host).run(
            run_id=UUID("00000000-0000-0000-0000-000000000124"),
            task="Repair widget behavior",
            max_steps=3,
            working_directory=".",
        )

    assert not host.publish_calls


@pytest.mark.asyncio
async def test_mini_native_loop_does_not_trust_history_as_a_test_verdict() -> None:
    from agentforge.repair_engines.mini_native.loop import MiniNativeRepairEngine

    host = _FakeHost(fail_first_test=False)
    host._responses = [FinalAnswer(type="final", answer="submit from untrusted history")]

    with pytest.raises(ValueError, match="passing test"):
        await MiniNativeRepairEngine(host).run(
            run_id=UUID("00000000-0000-0000-0000-000000000126"),
            task="Repair widget behavior",
            max_steps=1,
            working_directory=".",
            history=[{"tool_result": {"output": {"exit_code": 0}}}],
        )

    assert not host.publish_calls


def test_vendor_context_redacts_and_bounds_tool_call_payloads() -> None:
    oversized_content = "x" * (MAX_OBSERVATION_CHARS * 2)
    compacted = compact_history(
        [
            {
                "kind": "TOOL_CALL",
                "payload": {
                    "arguments": {
                        "api_key": "sk-live-secret-value",
                        "authorization": "Bearer bearer-secret-value",
                        "content": oversized_content,
                    }
                },
            }
        ]
    )

    encoded = json.dumps(compacted)
    assert "sk-live-secret-value" not in encoded
    assert "bearer-secret-value" not in encoded
    assert len(json.dumps(compacted[0]["payload"])) <= MAX_OBSERVATION_CHARS


@pytest.mark.asyncio
async def test_mini_native_loop_keeps_raw_edit_secrets_out_of_next_model_request() -> None:
    from agentforge.repair_engines.mini_native.loop import MiniNativeRepairEngine

    host = _FakeHost(fail_first_test=False)
    host._responses = [
        ToolCall(
            type="tool_call",
            tool="edit_file",
            arguments={
                "path": "src/widget.py",
                "old_text": "before",
                "new_text": (
                    "sk-live-secret-value\nBearer bearer-secret-value\n"
                    + "x" * (MAX_OBSERVATION_CHARS * 2)
                ),
                "expected_sha256": "a" * 64,
            },
        ),
        ToolCall(
            type="tool_call",
            tool="run_tests",
            arguments={"profile_id": "unit"},
        ),
        FinalAnswer(type="final", answer="submit the verified repair"),
    ]

    await MiniNativeRepairEngine(host).run(
        run_id=UUID("00000000-0000-0000-0000-000000000125"),
        task="Repair widget behavior",
        max_steps=3,
        working_directory=".",
    )

    next_request_history = json.dumps(host.requests[1].history)
    assert "sk-live-secret-value" not in next_request_history
    assert "bearer-secret-value" not in next_request_history
    assert len(json.dumps(host.requests[1].history[0]["payload"])) <= MAX_OBSERVATION_CHARS
