from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class RunRow(Base):
    __tablename__ = "runs"

    run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), index=True)
    current_step: Mapped[int] = mapped_column(Integer)
    max_steps: Mapped[int] = mapped_column(Integer)
    tool_call_count: Mapped[int] = mapped_column(Integer)
    max_tool_calls: Mapped[int] = mapped_column(Integer)
    model_provider: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    total_token_usage: Mapped[int] = mapped_column(Integer)
    estimated_cost: Mapped[float] = mapped_column(Float)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    final_output: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_event_sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    event_sequence_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class EventRow(Base):
    __tablename__ = "events"
    __table_args__ = (
        UniqueConstraint("run_id", "sequence_number"),
        CheckConstraint("schema_version = 1", name="ck_event_schema_version_one"),
        CheckConstraint(
            "scope_type IN ('RUN', 'CONVERSATION', 'WORKSPACE')",
            name="ck_event_scope_type_closed",
        ),
        CheckConstraint("length(scope_id) > 0", name="ck_event_scope_id_nonempty"),
        CheckConstraint("length(event_type) > 0", name="ck_event_type_nonempty"),
        CheckConstraint(
            "(scope_type = 'RUN' AND run_id IS NOT NULL "
            "AND sequence_number IS NOT NULL AND sequence_number > 0 "
            "AND scope_id = run_id) OR "
            "(scope_type IN ('CONVERSATION', 'WORKSPACE') "
            "AND run_id IS NULL AND sequence_number IS NULL)",
            name="ck_event_scope_topology",
        ),
        {"sqlite_autoincrement": True},
    )

    global_cursor: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    event_id: Mapped[str] = mapped_column(String(36), unique=True, nullable=False)
    scope_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    scope_id: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    run_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("runs.run_id", ondelete="CASCADE"),
        index=True,
        nullable=True,
    )
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    sequence_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class CheckpointRow(Base):
    __tablename__ = "checkpoints"

    checkpoint_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("runs.run_id", ondelete="CASCADE"), index=True
    )
    step_number: Mapped[int] = mapped_column(Integer, index=True)
    runtime_state: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ApprovalRequestRow(Base):
    __tablename__ = "approval_requests"
    __table_args__ = (
        UniqueConstraint("run_id", "checkpoint_id"),
        Index("ix_approval_status_requested", "status", "requested_at"),
    )

    approval_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("runs.run_id", ondelete="CASCADE"), index=True
    )
    checkpoint_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("checkpoints.checkpoint_id", ondelete="CASCADE")
    )
    tool_name: Mapped[str] = mapped_column(String(100))
    sanitized_arguments: Mapped[dict[str, Any]] = mapped_column(JSON)
    request_digest: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32))
    rejection_strategy: Mapped[str] = mapped_column(String(32))
    consumption_state: Mapped[str] = mapped_column(String(32))
    decision_note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    result_status: Mapped[str | None] = mapped_column(String(100), nullable=True)
    result_summary: Mapped[str | None] = mapped_column(String(500), nullable=True)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MutationApprovalBindingRow(Base):
    __tablename__ = "mutation_approval_bindings"

    approval_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("approval_requests.approval_id", ondelete="CASCADE"),
        primary_key=True,
    )
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("runs.run_id", ondelete="CASCADE"), index=True
    )
    checkpoint_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("checkpoints.checkpoint_id", ondelete="CASCADE")
    )
    tool_call_digest: Mapped[str] = mapped_column(String(64), unique=True)
    tool_name: Mapped[str] = mapped_column(String(100))
    target_path: Mapped[str] = mapped_column(Text)
    target_existed: Mapped[bool] = mapped_column(Boolean)
    before_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    expected_after_sha256: Mapped[str] = mapped_column(String(64))
    bytes_written: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class MutationExecutionRow(Base):
    __tablename__ = "mutation_executions"
    __table_args__ = (
        UniqueConstraint("approval_id"),
        UniqueConstraint("tool_call_digest"),
        Index("ix_mutation_run_created", "run_id", "created_at"),
        CheckConstraint(
            "length(before_workspace_digest) = 64",
            name="ck_mutation_before_workspace_digest_length",
        ),
        CheckConstraint(
            "length(expected_after_workspace_digest) = 64",
            name="ck_mutation_after_workspace_digest_length",
        ),
    )

    execution_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("runs.run_id", ondelete="CASCADE"), index=True
    )
    approval_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("approval_requests.approval_id", ondelete="CASCADE")
    )
    tool_call_digest: Mapped[str] = mapped_column(String(64))
    tool_name: Mapped[str] = mapped_column(String(100))
    target_path: Mapped[str] = mapped_column(Text)
    before_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    expected_after_sha256: Mapped[str] = mapped_column(String(64))
    before_workspace_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    expected_after_workspace_digest: Mapped[str] = mapped_column(
        String(64), nullable=False
    )
    actual_after_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    bytes_written: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), index=True)
    result_summary: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class TestApprovalBindingRow(Base):
    __tablename__ = "test_approval_bindings"
    __table_args__ = (
        CheckConstraint(
            "source_revision_number >= 0",
            name="ck_test_binding_source_revision_nonnegative",
        ),
        CheckConstraint(
            "length(source_revision_digest) = 64",
            name="ck_test_binding_source_digest_length",
        ),
    )

    approval_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("approval_requests.approval_id", ondelete="CASCADE"),
        primary_key=True,
    )
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("runs.run_id", ondelete="CASCADE"), index=True
    )
    checkpoint_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("checkpoints.checkpoint_id", ondelete="CASCADE")
    )
    tool_call_digest: Mapped[str] = mapped_column(String(64), unique=True)
    profile_id: Mapped[str] = mapped_column(String(100))
    profile_version: Mapped[int] = mapped_column(Integer)
    profile_digest: Mapped[str] = mapped_column(String(64))
    executable_path: Mapped[str] = mapped_column(Text)
    argv_digest: Mapped[str] = mapped_column(String(64))
    cwd: Mapped[str] = mapped_column(Text)
    environment_digest: Mapped[str] = mapped_column(String(64))
    source_revision_number: Mapped[int] = mapped_column(Integer, default=0)
    source_revision_digest: Mapped[str] = mapped_column(String(64), default="0" * 64)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ProcessExecutionRow(Base):
    __tablename__ = "process_executions"
    __table_args__ = (
        UniqueConstraint("approval_id"),
        UniqueConstraint("tool_call_digest"),
        UniqueConstraint("run_id", "attempt_number"),
        Index("ix_process_execution_run_created", "run_id", "created_at"),
        CheckConstraint(
            "source_revision_number >= 0",
            name="ck_process_execution_source_revision_nonnegative",
        ),
        CheckConstraint(
            "length(source_revision_digest) = 64",
            name="ck_process_execution_source_digest_length",
        ),
        CheckConstraint(
            "source_digest_at_start IS NULL OR length(source_digest_at_start) = 64",
            name="ck_process_execution_start_digest_length",
        ),
        CheckConstraint(
            "(verification_capsule_id IS NULL AND capsule_state IS NULL "
            "AND source_snapshot_digest IS NULL AND verifier_artifact_digest IS NULL "
            "AND executable_artifact_digest IS NULL AND artifact_algorithm_version IS NULL "
            "AND runtime_trust_class IS NULL) OR "
            "(length(verification_capsule_id) = 36 AND capsule_state = 'STAGING' "
            "AND source_snapshot_digest IS NULL AND verifier_artifact_digest IS NULL "
            "AND executable_artifact_digest IS NULL AND artifact_algorithm_version IS NULL "
            "AND runtime_trust_class IS NULL) OR "
            "(length(verification_capsule_id) = 36 AND capsule_state = 'SEALED' "
            "AND length(source_snapshot_digest) = 64 "
            "AND length(verifier_artifact_digest) = 64 "
            "AND length(executable_artifact_digest) = 64 "
            "AND artifact_algorithm_version >= 1 "
            "AND runtime_trust_class = 'NON_HERMETIC')",
            name="ck_process_execution_capsule_topology",
        ),
    )

    execution_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("runs.run_id", ondelete="CASCADE"), index=True
    )
    approval_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("approval_requests.approval_id", ondelete="CASCADE")
    )
    tool_call_digest: Mapped[str] = mapped_column(String(64))
    attempt_number: Mapped[int] = mapped_column(Integer)
    record_version: Mapped[int] = mapped_column(Integer)
    result_schema_version: Mapped[int] = mapped_column(Integer)
    profile_id: Mapped[str] = mapped_column(String(100))
    profile_version: Mapped[int] = mapped_column(Integer)
    profile_digest: Mapped[str] = mapped_column(String(64))
    executable_path: Mapped[str] = mapped_column(Text)
    argv_digest: Mapped[str] = mapped_column(String(64))
    cwd: Mapped[str] = mapped_column(Text)
    environment_digest: Mapped[str] = mapped_column(String(64))
    source_revision_number: Mapped[int] = mapped_column(Integer, default=0)
    source_revision_digest: Mapped[str] = mapped_column(String(64), default="0" * 64)
    source_digest_at_start: Mapped[str | None] = mapped_column(String(64), nullable=True)
    verification_capsule_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    capsule_state: Mapped[str | None] = mapped_column(String(16), nullable=True)
    source_snapshot_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    verifier_artifact_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    executable_artifact_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    artifact_algorithm_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    runtime_trust_class: Mapped[str | None] = mapped_column(String(32), nullable=True)
    root_pid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    process_group_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    job_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    failure_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stdout_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    stderr_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    stdout_size: Mapped[int] = mapped_column(Integer)
    stderr_size: Mapped[int] = mapped_column(Integer)
    stdout_summary: Mapped[str] = mapped_column(Text)
    stderr_summary: Mapped[str] = mapped_column(Text)
    stdout_truncated: Mapped[bool] = mapped_column(Boolean)
    stderr_truncated: Mapped[bool] = mapped_column(Boolean)
    duration_ms: Mapped[int] = mapped_column(Integer)
    termination_reason: Mapped[str | None] = mapped_column(String(100), nullable=True)
    termination_result: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ModelRuntimeStateRow(Base):
    __tablename__ = "model_runtime_states"

    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("runs.run_id", ondelete="CASCADE"), primary_key=True
    )
    model_request_count: Mapped[int] = mapped_column(Integer)
    input_tokens: Mapped[int] = mapped_column(Integer)
    output_tokens: Mapped[int] = mapped_column(Integer)
    total_tokens: Mapped[int] = mapped_column(Integer)
    cached_input_tokens: Mapped[int] = mapped_column(Integer)
    reasoning_tokens: Mapped[int] = mapped_column(Integer)
    max_model_requests: Mapped[int] = mapped_column(Integer)
    max_retries: Mapped[int] = mapped_column(Integer)
    max_output_tokens_per_request: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_total_input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_total_output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_total_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ModelAttemptRow(Base):
    __tablename__ = "model_attempts"
    __table_args__ = (
        UniqueConstraint("run_id", "logical_call_id", "attempt_number"),
        CheckConstraint(
            "status IN ('PREPARED', 'DISPATCHING', 'COMPLETED', 'FAILED', "
            "'INDETERMINATE')",
            name="ck_model_attempt_status_closed",
        ),
        CheckConstraint(
            "length(request_digest) = 64 AND length(budget_digest) = 64",
            name="ck_model_attempt_digest_lengths",
        ),
        CheckConstraint(
            "length(provider_identity) > 0 AND length(provider_identity) <= 200",
            name="ck_model_attempt_provider_identity",
        ),
        CheckConstraint(
            "(status = 'PREPARED' AND dispatched_at IS NULL AND completed_at IS NULL) OR "
            "(status = 'DISPATCHING' AND dispatched_at IS NOT NULL "
            "AND completed_at IS NULL) OR "
            "(status IN ('COMPLETED', 'FAILED', 'INDETERMINATE') "
            "AND dispatched_at IS NOT NULL AND completed_at IS NOT NULL)",
            name="ck_model_attempt_time_topology",
        ),
        CheckConstraint(
            "(status IN ('PREPARED', 'DISPATCHING', 'INDETERMINATE') "
            "AND error_type IS NULL AND retryable IS NULL "
            "AND duration_ms IS NULL AND usage IS NULL) OR "
            "(status = 'COMPLETED' AND error_type IS NULL AND retryable IS NULL "
            "AND duration_ms IS NOT NULL) OR "
            "(status = 'FAILED' AND error_type IS NOT NULL "
            "AND retryable IS NOT NULL AND usage IS NULL)",
            name="ck_model_attempt_terminal_facts",
        ),
    )

    attempt_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("runs.run_id", ondelete="CASCADE"), index=True
    )
    logical_call_id: Mapped[str] = mapped_column(String(36), index=True)
    attempt_number: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32))
    request_digest: Mapped[str] = mapped_column(String(64))
    provider_identity: Mapped[str] = mapped_column(String(200))
    budget_digest: Mapped[str] = mapped_column(String(64))
    error_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    retryable: Mapped[bool | None]
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    usage: Mapped[dict[str, Any] | None] = mapped_column(
        JSON(none_as_null=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    dispatched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RepairTaskPolicyRow(Base):
    __tablename__ = "repair_task_policies"

    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("runs.run_id", ondelete="CASCADE"), primary_key=True
    )
    task_id: Mapped[str] = mapped_column(String(200), index=True)
    policy_version: Mapped[int] = mapped_column(Integer)
    policy_digest: Mapped[str] = mapped_column(String(64), index=True)
    policy_data: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class RepairStateRow(Base):
    __tablename__ = "repair_states"

    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("runs.run_id", ondelete="CASCADE"), primary_key=True
    )
    task_id: Mapped[str] = mapped_column(String(200), index=True)
    policy_digest: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(40), index=True)
    model_calls_used: Mapped[int] = mapped_column(Integer)
    read_calls_used: Mapped[int] = mapped_column(Integer)
    edit_attempts_used: Mapped[int] = mapped_column(Integer)
    test_runs_used: Mapped[int] = mapped_column(Integer)
    completion_corrections_used: Mapped[int] = mapped_column(Integer)
    policy_violations: Mapped[int] = mapped_column(Integer)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    baseline_id: Mapped[str] = mapped_column(String(36))
    baseline_digest: Mapped[str] = mapped_column(String(64))
    last_mutation_execution_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    last_mutation_committed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_development_test_execution_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True
    )
    last_development_test_success: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    last_development_test_completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    final_verification_execution_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True
    )
    final_verification_success: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    final_verification_completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_diff_validation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    final_workspace_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    final_diff_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    latest_source_verified: Mapped[bool] = mapped_column(Boolean)
    pending_final_verification: Mapped[bool] = mapped_column(Boolean)
    final_answer_received: Mapped[bool] = mapped_column(Boolean)
    failure_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    state_version: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class RepairBudgetConsumptionRow(Base):
    __tablename__ = "repair_budget_consumptions"
    __table_args__ = (UniqueConstraint("run_id", "budget_kind", "fact_id"),)

    consumption_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("repair_states.run_id", ondelete="CASCADE"), index=True
    )
    budget_kind: Mapped[str] = mapped_column(String(40))
    fact_id: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class WorkspaceBaselineRow(Base):
    __tablename__ = "workspace_baselines"

    baseline_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(200), index=True)
    workspace_root: Mapped[str] = mapped_column(Text)
    root_digest: Mapped[str] = mapped_column(String(64), index=True)
    manifest_version: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class WorkspaceBaselineFileRow(Base):
    __tablename__ = "workspace_baseline_files"
    __table_args__ = (UniqueConstraint("baseline_id", "relative_path"),)

    file_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    baseline_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("workspace_baselines.baseline_id", ondelete="CASCADE"),
        index=True,
    )
    relative_path: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(Integer)
    file_kind: Mapped[str] = mapped_column(String(32))
    executable_bit: Mapped[bool] = mapped_column(Boolean)
    is_symlink: Mapped[bool] = mapped_column(Boolean)
    is_reparse_point: Mapped[bool] = mapped_column(Boolean)
    content_kind: Mapped[str] = mapped_column(String(32))


