from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from agentforge.domain.models import Run
from agentforge.models.base import ModelRequest
from agentforge.models.domain import ModelBudget, ModelErrorCode, ModelUsage
from agentforge.models.errors import ModelRequestError
from agentforge.persistence.database import Database
from agentforge.persistence.event_log import RunLeaseAuthority
from agentforge.persistence.model_workflow import ModelWorkflow
from agentforge.persistence.repositories import RunRepository
from agentforge.persistence.run_leases import RunLeaseStore
from agentforge.persistence.tables import ModelAttemptRow


def _workflow(
    tmp_path: Path,
) -> tuple[Database, ModelWorkflow, Run, RunLeaseAuthority]:
    database = Database.from_path(tmp_path / "attempts.sqlite3")
    database.create_schema()
    run = RunRepository(database).create(Run(task="attempt query"))
    workflow = ModelWorkflow(database)
    workflow._evaluator_only_ensure_state(run.run_id, ModelBudget(max_model_requests=10))
    authority = RunLeaseStore(database).acquire(
        run.run_id, owner_id="attempt-query", ttl=timedelta(seconds=30)
    ).authority
    return database, workflow, run, authority


def _prepare(
    workflow: ModelWorkflow,
    run: Run,
    authority: RunLeaseAuthority,
    logical_call_id: UUID,
    attempt_number: int,
) -> UUID:
    request = ModelRequest(task=run.task, step_number=1)
    attempt_id = workflow.prepare_attempt(
        run.run_id,
        logical_call_id,
        attempt_number,
        request=request,
        provider_identity="query-provider/model",
        authority=authority,
    )
    return attempt_id


def test_list_attempts_returns_stable_chronological_order(tmp_path: Path) -> None:
    database, workflow, run, authority = _workflow(tmp_path)
    first_logical_call = uuid4()
    second_logical_call = uuid4()
    first_id = _prepare(workflow, run, authority, first_logical_call, 1)
    second_id = _prepare(workflow, run, authority, first_logical_call, 2)
    third_id = _prepare(workflow, run, authority, second_logical_call, 1)

    attempts = workflow.list_attempts(run.run_id)

    assert [attempt.attempt_id for attempt in attempts] == [
        first_id,
        second_id,
        third_id,
    ]
    assert [attempt.attempt_number for attempt in attempts] == [1, 2, 1]
    database.close()


def test_completed_attempt_exposes_only_sanitized_usage_and_duration(
    tmp_path: Path,
) -> None:
    database, workflow, run, authority = _workflow(tmp_path)
    attempt_id = _prepare(workflow, run, authority, uuid4(), 1)
    request = ModelRequest(task=run.task, step_number=1)
    assert workflow.claim_dispatch(
        run.run_id,
        attempt_id,
        request=request,
        provider_identity="query-provider/model",
        authority=authority,
    )
    usage = ModelUsage(
        input_tokens=12,
        output_tokens=5,
        total_tokens=17,
        cached_input_tokens=3,
        reasoning_tokens=2,
    )
    workflow.complete_attempt(
        run.run_id,
        attempt_id,
        usage,
        duration_ms=41,
        authority=authority,
    )

    [attempt] = workflow.list_attempts(run.run_id)

    assert attempt.status == "COMPLETED"
    assert attempt.duration_ms == 41
    assert attempt.usage == usage
    assert attempt.error_type is None
    assert attempt.retryable is None
    assert attempt.completed_at is not None
    assert set(attempt.model_dump(mode="json")) == {
        "attempt_id",
        "run_id",
        "logical_call_id",
        "attempt_number",
        "status",
        "request_digest",
        "provider_identity",
        "budget_digest",
        "error_type",
        "retryable",
        "duration_ms",
        "usage",
        "created_at",
        "dispatched_at",
        "completed_at",
    }
    database.close()


