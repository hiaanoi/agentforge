from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict

from agentforge.context.builder import ContextBuilder
from agentforge.context.models import ContextPolicy
from agentforge.domain.enums import (
    EventType,
    MultiToolResponsePolicy,
    RunStatus,
    ToolRisk,
)
from agentforge.domain.models import ToolResult, ToolSpec
from agentforge.models.base import FinalAnswer, ModelRequest, ToolCall
from agentforge.models.domain import (
    ModelBudget,
    ModelErrorCode,
    ModelResponse,
    ModelUsage,
    MultiToolResponseInfo,
)
from agentforge.models.errors import ModelRequestError, ProviderContractDeviationError
from agentforge.models.executor import ModelExecutor
from agentforge.persistence.approval_workflow import ApprovalWorkflow
from agentforge.persistence.database import Database
from agentforge.persistence.model_workflow import ModelWorkflow
from agentforge.persistence.repositories import (
    ApprovalRepository,
    CheckpointRepository,
    EventRepository,
    RunRepository,
)
from agentforge.policy.engine import PolicyEngine
from agentforge.policy.sensitive import SensitiveFilePolicy
from agentforge.runtime.engine import AgentRuntime
from agentforge.tools.executor import ToolExecutor
from agentforge.tools.paths import WorkspacePathResolver
from agentforge.tools.registry import ToolRegistry
from agentforge.tools.safe import EchoTool


class ResponseProvider:
    def __init__(self, responses: list[ModelResponse]) -> None:
        self._responses = responses
        self._position = 0
        self.requests: list[ModelRequest] = []

    @property
    def name(self) -> str:
        return "response-provider"

    @property
    def journal_identity(self) -> str:
        return "response-provider/test-model"

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        response = self._responses[self._position]
        self._position += 1
        return response


class FailingProvider:
    @property
    def name(self) -> str:
        return "failing-provider"

    @property
    def journal_identity(self) -> str:
        return "failing-provider/test-model"

    async def generate(self, request: ModelRequest) -> ModelResponse:
        del request
        raise ModelRequestError(
            ModelErrorCode.MODEL_AUTH_ERROR,
            "provider detail must not enter the event",
            retryable=False,
        )


class DeviationProvider:
    def __init__(self, info: MultiToolResponseInfo) -> None:
        self._info = info
        self.calls = 0

    @property
    def name(self) -> str:
        return "deviation-provider"

    @property
    def journal_identity(self) -> str:
        return "deviation-provider/test-model"

    async def generate(self, request: ModelRequest) -> ModelResponse:
        del request
        self.calls += 1
        raise ProviderContractDeviationError(
            "Provider returned a STRICT multi-tool response",
            self._info,
        )


class NamedReadArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str


class NamedReadTool:
    def __init__(
        self,
        name: str,
        calls: list[str],
        *,
        requires_approval: bool = False,
    ) -> None:
        self._name = name
        self._calls = calls
        self._requires_approval = requires_approval

    @property
    def input_model(self) -> type[BaseModel]:
        return NamedReadArguments

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=self._name,
            description=f"Record execution of {self._name}.",
            input_schema=self.input_model.model_json_schema(),
            risk_level=ToolRisk.READ,
            requires_approval=self._requires_approval,
        )

    def execute(self, arguments: BaseModel) -> ToolResult:
        parsed = NamedReadArguments.model_validate(arguments)
        self._calls.append(self._name)
        return ToolResult(
            success=True,
            output={"tool": self._name, "value": parsed.value},
        )


def response(
    action: ToolCall | FinalAnswer,
    *,
    multi_tool_response: MultiToolResponseInfo | None = None,
) -> ModelResponse:
    return ModelResponse(
        action=action,
        usage=ModelUsage(input_tokens=5, output_tokens=2, total_tokens=7),
        provider="response-provider",
        model="test-model",
        provider_request_id=(
            multi_tool_response.provider_request_id
            if multi_tool_response is not None
            else None
        ),
        duration_ms=1,
        attempt_count=1,
        sanitized_metadata=(
            {
                "returned_function_call_count": (
                    multi_tool_response.returned_call_count
                ),
                "discarded_function_call_count": (
                    multi_tool_response.discarded_call_count
                ),
                "multi_tool_policy": multi_tool_response.policy.value,
                "provider_contract_deviation": True,
            }
            if multi_tool_response is not None
            else {}
        ),
        multi_tool_response=multi_tool_response,
    )


