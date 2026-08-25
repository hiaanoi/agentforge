from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from agentforge.application.runtime_factory import RuntimeComponentFactory
from agentforge.domain.enums import EventType, RunStatus
from agentforge.repair_engines.models import RepairEngineKind
from tests.integration.test_mini_native_runtime import _create_product_run, _request


@pytest.mark.asyncio
async def test_mini_native_resumes_a_durably_saved_response_without_replaying_the_model(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, workspace = _request(tmp_path)
    components = RuntimeComponentFactory().build(
        replace(request, repair_engine=RepairEngineKind.MINI_NATIVE)
    )
    run = _create_product_run(request, workspace)

    async def interrupt_before_first_tool(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise RuntimeError("interrupt after model response")

    monkeypatch.setattr(components.runtime._tools, "execute", interrupt_before_first_tool)
    with pytest.raises(RuntimeError, match="interrupt after model response"):
        await components.runtime.execute(run.run_id)

    monkeypatch.undo()
    reopened = RuntimeComponentFactory().build(
        replace(request, repair_engine=RepairEngineKind.MINI_NATIVE)
    )
    resumed = await reopened.runtime.resume(run.run_id)

    assert resumed.run_id == run.run_id
    assert resumed.status is RunStatus.WAITING_APPROVAL
    pending = reopened.runtime.list_pending_approvals(run.run_id)
    assert [approval.tool_name for approval in pending] == ["edit_file"]
    assert len(request.provider.requests) == 2
    events = request.events.list_for_run(run.run_id)
    assert [event.sequence_number for event in events] == list(
        range(1, len(events) + 1)
    )


@pytest.mark.asyncio
async def test_mini_native_recovers_a_claimed_mutation_without_a_duplicate_edit(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, workspace = _request(tmp_path)
    components = RuntimeComponentFactory().build(
        replace(request, repair_engine=RepairEngineKind.MINI_NATIVE)
    )
    run = _create_product_run(request, workspace)

    waiting = await components.runtime.execute(run.run_id)
    assert waiting.status is RunStatus.WAITING_APPROVAL
    [approval] = components.runtime.list_pending_approvals(run.run_id)
    components.runtime.approve(approval.approval_id)

    original = components.runtime._tools.execute

    async def interrupt_after_mutation(*args: Any, **kwargs: Any) -> object:
        result = await original(*args, **kwargs)
        if kwargs.get("approval") is not None:
            raise RuntimeError("interrupt during mutation approval")
        return result

    monkeypatch.setattr(components.runtime._tools, "execute", interrupt_after_mutation)
    with pytest.raises(RuntimeError, match="interrupt during mutation approval"):
        await components.runtime.resume(run.run_id)

    monkeypatch.undo()
    reopened = RuntimeComponentFactory().build(
        replace(request, repair_engine=RepairEngineKind.MINI_NATIVE)
    )
    recovered = await reopened.runtime.resume(run.run_id)

    assert recovered.run_id == run.run_id
    assert recovered.status is RunStatus.WAITING_APPROVAL
    assert (workspace / "src" / "value.py").read_bytes() == (
        b"VALUE = 1\n# sk-live-secret-value\n"
    )
    assert len(reopened.mutation_coordinator.list_executions(run.run_id)) == 1
    events = request.events.list_for_run(run.run_id)
    assert sum(event.event_type is EventType.MUTATION_COMMITTED for event in events) == 1
    assert [event.sequence_number for event in events] == list(
        range(1, len(events) + 1)
    )
