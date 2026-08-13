from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from agentforge.application.approval_commands import DecideApprovalCommand
from agentforge.application.kernel_errors import IncompleteRunBundleError
from agentforge.application.run_creation import RunCreationWorkflow, StartRun
from agentforge.domain.enums import ApprovalStatus, RejectionStrategy, RunStatus
from agentforge.domain.models import ApprovalRequest, Checkpoint, RuntimeSnapshot
from agentforge.domain.repair import BudgetProfile, RepairDifficulty, RepairTaskPolicy
from agentforge.models.domain import ModelBudget
from agentforge.persistence.approval_workflow import ApprovalWorkflow
from agentforge.persistence.database import Database
from agentforge.persistence.event_log import EventLog
from agentforge.persistence.product_tables import ApplicationCommandReceiptRow
from agentforge.persistence.receipts import ReceiptStore
from agentforge.persistence.repositories import ApprovalRepository, RunRepository
from agentforge.persistence.run_leases import RunLeaseStore
from agentforge.persistence.tables import EventRow, RepairStateRow


def _policy() -> RepairTaskPolicy:
    return RepairTaskPolicy(
        task_id="repair-provenance",
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
        max_total_changed_bytes=4096,
        max_single_file_changed_bytes=2048,
        path_case_sensitive=False,
    )


def _complete_run(database: Database):
    created = RunCreationWorkflow(database, ReceiptStore(), EventLog()).create(
        StartRun(
            command_id=uuid4(),
            task="repair provenance",
            max_steps=5,
            max_tool_calls=6,
            model_provider="mock",
            model_budget=ModelBudget(max_model_requests=4, max_retries=1),
            workspace_root_identity="workspace-1",
            git_head="a" * 64,
            initial_source_digest="b" * 64,
            digest_algorithm_version=1,
            config_digest="c" * 64,
            profile_digest="a" * 64,
            repair_policy=_policy(),
            baseline_id=UUID(int=5),
            baseline_digest="b" * 64,
        )
    )
    return RunRepository(database).get(created.run_id)


def _paused_approval(database: Database):
    run = _complete_run(database)
    workflow = ApprovalWorkflow(database)
    lease = RunLeaseStore(database).acquire(
        run.run_id, owner_id="repair-provenance", ttl=timedelta(seconds=30)
    )
    run.transition_to(RunStatus.RUNNING)
    RunRepository(database).save(run, authority=lease.authority)
    checkpoint_id = uuid4()
    approval_id = uuid4()
    workflow.pause_for_approval(
        run,
        Checkpoint(
            checkpoint_id=checkpoint_id,
            run_id=run.run_id,
            step_number=run.current_step,
            runtime_state=RuntimeSnapshot(
                run_id=run.run_id,
                step_number=run.current_step,
                pending_tool_call=None,
                pending_approval_id=approval_id,
                tool_call_digest="a" * 64,
                resume_phase="AWAITING_APPROVAL",
            ).model_dump(mode="json"),
        ),
        ApprovalRequest(
            approval_id=approval_id,
            run_id=run.run_id,
            checkpoint_id=checkpoint_id,
            tool_name="write_file",
            sanitized_arguments={"path": "src/fix.py"},
            request_digest="a" * 64,
        ),
        authority=lease.authority,
    )
    with database.session() as session:
        repair = session.get(RepairStateRow, str(run.run_id))
        assert repair is not None
        session.delete(repair)
    return workflow, run, approval_id


def _side_effect_counts(database: Database, run_id) -> tuple[int, int, int]:
    with database.session() as session:
        return (
            int(
                session.scalar(
                    select(func.count()).select_from(EventRow).where(EventRow.run_id == str(run_id))
                )
                or 0
            ),
            int(
                session.scalar(
                    select(func.count()).select_from(ApplicationCommandReceiptRow)
                )
                or 0
            ),
            int(session.scalar(select(func.count()).select_from(RepairStateRow)) or 0),
        )


