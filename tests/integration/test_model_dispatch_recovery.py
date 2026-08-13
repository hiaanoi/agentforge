import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from agentforge.application.contracts import OutcomeStatus
from agentforge.application.run_driver import RunOwnership
from agentforge.domain.enums import (
    EventType,
    ModelAttemptStatus,
    ModelRecoveryAction,
    MultiToolResponsePolicy,
)
from agentforge.domain.models import Run
from agentforge.models.base import FinalAnswer, ModelRequest
from agentforge.models.domain import (
    ModelBudget,
    ModelErrorCode,
    ModelResponse,
    MultiToolResponseInfo,
)
from agentforge.models.errors import (
    ModelAttemptConflictError,
    ModelOutputInvalidError,
    ModelProtocolError,
    ModelRequestError,
    ProviderContractDeviationError,
)
from agentforge.models.executor import ModelExecutor
from agentforge.persistence.database import Database
from agentforge.persistence.model_workflow import ModelWorkflow
from agentforge.persistence.repositories import EventRepository, RunRepository
from agentforge.persistence.run_leases import RunLeaseStore
from agentforge.persistence.tables import ModelAttemptRow, ModelRuntimeStateRow


class CountingProvider:
    def __init__(self, *, block: bool = False) -> None:
        self.calls = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.block = block

    @property
    def name(self) -> str:
        return "recovery-provider"

    @property
    def journal_identity(self) -> str:
        return "recovery-provider/recovery-model"

    async def generate(self, request: ModelRequest) -> ModelResponse:
        del request
        self.calls += 1
        self.started.set()
        if self.block:
            await self.release.wait()
        return ModelResponse(
            action=FinalAnswer(type="final", answer="done"),
            provider=self.name,
            model="recovery-model",
            duration_ms=3,
            attempt_count=1,
        )


class FailingProvider:
    def __init__(self) -> None:
        self.calls = 0

    @property
    def name(self) -> str:
        return "recovery-provider"

    @property
    def journal_identity(self) -> str:
        return "recovery-provider/recovery-model"

    async def generate(self, request: ModelRequest) -> ModelResponse:
        del request
        self.calls += 1
        raise ModelRequestError(
            ModelErrorCode.MODEL_TIMEOUT, "safe timeout", retryable=True
        )


class RestartRecordingProvider:
    def __init__(
        self,
        calls: list[str],
        *,
        block: bool = False,
        started: asyncio.Event | None = None,
        release: asyncio.Event | None = None,
    ) -> None:
        self._calls = calls
        self._block = block
        self._started = started
        self._release = release

    @property
    def name(self) -> str:
        return "recovery-provider"

    @property
    def journal_identity(self) -> str:
        return "recovery-provider/recovery-model"

    async def generate(self, request: ModelRequest) -> ModelResponse:
        del request
        self._calls.append("generate")
        if self._started is not None:
            self._started.set()
        if self._block:
            assert self._release is not None
            await self._release.wait()
        return ModelResponse(
            action=FinalAnswer(type="final", answer="done"),
            provider=self.name,
            model="recovery-model",
            duration_ms=3,
            attempt_count=1,
        )


class RaisingRecoveryProvider:
    def __init__(self, error: Exception) -> None:
        self._error = error
        self.calls = 0

    @property
    def name(self) -> str:
        return "recovery-provider"

    @property
    def journal_identity(self) -> str:
        return "recovery-provider/recovery-model"

    async def generate(self, request: ModelRequest) -> ModelResponse:
        del request
        self.calls += 1
        raise self._error


class BlockingRecoveryErrorProvider(RaisingRecoveryProvider):
    def __init__(
        self,
        error: Exception,
        *,
        started: asyncio.Event,
        release: asyncio.Event,
    ) -> None:
        super().__init__(error)
        self._started = started
        self._release = release

    async def generate(self, request: ModelRequest) -> ModelResponse:
        del request
        self.calls += 1
        self._started.set()
        await self._release.wait()
        raise self._error


