from __future__ import annotations

import hashlib
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from agentforge.application.kernel_errors import SourceRevisionConflictError
from agentforge.application.run_creation import RunCreationWorkflow, StartRun
from agentforge.domain.enums import (
    ApprovalConsumptionState,
    ApprovalStatus,
    EventType,
    MutationExecutionStatus,
    RejectionStrategy,
    RunStatus,
)
from agentforge.domain.models import ApprovalRequest, Checkpoint
from agentforge.domain.mutations import MutationApprovalBinding
from agentforge.domain.repair import BudgetProfile, RepairDifficulty, RepairTaskPolicy
from agentforge.models.domain import ModelBudget
from agentforge.persistence.approval_workflow import ApprovalWorkflow
from agentforge.persistence.database import Database
from agentforge.persistence.event_log import RunLeaseAuthority
from agentforge.persistence.mutation_workflow import MutationWorkflow
from agentforge.persistence.mutations import (
    MutationApprovalBindingRepository,
    MutationExecutionRepository,
)
from agentforge.persistence.product_tables import WorkspaceSourceBindingRow
from agentforge.persistence.repositories import ApprovalRepository, EventRepository, RunRepository
from agentforge.persistence.run_leases import RunLeaseStore
from agentforge.persistence.source_revisions import (
    DIGEST_ALGORITHM_VERSION,
    MutationRecoveryAction,
    WorkspaceDigester,
    classify_writing_recovery,
)
from agentforge.policy.sensitive import SensitiveFilePolicy
from agentforge.runtime.mutations import MutationCoordinator
from agentforge.tools.mutation.base import MutationLimits
from agentforge.tools.mutation.security import MutationSecurityPolicy
from agentforge.tools.paths import WorkspacePathResolver


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _policy() -> RepairTaskPolicy:
    return RepairTaskPolicy(
        task_id="revision-task",
        policy_version=1,
        difficulty=RepairDifficulty.BASIC,
        budget_profile=BudgetProfile.BASIC,
        allowed_write_paths=("src/**",),
        protected_paths=("tests/**",),
        allowed_development_test_profiles=("unit",),
        final_verification_profile_id="hidden",
        allow_file_creation=True,
        max_created_files=1,
        max_changed_files=2,
        max_total_changed_bytes=4096,
        max_single_file_changed_bytes=2048,
        path_case_sensitive=False,
    )


def _product_mutation(
    database: Database, workspace: Path, *, expected_bytes: bytes = b"after\n"
) -> tuple[UUID, UUID, str, str, RunLeaseAuthority]:
    workspace.mkdir(exist_ok=True)
    target = workspace / "src" / "app.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"before\n")
    digester = WorkspaceDigester()
    before = digester.digest(workspace)
    result = RunCreationWorkflow(database).create(
        StartRun(
            command_id=uuid4(),
            task="change source",
            max_steps=3,
            max_tool_calls=3,
            model_provider="mock",
            model_budget=ModelBudget(max_model_requests=2, max_retries=0),
            workspace_root_identity=str(workspace.resolve()),
            initial_source_digest=before,
            digest_algorithm_version=DIGEST_ALGORITHM_VERSION,
            config_digest="c" * 64,
            profile_digest="d" * 64,
            repair_policy=_policy(),
            baseline_id=UUID(int=7),
            baseline_digest="e" * 64,
        )
    )
    run = result.run
    setup = RunLeaseStore(database).acquire(
        run.run_id, owner_id="mutation-setup", ttl=timedelta(seconds=30)
    )
    run.transition_to(RunStatus.RUNNING)
    RunRepository(database).save(run, authority=setup.authority)
    checkpoint = Checkpoint(
        run_id=run.run_id,
        step_number=1,
        runtime_state={"history": [], "resume_phase": "AWAITING_APPROVAL"},
    )
    approval = ApprovalRequest(
        run_id=run.run_id,
        checkpoint_id=checkpoint.checkpoint_id,
        tool_name="write_file",
        sanitized_arguments={"path": "src/app.py", "content": "<redacted>"},
        request_digest="f" * 64,
    )
    binding = MutationApprovalBinding(
        approval_id=approval.approval_id,
        run_id=run.run_id,
        checkpoint_id=checkpoint.checkpoint_id,
        tool_call_digest=approval.request_digest,
        tool_name="write_file",
        target_path="src/app.py",
        target_existed=True,
        before_sha256=_sha(b"before\n"),
        expected_after_sha256=_sha(expected_bytes),
        bytes_written=len(expected_bytes),
    )
    ApprovalWorkflow(database).pause_for_approval(
        run,
        checkpoint,
        approval,
        mutation_binding=binding,
        authority=setup.authority,
    )
    ApprovalWorkflow._evaluator_only_create(database)._evaluator_only_resolve(
        approval.approval_id,
        ApprovalStatus.APPROVED,
        RejectionStrategy.CONTINUE,
        None,
    )
    snapshot = digester.snapshot(workspace)
    expected_after = digester.project_digest(
        snapshot,
        relative_path="src/app.py",
        size_bytes=len(expected_bytes),
        content_sha256=_sha(expected_bytes),
    )
    worker = RunLeaseStore(database).acquire(
        run.run_id, owner_id="mutation-worker", ttl=timedelta(seconds=30)
    )
    return run.run_id, approval.approval_id, before, expected_after, worker.authority