def normalized_multi_tool_info() -> MultiToolResponseInfo:
    return MultiToolResponseInfo(
        provider="response-provider",
        model="test-model",
        provider_request_id="resp_multi",
        returned_call_count=3,
        selected_call_count=1,
        discarded_call_count=2,
        selected_tool_name="selected_read",
        discarded_tool_names=["discarded_read", "third_read"],
        policy=MultiToolResponsePolicy.SEQUENTIAL_READ_ONLY,
        reason="multiple_local_read_calls",
    )


@pytest.mark.asyncio
async def test_model_executor_enters_runtime_and_persists_v3_checkpoint(
    tmp_path: Path,
) -> None:
    database = Database.from_path(tmp_path / "real-model-runtime.sqlite3")
    database.create_schema()
    runs = RunRepository(database)
    events = EventRepository(database)
    checkpoints = CheckpointRepository(database)
    provider = ResponseProvider(
        [
            response(
                ToolCall(
                    type="tool_call",
                    call_id="call_echo",
                    tool="echo",
                    arguments={"text": "evidence"},
                )
            ),
            response(FinalAnswer(type="final", answer="done")),
        ]
    )
    model_workflow = ModelWorkflow(database)
    runtime = AgentRuntime(
        run_repository=runs,
        event_repository=events,
        checkpoint_repository=checkpoints,
        model_provider=provider,
        model_executor=ModelExecutor(provider, model_workflow),
        model_workflow=model_workflow,
        model_budget=ModelBudget(max_model_requests=4),
        tool_executor=ToolExecutor(
            ToolRegistry([EchoTool()]),
            PolicyEngine(WorkspacePathResolver(tmp_path), SensitiveFilePolicy()),
            events,
            runs,
        ),
    )
    run = runtime.create_run("use a real response adapter", max_steps=3)

    completed = await runtime.execute(run.run_id)

    assert completed.final_output == "done"
    assert model_workflow.get_state(run.run_id).model_request_count == 2
    assert model_workflow.get_state(run.run_id).total_tokens == 14
    checkpoint = checkpoints.latest(run.run_id)
    assert checkpoint is not None
    assert checkpoint.runtime_state["schema_version"] == 4
    assert checkpoint.runtime_state["history"][0]["call_id"] == "call_echo"
    database.close()


@pytest.mark.asyncio
async def test_terminal_model_failure_emits_sanitized_model_failed_event(
    tmp_path: Path,
) -> None:
    database = Database.from_path(tmp_path / "model-failure.sqlite3")
    database.create_schema()
    runs = RunRepository(database)
    events = EventRepository(database)
    provider = FailingProvider()
    model_workflow = ModelWorkflow(database)
    runtime = AgentRuntime(
        run_repository=runs,
        event_repository=events,
        checkpoint_repository=CheckpointRepository(database),
        model_provider=provider,
        model_executor=ModelExecutor(provider, model_workflow),
        model_workflow=model_workflow,
        tool_executor=ToolExecutor(
            ToolRegistry([EchoTool()]),
            PolicyEngine(WorkspacePathResolver(tmp_path), SensitiveFilePolicy()),
            events,
            runs,
        ),
    )
    run = runtime.create_run("fail safely")

    failed = await runtime.execute(run.run_id)

    assert failed.status is RunStatus.FAILED
    model_failed = [
        event
        for event in events.list_for_run(run.run_id)
        if event.event_type is EventType.MODEL_FAILED
    ]
    assert len(model_failed) == 1
    assert model_failed[0].payload == {
        "step_number": 1,
        "error_type": "MODEL_AUTH_ERROR",
    }
    database.close()