def recovery_error(kind: str) -> Exception:
    if kind == "request":
        return ModelRequestError(
            ModelErrorCode.MODEL_TIMEOUT,
            "safe timeout without sk-secret",
            retryable=True,
        )
    if kind == "deviation":
        return ProviderContractDeviationError(
            "provider contract deviation without sk-secret",
            MultiToolResponseInfo(
                provider="recovery-provider",
                model="recovery-model",
                returned_call_count=2,
                selected_call_count=0,
                discarded_call_count=2,
                discarded_tool_names=["one", "two"],
                policy=MultiToolResponsePolicy.STRICT,
                reason="strict_policy",
            ),
        )
    if kind == "protocol":
        return ModelProtocolError("protocol failure without sk-secret")
    if kind == "output":
        return ModelOutputInvalidError("invalid output without sk-secret")
    raise AssertionError(kind)


def harness(tmp_path: Path) -> tuple[Database, Run, ModelWorkflow, RunOwnership]:
    database = Database.from_path(tmp_path / "dispatch-recovery.sqlite3")
    database.create_schema()
    run = RunRepository(database).create(Run(task="recover model request"))
    workflow = ModelWorkflow(database)
    workflow._evaluator_only_ensure_state(
        run.run_id, ModelBudget(max_model_requests=3, max_retries=1)
    )
    lease = RunLeaseStore(database).acquire(
        run.run_id, owner_id="recovery", ttl=timedelta(seconds=30)
    )
    return database, run, workflow, RunOwnership(lambda: lease.authority)


def test_prepared_attempt_can_be_claimed_for_dispatch_after_restart(
    tmp_path: Path,
) -> None:
    database, run, workflow, ownership = harness(tmp_path)
    request = ModelRequest(task=run.task, step_number=1)
    attempt_id = workflow.prepare_attempt(
        run.run_id,
        uuid4(),
        1,
        request=request,
        provider_identity="recovery-provider/recovery-model",
        authority=ownership.authority,
    )

    action = workflow.recover_attempt(
        run.run_id,
        attempt_id,
        request=request,
        provider_identity="recovery-provider/recovery-model",
        authority=ownership.authority,
    )

    assert action.action is ModelRecoveryAction.DISPATCH
    assert action.attempt_number == 1
    assert workflow.list_attempts(run.run_id)[0].status is ModelAttemptStatus.DISPATCHING
    database.close()


