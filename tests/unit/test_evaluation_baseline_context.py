from pathlib import Path

import pytest

from agentforge.context.builder import ContextBuilder
from agentforge.context.models import ContextItem, ContextItemKind, ContextPolicy
from agentforge.models.mock import MockModelProvider
from agentforge.persistence.database import Database
from agentforge.persistence.repositories import (
    CheckpointRepository,
    EventRepository,
    RunRepository,
)
from agentforge.policy.engine import PolicyEngine
from agentforge.policy.sensitive import SensitiveFilePolicy
from agentforge.runtime.engine import AgentRuntime
from agentforge.tools.executor import ToolExecutor
from agentforge.tools.paths import WorkspacePathResolver
from agentforge.tools.registry import ToolRegistry


def baseline_item() -> ContextItem:
    return ContextItem(
        kind=ContextItemKind.EVALUATION_BASELINE_FAILURE,
        payload={
            "schema_version": 1,
            "message": "Evaluator confirmed the buggy visible baseline failure.",
            "failed_node_ids": ["tests/visible/test_flow.py::test_once"],
            "diagnostic_summary": "AssertionError: expected one dispatch",
            "instruction": "Choose the read, edit, and retest sequence yourself.",
        },
    )


def test_baseline_context_is_protected_from_pair_compaction() -> None:
    builder = ContextBuilder(
        ContextPolicy(max_items=1, max_characters=10_000, max_utf8_bytes=10_000)
    )
    items = [
        ContextItem(kind=ContextItemKind.TOOL_CALL, payload={}, call_id="old"),
        ContextItem(kind=ContextItemKind.TOOL_RESULT, payload={}, call_id="old"),
        baseline_item(),
    ]

    result = builder.build(task="repair", items=items)

    assert [item.kind for item in result.items] == [ContextItemKind.EVALUATION_BASELINE_FAILURE]


@pytest.mark.asyncio
async def test_runtime_places_host_initial_context_in_first_model_request(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    database = Database.from_path(tmp_path / "runtime.db")
    database.create_schema()
    model = MockModelProvider([{"type": "final", "answer": "done"}])
    runs = RunRepository(database)
    runtime = AgentRuntime(
        runs,
        EventRepository(database),
        CheckpointRepository(database),
        model,
        ToolExecutor(
            ToolRegistry([]),
            PolicyEngine(WorkspacePathResolver(workspace), SensitiveFilePolicy()),
            EventRepository(database),
            runs,
        ),
    )
    run = runtime.create_run("repair")

    await runtime.execute(run.run_id, initial_context_items=[baseline_item()])

    assert model.requests[0].history[0]["kind"] == "EVALUATION_BASELINE_FAILURE"
    assert "buggy visible baseline" in str(model.requests[0].history[0])