@pytest.mark.asyncio
async def test_context_compaction_is_persisted_and_audited(tmp_path: Path) -> None:
    database = Database.from_path(tmp_path / "context-compaction.sqlite3")
    database.create_schema()
    runs = RunRepository(database)
    events = EventRepository(database)
    checkpoints = CheckpointRepository(database)
    provider = ResponseProvider(
        [
            response(
                ToolCall(
                    type="tool_call",
                    call_id="call_first",
                    tool="echo",
                    arguments={"text": "first"},
                )
            ),
            response(
                ToolCall(
                    type="tool_call",
                    call_id="call_second",
                    tool="echo",
                    arguments={"text": "second"},
                )
            ),
            response(FinalAnswer(type="final", answer="done")),
        ]
    )
    model_workflow = ModelWorkflow(database)
    runtime = AgentRuntime(
        run_repository=runs,
        event_repository=events,
        checkpoint_repository=checkpoints,
        model_provider=provider,
        model_executor=ModelExecutor(provider, model_workflow),
        model_workflow=model_workflow,
        model_budget=ModelBudget(max_model_requests=4),
        context_builder=ContextBuilder(ContextPolicy(max_items=2)),
        tool_executor=ToolExecutor(
            ToolRegistry([EchoTool()]),
            PolicyEngine(WorkspacePathResolver(tmp_path), SensitiveFilePolicy()),
            events,
            runs,
        ),
    )
    run = runtime.create_run("compact old evidence", max_steps=3)

    completed = await runtime.execute(run.run_id)

    assert completed.status is RunStatus.COMPLETED
    compacted = [
        event
        for event in events.list_for_run(run.run_id)
        if event.event_type is EventType.CONTEXT_COMPACTED
    ]
    assert len(compacted) == 1
    assert compacted[0].payload["removed_pair_count"] == 1
    checkpoint = checkpoints.latest(run.run_id)
    assert checkpoint is not None
    assert len(checkpoint.runtime_state["context_items"]) == 2
    assert checkpoint.runtime_state["context_items"][0]["call_id"] == "call_second"
    database.close()


@pytest.mark.asyncio
async def test_runtime_normalizes_multi_read_response_without_executing_discarded_calls(
    tmp_path: Path,
) -> None:
    database = Database.from_path(tmp_path / "multi-read-runtime.sqlite3")
    database.create_schema()
    runs = RunRepository(database)
    events = EventRepository(database)
    checkpoints = CheckpointRepository(database)
    calls: list[str] = []
    provider = ResponseProvider(
        [
            response(
                ToolCall(
                    type="tool_call",
                    call_id="call_selected",
                    tool="selected_read",
                    arguments={"value": "selected-sensitive-argument"},
                ),
                multi_tool_response=normalized_multi_tool_info(),
            ),
            response(FinalAnswer(type="final", answer="done")),
        ]
    )
    model_workflow = ModelWorkflow(database)
    runtime = AgentRuntime(
        run_repository=runs,
        event_repository=events,
        checkpoint_repository=checkpoints,
        model_provider=provider,
        model_executor=ModelExecutor(provider, model_workflow),
        model_workflow=model_workflow,
        model_budget=ModelBudget(max_model_requests=3),
        tool_executor=ToolExecutor(
            ToolRegistry(
                [
                    NamedReadTool("selected_read", calls),
                    NamedReadTool("discarded_read", calls),
                    NamedReadTool("third_read", calls),
                ]
            ),
            PolicyEngine(WorkspacePathResolver(tmp_path), SensitiveFilePolicy()),
            events,
            runs,
        ),
    )
    run = runtime.create_run("normalize multi read", max_steps=3, max_tool_calls=3)

    completed = await runtime.execute(run.run_id)

    assert completed.status is RunStatus.COMPLETED
    assert calls == ["selected_read"]
    assert completed.tool_call_count == 1
    event_types = [event.event_type for event in events.list_for_run(run.run_id)]
    assert event_types.count(EventType.TOOL_REQUESTED) == 1
    assert event_types.count(EventType.TOOL_STARTED) == 1
    assert event_types.count(EventType.TOOL_COMPLETED) == 1
    assert EventType.MODEL_PROVIDER_DEVIATION in event_types
    assert EventType.MULTI_TOOL_RESPONSE_NORMALIZED in event_types
    deviation_events = [
        event
        for event in events.list_for_run(run.run_id)
        if event.event_type
        in {
            EventType.MODEL_PROVIDER_DEVIATION,
            EventType.MULTI_TOOL_RESPONSE_NORMALIZED,
        }
    ]
    serialized_events = str([event.payload for event in deviation_events])
    assert "selected-sensitive-argument" not in serialized_events
    assert "tool_result" not in serialized_events
    checkpoint = checkpoints.latest(run.run_id)
    assert checkpoint is not None
    serialized_checkpoint = str(checkpoint.runtime_state)
    assert "call_selected" in serialized_checkpoint
    assert "discarded_read" not in serialized_checkpoint
    assert "third_read" not in serialized_checkpoint
    assert "call_discarded" not in serialized_checkpoint
    assert "discarded-sensitive-arguments" not in serialized_checkpoint
    assert len(provider.requests) == 2
    normalization_items = [
        item
        for item in provider.requests[1].history
        if isinstance(item, dict) and item.get("kind") == "MULTI_TOOL_NORMALIZATION"
    ]
    assert len(normalization_items) == 1
    payload = normalization_items[0]["payload"]
    assert "not executed" in str(payload)
    assert "discarded-sensitive-arguments" not in str(payload)
    model_responded = next(
        event
        for event in events.list_for_run(run.run_id)
        if event.event_type is EventType.MODEL_RESPONDED
    )
    assert model_responded.payload["provider_metadata"][
        "discarded_function_call_count"
    ] == 2
    database.close()