class DiffValidationResultRow(Base):
    __tablename__ = "diff_validation_results"
    __table_args__ = (
        UniqueConstraint("run_id", "baseline_digest", "final_workspace_digest"),
    )

    validation_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("runs.run_id", ondelete="CASCADE"), index=True
    )
    policy_digest: Mapped[str] = mapped_column(String(64))
    baseline_digest: Mapped[str] = mapped_column(String(64))
    final_workspace_digest: Mapped[str] = mapped_column(String(64))
    diff_digest: Mapped[str] = mapped_column(String(64))
    compliant: Mapped[bool] = mapped_column(Boolean)
    result_data: Mapped[dict[str, Any]] = mapped_column(JSON)
    validation_version: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class RepairEvaluationRunRow(Base):
    __tablename__ = "repair_evaluation_runs"
    __table_args__ = (
        Index("ix_evaluation_task_repetition", "task_id", "repetition_index"),
    )

    evaluation_run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    protocol_digest: Mapped[str] = mapped_column(String(64), index=True)
    campaign_id: Mapped[str] = mapped_column(String(36), index=True)
    slot_id: Mapped[str] = mapped_column(String(36), index=True)
    attempt_id: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    attempt_number: Mapped[int] = mapped_column(Integer)
    task_id: Mapped[str] = mapped_column(String(200), index=True)
    repetition_index: Mapped[int] = mapped_column(Integer)
    model_id: Mapped[str] = mapped_column(String(200))
    model_parameters_digest: Mapped[str] = mapped_column(String(64))
    system_prompt_digest: Mapped[str] = mapped_column(String(64))
    task_prompt_digest: Mapped[str] = mapped_column(String(64))
    tool_schema_digest: Mapped[str] = mapped_column(String(64))
    context_policy_version: Mapped[int] = mapped_column(Integer)
    initial_workspace_digest: Mapped[str] = mapped_column(String(64))
    task_policy_digest: Mapped[str] = mapped_column(String(64))
    budget_profile: Mapped[str] = mapped_column(String(32))
    completion_correction_mode: Mapped[str] = mapped_column(String(32))
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("runs.run_id", ondelete="CASCADE"), unique=True
    )
    baseline_execution_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    final_status: Mapped[str] = mapped_column(String(40), index=True)
    verified_success: Mapped[bool] = mapped_column(Boolean)
    model_calls: Mapped[int] = mapped_column(Integer)
    read_calls: Mapped[int] = mapped_column(Integer)
    edit_attempts: Mapped[int] = mapped_column(Integer)
    test_runs: Mapped[int] = mapped_column(Integer)
    completion_corrections: Mapped[int] = mapped_column(Integer)
    policy_violations: Mapped[int] = mapped_column(Integer)
    wall_time_ms: Mapped[int] = mapped_column(Integer)
    token_usage: Mapped[int | None] = mapped_column(Integer, nullable=True)
    final_workspace_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    final_diff_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    development_test_execution_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True
    )
    final_verification_execution_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True
    )
    failure_category: Mapped[str | None] = mapped_column(String(100), nullable=True)
    outcome_class: Mapped[str] = mapped_column(String(40), index=True)
    failure_class: Mapped[str] = mapped_column(String(40), index=True)
    infrastructure_failure: Mapped[bool] = mapped_column(Boolean)
    replacement_for_evaluation_run_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    result_schema_version: Mapped[int] = mapped_column(Integer)


