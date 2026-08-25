from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest

from agentforge.application.runtime_factory import RuntimeComponentFactory
from agentforge.domain.enums import EventType
from agentforge.domain.models import Run
from agentforge.evaluation.verified10_runner import Verified10Campaign
from agentforge.models.base import ModelRequest, ToolCall
from agentforge.models.domain import ModelBudget, ModelErrorCode, ModelResponse, ModelUsage
from agentforge.models.errors import ModelRequestError
from agentforge.persistence.database import Database
from agentforge.persistence.event_log import EventLog
from agentforge.persistence.model_workflow import ModelWorkflow
from agentforge.persistence.repositories import RunRepository
from agentforge.persistence.run_leases import RunLeaseStore
from agentforge.repair_engines.mini_native.agentforge_host import AgentForgeMiniNativeHost
from agentforge.repair_engines.models import RepairEngineKind
from agentforge.runtime.snapshots import RuntimeSnapshotV5, load_runtime_snapshot
from tests.integration.test_mini_native_runtime import (
    _create_product_run,
    _request,
    _ScriptedBashEnvironment,
)


class _ReadOnlyProvider:
    name = "telemetry"
    journal_identity = "telemetry/mini-native"

    def __init__(self, usage: ModelUsage | None) -> None:
        self._usage = usage
        self._calls = 0

    async def generate(self, request: ModelRequest) -> ModelResponse:
        del request
        self._calls += 1
        if self._calls > 1:
            raise ModelRequestError(
                ModelErrorCode.MODEL_BAD_REQUEST,
                "stop after collecting telemetry",
                retryable=False,
            )
        return ModelResponse(
            action=ToolCall(
                type="tool_call",
                tool="bash",
                arguments={"command": "rg -n VALUE src/value.py"},
            ),
            usage=self._usage,
            provider=self.name,
            model="mini-native",
            duration_ms=3,
            attempt_count=1,
        )


class _UsageProvider:
    def __init__(self, delegate: Any) -> None:
        self._delegate = delegate

    @property
    def name(self) -> str:
        return self._delegate.name

    @property
    def journal_identity(self) -> str:
        return self._delegate.journal_identity

    async def generate(self, request: ModelRequest) -> ModelResponse:
        response = await self._delegate.generate(request)
        return response.model_copy(
            update={
                "usage": ModelUsage(input_tokens=3, output_tokens=2, total_tokens=5),
                "sanitized_metadata": {"source": "usage-test"},
            }
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("usage", "available"),
    [
        (ModelUsage(input_tokens=5, output_tokens=7, total_tokens=12), True),
        (None, False),
    ],
)
async def test_mini_native_checkpoint_and_event_make_provider_usage_availability_explicit(
    tmp_path: Any, usage: ModelUsage | None, available: bool
) -> None:
    request, workspace = _request(tmp_path)
    request = replace(request, provider=_ReadOnlyProvider(usage))
    components = RuntimeComponentFactory().build(
        replace(request, repair_engine=RepairEngineKind.MINI_NATIVE)
    )
    components.runtime._mini_native_environment = _ScriptedBashEnvironment(workspace)
    run = _create_product_run(request, workspace)

    await components.runtime.execute(run.run_id)

    [responded] = [
        event
        for event in request.events.list_for_run(run.run_id)
        if event.event_type is EventType.MODEL_RESPONDED
    ]
    assert responded.payload["usage_available"] is available
    checkpoint = components.runtime._checkpoints.latest(run.run_id)
    assert checkpoint is not None
    snapshot = load_runtime_snapshot(
        checkpoint.runtime_state,
        run_id=run.run_id,
        step_number=checkpoint.step_number,
    )
    assert snapshot.provider_usage_available is available
    if usage is None:
        assert snapshot.model_usage.total_tokens == 0
    else:
        assert snapshot.model_usage == ModelUsage(
            input_tokens=5,
            output_tokens=7,
            total_tokens=12,
            cached_input_tokens=0,
            reasoning_tokens=0,
        )


