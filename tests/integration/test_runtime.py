import asyncio
import hashlib
from datetime import timedelta
from pathlib import Path

import pytest

from agentforge.application.kernel_errors import StaleFenceError
from agentforge.domain.enums import EventType, RunStatus
from agentforge.domain.models import RunBudget
from agentforge.models.base import ModelRequest, parse_model_output
from agentforge.models.domain import ModelResponse
from agentforge.models.mock import MockModelProvider
from agentforge.persistence.database import Database
from agentforge.persistence.repositories import (
    CheckpointRepository,
    EventRepository,
    RunRepository,
)
from agentforge.persistence.run_leases import RunLeaseStore
from agentforge.policy.engine import PolicyEngine
from agentforge.policy.sensitive import SensitiveFilePolicy
from agentforge.runtime.engine import AgentRuntime
from agentforge.tools.executor import ToolExecutor
from agentforge.tools.paths import WorkspacePathResolver
from agentforge.tools.registry import ToolRegistry
from agentforge.tools.safe import EchoTool


def build_runtime(
    path: Path,
    responses: list[object],
    *,
    provider: object | None = None,
) -> tuple[AgentRuntime, Database, RunRepository, EventRepository, CheckpointRepository]:
    database = Database.from_path(path)
    database.create_schema()
    runs = RunRepository(database)
    events = EventRepository(database)
    checkpoints = CheckpointRepository(database)
    policy = PolicyEngine(WorkspacePathResolver(path.parent), SensitiveFilePolicy())
    runtime = AgentRuntime(
        run_repository=runs,
        event_repository=events,
        checkpoint_repository=checkpoints,
        model_provider=provider or MockModelProvider(responses),  # type: ignore[arg-type]
        tool_executor=ToolExecutor(
            ToolRegistry([EchoTool()]),
            policy,
            events,
            runs,
        ),
    )
    return runtime, database, runs, events, checkpoints


class BlockingProvider:
    name = "blocking"

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def generate(self, request: ModelRequest) -> ModelResponse:
        del request
        self.started.set()
        await self.release.wait()
        return ModelResponse(
            action=parse_model_output({"type": "final", "answer": "done"}),
            provider=self.name,
            model="blocking",
            duration_ms=0,
            attempt_count=1,
        )


@pytest.mark.asyncio
async def test_runtime_owns_one_execution_lease_across_provider_wait(tmp_path: Path) -> None:
    provider = BlockingProvider()
    runtime, database, _, _, _ = build_runtime(
        tmp_path / "runtime-lease.sqlite3", [], provider=provider
    )
    run = runtime.create_run("hold lease across provider")

    execution = asyncio.create_task(runtime.execute(run.run_id))
    await asyncio.wait_for(provider.started.wait(), timeout=2)
    lease = RunLeaseStore(database).current(run.run_id)
    assert lease is not None
    with pytest.raises(StaleFenceError):
        RunLeaseStore(database).acquire(
            run.run_id,
            owner_id="competing-runtime",
            ttl=timedelta(seconds=30),
        )
    provider.release.set()
    completed = await asyncio.wait_for(execution, timeout=2)

    assert completed.status is RunStatus.COMPLETED
    assert RunLeaseStore(database).current(run.run_id) is None
    database.close()


@pytest.mark.asyncio
async def test_runtime_completes_tool_loop_and_persists_audit_trail(tmp_path: Path) -> None:
    runtime, database, runs, events, checkpoints = build_runtime(
        tmp_path / "success.sqlite3",
        [
            {
                "type": "tool_call",
                "tool": "echo",
                "arguments": {"text": "deterministic"},
                "reason": "exercise the tool path",
            },
            {"type": "final", "answer": "completed deterministically"},
        ],
    )
    created = runtime.create_run("run the deterministic loop", max_steps=3)
    created_event = events.list_for_run(created.run_id)[0]

    completed = await runtime.execute(created.run_id)

    assert created_event.payload == {
        "task_digest": hashlib.sha256(
            b"run the deterministic loop"
        ).hexdigest()
    }
    assert "run the deterministic loop" not in created_event.model_dump_json()
    assert completed.status is RunStatus.COMPLETED
    assert completed.final_output == "completed deterministically"
    assert completed.current_step == 2
    assert runs.get(created.run_id).final_output == "completed deterministically"
    assert [event.event_type for event in events.list_for_run(created.run_id)] == [
        EventType.RUN_CREATED,
        EventType.RUN_STARTED,
        EventType.MODEL_REQUESTED,
        EventType.MODEL_RESPONDED,
        EventType.TOOL_REQUESTED,
        EventType.TOOL_STARTED,
        EventType.TOOL_COMPLETED,
        EventType.CHECKPOINT_SAVED,
        EventType.MODEL_REQUESTED,
        EventType.MODEL_RESPONDED,
        EventType.RUN_COMPLETED,
    ]
    checkpoint = checkpoints.latest(created.run_id)
    assert checkpoint is not None
    assert checkpoint.step_number == 1
    assert checkpoint.runtime_state["history"][1]["tool_result"]["output"] == "deterministic"
    database.close()


