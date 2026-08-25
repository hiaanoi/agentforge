from __future__ import annotations

from dataclasses import replace
from typing import Any
from uuid import uuid4

import pytest

from agentforge.application.run_commands import ResumeRecoveryChoice, ResumeRun
from agentforge.application.runtime_factory import RuntimeComponentFactory
from agentforge.domain.enums import EventType, RunStatus
from agentforge.models.base import FinalAnswer, ModelRequest
from agentforge.models.domain import ModelResponse
from agentforge.repair_engines.mini_native.agentforge_host import AgentForgeMiniNativeHost
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
async def test_product_resume_command_continues_a_completed_mini_native_checkpoint(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, workspace = _request(tmp_path)
    components = RuntimeComponentFactory().build(
        replace(request, repair_engine=RepairEngineKind.MINI_NATIVE)
    )
    run = _create_product_run(request, workspace)
    original = AgentForgeMiniNativeHost.checkpoint

    async def interrupt_after_checkpoint(
        self: AgentForgeMiniNativeHost, *args: Any, **kwargs: Any
    ) -> None:
        await original(self, *args, **kwargs)
        raise RuntimeError("interrupt after completed checkpoint")

    monkeypatch.setattr(AgentForgeMiniNativeHost, "checkpoint", interrupt_after_checkpoint)
    with pytest.raises(RuntimeError, match="interrupt after completed checkpoint"):
        await components.runtime.execute(run.run_id)

    monkeypatch.undo()
    reopened = RuntimeComponentFactory().build(
        replace(request, repair_engine=RepairEngineKind.MINI_NATIVE)
    )
    outcome = await reopened.runtime.resume(ResumeRun(command_id=uuid4(), run_id=run.run_id))

    assert outcome.value is not None
    assert outcome.value.status is RunStatus.WAITING_APPROVAL


class _EarlyFinalProvider:
    name = "early-final"
    journal_identity = "early-final/mini-native"

    async def generate(self, request: ModelRequest) -> ModelResponse:
        del request
        return ModelResponse(
            action=FinalAnswer(type="final", answer="too early"),
            provider=self.name,
            model="mini-native",
            duration_ms=0,
            attempt_count=1,
        )


@pytest.mark.asyncio
async def test_mini_native_early_final_terminalizes_the_run(tmp_path: Any) -> None:
    request, workspace = _request(tmp_path)
    request = replace(request, provider=_EarlyFinalProvider())
    components = RuntimeComponentFactory().build(
        replace(request, repair_engine=RepairEngineKind.MINI_NATIVE)
    )
    run = _create_product_run(request, workspace)

    failed = await components.runtime.execute(run.run_id)

    assert failed.status is RunStatus.FAILED


@pytest.mark.asyncio
async def test_response_checkpoint_rejects_a_side_effect_recovery_choice(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, workspace = _request(tmp_path)
    components = RuntimeComponentFactory().build(
        replace(request, repair_engine=RepairEngineKind.MINI_NATIVE)
    )
    run = _create_product_run(request, workspace)
    original = AgentForgeMiniNativeHost.generate

    async def interrupt_after_generate(
        self: AgentForgeMiniNativeHost, *args: Any, **kwargs: Any
    ) -> object:
        await original(self, *args, **kwargs)
        raise RuntimeError("interrupt after generate returns")

    monkeypatch.setattr(AgentForgeMiniNativeHost, "generate", interrupt_after_generate)
    with pytest.raises(RuntimeError, match="interrupt after generate returns"):
        await components.runtime.execute(run.run_id)

    monkeypatch.undo()
    reopened = RuntimeComponentFactory().build(
        replace(request, repair_engine=RepairEngineKind.MINI_NATIVE)
    )
    outcome = await reopened.runtime.resume(
        ResumeRun(
            command_id=uuid4(),
            run_id=run.run_id,
            recovery_choice=ResumeRecoveryChoice.MUTATION,
        )
    )

    assert outcome.value is None
    assert outcome.disposition == "REJECTED"


@pytest.mark.asyncio
async def test_mini_native_checkpoints_before_control_returns_from_generate(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, workspace = _request(tmp_path)
    components = RuntimeComponentFactory().build(
        replace(request, repair_engine=RepairEngineKind.MINI_NATIVE)
    )
    run = _create_product_run(request, workspace)
    original = AgentForgeMiniNativeHost.generate

    async def interrupt_after_generate(
        self: AgentForgeMiniNativeHost, *args: Any, **kwargs: Any
    ) -> object:
        await original(self, *args, **kwargs)
        raise RuntimeError("interrupt after generate returns")

    monkeypatch.setattr(AgentForgeMiniNativeHost, "generate", interrupt_after_generate)
    with pytest.raises(RuntimeError, match="interrupt after generate returns"):
        await components.runtime.execute(run.run_id)

    monkeypatch.undo()
    reopened = RuntimeComponentFactory().build(
        replace(request, repair_engine=RepairEngineKind.MINI_NATIVE)
    )
    resumed = await reopened.runtime.resume(run.run_id)

    assert resumed.status is RunStatus.WAITING_APPROVAL
    assert len(request.provider.requests) == 2


@pytest.mark.asyncio
async def test_product_resume_command_recovers_a_running_mini_native_response_checkpoint(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, workspace = _request(tmp_path)
    components = RuntimeComponentFactory().build(
        replace(request, repair_engine=RepairEngineKind.MINI_NATIVE)
    )
    run = _create_product_run(request, workspace)
    original = AgentForgeMiniNativeHost.generate

    async def interrupt_after_generate(
        self: AgentForgeMiniNativeHost, *args: Any, **kwargs: Any
    ) -> object:
        await original(self, *args, **kwargs)
        raise RuntimeError("interrupt after generate returns")

    monkeypatch.setattr(AgentForgeMiniNativeHost, "generate", interrupt_after_generate)
    with pytest.raises(RuntimeError, match="interrupt after generate returns"):
        await components.runtime.execute(run.run_id)

    monkeypatch.undo()
    reopened = RuntimeComponentFactory().build(
        replace(request, repair_engine=RepairEngineKind.MINI_NATIVE)
    )
    outcome = await reopened.runtime.resume(ResumeRun(command_id=uuid4(), run_id=run.run_id))

    assert outcome.value is not None
    assert outcome.value.status is RunStatus.WAITING_APPROVAL
    assert len(request.provider.requests) == 2


@pytest.mark.asyncio
async def test_mini_native_reuses_a_saved_approval_free_action_result_on_restart(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, workspace = _request(tmp_path)
    components = RuntimeComponentFactory().build(
        replace(request, repair_engine=RepairEngineKind.MINI_NATIVE)
    )
    run = _create_product_run(request, workspace)
    original = AgentForgeMiniNativeHost.execute

    async def interrupt_after_action_result(
        self: AgentForgeMiniNativeHost, *args: Any, **kwargs: Any
    ) -> object:
        await original(self, *args, **kwargs)
        raise RuntimeError("interrupt after action result")

    monkeypatch.setattr(AgentForgeMiniNativeHost, "execute", interrupt_after_action_result)
    with pytest.raises(RuntimeError, match="interrupt after action result"):
        await components.runtime.execute(run.run_id)

    monkeypatch.undo()
    reopened = RuntimeComponentFactory().build(
        replace(request, repair_engine=RepairEngineKind.MINI_NATIVE)
    )
    original_tool_execute = reopened.runtime._tools.execute

    async def reject_duplicate_read(*args: Any, **kwargs: Any) -> object:
        if args[1] == "read_file":
            raise AssertionError("recovery re-executed saved read action")
        return await original_tool_execute(*args, **kwargs)

    monkeypatch.setattr(reopened.runtime._tools, "execute", reject_duplicate_read)
    resumed = await reopened.runtime.resume(run.run_id)

    assert resumed.status is RunStatus.WAITING_APPROVAL


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
