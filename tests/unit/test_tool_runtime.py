import asyncio
import threading
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from agentforge.application.run_driver import RunOwnership
from agentforge.domain.digests import compute_tool_call_digest
from agentforge.domain.enums import EventType, ToolErrorCode, ToolRisk
from agentforge.domain.errors import DuplicateToolError, ToolExecutionError, ToolRuntimeError
from agentforge.domain.models import (
    ApprovalAuthorization,
    ApprovalRequired,
    Run,
    ToolResult,
    ToolSpec,
)
from agentforge.persistence.database import Database
from agentforge.persistence.repositories import EventRepository, RunRepository
from agentforge.persistence.run_leases import RunLeaseStore
from agentforge.policy.engine import PolicyEngine
from agentforge.policy.sensitive import SensitiveFilePolicy
from agentforge.tools.executor import ToolExecutor
from agentforge.tools.paths import WorkspacePathResolver
from agentforge.tools.registry import ToolRegistry


class ValueArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str


class SyncValueTool:
    def __init__(
        self,
        name: str = "sync_value",
        *,
        risk: ToolRisk = ToolRisk.READ,
        output: str | None = None,
        requires_approval: bool = False,
    ) -> None:
        self._name = name
        self._risk = risk
        self._output = output
        self._requires_approval = requires_approval
        self.called = False

    @property
    def input_model(self) -> type[BaseModel]:
        return ValueArguments

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=self._name,
            description="Return a deterministic value",
            input_schema=self.input_model.model_json_schema(),
            risk_level=self._risk,
            requires_approval=self._requires_approval,
        )

    def execute(self, arguments: BaseModel) -> ToolResult:
        self.called = True
        parsed = ValueArguments.model_validate(arguments)
        return ToolResult(success=True, output=self._output or parsed.value)


class AsyncValueTool(SyncValueTool):
    async def execute(self, arguments: BaseModel) -> ToolResult:
        await asyncio.sleep(0)
        return super().execute(arguments)


class MismatchedSchemaTool(SyncValueTool):
    @property
    def spec(self) -> ToolSpec:
        return super().spec.model_copy(update={"input_schema": {"type": "object"}})


class CancelledTool(SyncValueTool):
    def __init__(self) -> None:
        super().__init__("cancelled_value")
        self.entered = asyncio.Event()

    async def execute(self, arguments: BaseModel) -> ToolResult:
        del arguments
        self.entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class SlowTool(SyncValueTool):
    def __init__(self) -> None:
        super().__init__("slow_value")
        self.entered = threading.Event()
        self.release = threading.Event()
        self.completed = threading.Event()

    @property
    def spec(self) -> ToolSpec:
        return self._base_spec(timeout_seconds=0.01)

    def _base_spec(self, timeout_seconds: float) -> ToolSpec:
        return ToolSpec(
            name="slow_value",
            description="Sleep longer than the configured timeout",
            input_schema=self.input_model.model_json_schema(),
            risk_level=ToolRisk.READ,
            timeout_seconds=timeout_seconds,
        )

    def execute(self, arguments: BaseModel) -> ToolResult:
        self.entered.set()
        self.release.wait(timeout=1.0)
        result = super().execute(arguments)
        self.completed.set()
        return result


class KnownFailureTool(SyncValueTool):
    def execute(self, arguments: BaseModel) -> ToolResult:
        del arguments
        raise ToolExecutionError(ToolErrorCode.FILE_TOO_LARGE, "Known safe failure")


class UnknownFailureTool(SyncValueTool):
    def execute(self, arguments: BaseModel) -> ToolResult:
        del arguments
        raise RuntimeError("C:\\outside\\private\\path.txt")


def make_harness(
    tmp_path: Path,
    tools: list[SyncValueTool],
    *,
    max_output_chars: int = 20_000,
    max_tool_calls: int = 10,
) -> tuple[ToolExecutor, Run, EventRepository, RunRepository, Database, RunOwnership]:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    database = Database.from_path(tmp_path / "tool-runtime.sqlite3")
    database.create_schema()
    runs = RunRepository(database)
    events = EventRepository(database)
    run = runs.create(Run(task="tool runtime", max_tool_calls=max_tool_calls))
    executor = ToolExecutor(
        ToolRegistry(tools),
        PolicyEngine(WorkspacePathResolver(workspace), SensitiveFilePolicy()),
        events,
        runs,
        max_output_chars=max_output_chars,
    )
    lease = RunLeaseStore(database).acquire(
        run.run_id, owner_id="test:tool-runtime", ttl=timedelta(seconds=30)
    )
    return executor, run, events, runs, database, RunOwnership(lambda: lease.authority)