@pytest.mark.asyncio
async def test_runtime_fails_when_max_steps_is_exhausted(tmp_path: Path) -> None:
    runtime, database, _, events, _ = build_runtime(
        tmp_path / "steps.sqlite3",
        [{"type": "tool_call", "tool": "echo", "arguments": {"text": "one step"}}],
    )
    run = runtime.create_run("exhaust steps", max_steps=1)

    failed = await runtime.execute(run.run_id)

    assert failed.status is RunStatus.FAILED
    assert failed.error_message == "Maximum step count of 1 exhausted"
    assert events.list_for_run(run.run_id)[-1].event_type is EventType.RUN_FAILED
    database.close()


@pytest.mark.asyncio
async def test_runtime_records_unknown_tool_as_failure(tmp_path: Path) -> None:
    runtime, database, _, events, _ = build_runtime(
        tmp_path / "missing.sqlite3",
        [{"type": "tool_call", "tool": "not_registered", "arguments": {}}],
    )
    run = runtime.create_run("request missing tool")

    failed = await runtime.execute(run.run_id)

    assert failed.status is RunStatus.FAILED
    assert "not_registered" in (failed.error_message or "")
    assert EventType.TOOL_FAILED in [event.event_type for event in events.list_for_run(run.run_id)]
    database.close()


@pytest.mark.asyncio
async def test_runtime_rejects_invalid_model_output(tmp_path: Path) -> None:
    runtime, database, _, events, _ = build_runtime(
        tmp_path / "invalid.sqlite3", [{"type": "unsupported", "answer": "do not trust me"}]
    )
    run = runtime.create_run("reject invalid response")

    failed = await runtime.execute(run.run_id)

    assert failed.status is RunStatus.FAILED
    assert "Model output failed validation" in (failed.error_message or "")
    assert events.list_for_run(run.run_id)[-1].event_type is EventType.RUN_FAILED
    database.close()


@pytest.mark.asyncio
async def test_runtime_saves_checkpoint_after_each_successful_tool_step(tmp_path: Path) -> None:
    runtime, database, _, _, checkpoints = build_runtime(
        tmp_path / "checkpoints.sqlite3",
        [
            {"type": "tool_call", "tool": "echo", "arguments": {"text": "first"}},
            {"type": "tool_call", "tool": "echo", "arguments": {"text": "second"}},
            {"type": "final", "answer": "done"},
        ],
    )
    run = runtime.create_run("checkpoint every tool step", max_steps=3)

    completed = await runtime.execute(run.run_id)

    assert completed.status is RunStatus.COMPLETED
    saved = checkpoints.list_for_run(run.run_id)
    assert [checkpoint.step_number for checkpoint in saved] == [1, 2]
    assert len(saved[1].runtime_state["history"]) == 4
    database.close()


@pytest.mark.asyncio
async def test_two_runtime_runs_keep_state_and_events_isolated(tmp_path: Path) -> None:
    runtime, database, runs, events, _ = build_runtime(
        tmp_path / "two-runs.sqlite3",
        [
            {"type": "final", "answer": "first result"},
            {"type": "final", "answer": "second result"},
        ],
    )
    first = runtime.create_run("first task")
    second = runtime.create_run("second task")

    await runtime.execute(first.run_id)
    await runtime.execute(second.run_id)

    assert runs.get(first.run_id).final_output == "first result"
    assert runs.get(second.run_id).final_output == "second result"
    first_events = events.list_for_run(first.run_id)
    second_events = events.list_for_run(second.run_id)
    assert {event.run_id for event in first_events} == {first.run_id}
    assert {event.run_id for event in second_events} == {second.run_id}
    assert [event.sequence_number for event in first_events] == list(
        range(1, len(first_events) + 1)
    )
    assert [event.sequence_number for event in second_events] == list(
        range(1, len(second_events) + 1)
    )
    database.close()


def test_runtime_maps_run_budget_to_persisted_limits(tmp_path: Path) -> None:
    runtime, database, runs, _, _ = build_runtime(
        tmp_path / "budget.sqlite3", [{"type": "final", "answer": "unused"}]
    )
    budget = RunBudget(max_steps=4, max_tool_calls=2)

    run = runtime.create_run("budget mapping", budget=budget)

    persisted = runs.get(run.run_id)
    assert persisted.max_steps == 4
    assert persisted.max_tool_calls == 2
    database.close()
