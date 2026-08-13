import json
import os
from pathlib import Path

import pytest

from agentforge.domain.enums import EventType, RunStatus
from agentforge.models.domain import ModelBudget, ModelProviderConfig
from agentforge.models.executor import ModelExecutor
from agentforge.models.identity import is_exact_or_dated_openai_snapshot
from agentforge.models.openai_provider import OpenAIModelProvider
from agentforge.persistence.database import Database
from agentforge.persistence.model_workflow import ModelWorkflow
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
from agentforge.tools.repository import ListFilesTool, ReadFileTool, SearchTextTool

pytestmark = pytest.mark.live


@pytest.mark.asyncio
async def test_live_provider_analyzes_read_only_fixture_repository(tmp_path: Path) -> None:
    api_key = os.getenv("OPENAI_API_KEY")
    model = os.getenv("AGENTFORGE_OPENAI_MODEL")
    if os.getenv("RUN_LIVE_TESTS") != "1" or not api_key or not model:
        pytest.skip("Live OpenAI test requires explicit opt-in, API key, and model")

    workspace = tmp_path / "fixture-repository"
    workspace.mkdir()
    (workspace / "runtime.py").write_text(
        "class Runtime:\n    def execute(self):\n        return 'runtime-event-path'\n",
        encoding="utf-8",
    )
    (workspace / "policy.py").write_text(
        "class Policy:\n    def allow_read(self):\n        return True\n",
        encoding="utf-8",
    )
    database = Database.from_path(tmp_path / "live.sqlite3")
    database.create_schema()
    runs = RunRepository(database)
    events = EventRepository(database)
    checkpoints = CheckpointRepository(database)
    resolver = WorkspacePathResolver(workspace)
    sensitive = SensitiveFilePolicy()
    registry = ToolRegistry(
        [
            ListFilesTool(resolver, sensitive),
            ReadFileTool(resolver, sensitive),
            SearchTextTool(resolver, sensitive),
        ]
    )
    provider = OpenAIModelProvider(
        ModelProviderConfig(
            api_key=api_key,
            model=model,
            timeout_seconds=45,
            max_retries=1,
            max_output_tokens=800,
        )
    )
    model_workflow = ModelWorkflow(database)
    runtime = AgentRuntime(
        run_repository=runs,
        event_repository=events,
        checkpoint_repository=checkpoints,
        model_provider=provider,
        model_executor=ModelExecutor(provider, model_workflow),
        model_workflow=model_workflow,
        model_budget=ModelBudget(
            max_model_requests=5,
            max_retries=1,
            max_total_tokens=10_000,
        ),
        tool_executor=ToolExecutor(
            registry,
            PolicyEngine(resolver, sensitive),
            events,
            runs,
        ),
    )
    run = runtime.create_run(
        "Use at least two repository tools to explain how runtime.py and policy.py relate. "
        "Cite their relative file paths.",
        max_steps=5,
        max_tool_calls=5,
    )

    completed = await runtime.execute(run.run_id)

    assert completed.status is RunStatus.COMPLETED
    assert "runtime.py" in (completed.final_output or "")
    assert "policy.py" in (completed.final_output or "")
    recorded = events.list_for_run(run.run_id)
    event_types = [event.event_type for event in recorded]
    assert event_types.count(EventType.TOOL_COMPLETED) >= 2
    state = model_workflow.get_state(run.run_id)
    assert state.model_request_count <= 5
    deviation_events = [
        event
        for event in recorded
        if event.event_type is EventType.MODEL_PROVIDER_DEVIATION
    ]
    normalized_events = [
        event
        for event in recorded
        if event.event_type is EventType.MULTI_TOOL_RESPONSE_NORMALIZED
    ]
    for event in [*deviation_events, *normalized_events]:
        serialized = json.dumps(event.payload, ensure_ascii=False, sort_keys=True)
        assert "arguments" not in serialized
        assert "output" not in serialized
    model_metadata = [
        event.payload.get("provider_metadata", {})
        for event in recorded
        if event.event_type is EventType.MODEL_RESPONDED
    ]
    model_response_events = [
        event
        for event in recorded
        if event.event_type is EventType.MODEL_RESPONDED
    ]
    assert model_response_events
    assert all(
        event.payload.get("provider") == "openai"
        for event in model_response_events
    )
    returned_model_ids = {
        str(event.payload.get("model")) for event in model_response_events
    }
    assert len(returned_model_ids) == 1
    resolved_model_id = next(iter(returned_model_ids))
    expected_response_model = os.getenv("AGENTFORGE_OPENAI_RESPONSE_MODEL")
    if expected_response_model:
        assert resolved_model_id == expected_response_model
    else:
        assert is_exact_or_dated_openai_snapshot(model, resolved_model_id)
    returned_counts = [
        metadata.get("returned_function_call_count", 0)
        for metadata in model_metadata
        if isinstance(metadata, dict)
    ]
    attempts = model_workflow.list_attempts(run.run_id)
    completed_attempts = [
        attempt for attempt in attempts if attempt.status == "COMPLETED"
    ]
    assert completed_attempts
    completed_usage_complete = all(
        attempt.usage is not None
        and attempt.usage.input_tokens is not None
        and attempt.usage.output_tokens is not None
        and attempt.usage.total_tokens is not None
        for attempt in completed_attempts
    )
    all_request_usage_complete = bool(attempts) and all(
        attempt.usage is not None
        and attempt.usage.input_tokens is not None
        and attempt.usage.output_tokens is not None
        and attempt.usage.total_tokens is not None
        for attempt in attempts
    )
    metrics = {
        "requested_model_id": model,
        "returned_model_ids": sorted(returned_model_ids),
        "resolved_model_id": resolved_model_id,
        "provider_returned_multiple_calls": bool(deviation_events),
        "max_returned_call_count": max(returned_counts, default=0),
        "selected_call_count": sum(
            int(event.payload["selected_call_count"]) for event in normalized_events
        ),
        "discarded_call_count": sum(
            int(event.payload["discarded_call_count"]) for event in normalized_events
        ),
        "model_request_count": state.model_request_count,
        "tool_call_count": completed.tool_call_count,
        "completed_final_answer": completed.status is RunStatus.COMPLETED,
        "cited_runtime_path": "runtime.py" in (completed.final_output or ""),
        "cited_policy_path": "policy.py" in (completed.final_output or ""),
        "within_request_budget": state.model_request_count <= 5,
        "within_tool_budget": completed.tool_call_count <= 5,
        "provider_deviation_event": bool(deviation_events),
        "normalization_event": bool(normalized_events),
        "completed_request_usage_complete": completed_usage_complete,
        "all_physical_request_usage_complete": all_request_usage_complete,
    }
    print("LIVE_METRICS " + json.dumps(metrics, sort_keys=True))
    database.close()