def test_dispatching_attempt_becomes_indeterminate_without_resend(
    tmp_path: Path,
) -> None:
    database, run, workflow, ownership = harness(tmp_path)
    provider = CountingProvider()
    request = ModelRequest(task=run.task, step_number=1)
    attempt_id = workflow.prepare_attempt(
        run.run_id,
        uuid4(),
        1,
        request=request,
        provider_identity="recovery-provider/recovery-model",
        authority=ownership.authority,
    )
    assert workflow.claim_dispatch(
        run.run_id,
        attempt_id,
        request=request,
        provider_identity="recovery-provider/recovery-model",
        authority=ownership.authority,
    )

    action = workflow.recover_attempt(
        run.run_id,
        attempt_id,
        request=request,
        provider_identity="recovery-provider/recovery-model",
        authority=ownership.authority,
    )

    assert action.action is ModelRecoveryAction.MARK_INDETERMINATE
    assert action.attempt_number == 1
    assert provider.calls == 0
    assert workflow.list_attempts(run.run_id)[0].status is ModelAttemptStatus.INDETERMINATE
    assert EventRepository(database).list_for_run(run.run_id)[-1].event_type is (
        EventType.MODEL_ATTEMPT_INDETERMINATE
    )
    database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error_kind", "error_code", "retryable", "has_deviation_event"),
    [
        ("request", ModelErrorCode.MODEL_TIMEOUT, True, False),
        ("deviation", ModelErrorCode.MODEL_PROTOCOL_ERROR, True, True),
        ("protocol", ModelErrorCode.MODEL_PROTOCOL_ERROR, False, False),
        ("output", ModelErrorCode.MODEL_OUTPUT_INVALID, False, False),
    ],
)
async def test_recovered_prepared_provider_errors_persist_known_failed_outcome(
    tmp_path: Path,
    error_kind: str,
    error_code: ModelErrorCode,
    retryable: bool,
    has_deviation_event: bool,
) -> None:
    database, run, workflow, ownership = harness(tmp_path)
    request = ModelRequest(task=run.task, step_number=1)
    attempt_id = workflow.prepare_attempt(
        run.run_id,
        uuid4(),
        3,
        request=request,
        provider_identity="recovery-provider/recovery-model",
        authority=ownership.authority,
    )
    provider = RaisingRecoveryProvider(recovery_error(error_kind))
    executor = ModelExecutor(provider, workflow)

    with pytest.raises(ModelRequestError) as raised:
        await executor.recover(run, request, attempt_id, ownership=ownership)

    assert raised.value.code is error_code
    assert raised.value.retryable is retryable
    assert provider.calls == 1
    [attempt] = workflow.list_attempts(run.run_id)
    assert attempt.attempt_number == 3
    assert attempt.status is ModelAttemptStatus.FAILED
    assert attempt.error_type is error_code
    assert attempt.retryable is retryable
    events = EventRepository(database).list_for_run(run.run_id)
    assert [event.event_type for event in events] == [
        EventType.MODEL_ATTEMPT_PREPARED,
        EventType.MODEL_ATTEMPT_DISPATCHING,
        *(
            [EventType.MODEL_PROVIDER_DEVIATION]
            if has_deviation_event
            else []
        ),
        EventType.MODEL_ATTEMPT_FAILED,
    ]
    assert events[-1].payload == {
        "error_type": error_code.value,
        "retryable": retryable,
        "attempt": 3,
    }
    assert "sk-secret" not in " ".join(event.model_dump_json() for event in events)
    database.close()


@pytest.mark.asyncio
async def test_recovered_success_uses_persisted_attempt_number_in_response(
    tmp_path: Path,
) -> None:
    database, run, workflow, ownership = harness(tmp_path)
    request = ModelRequest(task=run.task, step_number=1)
    attempt_id = workflow.prepare_attempt(
        run.run_id,
        uuid4(),
        2,
        request=request,
        provider_identity="recovery-provider/recovery-model",
        authority=ownership.authority,
    )

    recovered = await ModelExecutor(
        CountingProvider(),
        workflow,
        provider_identity="recovery-provider/recovery-model",
    ).recover(run, request, attempt_id, ownership=ownership)

    assert recovered.outcome is OutcomeStatus.UNVERIFIED
    assert recovered.value is not None
    assert recovered.value.attempt_count == 2
    [attempt] = workflow.list_attempts(run.run_id)
    assert attempt.attempt_number == 2
    assert attempt.status is ModelAttemptStatus.COMPLETED
    database.close()


@pytest.mark.asyncio
async def test_recovery_failpoint_after_response_leaves_dispatching_attempt_three(
    tmp_path: Path,
) -> None:
    database, run, workflow, ownership = harness(tmp_path)
    request = ModelRequest(task=run.task, step_number=1)
    attempt_id = workflow.prepare_attempt(
        run.run_id,
        uuid4(),
        3,
        request=request,
        provider_identity="recovery-provider/recovery-model",
        authority=ownership.authority,
    )

    def crash(name: str) -> None:
        if name == "after_provider_response":
            raise RuntimeError("crash after recovered response")

    with pytest.raises(RuntimeError, match="crash after recovered response"):
        await ModelExecutor(
            CountingProvider(),
            workflow,
            provider_identity="recovery-provider/recovery-model",
            failpoint=crash,
        ).recover(run, request, attempt_id, ownership=ownership)

    [attempt] = workflow.list_attempts(run.run_id)
    assert attempt.attempt_number == 3
    assert attempt.status is ModelAttemptStatus.DISPATCHING
    database.close()


