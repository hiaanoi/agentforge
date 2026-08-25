from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from agentforge.application.runtime_factory import RuntimeComponentFactory
from agentforge.domain.enums import EventType
from agentforge.models.base import ModelRequest, ToolCall
from agentforge.models.domain import ModelErrorCode, ModelResponse, ModelUsage
from agentforge.models.errors import ModelRequestError
from agentforge.repair_engines.models import RepairEngineKind
from agentforge.runtime.snapshots import load_runtime_snapshot
from tests.integration.test_mini_native_runtime import _create_product_run, _request


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
                type="tool_call", tool="read_file", arguments={"path": "src/value.py"}
            ),
            usage=self._usage,
            provider=self.name,
            model="mini-native",
            duration_ms=3,
            attempt_count=1,
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