class EvaluationRunTelemetryRow(Base):
    __tablename__ = "evaluation_run_telemetry"

    evaluation_run_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("repair_evaluation_runs.evaluation_run_id", ondelete="CASCADE"),
        primary_key=True,
    )
    run_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("runs.run_id", ondelete="CASCADE"),
        unique=True,
        index=True,
    )
    campaign_id: Mapped[str] = mapped_column(String(36), index=True)
    attempt_id: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    protocol_digest: Mapped[str] = mapped_column(String(64), index=True)
    telemetry_data: Mapped[dict[str, Any]] = mapped_column(JSON)
    telemetry_digest: Mapped[str] = mapped_column(String(64), unique=True)
    schema_version: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class EvaluationBaselineExecutionRow(Base):
    __tablename__ = "evaluation_baseline_executions"

    baseline_execution_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("runs.run_id", ondelete="CASCADE"), unique=True, index=True
    )
    task_id: Mapped[str] = mapped_column(String(200), index=True)
    workspace_baseline_id: Mapped[str] = mapped_column(String(36))
    initial_workspace_digest: Mapped[str] = mapped_column(String(64))
    test_plan_data: Mapped[dict[str, Any]] = mapped_column(JSON)
    expected_failure_data: Mapped[dict[str, Any]] = mapped_column(JSON)
    expected_fingerprint_digest: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(40), index=True)
    failure_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    record_version: Mapped[int] = mapped_column(Integer)
    result_schema_version: Mapped[int] = mapped_column(Integer)
    actual_fingerprint_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    failed_node_ids: Mapped[list[str]] = mapped_column(JSON)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    root_pid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    process_group_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    job_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer)
    termination_reason: Mapped[str | None] = mapped_column(String(100), nullable=True)
    termination_result: Mapped[str | None] = mapped_column(String(500), nullable=True)
    stdout_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    stderr_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    stdout_size: Mapped[int] = mapped_column(Integer)
    stderr_size: Mapped[int] = mapped_column(Integer)
    output_truncated: Mapped[bool] = mapped_column(Boolean)
    safe_failure_summary_data: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, nullable=True
    )
    safe_failure_summary_digest: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class EvaluationProtocolRow(Base):
    __tablename__ = "evaluation_protocols"

    protocol_digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    protocol_name: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    task_id: Mapped[str] = mapped_column(String(200), index=True)
    provider: Mapped[str] = mapped_column(String(40))
    model_id: Mapped[str] = mapped_column(String(200))
    protocol_data: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class EvaluationProtocolEventRow(Base):
    __tablename__ = "evaluation_protocol_events"

    event_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    protocol_digest: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("evaluation_protocols.protocol_digest", ondelete="CASCADE"),
        index=True,
    )
    event_type: Mapped[str] = mapped_column(String(80))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class EvaluationCampaignRow(Base):
    __tablename__ = "evaluation_campaigns"

    campaign_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    protocol_digest: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("evaluation_protocols.protocol_digest", ondelete="RESTRICT"),
        unique=True,
        index=True,
    )
    task_id: Mapped[str] = mapped_column(String(200), index=True)
    repetition_count: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(40), index=True)
    record_version: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class EvaluationSlotRow(Base):
    __tablename__ = "evaluation_slots"
    __table_args__ = (
        UniqueConstraint("campaign_id", "repetition_index"),
        Index("ix_evaluation_slot_campaign_status", "campaign_id", "status"),
    )

    slot_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    campaign_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("evaluation_campaigns.campaign_id", ondelete="CASCADE"),
        index=True,
    )
    protocol_digest: Mapped[str] = mapped_column(String(64), index=True)
    task_id: Mapped[str] = mapped_column(String(200), index=True)
    repetition_index: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(40), index=True)
    selected_attempt_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True
    )
    selected_evaluation_run_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True
    )
    record_version: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class EvaluationPilotAttemptRow(Base):
    __tablename__ = "evaluation_pilot_attempts"
    __table_args__ = (
        UniqueConstraint("slot_id", "attempt_number"),
        Index("ix_evaluation_attempt_slot_status", "slot_id", "status"),
    )

    attempt_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    campaign_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("evaluation_campaigns.campaign_id", ondelete="CASCADE"),
        index=True,
    )
    slot_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("evaluation_slots.slot_id", ondelete="CASCADE"),
        index=True,
    )
    protocol_digest: Mapped[str] = mapped_column(String(64), index=True)
    task_id: Mapped[str] = mapped_column(String(200), index=True)
    repetition_index: Mapped[int] = mapped_column(Integer)
    attempt_number: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(40), index=True)
    predecessor_attempt_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True
    )
    predecessor_evaluation_run_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True
    )
    workspace_lease_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True
    )
    workspace_root_digest: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    initial_workspace_digest: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    workspace_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    run_id: Mapped[str | None] = mapped_column(String(36), nullable=True, unique=True)
    baseline_execution_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, unique=True
    )
    evaluation_run_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, unique=True
    )
    failure_category: Mapped[str | None] = mapped_column(
        String(100), nullable=True
    )
    infrastructure_failure: Mapped[bool] = mapped_column(Boolean)
    record_version: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class EvaluationCampaignEventRow(Base):
    __tablename__ = "evaluation_campaign_events"
    __table_args__ = (UniqueConstraint("campaign_id", "sequence_number"),)

    event_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    campaign_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("evaluation_campaigns.campaign_id", ondelete="CASCADE"),
        index=True,
    )
    event_type: Mapped[str] = mapped_column(String(80), index=True)
    sequence_number: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class EvaluationStudyRow(Base):
    __tablename__ = "evaluation_studies"

    study_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    definition_digest: Mapped[str] = mapped_column(String(64), index=True)
    definition_data: Mapped[dict[str, Any]] = mapped_column(JSON)
    authorization_digest: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        index=True,
    )
    authorization_data: Mapped[dict[str, Any] | None] = mapped_column(
        JSON,
        nullable=True,
    )
    status: Mapped[str] = mapped_column(String(64), index=True)
    record_version: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )


class EvaluationStudyCampaignRow(Base):
    __tablename__ = "evaluation_study_campaigns"
    __table_args__ = (
        UniqueConstraint("study_id", "task_id"),
        UniqueConstraint("study_id", "protocol_digest"),
    )

    study_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("evaluation_studies.study_id", ondelete="CASCADE"),
        primary_key=True,
    )
    task_order: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[str] = mapped_column(String(200))
    protocol_digest: Mapped[str] = mapped_column(String(64))
    campaign_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("evaluation_campaigns.campaign_id", ondelete="RESTRICT"),
        unique=True,
        index=True,
    )
    campaign_status: Mapped[str | None] = mapped_column(
        String(40),
        nullable=True,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )


class EvaluationStudyEventRow(Base):
    __tablename__ = "evaluation_study_events"
    __table_args__ = (UniqueConstraint("study_id", "sequence_number"),)

    event_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    study_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("evaluation_studies.study_id", ondelete="CASCADE"),
        index=True,
    )
    event_type: Mapped[str] = mapped_column(String(80), index=True)
    sequence_number: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
