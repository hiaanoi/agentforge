from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError

from agentforge.context.models import ContextItem, ContextItemKind, LoopState, ResumeContextState
from agentforge.domain.enums import ProcessExecutionStatus, ResumePhase
from agentforge.domain.models import PendingToolCall, RuntimeSnapshot
from agentforge.domain.repair import RepairCompletionStatus
from agentforge.domain.test_execution import PendingTestExecution, TestResult
from agentforge.models.domain import ModelUsage
from agentforge.repair_engines.mini_native.contracts import RepairAction, RepairActionResult


class SnapshotVersionError(ValueError):
    """Raised when checkpoint data has an unsupported snapshot version."""


class RuntimeSnapshotV2(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[2] = 2
    run_id: UUID
    step_number: int = Field(ge=0)
    history: list[JsonValue] = Field(default_factory=list)
    context_items: list[ContextItem] = Field(default_factory=list)
    pending_tool_call: PendingToolCall | None = None
    pending_approval_id: UUID | None = None
    tool_call_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    resume_phase: ResumePhase
    model_usage: ModelUsage = Field(default_factory=ModelUsage)
    model_request_count: int = Field(default=0, ge=0)
    loop_state: LoopState = Field(default_factory=LoopState)
    context_state: ResumeContextState = Field(default_factory=ResumeContextState)
    last_provider_metadata: dict[str, JsonValue] = Field(default_factory=dict)
    last_model_error: str | None = None
    context_policy_version: str = "1"
    system_prompt_version: str = "1"


class RuntimeSnapshotV3(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[3] = 3
    run_id: UUID
    step_number: int = Field(ge=0)
    history: list[JsonValue] = Field(default_factory=list)
    context_items: list[ContextItem] = Field(default_factory=list)
    pending_tool_call: PendingToolCall | None = None
    pending_approval_id: UUID | None = None
    tool_call_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    resume_phase: ResumePhase
    model_usage: ModelUsage = Field(default_factory=ModelUsage)
    model_request_count: int = Field(default=0, ge=0)
    loop_state: LoopState = Field(default_factory=LoopState)
    context_state: ResumeContextState = Field(default_factory=ResumeContextState)
    last_provider_metadata: dict[str, JsonValue] = Field(default_factory=dict)
    last_model_error: str | None = None
    context_policy_version: str = "1"
    system_prompt_version: str = "1"
    pending_test_execution: PendingTestExecution | None = None
    last_test_result: TestResult | None = None
    test_execution_state: ProcessExecutionStatus | None = None


class RepairSnapshotState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(min_length=1)
    policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: RepairCompletionStatus
    state_version: int = Field(gt=0)
    model_calls_used: int = Field(ge=0)
    read_calls_used: int = Field(ge=0)
    edit_attempts_used: int = Field(ge=0)
    test_runs_used: int = Field(ge=0)
    completion_corrections_used: int = Field(ge=0)
    policy_violations: int = Field(ge=0)
    baseline_id: UUID
    baseline_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    last_mutation_execution_id: UUID | None = None
    last_development_test_execution_id: UUID | None = None
    latest_source_verified: bool
    pending_final_verification: bool
    final_verification_execution_id: UUID | None = None
    final_workspace_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    final_diff_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class RuntimeSnapshotV4(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[4] = 4
    run_id: UUID
    step_number: int = Field(ge=0)
    history: list[JsonValue] = Field(default_factory=list)
    context_items: list[ContextItem] = Field(default_factory=list)
    pending_tool_call: PendingToolCall | None = None
    pending_approval_id: UUID | None = None
    tool_call_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    resume_phase: ResumePhase
    model_usage: ModelUsage = Field(default_factory=ModelUsage)
    model_request_count: int = Field(default=0, ge=0)
    loop_state: LoopState = Field(default_factory=LoopState)
    context_state: ResumeContextState = Field(default_factory=ResumeContextState)
    last_provider_metadata: dict[str, JsonValue] = Field(default_factory=dict)
    last_model_error: str | None = None
    context_policy_version: str = "1"
    system_prompt_version: str = "1"
    pending_test_execution: PendingTestExecution | None = None
    last_test_result: TestResult | None = None
    test_execution_state: ProcessExecutionStatus | None = None
    repair: RepairSnapshotState | None = None


class _RuntimeSnapshotV4MiniNative(RuntimeSnapshotV4):
    """The short-lived V4 shape emitted by mini-native before the V5 migration."""

    mini_native_pending_action: RepairAction | None = None
    mini_native_last_test_passed: bool = False
    provider_usage_available: bool = False


class RuntimeSnapshotV5(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[5] = 5
    run_id: UUID
    step_number: int = Field(ge=0)
    history: list[JsonValue] = Field(default_factory=list)
    context_items: list[ContextItem] = Field(default_factory=list)
    pending_tool_call: PendingToolCall | None = None
    pending_approval_id: UUID | None = None
    tool_call_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    resume_phase: ResumePhase
    model_usage: ModelUsage = Field(default_factory=ModelUsage)
    model_request_count: int = Field(default=0, ge=0)
    loop_state: LoopState = Field(default_factory=LoopState)
    context_state: ResumeContextState = Field(default_factory=ResumeContextState)
    last_provider_metadata: dict[str, JsonValue] = Field(default_factory=dict)
    last_model_error: str | None = None
    context_policy_version: str = "1"
    system_prompt_version: str = "1"
    pending_test_execution: PendingTestExecution | None = None
    last_test_result: TestResult | None = None
    test_execution_state: ProcessExecutionStatus | None = None
    repair: RepairSnapshotState | None = None
    mini_native_pending_action: RepairAction | None = None
    mini_native_pending_action_result: RepairActionResult | None = None
    mini_native_last_test_passed: bool = False
    mini_native_source_digest_before: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    provider_usage_available: bool = False


def load_runtime_snapshot(
    state: dict[str, JsonValue],
    *,
    run_id: UUID,
    step_number: int,
) -> RuntimeSnapshotV5:
    raw_version = state.get("schema_version")
    if raw_version is None:
        history = state.get("history", [])
        if not isinstance(history, list):
            raise SnapshotVersionError("Unversioned checkpoint history must be a list")
        return _upgrade_v3(
            _upgrade_v2(
                RuntimeSnapshotV2(
                    run_id=run_id,
                    step_number=step_number,
                    history=list(history),
                    context_items=[
                        ContextItem(kind=ContextItemKind.LEGACY, payload=item) for item in history
                    ],
                    resume_phase=ResumePhase.READY_FOR_MODEL,
                )
            )
        )
    if raw_version == 1:
        legacy = RuntimeSnapshot.model_validate(state)
        return _upgrade_v3(
            _upgrade_v2(
                RuntimeSnapshotV2(
                    run_id=legacy.run_id,
                    step_number=legacy.step_number,
                    history=list(legacy.history),
                    context_items=[
                        ContextItem(kind=ContextItemKind.LEGACY, payload=item)
                        for item in legacy.history
                    ],
                    pending_tool_call=legacy.pending_tool_call,
                    pending_approval_id=legacy.pending_approval_id,
                    tool_call_digest=legacy.tool_call_digest,
                    resume_phase=legacy.resume_phase,
                )
            )
        )
    if raw_version == 2:
        snapshot = RuntimeSnapshotV2.model_validate(state)
        if snapshot.run_id != run_id or snapshot.step_number != step_number:
            raise SnapshotVersionError("Snapshot identity does not match checkpoint")
        return _upgrade_v3(_upgrade_v2(snapshot))
    if raw_version == 3:
        snapshot_v3 = RuntimeSnapshotV3.model_validate(state)
        if snapshot_v3.run_id != run_id or snapshot_v3.step_number != step_number:
            raise SnapshotVersionError("Snapshot identity does not match checkpoint")
        return _upgrade_v3(snapshot_v3)
    if raw_version == 4:
        try:
            snapshot_v4 = RuntimeSnapshotV4.model_validate(state)
        except ValidationError:
            snapshot_v4 = _RuntimeSnapshotV4MiniNative.model_validate(state)
        if snapshot_v4.run_id != run_id or snapshot_v4.step_number != step_number:
            raise SnapshotVersionError("Snapshot identity does not match checkpoint")
        return _upgrade_v4(snapshot_v4)
    if raw_version == 5:
        snapshot_v5 = RuntimeSnapshotV5.model_validate(state)
        if snapshot_v5.run_id != run_id or snapshot_v5.step_number != step_number:
            raise SnapshotVersionError("Snapshot identity does not match checkpoint")
        return snapshot_v5
    raise SnapshotVersionError(f"Unsupported RuntimeSnapshot version {raw_version!r}")


def _upgrade_v2(snapshot: RuntimeSnapshotV2) -> RuntimeSnapshotV3:
    return RuntimeSnapshotV3(
        run_id=snapshot.run_id,
        step_number=snapshot.step_number,
        history=list(snapshot.history),
        context_items=list(snapshot.context_items),
        pending_tool_call=snapshot.pending_tool_call,
        pending_approval_id=snapshot.pending_approval_id,
        tool_call_digest=snapshot.tool_call_digest,
        resume_phase=snapshot.resume_phase,
        model_usage=snapshot.model_usage,
        model_request_count=snapshot.model_request_count,
        loop_state=snapshot.loop_state,
        context_state=snapshot.context_state,
        last_provider_metadata=dict(snapshot.last_provider_metadata),
        last_model_error=snapshot.last_model_error,
        context_policy_version=snapshot.context_policy_version,
        system_prompt_version=snapshot.system_prompt_version,
    )


def _upgrade_v3(snapshot: RuntimeSnapshotV3) -> RuntimeSnapshotV5:
    return RuntimeSnapshotV5(
        run_id=snapshot.run_id,
        step_number=snapshot.step_number,
        history=list(snapshot.history),
        context_items=list(snapshot.context_items),
        pending_tool_call=snapshot.pending_tool_call,
        pending_approval_id=snapshot.pending_approval_id,
        tool_call_digest=snapshot.tool_call_digest,
        resume_phase=snapshot.resume_phase,
        model_usage=snapshot.model_usage,
        model_request_count=snapshot.model_request_count,
        loop_state=snapshot.loop_state,
        context_state=snapshot.context_state,
        last_provider_metadata=dict(snapshot.last_provider_metadata),
        last_model_error=snapshot.last_model_error,
        context_policy_version=snapshot.context_policy_version,
        system_prompt_version=snapshot.system_prompt_version,
        pending_test_execution=snapshot.pending_test_execution,
        last_test_result=snapshot.last_test_result,
        test_execution_state=snapshot.test_execution_state,
    )


def _upgrade_v4(
    snapshot: RuntimeSnapshotV4 | _RuntimeSnapshotV4MiniNative,
) -> RuntimeSnapshotV5:
    return RuntimeSnapshotV5(
        run_id=snapshot.run_id,
        step_number=snapshot.step_number,
        history=list(snapshot.history),
        context_items=list(snapshot.context_items),
        pending_tool_call=snapshot.pending_tool_call,
        pending_approval_id=snapshot.pending_approval_id,
        tool_call_digest=snapshot.tool_call_digest,
        resume_phase=snapshot.resume_phase,
        model_usage=snapshot.model_usage,
        model_request_count=snapshot.model_request_count,
        loop_state=snapshot.loop_state,
        context_state=snapshot.context_state,
        last_provider_metadata=dict(snapshot.last_provider_metadata),
        last_model_error=snapshot.last_model_error,
        context_policy_version=snapshot.context_policy_version,
        system_prompt_version=snapshot.system_prompt_version,
        pending_test_execution=snapshot.pending_test_execution,
        last_test_result=snapshot.last_test_result,
        test_execution_state=snapshot.test_execution_state,
        repair=snapshot.repair,
        mini_native_pending_action=getattr(snapshot, "mini_native_pending_action", None),
        mini_native_last_test_passed=getattr(snapshot, "mini_native_last_test_passed", False),
        provider_usage_available=getattr(snapshot, "provider_usage_available", False),
    )
