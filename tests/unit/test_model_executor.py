import asyncio
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from agentforge.application.kernel_errors import StaleFenceError
from agentforge.application.run_driver import RunDriver, RunOwnership
from agentforge.domain.enums import EventType, MultiToolResponsePolicy
from agentforge.domain.models import Run
from agentforge.models.base import FinalAnswer, ModelRequest
from agentforge.models.domain import (
    ModelBudget,
    ModelErrorCode,
    ModelResponse,
    ModelUsage,
    MultiToolResponseInfo,
)
from agentforge.models.errors import (
    ModelProtocolError,
    ModelRequestError,
    ProviderContractDeviationError,
)
from agentforge.models.executor import ModelExecutor
from agentforge.persistence.database import Database
from agentforge.persistence.event_log import RunLeaseAuthority
from agentforge.persistence.model_workflow import ModelWorkflow
from agentforge.persistence.product_tables import RunLeaseRow
from agentforge.persistence.repositories import EventRepository, RunRepository
from agentforge.persistence.run_leases import RunLeaseStore


class SequenceProvider:
    def __init__(self, outcomes: list[object]) -> None:
        self._outcomes = outcomes
        self.calls = 0

    @property
    def name(self) -> str:
        return "sequence"

    @property
    def journal_identity(self) -> str:
        return "sequence/test"

    async def generate(self, request: ModelRequest) -> ModelResponse:
        del request
        outcome = self._outcomes[self.calls]
        self.calls += 1
        if isinstance(outcome, BaseException):
            raise outcome
        assert isinstance(outcome, ModelResponse)
        return outcome


class BlockingProvider:
    def __init__(self) -> None:
        self.started = asyncio.Event()

    @property
    def name(self) -> str:
        return "blocking"

    @property
    def journal_identity(self) -> str:
        return "blocking/test"

    async def generate(self, request: ModelRequest) -> ModelResponse:
        del request
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class SlowSequenceProvider(SequenceProvider):
    def __init__(self, outcomes: list[object], *, delay: float = 0.06) -> None:
        super().__init__(outcomes)
        self._delay = delay

    async def generate(self, request: ModelRequest) -> ModelResponse:
        await asyncio.sleep(self._delay)
        return await super().generate(request)


def response() -> ModelResponse:
    return ModelResponse(
        action=FinalAnswer(type="final", answer="done"),
        usage=ModelUsage(input_tokens=10, output_tokens=4, total_tokens=14),
        provider="sequence",
        model="test",
        duration_ms=5,
        attempt_count=1,
    )


def fixed_ownership(authority: RunLeaseAuthority) -> RunOwnership:
    return RunOwnership(lambda: authority)


def deviation() -> ProviderContractDeviationError:
    return ProviderContractDeviationError(
        "Provider returned multiple function calls",
        MultiToolResponseInfo(
            provider="openai",
            model="test-model",
            provider_request_id="resp_deviation",
            returned_call_count=2,
            selected_call_count=0,
            discarded_call_count=2,
            selected_tool_name=None,
            discarded_tool_names=["read_file", "search_text"],
            policy=MultiToolResponsePolicy.STRICT,
            reason="strict_policy",
        ),
    )


def make_harness(
    tmp_path: Path,
    provider: SequenceProvider,
    *,
    budget: ModelBudget | None = None,
    sleep_calls: list[float] | None = None,
) -> tuple[ModelExecutor, Run, ModelWorkflow, EventRepository, Database]:
    database = Database.from_path(tmp_path / "model-executor.sqlite3")
    database.create_schema()
    run = RunRepository(database).create(Run(task="model executor"))
    workflow = ModelWorkflow(database)
    workflow._evaluator_only_ensure_state(run.run_id, budget or ModelBudget())

    async def fake_sleep(delay: float) -> None:
        if sleep_calls is not None:
            sleep_calls.append(delay)

    executor = ModelExecutor(
        provider,
        workflow,
        sleep=fake_sleep,
        jitter=lambda: 0.0,
    )
    return executor, run, workflow, EventRepository(database), database