@pytest.mark.asyncio
async def test_recovered_protocol_failure_losing_terminal_cas_returns_unknown(
    tmp_path: Path,
) -> None:
    database, run, workflow, ownership = harness(tmp_path)
    request = ModelRequest(task=run.task, step_number=1)
    attempt_id = workflow.prepare_attempt(
        run.run_id,
        uuid4(),
        2,
        request=request,
        provider_identity="recovery-provider/recovery-model",
        authority=ownership.authority,
    )
    started = asyncio.Event()
    release = asyncio.Event()
    provider = BlockingRecoveryErrorProvider(
        ModelProtocolError("sensitive sk-secret"),
        started=started,
        release=release,
    )
    executor = ModelExecutor(provider, workflow)
    first = asyncio.create_task(
        executor.recover(run, request, attempt_id, ownership=ownership)
    )
    await started.wait()
    old_authority = ownership.authority
    leases = RunLeaseStore(database)
    leases.release(old_authority)
    replacement = leases.acquire(
        run.run_id,
        owner_id="protocol-takeover",
        ttl=timedelta(seconds=30),
    )
    competing = workflow.recover_attempt(
        run.run_id,
        attempt_id,
        request=request,
        provider_identity="recovery-provider/recovery-model",
        authority=replacement.authority,
    )
    assert competing.action is ModelRecoveryAction.MARK_INDETERMINATE
    release.set()

    result = await first

    assert result.outcome is OutcomeStatus.UNKNOWN
    assert result.value is None
    [attempt] = workflow.list_attempts(run.run_id)
    assert attempt.status is ModelAttemptStatus.INDETERMINATE
    assert attempt.attempt_number == 2
    events = EventRepository(database).list_for_run(run.run_id)
    assert sum(
        event.event_type is EventType.MODEL_ATTEMPT_INDETERMINATE
        for event in events
    ) == 1
    assert EventType.MODEL_ATTEMPT_FAILED not in {
        event.event_type for event in events
    }
    assert "sk-secret" not in " ".join(event.model_dump_json() for event in events)
    database.close()


def test_same_attempt_with_different_request_is_a_conflict(tmp_path: Path) -> None:
    database, run, workflow, ownership = harness(tmp_path)
    attempt_id = workflow.prepare_attempt(
        run.run_id,
        uuid4(),
        1,
        request=ModelRequest(task=run.task, step_number=1),
        provider_identity="recovery-provider/recovery-model",
        authority=ownership.authority,
    )

    with pytest.raises(ModelAttemptConflictError):
        workflow.recover_attempt(
            run.run_id,
            attempt_id,
            request=ModelRequest(task="different", step_number=1),
            provider_identity="recovery-provider/recovery-model",
            authority=ownership.authority,
        )
    database.close()


def test_same_attempt_and_request_prepare_replay_is_idempotent(
    tmp_path: Path,
) -> None:
    database, run, workflow, ownership = harness(tmp_path)
    request = ModelRequest(task=run.task, step_number=1)
    logical_call_id = uuid4()
    attempt_id = uuid4()

    first = workflow.prepare_attempt(
        run.run_id,
        logical_call_id,
        1,
        attempt_id=attempt_id,
        request=request,
        provider_identity="recovery-provider/recovery-model",
        authority=ownership.authority,
    )
    replay = workflow.prepare_attempt(
        run.run_id,
        logical_call_id,
        1,
        attempt_id=attempt_id,
        request=request,
        provider_identity="recovery-provider/recovery-model",
        authority=ownership.authority,
    )

    assert first == replay == attempt_id
    assert len(workflow.list_attempts(run.run_id)) == 1
    assert workflow.get_state(run.run_id).model_request_count == 1
    database.close()


