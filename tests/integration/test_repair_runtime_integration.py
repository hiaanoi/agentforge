from pathlib import Path
from uuid import uuid4

import pytest

from agentforge.context.models import LoopObservation, LoopState
from agentforge.domain.enums import EventType, RunStatus
from agentforge.domain.repair import (
    BudgetProfile,
    RepairCompletionStatus,
    RepairDifficulty,
    RepairTaskPolicy,
    RepairTerminationReason,
)
from agentforge.models.base import ModelRequest
from agentforge.models.domain import ModelErrorCode, ModelResponse
from agentforge.models.errors import ModelRequestError
from agentforge.models.executor import ModelExecutor
from agentforge.models.mock import MockModelProvider
from agentforge.persistence.database import Database
from agentforge.persistence.model_workflow import ModelWorkflow
from agentforge.persistence.repair_workflow import RepairWorkflow
from agentforge.persistence.repositories import (
    CheckpointRepository,
    EventRepository,
    RunRepository,
)
from agentforge.policy.engine import PolicyEngine
from agentforge.policy.sensitive import SensitiveFilePolicy
from agentforge.runtime.engine import AgentRuntime
from agentforge.runtime.repair import RepairCoordinator
from agentforge.tools.executor import ToolExecutor
from agentforge.tools.paths import WorkspacePathResolver
from agentforge.tools.registry import ToolRegistry
from agentforge.tools.repository.read_file import ReadFileTool


class TimeoutProvider:
    def __init__(self) -> None:
        self.requests: list[ModelRequest] = []

    @property
    def name(self) -> str:
        return "timeout-provider"

    @property
    def journal_identity(self) -> str:
        return "timeout-provider/test-model"

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request.model_copy(deep=True))
        raise ModelRequestError(
            ModelErrorCode.MODEL_TIMEOUT,
            "provider detail must not enter persisted events",
            retryable=False,
        )


def repair_policy() -> RepairTaskPolicy:
    return RepairTaskPolicy(
        task_id="runtime-repair",
        policy_version=1,
        difficulty=RepairDifficulty.BASIC,
        budget_profile=BudgetProfile.BASIC,
        allowed_write_paths=("src/**",),
        protected_paths=("tests/**",),
        allowed_development_test_profiles=("unit",),
        final_verification_profile_id="hidden",
        allow_file_creation=False,
        max_created_files=0,
        max_changed_files=2,
        max_total_changed_bytes=1024,
        max_single_file_changed_bytes=1024,
        path_case_sensitive=False,
    )