def test_stale_model_completion_has_zero_budget_or_attempt_side_effects(
    tmp_path: Path,
) -> None:
    _, run, workflow, _, database = make_harness(tmp_path, SequenceProvider([]))
    leases = RunLeaseStore(database)
    first = leases.acquire(run.run_id, owner_id="first", ttl=timedelta(seconds=30))
    request = ModelRequest(task=run.task, step_number=1)
    attempt_id = workflow.prepare_attempt(
        run.run_id,
        uuid4(),
        1,
        request=request,
        provider_identity="sequence",
        authority=first.authority,
    )
    assert workflow.claim_dispatch(
        run.run_id,
        attempt_id,
        request=request,
        provider_identity="sequence",
        authority=first.authority,
    )
    leases.release(first.authority)
    leases.acquire(run.run_id, owner_id="replacement", ttl=timedelta(seconds=30))

    with pytest.raises(StaleFenceError):
        workflow.complete_attempt(
            run.run_id,
            attempt_id,
            ModelUsage(input_tokens=10, output_tokens=4, total_tokens=14),
            5,
            authority=first.authority,
        )

    state = workflow.get_state(run.run_id)
    attempt = workflow.list_attempts(run.run_id)[0]
    assert state.total_tokens == 0
    assert attempt.status == "DISPATCHING"
    database.close()


def test_prepare_commit_before_provider_call_is_not_dispatched(tmp_path: Path) -> None:
    provider = SequenceProvider([response()])
    _, run, workflow, events, database = make_harness(tmp_path, provider)
    lease = RunLeaseStore(database).acquire(
        run.run_id, owner_id="crashed-worker", ttl=timedelta(seconds=30)
    )

    workflow.prepare_attempt(
        run.run_id,
        uuid4(),
        1,
        request=ModelRequest(task=run.task, step_number=1),
        provider_identity="sequence",
        authority=lease.authority,
    )

    [attempt] = workflow.list_attempts(run.run_id)
    assert attempt.status == "PREPARED"
    assert attempt.completed_at is None
    assert provider.calls == 0
    assert events.list_for_run(run.run_id)[-1].event_type is (
        EventType.MODEL_ATTEMPT_PREPARED
    )
    database.close()


@pytest.mark.asyncio
async def test_cancelled_blocked_provider_leaves_attempt_dispatching(tmp_path: Path) -> None:
    provider = BlockingProvider()
    executor, run, workflow, _, database = make_harness(tmp_path, provider)  # type: ignore[arg-type]
    leases = RunLeaseStore(database)
    lease = leases.acquire(run.run_id, owner_id="driver", ttl=timedelta(seconds=30))
    task = asyncio.create_task(
        executor.generate(
            run,
            ModelRequest(task=run.task, step_number=1),
            ownership=RunOwnership(lambda: lease.authority),
        )
    )
    await provider.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    attempts = workflow.list_attempts(run.run_id)
    assert len(attempts) == 1 and attempts[0].status == "DISPATCHING"
    database.close()


@pytest.mark.asyncio
async def test_slow_provider_success_uses_live_ownership_after_many_heartbeats(
    tmp_path: Path,
) -> None:
    provider = SlowSequenceProvider([response()], delay=0.25)
    executor, run, workflow, _, database = make_harness(tmp_path, provider)
    driver = RunDriver(
        RunLeaseStore(database),
        run_id=run.run_id,
        owner_id="slow-provider",
        ttl=timedelta(seconds=2),
        heartbeat_interval=timedelta(milliseconds=25),
    )

    async def owned(ownership: RunOwnership) -> ModelResponse:
        return await executor.generate(
            run,
            ModelRequest(task=run.task, step_number=1),
            ownership=ownership,
        )

    result = await driver.run_outcome(owned)

    assert result.value is not None and result.value.action.answer == "done"
    assert workflow.list_attempts(run.run_id)[0].status == "COMPLETED"
    with database.session() as session:
        lease = session.get(RunLeaseRow, str(run.run_id))
        assert lease is not None and lease.version >= 4
    database.close()


