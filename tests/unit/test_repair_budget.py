from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from agentforge.application.kernel_errors import StaleFenceError
from agentforge.domain.models import Run
from agentforge.domain.repair import (
    BudgetKind,
    BudgetProfile,
    RepairCompletionStatus,
    RepairDifficulty,
    RepairTaskPolicy,
    RepairTerminationReason,
)
from agentforge.persistence.database import Database
from agentforge.persistence.repair_workflow import RepairWorkflow
from agentforge.persistence.repositories import EventRepository, RunRepository
from agentforge.persistence.run_leases import RunLeaseStore


def make_policy(**overrides: object) -> RepairTaskPolicy:
    values: dict[str, object] = {
        "task_id": "budget-task",
        "policy_version": 1,
        "difficulty": RepairDifficulty.BASIC,
        "budget_profile": BudgetProfile.BASIC,
        "allowed_write_paths": ("src/**",),
        "forbidden_write_paths": (),
        "protected_paths": ("tests/**",),
        "allowed_development_test_profiles": ("unit",),
        "final_verification_profile_id": "hidden",
        "allow_file_creation": False,
        "allowed_create_paths": (),
        "max_created_files": 0,
        "max_changed_files": 2,
        "max_total_changed_bytes": 1024,
        "max_single_file_changed_bytes": 1024,
        "path_case_sensitive": False,
    }
    values.update(overrides)
    return RepairTaskPolicy.model_validate(values)


@pytest.fixture
def workflow(tmp_path: Path) -> tuple[RepairWorkflow, Run, EventRepository, Database]:
    database = Database.from_path(tmp_path / "repair.db")
    database.create_schema()
    runs = RunRepository(database)
    events = EventRepository(database)
    run = runs.create(Run(task="repair a bug"))
    return RepairWorkflow(database), run, events, database


def test_start_binds_immutable_policy_and_initial_state(
    workflow: tuple[RepairWorkflow, Run, EventRepository, Database],
) -> None:
    repair, run, _, _ = workflow
    policy = make_policy()

    state = repair._evaluator_only_start(
        run.run_id,
        policy,
        baseline_id=uuid4(),
        baseline_digest="a" * 64,
    )

    assert repair.get_policy(run.run_id) == policy
    assert state.policy_digest == policy.policy_digest
    assert state.status is RepairCompletionStatus.RUNNING
    assert state.state_version == 1
    assert state.model_calls_used == 0
    assert state.edit_attempts_used == 0


def test_repeated_start_is_idempotent_but_policy_drift_conflicts(
    workflow: tuple[RepairWorkflow, Run, EventRepository, Database],
) -> None:
    repair, run, _, _ = workflow
    baseline_id = uuid4()
    policy = make_policy()

    first = repair._evaluator_only_start(run.run_id, policy, baseline_id, "b" * 64)
    second = repair._evaluator_only_start(run.run_id, policy, baseline_id, "b" * 64)

    assert second == first
    with pytest.raises(RuntimeError, match="policy binding"):
        repair._evaluator_only_start(
            run.run_id,
            make_policy(task_id="different"),
            baseline_id,
            "b" * 64,
        )


def test_budget_fact_is_counted_once_across_duplicate_resume(
    workflow: tuple[RepairWorkflow, Run, EventRepository, Database],
) -> None:
    repair, run, events, database = workflow
    repair._evaluator_only_start(run.run_id, make_policy(), uuid4(), "c" * 64)
    lease = RunLeaseStore(database).acquire(
        run.run_id, owner_id="test", ttl=timedelta(seconds=30)
    )
    fact_id = str(uuid4())

    first = repair.consume_budget(
        run.run_id, BudgetKind.EDIT, fact_id, authority=lease.authority
    )
    duplicate = repair.consume_budget(
        run.run_id, BudgetKind.EDIT, fact_id, authority=lease.authority
    )

    assert first.consumed
    assert not duplicate.consumed
    assert duplicate.state.edit_attempts_used == 1
    consumed_events = [
        event
        for event in events.list_for_run(run.run_id)
        if event.event_type.value == "REPAIR_BUDGET_CONSUMED"
    ]
    assert len(consumed_events) == 1


