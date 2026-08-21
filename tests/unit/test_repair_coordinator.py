from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from agentforge.domain.enums import (
    MutationExecutionStatus,
    ProcessExecutionStatus,
    ProcessFailureKind,
)
from agentforge.domain.models import Run, utc_now
from agentforge.domain.mutations import MutationExecutionRecord
from agentforge.domain.repair import (
    BudgetProfile,
    CompletionAction,
    RepairCompletionStatus,
    RepairDifficulty,
    RepairTaskPolicy,
    RepairTerminationReason,
)
from agentforge.domain.test_execution import ProcessExecutionRecord
from agentforge.models.domain import ModelErrorCode
from agentforge.persistence.database import Database
from agentforge.persistence.event_log import RunLeaseAuthority
from agentforge.persistence.repair_workflow import RepairWorkflow
from agentforge.persistence.repositories import RunRepository
from agentforge.persistence.run_leases import RunLeaseStore
from agentforge.runtime.repair import CompletionContext, RepairCoordinator

SHA = "a" * 64


def policy() -> RepairTaskPolicy:
    return RepairTaskPolicy(
        task_id="coordinator-task",
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


def setup_coordinator(
    tmp_path: Path,
) -> tuple[RepairCoordinator, RepairWorkflow, Run, RunLeaseAuthority]:
    database = Database.from_path(tmp_path / "coordinator.db")
    database.create_schema()
    run = RunRepository(database).create(Run(task="repair"))
    workflow = RepairWorkflow(database)
    workflow._evaluator_only_start(run.run_id, policy(), uuid4(), "b" * 64)
    authority = (
        RunLeaseStore(database)
        .acquire(run.run_id, owner_id="test:repair-coordinator", ttl=timedelta(seconds=30))
        .authority
    )
    return RepairCoordinator(workflow), workflow, run, authority


def mutation(run_id: UUID, *, committed_at_offset: int = 0) -> MutationExecutionRecord:
    now = utc_now() + timedelta(seconds=committed_at_offset)
    return MutationExecutionRecord(
        run_id=run_id,
        approval_id=uuid4(),
        tool_call_digest=SHA,
        tool_name="edit_file",
        target_path="src/module.py",
        before_sha256="b" * 64,
        expected_after_sha256="c" * 64,
        before_workspace_digest="d" * 64,
        expected_after_workspace_digest="e" * 64,
        actual_after_sha256="c" * 64,
        bytes_written=10,
        status=MutationExecutionStatus.COMMITTED,
        created_at=now,
        updated_at=now,
    )


def process_result(
    run_id: UUID,
    *,
    success: bool,
    profile_id: str = "unit",
    completed_at_offset: int = 0,
) -> ProcessExecutionRecord:
    now = utc_now() + timedelta(seconds=completed_at_offset)
    return ProcessExecutionRecord(
        run_id=run_id,
        approval_id=uuid4(),
        tool_call_digest="d" * 64,
        attempt_number=1,
        profile_id=profile_id,
        profile_version=1,
        profile_digest=SHA,
        executable_path=str(Path("C:/Python/python.exe")),
        argv_digest=SHA,
        cwd=str(Path("C:/workspace")),
        environment_digest=SHA,
        status=(ProcessExecutionStatus.COMPLETED if success else ProcessExecutionStatus.FAILED),
        failure_kind=None if success else ProcessFailureKind.TEST_FAILURE,
        exit_code=0 if success else 1,
        stdout_digest=SHA,
        stderr_digest=SHA,
        updated_at=now,
        created_at=now,
    )


def test_mark_unverified_final_terminalizes_published_candidate(tmp_path: Path) -> None:
    coordinator, _, run, authority = setup_coordinator(tmp_path)

    state = coordinator.mark_unverified_final(run.run_id, authority=authority)

    assert state.status is RepairCompletionStatus.UNVERIFIED_FINAL
    assert state.failure_reason is RepairTerminationReason.LATEST_MUTATION_NOT_VERIFIED


def test_latest_source_is_verified_only_by_a_later_successful_development_test(
    tmp_path: Path,
) -> None:
    coordinator, workflow, run, authority = setup_coordinator(tmp_path)
    change = mutation(run.run_id, committed_at_offset=1)

    coordinator.observe_mutation(change, authority=authority)
    coordinator.observe_test(
        process_result(run.run_id, success=True, completed_at_offset=2),
        authority=authority,
    )
    verified = workflow.get_state(run.run_id)
    coordinator.observe_mutation(mutation(run.run_id, committed_at_offset=3), authority=authority)
    stale = workflow.get_state(run.run_id)

    assert verified.latest_source_verified
    assert verified.last_mutation_execution_id == change.execution_id
    assert not stale.latest_source_verified
    assert stale.edit_attempts_used == 2
    assert stale.test_runs_used == 1


def test_failed_development_test_never_verifies_source(tmp_path: Path) -> None:
    coordinator, workflow, run, authority = setup_coordinator(tmp_path)
    coordinator.observe_mutation(mutation(run.run_id), authority=authority)

    coordinator.observe_test(
        process_result(run.run_id, success=False, completed_at_offset=1),
        authority=authority,
    )

    state = workflow.get_state(run.run_id)
    assert state.last_development_test_success is False
    assert not state.latest_source_verified


def test_verified_completion_requests_trusted_final_verification(tmp_path: Path) -> None:
    coordinator, _, run, authority = setup_coordinator(tmp_path)
    coordinator.observe_mutation(mutation(run.run_id), authority=authority)
    coordinator.observe_test(
        process_result(run.run_id, success=True, completed_at_offset=1),
        authority=authority,
    )

    decision = coordinator.evaluate_completion(
        run.run_id,
        answer_digest="e" * 64,
        context=CompletionContext(),
        authority=authority,
    )

    assert decision.action is CompletionAction.REQUEST_FINAL_VERIFICATION
    assert decision.feedback is None
    assert decision.state.pending_final_verification


def test_runtime_context_exposes_numeric_budget_facts_without_trusted_labels(
    tmp_path: Path,
) -> None:
    coordinator, _, run, _ = setup_coordinator(tmp_path)

    payload = coordinator.runtime_context(run.run_id)
    serialized = str(payload)

    assert payload["remaining_model_calls"] == 6
    assert payload["remaining_read_calls"] == 20
    assert payload["remaining_edit_attempts"] == 2
    assert payload["remaining_test_runs"] == 3
    assert payload["remaining_completion_corrections"] == 1
    assert "BASIC" not in serialized
    assert "difficulty" not in serialized.casefold()
    assert "budget_profile" not in serialized


def test_indeterminate_execution_facts_fail_closed_and_are_idempotent(
    tmp_path: Path,
) -> None:
    coordinator, workflow, run, authority = setup_coordinator(tmp_path)
    uncertain = mutation(run.run_id).model_copy(
        update={
            "status": MutationExecutionStatus.INDETERMINATE,
            "actual_after_sha256": None,
        }
    )

    first = coordinator.observe_mutation(uncertain, authority=authority)
    repeated = coordinator.observe_mutation(uncertain, authority=authority)

    assert first.status is RepairCompletionStatus.INDETERMINATE
    assert repeated == first
    assert workflow.get_state(run.run_id).edit_attempts_used == 1


def test_indeterminate_test_fact_fails_closed_and_is_not_double_counted(
    tmp_path: Path,
) -> None:
    coordinator, workflow, run, authority = setup_coordinator(tmp_path)
    uncertain = process_result(run.run_id, success=False).model_copy(
        update={
            "status": ProcessExecutionStatus.INDETERMINATE,
            "failure_kind": ProcessFailureKind.INDETERMINATE,
            "exit_code": None,
        }
    )

    first = coordinator.observe_test(uncertain, authority=authority)
    repeated = coordinator.observe_test(uncertain, authority=authority)

    assert first.status is RepairCompletionStatus.INDETERMINATE
    assert repeated == first
    assert workflow.get_state(run.run_id).test_runs_used == 1


@pytest.mark.parametrize(
    ("error_code", "status", "reason"),
    [
        (
            ModelErrorCode.MODEL_AUTH_ERROR,
            RepairCompletionStatus.RUNTIME_FAILURE,
            RepairTerminationReason.MODEL_AUTH_ERROR,
        ),
        (
            ModelErrorCode.MODEL_RATE_LIMITED,
            RepairCompletionStatus.RUNTIME_FAILURE,
            RepairTerminationReason.MODEL_RATE_LIMITED,
        ),
        (
            ModelErrorCode.MODEL_TIMEOUT,
            RepairCompletionStatus.RUNTIME_FAILURE,
            RepairTerminationReason.MODEL_TIMEOUT,
        ),
        (
            ModelErrorCode.MODEL_TRANSPORT_ERROR,
            RepairCompletionStatus.RUNTIME_FAILURE,
            RepairTerminationReason.MODEL_TRANSPORT_ERROR,
        ),
        (
            ModelErrorCode.MODEL_BAD_REQUEST,
            RepairCompletionStatus.RUNTIME_FAILURE,
            RepairTerminationReason.MODEL_BAD_REQUEST,
        ),
        (
            ModelErrorCode.MODEL_PROVIDER_ERROR,
            RepairCompletionStatus.RUNTIME_FAILURE,
            RepairTerminationReason.MODEL_PROVIDER_ERROR,
        ),
        (
            ModelErrorCode.MODEL_PROTOCOL_ERROR,
            RepairCompletionStatus.MODEL_PROTOCOL_ERROR,
            RepairTerminationReason.MODEL_PROTOCOL_ERROR,
        ),
        (
            ModelErrorCode.MODEL_OUTPUT_INVALID,
            RepairCompletionStatus.MODEL_PROTOCOL_ERROR,
            RepairTerminationReason.MODEL_PROTOCOL_ERROR,
        ),
        (
            ModelErrorCode.MODEL_BUDGET_EXCEEDED,
            RepairCompletionStatus.BUDGET_EXHAUSTED,
            RepairTerminationReason.MODEL_CALL_LIMIT,
        ),
    ],
)
def test_model_failures_have_durable_terminal_repair_mapping(
    tmp_path: Path,
    error_code: ModelErrorCode,
    status: RepairCompletionStatus,
    reason: RepairTerminationReason,
) -> None:
    coordinator, workflow, run, authority = setup_coordinator(tmp_path)

    first = coordinator.terminalize_model_failure(
        run.run_id,
        error_code=error_code,
        authority=authority,
    )
    repeated = coordinator.terminalize_model_failure(
        run.run_id,
        error_code=error_code,
        authority=authority,
    )

    assert first.status is status
    assert first.failure_reason is reason
    assert repeated == first
    assert workflow.get_state(run.run_id) == first