@pytest.mark.asyncio
async def test_slow_retryable_failure_and_retry_use_live_ownership(
    tmp_path: Path,
) -> None:
    temporary = ModelRequestError(
        ModelErrorCode.MODEL_TRANSPORT_ERROR,
        "temporary transport failure",
        retryable=True,
    )
    provider = SlowSequenceProvider([temporary, response()], delay=0.25)
    executor, run, workflow, _, database = make_harness(
        tmp_path,
        provider,
        budget=ModelBudget(max_model_requests=3, max_retries=1),
    )
    driver = RunDriver(
        RunLeaseStore(database),
        run_id=run.run_id,
        owner_id="slow-retry",
        ttl=timedelta(seconds=2),
        heartbeat_interval=timedelta(milliseconds=25),
    )

    async def owned(ownership: RunOwnership) -> ModelResponse:
        return await executor.generate(
            run,
            ModelRequest(task=run.task, step_number=1),
            ownership=ownership,
        )

    result = await driver.run_outcome(owned)

    assert result.value is not None and result.value.attempt_count == 2
    assert [attempt.status for attempt in workflow.list_attempts(run.run_id)] == [
        "FAILED",
        "COMPLETED",
    ]
    with database.session() as session:
        lease = session.get(RunLeaseRow, str(run.run_id))
        assert lease is not None and lease.version >= 6
    database.close()


@pytest.mark.asyncio
async def test_retryable_failures_count_every_request_but_one_logical_turn(
    tmp_path: Path,
) -> None:
    sleep_calls: list[float] = []
    temporary = ModelRequestError(
        ModelErrorCode.MODEL_TRANSPORT_ERROR,
        "temporary transport failure",
        retryable=True,
    )
    provider = SequenceProvider([temporary, temporary, response()])
    executor, run, workflow, _, database = make_harness(
        tmp_path,
        provider,
        budget=ModelBudget(max_model_requests=5, max_retries=2),
        sleep_calls=sleep_calls,
    )
    lease = RunLeaseStore(database).acquire(
        run.run_id, owner_id="test", ttl=timedelta(seconds=30)
    )

    result = await executor.generate(
        run,
        ModelRequest(task=run.task, step_number=1),
        ownership=fixed_ownership(lease.authority),
    )

    state = workflow.get_state(run.run_id)
    assert result.attempt_count == 3
    assert provider.calls == 3
    assert state.model_request_count == 3
    assert state.total_tokens == 14
    assert sleep_calls == [1.0, 2.0]
    database.close()


@pytest.mark.asyncio
async def test_non_retryable_auth_error_is_attempted_once(tmp_path: Path) -> None:
    provider = SequenceProvider(
        [
            ModelRequestError(
                ModelErrorCode.MODEL_AUTH_ERROR,
                "Authentication failed",
                retryable=False,
            )
        ]
    )
    executor, run, workflow, _, database = make_harness(tmp_path, provider)
    lease = RunLeaseStore(database).acquire(
        run.run_id, owner_id="test", ttl=timedelta(seconds=30)
    )

    with pytest.raises(ModelRequestError) as error:
        await executor.generate(
            run,
            ModelRequest(task=run.task, step_number=1),
            ownership=fixed_ownership(lease.authority),
        )

    assert error.value.code is ModelErrorCode.MODEL_AUTH_ERROR
    assert provider.calls == 1
    assert workflow.get_state(run.run_id).model_request_count == 1
    database.close()


@pytest.mark.asyncio
async def test_request_budget_blocks_network_before_provider_call(tmp_path: Path) -> None:
    provider = SequenceProvider([response(), response()])
    executor, run, workflow, events, database = make_harness(
        tmp_path,
        provider,
        budget=ModelBudget(max_model_requests=1),
    )
    request = ModelRequest(task=run.task, step_number=1)
    lease = RunLeaseStore(database).acquire(
        run.run_id, owner_id="test", ttl=timedelta(seconds=30)
    )

    await executor.generate(run, request, ownership=fixed_ownership(lease.authority))
    with pytest.raises(ModelRequestError) as error:
        await executor.generate(run, request, ownership=fixed_ownership(lease.authority))

    assert error.value.code is ModelErrorCode.MODEL_BUDGET_EXCEEDED
    assert provider.calls == 1
    assert workflow.get_state(run.run_id).model_request_count == 1
    assert events.list_for_run(run.run_id)[-1].event_type.value == "BUDGET_EXCEEDED"
    database.close()