@pytest.mark.parametrize(
    ("actual", "expected"),
    [
        ("before", MutationRecoveryAction.RETRY),
        ("after", MutationRecoveryAction.FINALIZE),
        ("partial", MutationRecoveryAction.MARK_INDETERMINATE),
    ],
)
def test_writing_recovery_uses_before_and_expected_after(
    actual: str, expected: MutationRecoveryAction
) -> None:
    assert classify_writing_recovery(actual, before="before", expected_after="after") is expected


def test_commit_advances_mutation_and_source_revision_in_one_transaction(
    tmp_path: Path,
) -> None:
    database = Database.from_path(tmp_path / "runtime.db")
    database.create_schema()
    workspace = tmp_path / "workspace"
    run_id, approval_id, before, expected_after, authority = _product_mutation(database, workspace)
    workflow = MutationWorkflow(database)
    coordinator = MutationCoordinator(
        MutationApprovalBindingRepository(database),
        MutationExecutionRepository(database),
        workflow,
        MutationSecurityPolicy(
            WorkspacePathResolver(workspace), SensitiveFilePolicy(), MutationLimits()
        ),
    )
    prepared = coordinator.ensure_prepared(approval_id, authority=authority)
    assert prepared.result_summary == ("BOUND_REVISION_V1|source_verified=true: Mutation prepared")
    assert workflow.claim_resume(
        run_id,
        approval_id,
        actual_workspace_digest=before,
        workspace_root_identity=str(workspace.resolve()),
        authority=authority,
    )
    (workspace / "src" / "app.py").write_bytes(b"after\n")

    committed = workflow.mark_committed(
        run_id,
        approval_id,
        actual_after_sha256=_sha(b"after\n"),
        actual_workspace_digest=WorkspaceDigester().digest(workspace),
        bytes_written=len(b"after\n"),
        duration_ms=1,
        authority=authority,
    )

    assert committed.status is MutationExecutionStatus.COMMITTED
    assert committed.result_summary == (
        "BOUND_REVISION_V1|source_verified=true: Mutation committed"
    )
    assert committed.before_workspace_digest == before
    assert committed.expected_after_workspace_digest == expected_after
    replayed = coordinator.recover_claimed(run_id, approval_id, authority=authority)
    assert replayed is not None
    assert replayed.metadata["source_revision_semantics"] == "BOUND_REVISION_V1"
    assert replayed.metadata["source_verified"] is True
    with database.session() as session:
        source = session.get(WorkspaceSourceBindingRow, str(run_id))
        assert source is not None
        assert source.expected_source_digest == expected_after
        assert source.source_revision_number == 1
    mutation_events = [
        event
        for event in EventRepository(database).list_for_run(run_id)
        if event.event_type.value.startswith("MUTATION_")
    ]
    assert {event.event_type for event in mutation_events} >= {
        EventType.MUTATION_REQUESTED,
        EventType.MUTATION_STARTED,
        EventType.MUTATION_COMMITTED,
    }
    expected_verification = {
        EventType.MUTATION_REQUESTED: False,
        EventType.MUTATION_STARTED: True,
        EventType.MUTATION_COMMITTED: True,
    }
    assert all(
        event.payload["source_revision_semantics"] == "BOUND_REVISION_V1"
        and event.payload["source_verified"] is expected_verification[event.event_type]
        for event in mutation_events
    )
    database.close()


