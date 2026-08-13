from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from agentforge.domain.models import Run
from agentforge.domain.repair import (
    BudgetProfile,
    CompletionAction,
    RepairCompletionStatus,
    RepairDifficulty,
    RepairTaskPolicy,
)
from agentforge.persistence.database import Database
from agentforge.persistence.event_log import RunLeaseAuthority
from agentforge.persistence.repair_workflow import RepairWorkflow
from agentforge.persistence.repositories import RunRepository
from agentforge.persistence.run_leases import RunLeaseStore
from agentforge.runtime.repair import CompletionContext, RepairCoordinator


def setup_coordinator(
    tmp_path: Path,
) -> tuple[RepairCoordinator, RepairWorkflow, Run, RunLeaseAuthority]:
    database = Database.from_path(tmp_path / "completion.db")
    database.create_schema()
    run = RunRepository(database).create(Run(task="repair"))
    workflow = RepairWorkflow(database)
    policy = RepairTaskPolicy(
        task_id="completion-task",
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
    workflow._evaluator_only_start(run.run_id, policy, uuid4(), "b" * 64)
    authority = (
        RunLeaseStore(database)
        .acquire(run.run_id, owner_id="test:completion", ttl=timedelta(seconds=30))
        .authority
    )
    return RepairCoordinator(workflow), workflow, run, authority


def test_default_mode_injects_one_objective_correction_then_stops(tmp_path: Path) -> None:
    coordinator, workflow, run, authority = setup_coordinator(tmp_path)

    first = coordinator.evaluate_completion(
        run.run_id,
        answer_digest="1" * 64,
        context=CompletionContext(),
        authority=authority,
    )
    second = coordinator.evaluate_completion(
        run.run_id,
        answer_digest="2" * 64,
        context=CompletionContext(),
        authority=authority,
    )

    assert first.action is CompletionAction.CORRECT
    assert first.feedback == (
        "The repair cannot be marked complete because no allowed development test "
        "has completed successfully for the latest source state."
    )
    assert first.state.completion_corrections_used == 1
    assert second.action is CompletionAction.TERMINAL
    assert second.state.status is RepairCompletionStatus.UNVERIFIED_FINAL
    assert workflow.get_state(run.run_id).completion_corrections_used == 1


def test_duplicate_completion_recovery_does_not_consume_correction_twice(
    tmp_path: Path,
) -> None:
    coordinator, workflow, run, authority = setup_coordinator(tmp_path)

    first = coordinator.evaluate_completion(
        run.run_id,
        answer_digest="3" * 64,
        context=CompletionContext(pending_approval=True),
        authority=authority,
    )
    recovered = coordinator.evaluate_completion(
        run.run_id,
        answer_digest="3" * 64,
        context=CompletionContext(pending_approval=True),
        authority=authority,
    )

    assert first.action is CompletionAction.CORRECT
    assert recovered.action is CompletionAction.CORRECT
    assert workflow.get_state(run.run_id).completion_corrections_used == 1


def test_strict_mode_has_zero_completion_corrections(tmp_path: Path) -> None:
    coordinator, workflow, run, authority = setup_coordinator(tmp_path)

    decision = coordinator.evaluate_completion(
        run.run_id,
        answer_digest="4" * 64,
        context=CompletionContext(),
        max_completion_corrections=0,
        authority=authority,
    )

    assert decision.action is CompletionAction.TERMINAL
    assert decision.state.status is RepairCompletionStatus.UNVERIFIED_FINAL
    assert workflow.get_state(run.run_id).completion_corrections_used == 0


def test_correction_feedback_never_prescribes_a_repair(tmp_path: Path) -> None:
    coordinator, _, run, authority = setup_coordinator(tmp_path)

    decision = coordinator.evaluate_completion(
        run.run_id,
        answer_digest="5" * 64,
        context=CompletionContext(pending_side_effect=True),
        authority=authority,
    )

    feedback = (decision.feedback or "").casefold()
    assert "pending side effect" in feedback
    for forbidden in ("edit_file", "run_tests", "src/", "root cause", "change the"):
        assert forbidden not in feedback