def test_registry_registers_lists_exports_and_rejects_duplicates() -> None:
    first = SyncValueTool("alpha")
    second = SyncValueTool("beta")
    registry = ToolRegistry([second, first])

    assert registry.get("alpha") is first
    assert [tool.spec.name for tool in registry.list_tools()] == ["alpha", "beta"]
    assert [schema["name"] for schema in registry.export_schemas()] == ["alpha", "beta"]
    assert registry.export_schemas()[0]["input_schema"] == ValueArguments.model_json_schema()
    with pytest.raises(DuplicateToolError):
        registry.register(SyncValueTool("alpha"))


def test_registry_rejects_schema_mismatch_with_domain_error() -> None:
    with pytest.raises(ToolRuntimeError) as error:
        ToolRegistry([MismatchedSchemaTool()])

    assert error.value.code is ToolErrorCode.INVALID_TOOL_SPEC


@pytest.mark.parametrize("name", ["1tool", "BadTool", "bad-tool", "bad tool"])
def test_tool_spec_rejects_invalid_names(name: str) -> None:
    with pytest.raises(ValidationError):
        ToolSpec(
            name=name,
            description="invalid name",
            input_schema={},
            risk_level=ToolRisk.READ,
        )


def test_tool_spec_rejects_blank_description() -> None:
    with pytest.raises(ValidationError):
        ToolSpec(name="valid", description="   ", input_schema={}, risk_level=ToolRisk.READ)


def test_argument_sanitization_is_recursive_and_bounded() -> None:
    sanitized = ToolExecutor._sanitize_arguments(
        {
            "path": "src/app.py",
            "nested": {
                "api_token": "do-not-store",
                "safe": "x" * 250,
            },
        }
    )

    nested = sanitized["nested"]
    assert isinstance(nested, dict)
    assert nested["api_token"] == "<redacted>"
    assert isinstance(nested["safe"], str) and len(nested["safe"]) == 200


@pytest.mark.parametrize("path", [".env", "nested/API_TOKEN.txt", "C:\\Users\\private.txt"])
def test_argument_sanitization_redacts_sensitive_or_absolute_paths(path: str) -> None:
    assert ToolExecutor._sanitize_arguments({"path": path}) == {"path": "<redacted>"}


def test_argument_sanitization_keeps_normal_relative_path() -> None:
    assert ToolExecutor._sanitize_arguments({"path": "src/app.py"}) == {"path": "src/app.py"}


@pytest.mark.asyncio
async def test_executor_supports_sync_async_and_audits_success(tmp_path: Path) -> None:
    sync_tool = SyncValueTool()
    async_tool = AsyncValueTool("async_value")
    executor, run, events, _, database, ownership = make_harness(tmp_path, [sync_tool, async_tool])

    sync_result = await executor.execute(run, "sync_value", {"value": "sync"}, ownership=ownership)
    async_result = await executor.execute(
        run, "async_value", {"value": "async"}, ownership=ownership
    )

    assert sync_result.success and sync_result.output == "sync"
    assert async_result.success and async_result.output == "async"
    assert run.tool_call_count == 2
    assert [event.event_type for event in events.list_for_run(run.run_id)] == [
        EventType.TOOL_REQUESTED,
        EventType.TOOL_STARTED,
        EventType.TOOL_COMPLETED,
        EventType.TOOL_REQUESTED,
        EventType.TOOL_STARTED,
        EventType.TOOL_COMPLETED,
    ]
    completed_payload = events.list_for_run(run.run_id)[2].payload
    assert "output" not in completed_payload
    assert completed_payload["success"] is True
    database.close()


@pytest.mark.asyncio
async def test_executor_rejects_bad_arguments_without_starting_tool(tmp_path: Path) -> None:
    tool = SyncValueTool()
    executor, run, events, _, database, ownership = make_harness(tmp_path, [tool])

    result = await executor.execute(run, "sync_value", {"unexpected": "value"}, ownership=ownership)

    assert result.error_type is ToolErrorCode.INVALID_ARGUMENTS
    assert not tool.called
    assert [event.event_type for event in events.list_for_run(run.run_id)] == [
        EventType.TOOL_REQUESTED,
        EventType.TOOL_FAILED,
    ]
    database.close()


