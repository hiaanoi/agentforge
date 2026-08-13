from pathlib import Path
from uuid import uuid4

import pytest

from agentforge.domain.models import Run
from agentforge.domain.test_execution import TestExecutionPlan as ManagedTestExecutionPlan
from agentforge.evaluation.baseline_models import (
    BaselineExecutionRecord,
    BaselineExecutionStatus,
    BaselineFailureReason,
    ExpectedBaselineFailure,
)
from agentforge.evaluation.baseline_persistence import (
    BaselineExecutionRepository,
    BaselineExecutionWorkflow,
)
from agentforge.persistence.database import Database
from agentforge.persistence.repositories import EventRepository, RunRepository

SHA = "a" * 64


def plan() -> ManagedTestExecutionPlan:
    return ManagedTestExecutionPlan(
        profile_id="visible",
        profile_version=1,
        profile_digest=SHA,
        executable_path="C:\\Python314\\python.exe",
        argv_digest=SHA,
        cwd="C:\\workspace",
        environment_digest=SHA,
    )


def record(run_id: object, *, task_id: str = "task-one") -> BaselineExecutionRecord:
    from uuid import UUID

    assert isinstance(run_id, UUID)
    return BaselineExecutionRecord.create(
        run_id=run_id,
        task_id=task_id,
        workspace_baseline_id=uuid4(),
        initial_workspace_digest=SHA,
        test_plan=plan(),
        expected_failure=ExpectedBaselineFailure(
            failed_node_ids=("tests/visible/test_flow.py::test_once",)
        ),
    )


def setup_database(tmp_path: Path) -> tuple[Database, Run]:
    database = Database.from_path(tmp_path / "baseline.db")
    database.create_schema()
    run = RunRepository(database).create(Run(task="repair"))
    return database, run


def test_repository_round_trips_and_rejects_identity_conflict(tmp_path: Path) -> None:
    database, run = setup_database(tmp_path)
    repository = BaselineExecutionRepository(database)
    original = record(run.run_id)

    assert repository.create(original) == original
    assert repository.get_for_run(run.run_id) == original
    assert repository.create(original) == original
    with pytest.raises(RuntimeError, match="identity conflict"):
        repository.create(record(run.run_id, task_id="different"))


def test_workflow_claims_once_and_terminal_state_is_immutable(tmp_path: Path) -> None:
    database, run = setup_database(tmp_path)
    repository = BaselineExecutionRepository(database)
    workflow = BaselineExecutionWorkflow(database)
    original = repository.create(record(run.run_id))

    started = workflow.claim(run.run_id, expected_version=original.record_version)
    assert started is not None
    assert started.status is BaselineExecutionStatus.STARTED
    assert workflow.claim(run.run_id, expected_version=original.record_version) is None

    blocked = workflow.finish(
        run.run_id,
        expected_version=started.record_version,
        status=BaselineExecutionStatus.BLOCKED,
        failure_reason=BaselineFailureReason.UNEXPECTED_PASS,
        exit_code=0,
    )
    assert blocked.status is BaselineExecutionStatus.BLOCKED
    with pytest.raises(RuntimeError, match="terminal transition"):
        workflow.finish(
            run.run_id,
            expected_version=blocked.record_version,
            status=BaselineExecutionStatus.INDETERMINATE,
            failure_reason=BaselineFailureReason.PROCESS_OUTCOME_INDETERMINATE,
        )


def test_recover_started_marks_indeterminate_and_audits(tmp_path: Path) -> None:
    database, run = setup_database(tmp_path)
    repository = BaselineExecutionRepository(database)
    workflow = BaselineExecutionWorkflow(database)
    created = repository.create(record(run.run_id))
    workflow.claim(run.run_id, expected_version=created.record_version)

    recovered = workflow.recover(run.run_id)

    assert recovered.status is BaselineExecutionStatus.INDETERMINATE
    assert recovered.failure_reason is BaselineFailureReason.PROCESS_OUTCOME_INDETERMINATE
    events = EventRepository(database).list_for_run(run.run_id)
    assert events[-1].event_type.value == "EVALUATION_BASELINE_INDETERMINATE"