@pytest.mark.asyncio
async def test_provider_contract_deviation_retries_then_succeeds_and_audits(
    tmp_path: Path,
) -> None:
    sleep_calls: list[float] = []
    provider = SequenceProvider([deviation(), response()])
    executor, run, workflow, events, database = make_harness(
        tmp_path,
        provider,
        budget=ModelBudget(max_model_requests=3, max_retries=1),
        sleep_calls=sleep_calls,
    )
    lease = RunLeaseStore(database).acquire(
        run.run_id, owner_id="test", ttl=timedelta(seconds=30)
    )

    result = await executor.generate(
        run,
        ModelRequest(task=run.task, step_number=1),
        ownership=fixed_ownership(lease.authority),
    )

    assert result.action.answer == "done"
    assert provider.calls == 2
    assert workflow.get_state(run.run_id).model_request_count == 2
    assert sleep_calls == [1.0]
    assert [event.event_type for event in events.list_for_run(run.run_id)] == [
        EventType.MODEL_ATTEMPT_PREPARED,
        EventType.MODEL_ATTEMPT_DISPATCHING,
        EventType.MODEL_PROVIDER_DEVIATION,
        EventType.MODEL_ATTEMPT_FAILED,
        EventType.MODEL_RETRY_SCHEDULED,
        EventType.MODEL_ATTEMPT_PREPARED,
        EventType.MODEL_ATTEMPT_DISPATCHING,
        EventType.MODEL_ATTEMPT_COMPLETED,
    ]
    deviation_payload = events.list_for_run(run.run_id)[2].payload
    assert deviation_payload["returned_call_count"] == 2
    assert "arguments" not in deviation_payload
    database.close()


@pytest.mark.asyncio
async def test_provider_contract_deviation_fails_after_retry_limit(
    tmp_path: Path,
) -> None:
    provider = SequenceProvider([deviation(), deviation()])
    executor, run, workflow, events, database = make_harness(
        tmp_path,
        provider,
        budget=ModelBudget(max_model_requests=2, max_retries=1),
    )
    lease = RunLeaseStore(database).acquire(
        run.run_id, owner_id="test", ttl=timedelta(seconds=30)
    )

    with pytest.raises(ModelRequestError) as error:
        await executor.generate(
            run,
            ModelRequest(task=run.task, step_number=1),
            ownership=fixed_ownership(lease.authority),
        )

    assert error.value.code is ModelErrorCode.MODEL_PROTOCOL_ERROR
    assert error.value.retryable is True
    assert provider.calls == 2
    assert workflow.get_state(run.run_id).model_request_count == 2
    assert sum(
        event.event_type is EventType.MODEL_PROVIDER_DEVIATION
        for event in events.list_for_run(run.run_id)
    ) == 2
    database.close()


@pytest.mark.asyncio
async def test_ordinary_protocol_error_remains_non_retryable(tmp_path: Path) -> None:
    provider = SequenceProvider([ModelProtocolError("ambiguous response"), response()])
    executor, run, _, events, database = make_harness(
        tmp_path,
        provider,
        budget=ModelBudget(max_model_requests=3, max_retries=2),
    )
    lease = RunLeaseStore(database).acquire(
        run.run_id, owner_id="test", ttl=timedelta(seconds=30)
    )

    with pytest.raises(ModelRequestError) as error:
        await executor.generate(
            run,
            ModelRequest(task=run.task, step_number=1),
            ownership=fixed_ownership(lease.authority),
        )

    assert error.value.retryable is False
    assert provider.calls == 1
    assert EventType.MODEL_PROVIDER_DEVIATION not in {
        event.event_type for event in events.list_for_run(run.run_id)
    }
    database.close()