def test_source_drift_rejects_before_side_effect_and_preserves_prepared_state(
    tmp_path: Path,
) -> None:
    database = Database.from_path(tmp_path / "runtime.db")
    database.create_schema()
    workspace = tmp_path / "workspace"
    run_id, approval_id, before, expected_after, authority = _product_mutation(database, workspace)
    workflow = MutationWorkflow(database)
    record = workflow.ensure_prepared(
        approval_id,
        before_workspace_digest=before,
        expected_after_workspace_digest=expected_after,
        authority=authority,
    )
    (workspace / "unplanned.py").write_text("drift", encoding="utf-8")
    actual = WorkspaceDigester().digest(workspace)

    with pytest.raises(SourceRevisionConflictError, match=r"^source revision conflict$"):
        workflow.claim_resume(
            run_id,
            approval_id,
            actual_workspace_digest=actual,
            workspace_root_identity=str(workspace.resolve()),
            authority=authority,
        )
    assert MutationExecutionRepository(database).get(record.execution_id).status is (
        MutationExecutionStatus.PREPARED
    )
    database.close()


def test_bound_mutation_cancel_event_does_not_claim_actual_digest_verification(
    tmp_path: Path,
) -> None:
    database = Database.from_path(tmp_path / "runtime.db")
    database.create_schema()
    workspace = tmp_path / "workspace"
    run_id, approval_id, before, expected_after, authority = _product_mutation(database, workspace)
    MutationWorkflow(database).ensure_prepared(
        approval_id,
        before_workspace_digest=before,
        expected_after_workspace_digest=expected_after,
        authority=authority,
    )

    ApprovalWorkflow(database).cancel(run_id, "operator cancelled", authority=authority)

    failed = next(
        event
        for event in reversed(EventRepository(database).list_for_run(run_id))
        if event.event_type is EventType.MUTATION_FAILED
    )
    assert failed.payload["source_revision_semantics"] == "BOUND_REVISION_V1"
    assert failed.payload["source_verified"] is False
    record = MutationExecutionRepository(database).get_for_approval(approval_id)
    assert record.result_summary == (
        "BOUND_REVISION_V1|source_verified=false: Mutation cancelled before execution"
    )
    database.close()


def test_workspace_identity_mismatch_rejects_before_side_effect(tmp_path: Path) -> None:
    database = Database.from_path(tmp_path / "runtime.db")
    database.create_schema()
    workspace = tmp_path / "workspace"
    run_id, approval_id, before, expected_after, authority = _product_mutation(database, workspace)
    workflow = MutationWorkflow(database)
    record = workflow.ensure_prepared(
        approval_id,
        before_workspace_digest=before,
        expected_after_workspace_digest=expected_after,
        authority=authority,
    )

    with pytest.raises(SourceRevisionConflictError):
        workflow.claim_resume(
            run_id,
            approval_id,
            actual_workspace_digest=before,
            workspace_root_identity=str((tmp_path / "other").resolve()),
            authority=authority,
        )
    assert MutationExecutionRepository(database).get(record.execution_id).status is (
        MutationExecutionStatus.PREPARED
    )
    database.close()


def test_failed_source_cas_rolls_back_mutation_commit(tmp_path: Path) -> None:
    database = Database.from_path(tmp_path / "runtime.db")
    database.create_schema()
    workspace = tmp_path / "workspace"
    run_id, approval_id, before, expected_after, authority = _product_mutation(database, workspace)
    workflow = MutationWorkflow(database)
    record = workflow.ensure_prepared(
        approval_id,
        before_workspace_digest=before,
        expected_after_workspace_digest=expected_after,
        authority=authority,
    )
    assert workflow.claim_resume(
        run_id,
        approval_id,
        actual_workspace_digest=before,
        workspace_root_identity=str(workspace.resolve()),
        authority=authority,
    )
    (workspace / "src" / "app.py").write_bytes(b"after\n")
    with database.session() as session:
        source = session.get(WorkspaceSourceBindingRow, str(run_id))
        assert source is not None
        source.expected_source_digest = "9" * 64

    with pytest.raises(SourceRevisionConflictError):
        workflow.mark_committed(
            run_id,
            approval_id,
            actual_after_sha256=_sha(b"after\n"),
            actual_workspace_digest=expected_after,
            bytes_written=len(b"after\n"),
            duration_ms=1,
            authority=authority,
        )
    assert MutationExecutionRepository(database).get(record.execution_id).status is (
        MutationExecutionStatus.WRITING
    )
    with database.session() as session:
        source = session.get(WorkspaceSourceBindingRow, str(run_id))
        assert source is not None
        assert source.expected_source_digest == "9" * 64
        assert source.source_revision_number == 0
    database.close()