def test_attempt_identity_replay_with_different_request_conflicts(
    tmp_path: Path,
) -> None:
    database, run, workflow, ownership = harness(tmp_path)
    logical_call_id = uuid4()
    attempt_id = uuid4()
    workflow.prepare_attempt(
        run.run_id,
        logical_call_id,
        1,
        attempt_id=attempt_id,
        request=ModelRequest(task=run.task, step_number=1),
        provider_identity="recovery-provider/recovery-model",
        authority=ownership.authority,
    )

    with pytest.raises(ModelAttemptConflictError):
        workflow.prepare_attempt(
            run.run_id,
            logical_call_id,
            1,
            attempt_id=attempt_id,
            request=ModelRequest(task="changed", step_number=1),
            provider_identity="recovery-provider/recovery-model",
            authority=ownership.authority,
        )
    assert workflow.get_state(run.run_id).model_request_count == 1
    database.close()


def test_same_attempt_with_changed_budget_identity_is_a_conflict(
    tmp_path: Path,
) -> None:
    database, run, workflow, ownership = harness(tmp_path)
    request = ModelRequest(task=run.task, step_number=1)
    attempt_id = workflow.prepare_attempt(
        run.run_id,
        uuid4(),
        1,
        request=request,
        provider_identity="recovery-provider/recovery-model",
        authority=ownership.authority,
    )
    with database.session() as session:
        state = session.get(ModelRuntimeStateRow, str(run.run_id))
        assert state is not None
        state.max_retries += 1

    with pytest.raises(ModelAttemptConflictError):
        workflow.recover_attempt(
            run.run_id,
            attempt_id,
            request=request,
            provider_identity="recovery-provider/recovery-model",
            authority=ownership.authority,
        )
    database.close()


@pytest.mark.asyncio
async def test_concurrent_recovery_dispatches_provider_at_most_once(
    tmp_path: Path,
) -> None:
    database, run, workflow, ownership = harness(tmp_path)
    provider = CountingProvider(block=True)
    executor = ModelExecutor(
        provider,
        workflow,
        provider_identity="recovery-provider/recovery-model",
    )
    request = ModelRequest(task=run.task, step_number=1)
    attempt_id = workflow.prepare_attempt(
        run.run_id,
        uuid4(),
        1,
        request=request,
        provider_identity="recovery-provider/recovery-model",
        authority=ownership.authority,
    )

    first = asyncio.create_task(
        executor.recover(run, request, attempt_id, ownership=ownership)
    )
    await provider.started.wait()
    second = asyncio.create_task(
        executor.recover(run, request, attempt_id, ownership=ownership)
    )
    second_result = await second
    provider.release.set()
    await first

    assert provider.calls == 1
    assert second_result.outcome is OutcomeStatus.UNKNOWN
    assert workflow.list_attempts(run.run_id)[0].status is (
        ModelAttemptStatus.INDETERMINATE
    )
    database.close()