@pytest.mark.asyncio
async def test_executor_standardizes_timeout_known_and_unknown_errors(tmp_path: Path) -> None:
    slow_tool = SlowTool()
    tools = [slow_tool, KnownFailureTool("known_failure"), UnknownFailureTool("unknown_failure")]
    executor, run, _, _, database, ownership = make_harness(tmp_path, tools)

    timeout = await executor.execute(run, "slow_value", {"value": "slow"}, ownership=ownership)
    assert slow_tool.entered.is_set()
    assert not slow_tool.completed.is_set()
    slow_tool.release.set()
    known = await executor.execute(run, "known_failure", {"value": "known"}, ownership=ownership)
    unknown = await executor.execute(
        run, "unknown_failure", {"value": "unknown"}, ownership=ownership
    )

    assert timeout.error_type is ToolErrorCode.TOOL_TIMEOUT
    assert known.error_type is ToolErrorCode.FILE_TOO_LARGE
    assert known.error_message == "Known safe failure"
    assert unknown.error_type is ToolErrorCode.TOOL_EXECUTION_ERROR
    assert unknown.error_message == "Tool execution failed unexpectedly"
    assert "outside" not in (unknown.error_message or "")
    assert await asyncio.to_thread(slow_tool.completed.wait, 1.0)
    database.close()


@pytest.mark.asyncio
async def test_executor_audits_cancellation_before_propagating_it(tmp_path: Path) -> None:
    tool = CancelledTool()
    executor, run, events, _, database, ownership = make_harness(tmp_path, [tool])
    execution = asyncio.create_task(
        executor.execute(run, "cancelled_value", {"value": "unused"}, ownership=ownership)
    )
    await tool.entered.wait()

    execution.cancel()

    with pytest.raises(asyncio.CancelledError):
        await execution
    recorded = events.list_for_run(run.run_id)
    assert [event.event_type for event in recorded] == [
        EventType.TOOL_REQUESTED,
        EventType.TOOL_STARTED,
        EventType.TOOL_FAILED,
    ]
    assert recorded[-1].payload["error_type"] == "TOOL_CANCELLED"
    database.close()


@pytest.mark.asyncio
async def test_executor_truncates_output_and_records_original_size(tmp_path: Path) -> None:
    executor, run, events, _, database, ownership = make_harness(
        tmp_path,
        [SyncValueTool(output="x" * 500)],
        max_output_chars=40,
    )

    result = await executor.execute(run, "sync_value", {"value": "unused"}, ownership=ownership)

    assert result.success
    assert result.truncated
    assert isinstance(result.output, str) and len(result.output) == 40
    assert result.metadata["original_output_chars"] > 40
    assert events.list_for_run(run.run_id)[-1].payload["truncated"] is True
    database.close()


@pytest.mark.asyncio
async def test_policy_deny_prevents_write_tool_and_omits_started_event(tmp_path: Path) -> None:
    tool = SyncValueTool("write_value", risk=ToolRisk.WRITE)
    executor, run, events, _, database, ownership = make_harness(tmp_path, [tool])

    result = await executor.execute(run, "write_value", {"value": "blocked"}, ownership=ownership)

    assert result.error_type is ToolErrorCode.POLICY_DENIED
    assert not tool.called
    assert run.tool_call_count == 0
    assert [event.event_type for event in events.list_for_run(run.run_id)] == [
        EventType.TOOL_REQUESTED,
        EventType.TOOL_FAILED,
    ]
    database.close()


@pytest.mark.asyncio
async def test_policy_blocks_dangerous_tool_before_execution(tmp_path: Path) -> None:
    tool = SyncValueTool(
        "blocked_value",
        risk=ToolRisk.DANGEROUS,
    )
    executor, run, events, _, database, ownership = make_harness(tmp_path, [tool])

    result = await executor.execute(run, "blocked_value", {"value": "blocked"}, ownership=ownership)

    assert isinstance(result, ToolResult)
    assert result.error_type is ToolErrorCode.POLICY_DENIED
    assert not tool.called
    assert EventType.TOOL_STARTED not in {
        event.event_type for event in events.list_for_run(run.run_id)
    }
    database.close()