@pytest.mark.parametrize(
    ("actual_kind", "expected_action"),
    [
        ("before", MutationRecoveryAction.RETRY),
        ("after", MutationRecoveryAction.FINALIZE),
        ("partial", MutationRecoveryAction.MARK_INDETERMINATE),
    ],
)
def test_writing_recovery_persists_exact_action_across_database_reopen(
    tmp_path: Path,
    actual_kind: str,
    expected_action: MutationRecoveryAction,
) -> None:
    path = tmp_path / f"{actual_kind}.db"
    database = Database.from_path(path)
    database.create_schema()
    workspace = tmp_path / f"workspace-{actual_kind}"
    run_id, approval_id, before, expected_after, authority = _product_mutation(database, workspace)
    workflow = MutationWorkflow(database)
    workflow.ensure_prepared(
        approval_id,
        before_workspace_digest=before,
        expected_after_workspace_digest=expected_after,
        authority=authority,
    )
    assert workflow.claim_resume(
        run_id,
        approval_id,
        actual_workspace_digest=before,
        workspace_root_identity=str(workspace.resolve()),
        authority=authority,
    )
    if actual_kind == "after":
        (workspace / "src" / "app.py").write_bytes(b"after\n")
    elif actual_kind == "partial":
        (workspace / "src" / "app.py").write_bytes(b"partial")
    database.close()

    reopened = Database.from_path(path)
    reopened.validate_product_schema()
    actual = WorkspaceDigester().digest(workspace)
    action = MutationWorkflow(reopened).recover_writing(
        run_id,
        approval_id,
        actual_workspace_digest=actual,
        workspace_root_identity=str(workspace.resolve()),
        authority=authority,
    )
    assert action is expected_action
    record = MutationExecutionRepository(reopened).get_for_approval(approval_id)
    approval = ApprovalRepository(reopened).get(approval_id)
    run = RunRepository(reopened).get(run_id)
    if expected_action is MutationRecoveryAction.RETRY:
        assert record.status is MutationExecutionStatus.PREPARED
        assert approval.consumption_state is ApprovalConsumptionState.NOT_STARTED
        assert run.status is RunStatus.PAUSED
    elif expected_action is MutationRecoveryAction.FINALIZE:
        assert record.status is MutationExecutionStatus.COMMITTED
        with reopened.session() as session:
            source = session.get(WorkspaceSourceBindingRow, str(run_id))
            assert source is not None
            assert source.expected_source_digest == expected_after
            assert source.source_revision_number == 1
    else:
        assert record.status is MutationExecutionStatus.INDETERMINATE
        assert approval.consumption_state is ApprovalConsumptionState.INDETERMINATE
        assert run.status is RunStatus.FAILED
    mutation_events = [
        event
        for event in EventRepository(reopened).list_for_run(run_id)
        if event.event_type.value.startswith("MUTATION_")
    ]
    verification_by_type = {
        EventType.MUTATION_REQUESTED: False,
        EventType.MUTATION_STARTED: True,
        EventType.MUTATION_COMMITTED: True,
        EventType.MUTATION_INDETERMINATE: False,
    }
    assert all(
        event.payload["source_revision_semantics"] == "BOUND_REVISION_V1"
        and event.payload["source_verified"] is verification_by_type[event.event_type]
        for event in mutation_events
    )
    reopened.close()


def test_finalize_replay_is_idempotent_and_does_not_increment_revision_twice(
    tmp_path: Path,
) -> None:
    database = Database.from_path(tmp_path / "runtime.db")
    database.create_schema()
    workspace = tmp_path / "workspace"
    run_id, approval_id, before, expected_after, authority = _product_mutation(database, workspace)
    workflow = MutationWorkflow(database)
    workflow.ensure_prepared(
        approval_id,
        before_workspace_digest=before,
        expected_after_workspace_digest=expected_after,
        authority=authority,
    )
    assert workflow.claim_resume(
        run_id,
        approval_id,
        actual_workspace_digest=before,
        workspace_root_identity=str(workspace.resolve()),
        authority=authority,
    )
    (workspace / "src" / "app.py").write_bytes(b"after\n")
    assert (
        workflow.recover_writing(
            run_id,
            approval_id,
            actual_workspace_digest=expected_after,
            workspace_root_identity=str(workspace.resolve()),
            authority=authority,
        )
        is MutationRecoveryAction.FINALIZE
    )
    assert (
        workflow.recover_writing(
            run_id,
            approval_id,
            actual_workspace_digest=expected_after,
            workspace_root_identity=str(workspace.resolve()),
            authority=authority,
        )
        is MutationRecoveryAction.FINALIZE
    )
    with database.session() as session:
        revision = session.scalar(
            select(WorkspaceSourceBindingRow.source_revision_number).where(
                WorkspaceSourceBindingRow.run_id == str(run_id)
            )
        )
    assert revision == 1
    database.close()