def test_failure_before_prepared_commit_leaves_no_attempt_or_budget_charge(
    tmp_path: Path,
) -> None:
    database, run, _, ownership = harness(tmp_path)

    def crash(name: str) -> None:
        if name == "before_prepared_commit":
            raise RuntimeError("simulated crash")

    workflow = ModelWorkflow(database, failpoint=crash)
    with pytest.raises(RuntimeError, match="simulated crash"):
        workflow.prepare_attempt(
            run.run_id,
            uuid4(),
            1,
            request=ModelRequest(task=run.task, step_number=1),
            provider_identity="recovery-provider/recovery-model",
            authority=ownership.authority,
        )

    assert workflow.list_attempts(run.run_id) == []
    assert workflow.get_state(run.run_id).model_request_count == 0
    database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failpoint", "expected_status", "provider_calls"),
    [
        ("after_prepared_commit", ModelAttemptStatus.PREPARED, 0),
        ("after_dispatching_commit", ModelAttemptStatus.DISPATCHING, 0),
        ("after_provider_response", ModelAttemptStatus.DISPATCHING, 1),
    ],
)
async def test_executor_crash_windows_preserve_safe_recovery_boundary(
    tmp_path: Path,
    failpoint: str,
    expected_status: ModelAttemptStatus,
    provider_calls: int,
) -> None:
    database, run, workflow, ownership = harness(tmp_path)
    provider = CountingProvider()

    def crash(name: str) -> None:
        if name == failpoint:
            raise RuntimeError("simulated crash")

    executor = ModelExecutor(
        provider,
        workflow,
        provider_identity="recovery-provider/recovery-model",
        failpoint=crash,
    )
    request = ModelRequest(task=run.task, step_number=1)
    with pytest.raises(RuntimeError, match="simulated crash"):
        await executor.generate(run, request, ownership=ownership)

    [attempt] = workflow.list_attempts(run.run_id)
    assert attempt.status is expected_status
    assert provider.calls == provider_calls
    recovered = await ModelExecutor(
        provider,
        workflow,
        provider_identity="recovery-provider/recovery-model",
    ).recover(run, request, attempt.attempt_id, ownership=ownership)
    if expected_status is ModelAttemptStatus.PREPARED:
        assert recovered.outcome is OutcomeStatus.UNVERIFIED
        assert recovered.value is not None
        assert provider.calls == 1
        assert workflow.list_attempts(run.run_id)[0].status is (
            ModelAttemptStatus.COMPLETED
        )
    else:
        assert recovered.outcome is OutcomeStatus.UNKNOWN
        assert recovered.value is None
        assert provider.calls == provider_calls
        assert workflow.list_attempts(run.run_id)[0].status is (
            ModelAttemptStatus.INDETERMINATE
        )
    assert workflow.get_state(run.run_id).model_request_count == 1
    database.close()


@pytest.mark.asyncio
async def test_terminal_replay_never_dispatches(tmp_path: Path) -> None:
    database, run, workflow, ownership = harness(tmp_path)
    provider = CountingProvider()
    executor = ModelExecutor(
        provider,
        workflow,
        provider_identity="recovery-provider/recovery-model",
    )
    request = ModelRequest(task=run.task, step_number=1)
    await executor.generate(run, request, ownership=ownership)
    [attempt] = workflow.list_attempts(run.run_id)

    replay = await executor.recover(
        run, request, attempt.attempt_id, ownership=ownership
    )

    assert replay.outcome is OutcomeStatus.UNKNOWN
    assert replay.value is None
    assert provider.calls == 1
    assert workflow.get_state(run.run_id).model_request_count == 1
    database.close()


@pytest.mark.asyncio
async def test_crash_after_provider_error_leaves_uncertain_dispatch_for_recovery(
    tmp_path: Path,
) -> None:
    database, run, workflow, ownership = harness(tmp_path)
    provider = FailingProvider()

    def crash(name: str) -> None:
        if name == "after_provider_error":
            raise RuntimeError("simulated crash")

    request = ModelRequest(task=run.task, step_number=1)
    executor = ModelExecutor(
        provider,
        workflow,
        provider_identity="recovery-provider/recovery-model",
        failpoint=crash,
    )
    with pytest.raises(RuntimeError, match="simulated crash"):
        await executor.generate(run, request, ownership=ownership)
    [attempt] = workflow.list_attempts(run.run_id)
    assert attempt.status is ModelAttemptStatus.DISPATCHING

    recovered = await ModelExecutor(
        provider,
        workflow,
        provider_identity="recovery-provider/recovery-model",
    ).recover(run, request, attempt.attempt_id, ownership=ownership)
    assert recovered.outcome is OutcomeStatus.UNKNOWN
    assert provider.calls == 1
    assert workflow.list_attempts(run.run_id)[0].status is (
        ModelAttemptStatus.INDETERMINATE
    )
    database.close()


def test_request_digest_rejects_nan_and_type_drift(tmp_path: Path) -> None:
    database, run, workflow, ownership = harness(tmp_path)
    invalid_number = ModelRequest(task=run.task, step_number=1, history=[float("nan")])
    type_drift = ModelRequest.model_construct(task=run.task, step_number="1")

    with pytest.raises(ValueError):
        workflow.prepare_attempt(
            run.run_id,
            uuid4(),
            1,
            request=invalid_number,
            provider_identity="recovery-provider/recovery-model",
            authority=ownership.authority,
        )
    with pytest.raises(ValueError):
        workflow.prepare_attempt(
            run.run_id,
            uuid4(),
            1,
            request=type_drift,
            provider_identity="recovery-provider/recovery-model",
            authority=ownership.authority,
        )
    assert workflow.list_attempts(run.run_id) == []
    database.close()


