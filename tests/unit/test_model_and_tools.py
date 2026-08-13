from datetime import timedelta
from pathlib import Path

import pytest

from agentforge.application.run_driver import RunOwnership
from agentforge.domain.errors import ModelOutputError, ModelProviderError
from agentforge.domain.models import Run
from agentforge.models.base import FinalAnswer, ModelRequest, ToolCall, parse_model_output
from agentforge.models.mock import MockModelProvider
from agentforge.persistence.database import Database
from agentforge.persistence.repositories import EventRepository, RunRepository
from agentforge.persistence.run_leases import RunLeaseStore
from agentforge.policy.engine import PolicyEngine
from agentforge.policy.sensitive import SensitiveFilePolicy
from agentforge.tools.executor import ToolExecutor
from agentforge.tools.paths import WorkspacePathResolver
from agentforge.tools.registry import ToolRegistry
from agentforge.tools.safe import AddNumbersTool, EchoTool


def test_structured_model_output_accepts_only_supported_actions() -> None:
    tool_call = parse_model_output(
        {"type": "tool_call", "tool": "echo", "arguments": {"text": "hello"}, "reason": "test"}
    )
    final = parse_model_output({"type": "final", "answer": "done"})

    assert isinstance(tool_call, ToolCall)
    assert isinstance(final, FinalAnswer)

    with pytest.raises(ModelOutputError):
        parse_model_output({"type": "unknown", "payload": "untrusted"})


@pytest.mark.asyncio
async def test_mock_provider_returns_responses_in_order() -> None:
    provider = MockModelProvider(
        [
            {"type": "tool_call", "tool": "echo", "arguments": {"text": "hello"}},
            {"type": "final", "answer": "done"},
        ]
    )
    request = ModelRequest(task="test", step_number=1)

    assert (await provider.generate(request)).action.type == "tool_call"
    assert (await provider.generate(request)).action.type == "final"
    assert len(provider.requests) == 2

    with pytest.raises(ModelProviderError):
        await provider.generate(request)


@pytest.mark.asyncio
async def test_mock_provider_can_replay_a_durable_step_after_restart() -> None:
    provider = MockModelProvider(
        [
            {"type": "tool_call", "tool": "echo", "arguments": {"text": "one"}},
            {"type": "final", "answer": "two"},
        ],
        response_by_step=True,
    )

    second = await provider.generate(ModelRequest(task="test", step_number=2))
    first = await provider.generate(ModelRequest(task="test", step_number=1))

    assert second.action.type == "final"
    assert first.action.type == "tool_call"


@pytest.mark.asyncio
async def test_tool_executor_runs_registered_safe_tools_and_contains_failures(
    tmp_path: Path,
) -> None:
    database = Database.from_path(tmp_path / "tools.sqlite3")
    database.create_schema()
    runs = RunRepository(database)
    events = EventRepository(database)
    registry = ToolRegistry([EchoTool(), AddNumbersTool()])
    executor = ToolExecutor(
        registry,
        PolicyEngine(WorkspacePathResolver(tmp_path), SensitiveFilePolicy()),
        events,
        runs,
    )
    run = runs.create(Run(task="safe tools"))
    leases = RunLeaseStore(database)
    lease = leases.acquire(
        run.run_id,
        owner_id="model-and-tools-evaluator",
        ttl=timedelta(seconds=30),
    )
    ownership = RunOwnership(lambda: lease.authority)
    try:
        echo = await executor.execute(
            run, "echo", {"text": "hello"}, ownership=ownership
        )
        addition = await executor.execute(
            run,
            "add_numbers",
            {"left": 2, "right": 3},
            ownership=ownership,
        )
        invalid = await executor.execute(
            run,
            "add_numbers",
            {"left": "not-a-number", "right": 3},
            ownership=ownership,
        )
        missing = await executor.execute(run, "missing", {}, ownership=ownership)

        assert echo.success and echo.output == "hello"
        assert addition.success and addition.output == 5
        assert not invalid.success and invalid.error_type == "INVALID_ARGUMENTS"
        assert not missing.success and missing.error_type == "TOOL_NOT_FOUND"
    finally:
        leases.release(lease.authority)
        database.close()