@pytest.mark.asyncio
async def test_approval_required_is_not_a_tool_failure_or_execution(tmp_path: Path) -> None:
    tool = SyncValueTool("approval_value", requires_approval=True)
    executor, run, events, _, database, ownership = make_harness(tmp_path, [tool])

    outcome = await executor.execute(run, "approval_value", {"value": "raw"}, ownership=ownership)

    assert isinstance(outcome, ApprovalRequired)
    assert outcome.validated_arguments == {"value": "raw"}
    assert not tool.called
    assert run.tool_call_count == 0
    assert [event.event_type for event in events.list_for_run(run.run_id)] == [
        EventType.TOOL_REQUESTED
    ]
    database.close()


@pytest.mark.asyncio
async def test_digest_bound_approval_executes_once_without_duplicate_request(
    tmp_path: Path,
) -> None:
    tool = SyncValueTool("approval_value", requires_approval=True)
    executor, run, events, _, database, ownership = make_harness(tmp_path, [tool])
    initial = await executor.execute(run, "approval_value", {"value": "raw"}, ownership=ownership)
    assert isinstance(initial, ApprovalRequired)
    checkpoint_id = uuid4()
    authorization = ApprovalAuthorization(
        approval_id=uuid4(),
        checkpoint_id=checkpoint_id,
        step_number=1,
        request_digest=compute_tool_call_digest(
            tool_name="approval_value",
            validated_arguments=initial.validated_arguments,
            checkpoint_id=checkpoint_id,
            step_number=1,
        ),
    )

    result = await executor.execute(
        run,
        "approval_value",
        {"value": "raw"},
        approval=authorization,
        ownership=ownership,
    )

    assert isinstance(result, ToolResult) and result.success
    assert tool.called
    assert run.tool_call_count == 1
    assert [event.event_type for event in events.list_for_run(run.run_id)] == [
        EventType.TOOL_REQUESTED,
        EventType.TOOL_STARTED,
        EventType.TOOL_COMPLETED,
    ]
    database.close()


@pytest.mark.asyncio
async def test_mismatched_approval_authorization_does_not_execute(tmp_path: Path) -> None:
    tool = SyncValueTool("approval_value", requires_approval=True)
    executor, run, events, _, database, ownership = make_harness(tmp_path, [tool])
    initial = await executor.execute(run, "approval_value", {"value": "raw"}, ownership=ownership)
    assert isinstance(initial, ApprovalRequired)
    authorization = ApprovalAuthorization(
        approval_id=uuid4(),
        checkpoint_id=uuid4(),
        step_number=1,
        request_digest="f" * 64,
    )

    result = await executor.execute(
        run,
        "approval_value",
        {"value": "changed"},
        approval=authorization,
        ownership=ownership,
    )

    assert isinstance(result, ToolResult)
    assert result.error_type is ToolErrorCode.APPROVAL_CONFLICT
    assert not tool.called
    assert EventType.TOOL_STARTED not in {
        event.event_type for event in events.list_for_run(run.run_id)
    }
    database.close()


@pytest.mark.asyncio
async def test_tool_budget_counts_execution_once_and_blocks_excess(tmp_path: Path) -> None:
    tool = SyncValueTool()
    executor, run, events, runs, database, ownership = make_harness(
        tmp_path, [tool], max_tool_calls=1
    )

    first = await executor.execute(run, "sync_value", {"value": "first"}, ownership=ownership)
    second = await executor.execute(run, "sync_value", {"value": "second"}, ownership=ownership)

    assert first.success
    assert second.error_type is ToolErrorCode.TOOL_BUDGET_EXCEEDED
    assert run.tool_call_count == 1
    assert runs.get(run.run_id).tool_call_count == 1
    assert [event.event_type for event in events.list_for_run(run.run_id)][-2:] == [
        EventType.TOOL_REQUESTED,
        EventType.TOOL_FAILED,
    ]
    database.close()


@pytest.mark.asyncio
async def test_two_runs_keep_tool_events_isolated(tmp_path: Path) -> None:
    executor, first, events, runs, database, first_ownership = make_harness(
        tmp_path, [SyncValueTool()]
    )
    second = runs.create(Run(task="second tool run"))
    second_lease = RunLeaseStore(database).acquire(
        second.run_id, owner_id="test:tool-runtime:second", ttl=timedelta(seconds=30)
    )
    second_ownership = RunOwnership(lambda: second_lease.authority)

    await executor.execute(first, "sync_value", {"value": "first"}, ownership=first_ownership)
    await executor.execute(second, "sync_value", {"value": "second"}, ownership=second_ownership)

    assert {event.run_id for event in events.list_for_run(first.run_id)} == {first.run_id}
    assert {event.run_id for event in events.list_for_run(second.run_id)} == {second.run_id}
    database.close()