@pytest.mark.asyncio
async def test_runtime_strict_deviation_executes_nothing_and_creates_no_approval(
    tmp_path: Path,
) -> None:
    database = Database.from_path(tmp_path / "strict-runtime.sqlite3")
    database.create_schema()
    runs = RunRepository(database)
    events = EventRepository(database)
    approvals = ApprovalRepository(database)
    calls: list[str] = []
    info = MultiToolResponseInfo(
        provider="deviation-provider",
        model="test-model",
        provider_request_id="resp_strict",
        returned_call_count=2,
        selected_call_count=0,
        discarded_call_count=2,
        selected_tool_name=None,
        discarded_tool_names=["selected_read", "approval_read"],
        policy=MultiToolResponsePolicy.STRICT,
        reason="approval_required_tool",
    )
    provider = DeviationProvider(info)
    model_workflow = ModelWorkflow(database)
    runtime = AgentRuntime(
        run_repository=runs,
        event_repository=events,
        checkpoint_repository=CheckpointRepository(database),
        model_provider=provider,
        model_executor=ModelExecutor(provider, model_workflow),
        model_workflow=model_workflow,
        model_budget=ModelBudget(max_model_requests=2, max_retries=1),
        tool_executor=ToolExecutor(
            ToolRegistry(
                [
                    NamedReadTool("selected_read", calls),
                    NamedReadTool("approval_read", calls, requires_approval=True),
                ]
            ),
            PolicyEngine(WorkspacePathResolver(tmp_path), SensitiveFilePolicy()),
            events,
            runs,
        ),
        approval_repository=approvals,
        approval_workflow=ApprovalWorkflow(database),
    )
    run = runtime.create_run("strict response", max_steps=2, max_tool_calls=2)

    failed = await runtime.execute(run.run_id)

    assert failed.status is RunStatus.FAILED
    assert failed.tool_call_count == 0
    assert calls == []
    assert approvals.list_for_run(run.run_id) == []
    recorded = events.list_for_run(run.run_id)
    assert sum(
        event.event_type is EventType.MODEL_PROVIDER_DEVIATION for event in recorded
    ) == 2
    assert not {
        EventType.TOOL_REQUESTED,
        EventType.TOOL_STARTED,
        EventType.APPROVAL_REQUESTED,
    }.intersection(event.event_type for event in recorded)
    serialized = str([event.payload for event in recorded])
    assert "private-argument" not in serialized
    database.close()