def test_verified10_projection_marks_explicitly_unavailable_provider_usage(
    tmp_path: Any,
) -> None:
    workspace = tmp_path / "workspace"
    database = Database.from_path(workspace / ".agentforge" / "agentforge.db")
    database.create_schema()
    run = RunRepository(database).create(Run(task="telemetry"))
    ModelWorkflow(database)._evaluator_only_ensure_state(
        run.run_id, ModelBudget(max_model_requests=1)
    )
    authority = RunLeaseStore(database).acquire(
        run.run_id, owner_id="telemetry", ttl=timedelta(seconds=30)
    ).authority
    with database.session() as session:
        EventLog().append(
            session,
            authority,
            EventType.MODEL_RESPONDED,
            {"usage": None, "usage_available": False},
        )
    database.close()

    telemetry = Verified10Campaign._agentforge_telemetry(workspace, str(run.run_id))

    assert "model_usage" in telemetry["unavailable"]
    assert "total_tokens" not in telemetry


@pytest.mark.asyncio
async def test_mini_native_approval_snapshot_restores_available_usage_on_restart(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, workspace = _request(tmp_path)
    request = replace(request, provider=_UsageProvider(request.provider))
    components = RuntimeComponentFactory().build(
        replace(request, repair_engine=RepairEngineKind.MINI_NATIVE)
    )
    components.runtime._mini_native_environment = _ScriptedBashEnvironment(workspace)
    run = _create_product_run(request, workspace)

    waiting = await components.runtime.execute(run.run_id)

    assert waiting.status.value == "WAITING_APPROVAL"
    checkpoint = components.runtime._checkpoints.latest(run.run_id)
    assert checkpoint is not None
    snapshot = RuntimeSnapshotV5.model_validate(checkpoint.runtime_state)
    assert snapshot.provider_usage_available
    assert snapshot.model_usage.total_tokens == 10
    assert snapshot.last_provider_metadata == {"source": "usage-test"}
    [approval] = components.runtime.list_pending_approvals(run.run_id)
    components.runtime.approve(approval.approval_id)

    original = AgentForgeMiniNativeHost.generate

    async def assert_restored(
        self: AgentForgeMiniNativeHost, *args: Any, **kwargs: Any
    ) -> ModelResponse:
        assert self._provider_usage_available is True
        assert self._last_provider_metadata == {"source": "usage-test"}
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(AgentForgeMiniNativeHost, "generate", assert_restored)
    reopened = RuntimeComponentFactory().build(
        replace(request, repair_engine=RepairEngineKind.MINI_NATIVE)
    )
    reopened.runtime._mini_native_environment = _ScriptedBashEnvironment(workspace)

    resumed = await reopened.runtime.resume(run.run_id)

    assert resumed.status.value == "WAITING_APPROVAL"


@pytest.mark.asyncio
async def test_direct_provider_approval_keeps_last_usage_in_its_checkpoint(
    tmp_path: Any,
) -> None:
    request, workspace = _request(tmp_path)
    request = replace(request, provider=_UsageProvider(request.provider))
    components = RuntimeComponentFactory().build(
        replace(request, repair_engine=RepairEngineKind.MINI_NATIVE)
    )
    components.runtime._mini_native_environment = _ScriptedBashEnvironment(workspace)
    components.runtime._model_executor = None
    components.runtime._model_workflow = None
    run = _create_product_run(request, workspace)

    waiting = await components.runtime.execute(run.run_id)

    assert waiting.status.value == "WAITING_APPROVAL"
    checkpoint = components.runtime._checkpoints.latest(run.run_id)
    assert checkpoint is not None
    snapshot = RuntimeSnapshotV5.model_validate(checkpoint.runtime_state)
    assert snapshot.provider_usage_available
    assert snapshot.model_usage == ModelUsage(
        input_tokens=3,
        output_tokens=2,
        total_tokens=5,
    )