def test_persisted_attempt_and_events_never_store_request_or_secret(
    tmp_path: Path,
) -> None:
    database, run, workflow, ownership = harness(tmp_path)
    secret = "sk-must-not-persist"
    request = ModelRequest(
        task=f"private prompt {secret}",
        step_number=1,
        history=[{"api_key": secret}],
    )
    attempt_id = workflow.prepare_attempt(
        run.run_id,
        uuid4(),
        1,
        request=request,
        provider_identity="recovery-provider/recovery-model",
        authority=ownership.authority,
    )

    with database.session() as session:
        row = session.get(ModelAttemptRow, str(attempt_id))
        assert row is not None
        persisted = " ".join(str(value) for value in vars(row).values())
    events = EventRepository(database).list_for_run(run.run_id)
    assert secret not in persisted
    assert secret not in " ".join(event.model_dump_json() for event in events)
    database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("crash_window", "expected_calls", "expected_outcome", "expected_status"),
    [
        (
            "prepared",
            1,
            OutcomeStatus.UNVERIFIED,
            ModelAttemptStatus.COMPLETED,
        ),
        (
            "dispatching_before_provider",
            0,
            OutcomeStatus.UNKNOWN,
            ModelAttemptStatus.INDETERMINATE,
        ),
        (
            "after_provider_response",
            1,
            OutcomeStatus.UNKNOWN,
            ModelAttemptStatus.INDETERMINATE,
        ),
    ],
)
async def test_restart_recovery_uses_new_database_owner_and_executor(
    tmp_path: Path,
    crash_window: str,
    expected_calls: int,
    expected_outcome: OutcomeStatus,
    expected_status: ModelAttemptStatus,
) -> None:
    path = tmp_path / f"real-restart-{crash_window}.sqlite3"
    now = [datetime.now(UTC) + timedelta(minutes=1)]
    old_database = Database.from_path(path)
    old_database.create_schema()
    run = RunRepository(old_database).create(Run(task="real provider restart"))
    old_workflow = ModelWorkflow(old_database)
    old_workflow._evaluator_only_ensure_state(run.run_id, ModelBudget())
    old_lease = RunLeaseStore(old_database, clock=lambda: now[0]).acquire(
        run.run_id, owner_id="old-worker", ttl=timedelta(seconds=1)
    )
    request = ModelRequest(task=run.task, step_number=1)
    attempt_number = 3 if crash_window == "after_provider_response" else 2
    attempt_id = old_workflow.prepare_attempt(
        run.run_id,
        uuid4(),
        attempt_number,
        request=request,
        provider_identity="recovery-provider/recovery-model",
        authority=old_lease.authority,
    )
    calls: list[str] = []
    if crash_window == "dispatching_before_provider":
        assert old_workflow.claim_dispatch(
            run.run_id,
            attempt_id,
            request=request,
            provider_identity="recovery-provider/recovery-model",
            authority=old_lease.authority,
        )
    elif crash_window == "after_provider_response":
        def crash(name: str) -> None:
            if name == "after_provider_response":
                raise RuntimeError("simulated recovered response crash")

        with pytest.raises(RuntimeError, match="simulated recovered response crash"):
            await ModelExecutor(
                RestartRecordingProvider(calls),
                old_workflow,
                failpoint=crash,
            ).recover(
                run,
                request,
                attempt_id,
                ownership=RunOwnership(lambda: old_lease.authority),
            )
    old_database.close()

    now[0] += timedelta(seconds=2)
    restarted_database = Database.from_path(path)
    restarted_database.create_schema()
    restarted_lease = RunLeaseStore(
        restarted_database, clock=lambda: now[0]
    ).acquire(run.run_id, owner_id="replacement-worker", ttl=timedelta(seconds=30))
    restarted_workflow = ModelWorkflow(restarted_database)
    restarted_executor = ModelExecutor(
        RestartRecordingProvider(calls), restarted_workflow
    )
    recovered = await restarted_executor.recover(
        run,
        request,
        attempt_id,
        ownership=RunOwnership(lambda: restarted_lease.authority),
    )

    [attempt] = restarted_workflow.list_attempts(run.run_id)
    assert recovered.outcome is expected_outcome
    if recovered.value is not None:
        assert recovered.value.attempt_count == attempt_number
    assert calls.count("generate") == expected_calls
    assert attempt.attempt_number == attempt_number
    assert attempt.status is expected_status
    events = EventRepository(restarted_database).list_for_run(run.run_id)
    indeterminate_events = [
        event
        for event in events
        if event.event_type is EventType.MODEL_ATTEMPT_INDETERMINATE
    ]
    assert len(indeterminate_events) == (
        1 if expected_status is ModelAttemptStatus.INDETERMINATE else 0
    )
    if indeterminate_events:
        assert indeterminate_events[0].payload == {
            "attempt_id": str(attempt_id),
            "attempt": attempt_number,
            "reason": "PROVIDER_DISPATCH_OUTCOME_UNKNOWN",
        }
    restarted_database.close()