@pytest.mark.asyncio
async def test_discarded_call_is_not_replayed_after_approval_restart(
    tmp_path: Path,
) -> None:
    path = tmp_path / "multi-restart.sqlite3"
    calls: list[str] = []
    database = Database.from_path(path)
    database.create_schema()
    runs = RunRepository(database)
    events = EventRepository(database)
    approvals = ApprovalRepository(database)
    checkpoints = CheckpointRepository(database)
    provider = ResponseProvider(
        [
            response(
                ToolCall(
                    type="tool_call",
                    call_id="call_selected",
                    tool="selected_read",
                    arguments={"value": "selected"},
                ),
                multi_tool_response=normalized_multi_tool_info(),
            ),
            response(
                ToolCall(
                    type="tool_call",
                    call_id="call_approval",
                    tool="approval_read",
                    arguments={"value": "approved"},
                )
            ),
        ]
    )
    model_workflow = ModelWorkflow(database)
    registry = ToolRegistry(
        [
            NamedReadTool("selected_read", calls),
            NamedReadTool("discarded_read", calls),
            NamedReadTool("third_read", calls),
            NamedReadTool("approval_read", calls, requires_approval=True),
        ]
    )
    runtime = AgentRuntime(
        run_repository=runs,
        event_repository=events,
        checkpoint_repository=checkpoints,
        model_provider=provider,
        model_executor=ModelExecutor(provider, model_workflow),
        model_workflow=model_workflow,
        model_budget=ModelBudget(max_model_requests=4),
        tool_executor=ToolExecutor(
            registry,
            PolicyEngine(WorkspacePathResolver(tmp_path), SensitiveFilePolicy()),
            events,
            runs,
        ),
        approval_repository=approvals,
        approval_workflow=ApprovalWorkflow(database),
    )
    run = runtime.create_run("restart after normalization", max_steps=4, max_tool_calls=4)

    waiting = await runtime.execute(run.run_id)

    assert waiting.status is RunStatus.WAITING_APPROVAL
    assert calls == ["selected_read"]
    pending = approvals.list_pending(run.run_id)
    assert len(pending) == 1
    checkpoint = checkpoints.get(pending[0].checkpoint_id)
    assert checkpoint is not None
    serialized_checkpoint = str(checkpoint.runtime_state)
    assert "call_selected" in serialized_checkpoint
    assert "call_approval" in serialized_checkpoint
    assert "call_discarded" not in serialized_checkpoint
    assert "discarded-secret" not in serialized_checkpoint
    database.close()

    restored_database = Database.from_path(path)
    restored_database.create_schema()
    restored_runs = RunRepository(restored_database)
    restored_events = EventRepository(restored_database)
    restored_approvals = ApprovalRepository(restored_database)
    restored_provider = ResponseProvider(
        [response(FinalAnswer(type="final", answer="restored"))]
    )
    restored_workflow = ModelWorkflow(restored_database)
    restored_runtime = AgentRuntime(
        run_repository=restored_runs,
        event_repository=restored_events,
        checkpoint_repository=CheckpointRepository(restored_database),
        model_provider=restored_provider,
        model_executor=ModelExecutor(restored_provider, restored_workflow),
        model_workflow=restored_workflow,
        tool_executor=ToolExecutor(
            ToolRegistry(
                [
                    NamedReadTool("selected_read", calls),
                    NamedReadTool("discarded_read", calls),
                    NamedReadTool("third_read", calls),
                    NamedReadTool("approval_read", calls, requires_approval=True),
                ]
            ),
            PolicyEngine(WorkspacePathResolver(tmp_path), SensitiveFilePolicy()),
            restored_events,
            restored_runs,
        ),
        approval_repository=restored_approvals,
        approval_workflow=ApprovalWorkflow(restored_database),
    )
    restored_runtime.approve(pending[0].approval_id)

    completed = await restored_runtime.resume(run.run_id)

    assert completed.status is RunStatus.COMPLETED
    assert calls == ["selected_read", "approval_read"]
    assert completed.tool_call_count == 2
    assert len(restored_provider.requests) == 1
    normalization_items = [
        item
        for item in restored_provider.requests[0].history
        if isinstance(item, dict) and item.get("kind") == "MULTI_TOOL_NORMALIZATION"
    ]
    assert len(normalization_items) == 1
    restored_database.close()