def test_failed_attempt_exposes_code_retryability_and_timestamp_elapsed_time(
    tmp_path: Path,
) -> None:
    database, workflow, run, authority = _workflow(tmp_path)
    attempt_id = _prepare(workflow, run, authority, uuid4(), 1)
    request = ModelRequest(task=run.task, step_number=1)
    assert workflow.claim_dispatch(
        run.run_id,
        attempt_id,
        request=request,
        provider_identity="query-provider/model",
        authority=authority,
    )
    workflow.fail_attempt(
        run.run_id,
        attempt_id,
        ModelRequestError(
            ModelErrorCode.MODEL_TIMEOUT,
            "provider details must not be persisted in the public fact",
            retryable=True,
        ),
        authority=authority,
    )
    started = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    with database.session() as session:
        row = session.get(ModelAttemptRow, str(attempt_id))
        assert row is not None
        row.created_at = started
        row.completed_at = started + timedelta(milliseconds=137)
        row.duration_ms = None

    [attempt] = workflow.list_attempts(run.run_id)

    assert attempt.status == "FAILED"
    assert attempt.error_type is ModelErrorCode.MODEL_TIMEOUT
    assert attempt.retryable is True
    assert attempt.duration_ms == 137
    assert attempt.usage is None
    dumped = attempt.model_dump_json()
    assert "provider details" not in dumped
    assert "provider_request_id" not in dumped
    assert "api_key" not in dumped
    database.close()


def test_list_attempts_isolated_by_run(tmp_path: Path) -> None:
    database, workflow, first_run, authority = _workflow(tmp_path)
    second_run = RunRepository(database).create(Run(task="other run"))
    workflow._evaluator_only_ensure_state(
        second_run.run_id, ModelBudget(max_model_requests=10)
    )
    second_authority = RunLeaseStore(database).acquire(
        second_run.run_id, owner_id="second-query", ttl=timedelta(seconds=30)
    ).authority
    first_id = _prepare(workflow, first_run, authority, uuid4(), 1)
    _prepare(workflow, second_run, second_authority, uuid4(), 1)

    attempts = workflow.list_attempts(first_run.run_id)

    assert [attempt.attempt_id for attempt in attempts] == [first_id]
    assert all(attempt.run_id == first_run.run_id for attempt in attempts)
    database.close()


def test_query_projection_exposes_all_five_closed_attempt_states(
    tmp_path: Path,
) -> None:
    database, workflow, run, authority = _workflow(tmp_path)
    request = ModelRequest(task=run.task, step_number=1)
    prepared = _prepare(workflow, run, authority, uuid4(), 1)
    dispatching = _prepare(workflow, run, authority, uuid4(), 1)
    completed = _prepare(workflow, run, authority, uuid4(), 1)
    failed = _prepare(workflow, run, authority, uuid4(), 1)
    indeterminate = _prepare(workflow, run, authority, uuid4(), 1)
    for attempt_id in (dispatching, completed, failed, indeterminate):
        assert workflow.claim_dispatch(
            run.run_id,
            attempt_id,
            request=request,
            provider_identity="query-provider/model",
            authority=authority,
        )
    workflow.complete_attempt(
        run.run_id, completed, None, 1, authority=authority
    )
    workflow.fail_attempt(
        run.run_id,
        failed,
        ModelRequestError(
            ModelErrorCode.MODEL_TIMEOUT, "timeout", retryable=True
        ),
        authority=authority,
    )
    assert workflow.recover_attempt(
        run.run_id,
        indeterminate,
        request=request,
        provider_identity="query-provider/model",
        authority=authority,
    ).action.value == "MARK_INDETERMINATE"

    by_id = {attempt.attempt_id: attempt for attempt in workflow.list_attempts(run.run_id)}
    assert [by_id[item].status.value for item in (
        prepared, dispatching, completed, failed, indeterminate
    )] == ["PREPARED", "DISPATCHING", "COMPLETED", "FAILED", "INDETERMINATE"]
    database.close()