@pytest.mark.asyncio
async def test_concurrent_restart_recovery_has_at_most_one_dispatch_winner(
    tmp_path: Path,
) -> None:
    path = tmp_path / "concurrent-real-restart.sqlite3"
    now = [datetime.now(UTC) + timedelta(minutes=1)]
    old_database = Database.from_path(path)
    old_database.create_schema()
    run = RunRepository(old_database).create(Run(task="concurrent restart"))
    old_workflow = ModelWorkflow(old_database)
    old_workflow._evaluator_only_ensure_state(run.run_id, ModelBudget())
    old_lease = RunLeaseStore(old_database, clock=lambda: now[0]).acquire(
        run.run_id, owner_id="old-worker", ttl=timedelta(seconds=1)
    )
    request = ModelRequest(task=run.task, step_number=1)
    attempt_id = old_workflow.prepare_attempt(
        run.run_id,
        uuid4(),
        1,
        request=request,
        provider_identity="recovery-provider/recovery-model",
        authority=old_lease.authority,
    )
    old_database.close()

    now[0] += timedelta(seconds=2)
    first_database = Database.from_path(path)
    second_database = Database.from_path(path)
    first_database.create_schema()
    second_database.create_schema()
    replacement = RunLeaseStore(first_database, clock=lambda: now[0]).acquire(
        run.run_id, owner_id="replacement-worker", ttl=timedelta(seconds=30)
    )
    ownership = RunOwnership(lambda: replacement.authority)
    calls: list[str] = []
    started = asyncio.Event()
    release = asyncio.Event()
    first_executor = ModelExecutor(
        RestartRecordingProvider(
            calls, block=True, started=started, release=release
        ),
        ModelWorkflow(first_database),
    )
    second_executor = ModelExecutor(
        RestartRecordingProvider(calls), ModelWorkflow(second_database)
    )

    first = asyncio.create_task(
        first_executor.recover(run, request, attempt_id, ownership=ownership)
    )
    await started.wait()
    second = asyncio.create_task(
        second_executor.recover(run, request, attempt_id, ownership=ownership)
    )
    second_result = await second
    release.set()
    first_result = await first

    assert calls == ["generate"]
    assert second_result.outcome is OutcomeStatus.UNKNOWN
    assert first_result.outcome is OutcomeStatus.UNKNOWN
    [attempt] = ModelWorkflow(first_database).list_attempts(run.run_id)
    assert attempt.status is ModelAttemptStatus.INDETERMINATE
    first_database.close()
    second_database.close()