@pytest.mark.asyncio
async def test_unverified_final_answer_is_corrected_before_run_completion(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    database = Database.from_path(tmp_path / "runtime.db")
    database.create_schema()
    runs = RunRepository(database)
    events = EventRepository(database)
    checkpoints = CheckpointRepository(database)
    provider = MockModelProvider(
        [
            {"type": "final", "answer": "done too early"},
            {"type": "final", "answer": "still done"},
        ]
    )
    workflow = RepairWorkflow(database)
    coordinator = RepairCoordinator(workflow)
    resolver = WorkspacePathResolver(workspace)
    runtime = AgentRuntime(
        runs,
        events,
        checkpoints,
        provider,
        ToolExecutor(
            ToolRegistry(),
            PolicyEngine(resolver, SensitiveFilePolicy()),
            events,
            runs,
        ),
        repair_coordinator=coordinator,
    )
    run = runtime.create_run("repair the fixture")
    workflow._evaluator_only_start(run.run_id, repair_policy(), uuid4(), "a" * 64)

    completed = await runtime.execute(run.run_id)

    assert completed.status is RunStatus.FAILED
    assert workflow.get_state(run.run_id).status is RepairCompletionStatus.UNVERIFIED_FINAL
    assert len(provider.requests) == 2
    second_history = provider.requests[1].model_dump_json()
    assert "no allowed development test" in second_history
    assert "edit_file" not in second_history


@pytest.mark.asyncio
async def test_model_timeout_terminalizes_repair_state_with_safe_failure_facts(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    database = Database.from_path(tmp_path / "runtime.db")
    database.create_schema()
    runs = RunRepository(database)
    events = EventRepository(database)
    workflow = RepairWorkflow(database)
    provider = TimeoutProvider()
    model_workflow = ModelWorkflow(database)
    runtime = AgentRuntime(
        runs,
        events,
        CheckpointRepository(database),
        provider,
        ToolExecutor(
            ToolRegistry(),
            PolicyEngine(WorkspacePathResolver(workspace), SensitiveFilePolicy()),
            events,
            runs,
        ),
        model_executor=ModelExecutor(provider, model_workflow),
        model_workflow=model_workflow,
        repair_coordinator=RepairCoordinator(workflow),
    )
    run = runtime.create_run("repair the fixture")
    workflow._evaluator_only_start(run.run_id, repair_policy(), uuid4(), "a" * 64)

    failed = await runtime.execute(run.run_id)

    state = workflow.get_state(run.run_id)
    assert failed.status is RunStatus.FAILED
    assert failed.error_message == RepairCompletionStatus.RUNTIME_FAILURE.value
    assert state.status is RepairCompletionStatus.RUNTIME_FAILURE
    assert state.failure_reason is RepairTerminationReason.MODEL_TIMEOUT
    assert len(provider.requests) == 1
    serialized_events = " ".join(
        event.model_dump_json() for event in events.list_for_run(run.run_id)
    )
    assert "provider detail must not enter persisted events" not in serialized_events
    assert any(
        event.event_type is EventType.REPAIR_TASK_FAILED
        and event.payload
        == {
            "status": RepairCompletionStatus.RUNTIME_FAILURE.value,
            "reason": RepairTerminationReason.MODEL_TIMEOUT.value,
        }
        for event in events.list_for_run(run.run_id)
    )
    database.close()


@pytest.mark.asyncio
async def test_recoverable_model_tool_failures_use_a_bounded_correction_budget(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    database = Database.from_path(tmp_path / "runtime.db")
    database.create_schema()
    runs = RunRepository(database)
    events = EventRepository(database)
    workflow = RepairWorkflow(database)
    resolver = WorkspacePathResolver(workspace)
    provider = MockModelProvider(
        [
            {
                "type": "tool_call",
                "tool": "read_file",
                "arguments": {"path": "missing.py", "unexpected": 1},
            },
            {
                "type": "tool_call",
                "tool": "read_file",
                "arguments": {"path": "missing.py", "unexpected": 2},
            },
            {
                "type": "tool_call",
                "tool": "read_file",
                "arguments": {"path": "missing.py", "unexpected": 3},
            },
        ]
    )
    runtime = AgentRuntime(
        runs,
        events,
        CheckpointRepository(database),
        provider,
        ToolExecutor(
            ToolRegistry([ReadFileTool(resolver, SensitiveFilePolicy())]),
            PolicyEngine(resolver, SensitiveFilePolicy()),
            events,
            runs,
        ),
        repair_coordinator=RepairCoordinator(workflow),
    )
    run = runtime.create_run("correct malformed read requests")
    workflow._evaluator_only_start(run.run_id, repair_policy(), uuid4(), "a" * 64)

    failed = await runtime.execute(run.run_id)

    state = workflow.get_state(run.run_id)
    assert failed.status is RunStatus.FAILED
    assert state.status is RepairCompletionStatus.POLICY_BLOCKED
    assert state.failure_reason is RepairTerminationReason.POLICY_VIOLATION_LIMIT
    assert state.policy_violations == 2
    assert len(provider.requests) == 3
    second_request = provider.requests[1].model_dump_json()
    assert "INVALID_ARGUMENTS" in second_request
    assert "Tool arguments failed schema validation" in second_request
    database.close()


@pytest.mark.asyncio
async def test_sensitive_tool_failure_remains_fail_closed(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".env").write_text("SECRET=must-not-be-read\n", encoding="utf-8")
    database = Database.from_path(tmp_path / "runtime.db")
    database.create_schema()
    runs = RunRepository(database)
    events = EventRepository(database)
    workflow = RepairWorkflow(database)
    resolver = WorkspacePathResolver(workspace)
    provider = MockModelProvider(
        [
            {
                "type": "tool_call",
                "tool": "read_file",
                "arguments": {"path": ".env"},
            },
            {"type": "final_answer", "answer": "should not be reached"},
        ]
    )
    runtime = AgentRuntime(
        runs,
        events,
        CheckpointRepository(database),
        provider,
        ToolExecutor(
            ToolRegistry([ReadFileTool(resolver, SensitiveFilePolicy())]),
            PolicyEngine(resolver, SensitiveFilePolicy()),
            events,
            runs,
        ),
        repair_coordinator=RepairCoordinator(workflow),
    )
    run = runtime.create_run("read a sensitive fixture file")
    workflow._evaluator_only_start(run.run_id, repair_policy(), uuid4(), "a" * 64)

    failed = await runtime.execute(run.run_id)

    state = workflow.get_state(run.run_id)
    assert failed.status is RunStatus.FAILED
    assert state.status is RepairCompletionStatus.MODEL_TOOL_FAILED
    assert state.failure_reason is RepairTerminationReason.MODEL_TOOL_FAILED
    assert state.policy_violations == 0
    assert len(provider.requests) == 1
    database.close()


@pytest.mark.asyncio
async def test_repeated_successful_tool_loop_terminalizes_repair_state(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "module.py").write_text("value = 1\n", encoding="utf-8")
    database = Database.from_path(tmp_path / "runtime.db")
    database.create_schema()
    runs = RunRepository(database)
    events = EventRepository(database)
    workflow = RepairWorkflow(database)
    resolver = WorkspacePathResolver(workspace)
    provider = MockModelProvider(
        [
            {"type": "tool_call", "tool": "read_file", "arguments": {"path": "module.py"}},
        ]
    )

    class TerminalLoopDetector:
        def observe(self, state: LoopState, **_: object) -> LoopObservation:
            return LoopObservation(state=state, warning=False, terminal=True)

    runtime = AgentRuntime(
        runs,
        events,
        CheckpointRepository(database),
        provider,
        ToolExecutor(
            ToolRegistry([ReadFileTool(resolver, SensitiveFilePolicy())]),
            PolicyEngine(resolver, SensitiveFilePolicy()),
            events,
            runs,
        ),
        repair_coordinator=RepairCoordinator(workflow),
        loop_detector=TerminalLoopDetector(),  # type: ignore[arg-type]
    )
    run = runtime.create_run("read the same file repeatedly")
    workflow._evaluator_only_start(run.run_id, repair_policy(), uuid4(), "a" * 64)

    failed = await runtime.execute(run.run_id)

    state = workflow.get_state(run.run_id)
    assert failed.status is RunStatus.FAILED
    assert state.status is RepairCompletionStatus.RUNTIME_FAILURE
    assert state.failure_reason is RepairTerminationReason.RUNTIME_FAILURE
    database.close()