def test_product_reject_rolls_back_when_a_complete_bundle_lacks_repair_state(
    tmp_path: Path,
) -> None:
    database = Database.from_path(tmp_path / "repair-provenance.sqlite3")
    database.create_schema()
    workflow, run, approval_id = _paused_approval(database)
    before = _side_effect_counts(database, run.run_id)

    with pytest.raises(IncompleteRunBundleError):
        workflow.resolve_command(
            DecideApprovalCommand(
                command_id=uuid4(),
                approval_id=approval_id,
                status=ApprovalStatus.REJECTED,
                strategy=RejectionStrategy.FAIL_RUN,
            )
        )

    assert _side_effect_counts(database, run.run_id) == before
    assert RunRepository(database).get(run.run_id).status is RunStatus.WAITING_APPROVAL
    assert ApprovalRepository(database).get(approval_id).status is ApprovalStatus.PENDING


def test_product_cancel_rolls_back_when_a_complete_bundle_lacks_repair_state(
    tmp_path: Path,
) -> None:
    database = Database.from_path(tmp_path / "cancel-provenance.sqlite3")
    database.create_schema()
    workflow, run, approval_id = _paused_approval(database)
    authority = RunLeaseStore(database).acquire(
        run.run_id, owner_id="repair-cancel", ttl=timedelta(seconds=30)
    ).authority
    before = _side_effect_counts(database, run.run_id)

    with pytest.raises(IncompleteRunBundleError):
        workflow.cancel(run.run_id, "operator cancelled", authority=authority)

    assert _side_effect_counts(database, run.run_id) == before
    assert RunRepository(database).get(run.run_id).status is RunStatus.WAITING_APPROVAL
    assert ApprovalRepository(database).get(approval_id).status is ApprovalStatus.PENDING


def test_product_indeterminate_rolls_back_when_a_complete_bundle_lacks_repair_state(
    tmp_path: Path,
) -> None:
    database = Database.from_path(tmp_path / "indeterminate-provenance.sqlite3")
    database.create_schema()
    workflow, run, approval_id = _paused_approval(database)
    workflow.resolve_command(
        DecideApprovalCommand(
            command_id=uuid4(),
            approval_id=approval_id,
            status=ApprovalStatus.APPROVED,
            strategy=RejectionStrategy.CONTINUE,
        )
    )
    authority = RunLeaseStore(database).acquire(
        run.run_id, owner_id="repair-indeterminate", ttl=timedelta(seconds=30)
    ).authority
    assert workflow.claim_resume(run.run_id, approval_id, authority=authority)
    before = _side_effect_counts(database, run.run_id)

    with pytest.raises(IncompleteRunBundleError):
        workflow.mark_indeterminate(run.run_id, approval_id, authority=authority)

    assert _side_effect_counts(database, run.run_id) == before
    assert RunRepository(database).get(run.run_id).status is RunStatus.RUNNING
    assert ApprovalRepository(database).get(approval_id).status is ApprovalStatus.APPROVED


def test_legacy_evaluator_resolution_requires_an_evaluator_fixed_workflow(
    tmp_path: Path,
) -> None:
    database = Database.from_path(tmp_path / "normal-workflow.sqlite3")
    database.create_schema()
    workflow, run, approval_id = _paused_approval(database)
    before = _side_effect_counts(database, run.run_id)

    with pytest.raises(IncompleteRunBundleError):
        workflow._evaluator_only_resolve(
            approval_id,
            ApprovalStatus.REJECTED,
            RejectionStrategy.FAIL_RUN,
            None,
        )

    assert _side_effect_counts(database, run.run_id) == before
    assert RunRepository(database).get(run.run_id).status is RunStatus.WAITING_APPROVAL
    assert ApprovalRepository(database).get(approval_id).status is ApprovalStatus.PENDING


def test_evaluator_fixed_workflow_can_terminalize_an_unbundled_legacy_run(
    tmp_path: Path,
) -> None:
    database = Database.from_path(tmp_path / "evaluator-workflow.sqlite3")
    database.create_schema()
    _, run, approval_id = _paused_approval(database)
    workflow = ApprovalWorkflow._evaluator_only_create(database)

    workflow._evaluator_only_resolve(
        approval_id,
        ApprovalStatus.REJECTED,
        RejectionStrategy.FAIL_RUN,
        None,
    )

    assert RunRepository(database).get(run.run_id).status is RunStatus.FAILED
    with pytest.raises(AttributeError, match="immutable"):
        workflow._repair_state_provenance = object()  # type: ignore[assignment]