def test_budget_limit_fails_closed_without_counting_an_extra_fact(
    workflow: tuple[RepairWorkflow, Run, EventRepository, Database],
) -> None:
    repair, run, _, database = workflow
    repair._evaluator_only_start(run.run_id, make_policy(), uuid4(), "d" * 64)
    lease = RunLeaseStore(database).acquire(
        run.run_id, owner_id="test", ttl=timedelta(seconds=30)
    )
    repair.consume_budget(
        run.run_id, BudgetKind.EDIT, "edit-1", authority=lease.authority
    )
    repair.consume_budget(
        run.run_id, BudgetKind.EDIT, "edit-2", authority=lease.authority
    )

    denied = repair.consume_budget(
        run.run_id, BudgetKind.EDIT, "edit-3", authority=lease.authority
    )

    assert not denied.consumed
    assert denied.exhausted
    assert denied.state.edit_attempts_used == 2
    assert denied.state.status is RepairCompletionStatus.BUDGET_EXHAUSTED
    assert denied.state.failure_reason is RepairTerminationReason.EDIT_LIMIT


def test_state_update_uses_compare_and_swap(
    workflow: tuple[RepairWorkflow, Run, EventRepository, Database],
) -> None:
    repair, run, _, database = workflow
    state = repair._evaluator_only_start(run.run_id, make_policy(), uuid4(), "e" * 64)
    lease = RunLeaseStore(database).acquire(
        run.run_id, owner_id="test", ttl=timedelta(seconds=30)
    )

    updated = repair.mark_final_answer_received(
        run.run_id, state.state_version, authority=lease.authority
    )

    assert updated.final_answer_received
    assert updated.state_version == state.state_version + 1
    with pytest.raises(RuntimeError, match="state version"):
        repair.mark_final_answer_received(
            run.run_id, state.state_version, authority=lease.authority
        )


def test_stale_authority_cannot_update_repair_state(tmp_path: Path) -> None:
    database = Database.from_path(tmp_path / "stale-repair.db")
    database.create_schema()
    run = RunRepository(database).create(Run(task="repair stale fence"))
    repair = RepairWorkflow(database)
    state = repair._evaluator_only_start(run.run_id, make_policy(), uuid4(), "e" * 64)
    leases = RunLeaseStore(database)
    stale = leases.acquire(run.run_id, owner_id="stale", ttl=timedelta(seconds=30))
    leases.release(stale.authority)
    leases.acquire(run.run_id, owner_id="replacement", ttl=timedelta(seconds=30))

    with pytest.raises(StaleFenceError):
        repair.mark_final_answer_received(
            run.run_id, state.state_version, authority=stale.authority
        )

    assert repair.get_state(run.run_id).final_answer_received is False
    database.close()


def test_higher_priority_terminal_status_cannot_be_overwritten(
    workflow: tuple[RepairWorkflow, Run, EventRepository, Database],
) -> None:
    repair, run, _, database = workflow
    state = repair._evaluator_only_start(run.run_id, make_policy(), uuid4(), "f" * 64)
    lease = RunLeaseStore(database).acquire(
        run.run_id, owner_id="test", ttl=timedelta(seconds=30)
    )
    indeterminate = repair.transition_terminal(
        run.run_id,
        expected_version=state.state_version,
        status=RepairCompletionStatus.INDETERMINATE,
        reason=RepairTerminationReason.INDETERMINATE_SIDE_EFFECT,
        authority=lease.authority,
    )

    preserved = repair.transition_terminal(
        run.run_id,
        expected_version=indeterminate.state_version,
        status=RepairCompletionStatus.CANCELLED,
        reason=RepairTerminationReason.CANCELLED,
        authority=lease.authority,
    )

    assert preserved.status is RepairCompletionStatus.INDETERMINATE
    assert preserved.failure_reason is RepairTerminationReason.INDETERMINATE_SIDE_EFFECT
    assert preserved.state_version == indeterminate.state_version
