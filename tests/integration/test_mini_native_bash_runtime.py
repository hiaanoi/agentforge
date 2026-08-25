from __future__ import annotations

import os
import sys
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from agentforge.application.contracts import ProfilePurpose
from agentforge.application.runtime_factory import (
    RuntimeAssemblyRequest,
    RuntimeComponentFactory,
)
from agentforge.domain.enums import EventType, RunStatus
from agentforge.domain.repair import RepairTaskPolicy
from agentforge.models.base import ModelRequest, parse_model_output
from agentforge.models.domain import ModelResponse
from agentforge.persistence.profile_trust import ProfileKernel
from agentforge.repair_engines.mini_native.agentforge_host import AgentForgeMiniNativeHost
from agentforge.repair_engines.mini_native.environment import BashObservation
from agentforge.repair_engines.models import RepairEngineKind
from agentforge.tools.testing.profiles import TestProfileDefinition as ProfileDefinition
from tests.integration.test_mini_native_runtime import _create_product_run, _request


class _BashProvider:
    name = "scripted"
    journal_identity = "scripted/mini-native-bash"

    def __init__(self) -> None:
        self.requests: list[ModelRequest] = []
        self._commands = [
            "rg -n VALUE src/value.py",
            "sed -i 's/VALUE = 0/VALUE = 2/' src/value.py",
            "python -m pytest -q",
            "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT",
        ]

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request.model_copy(deep=True))
        command = self._commands.pop(0)
        return ModelResponse(
            action=parse_model_output(
                {
                    "type": "tool_call",
                    "tool": "bash",
                    "call_id": f"bash-{len(self.requests)}",
                    "arguments": {"command": command},
                }
            ),
            provider=self.name,
            model="mini-native-bash",
            duration_ms=0,
            attempt_count=1,
        )


class _FakeBashEnvironment:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.commands: list[str] = []

    async def execute(
        self,
        command: str,
        *,
        cwd: str,
        timeout_seconds: float | None,
    ) -> BashObservation:
        del cwd, timeout_seconds
        self.commands.append(command)
        target = self.workspace / "src" / "value.py"
        if command.startswith("rg "):
            output = f"src/value.py:1:{target.read_text(encoding='utf-8').strip()}\n"
        elif command.startswith("sed -i "):
            target.write_text("VALUE = 2\n", encoding="utf-8")
            output = ""
        elif command.startswith("echo COMPLETE_TASK"):
            output = "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\n"
        else:  # managed test commands must not bypass the test coordinator
            raise AssertionError(f"unexpected direct bash execution: {command}")
        return BashObservation(
            output=output,
            returncode=0,
            exception_info=None,
            timed_out=False,
            duration_ms=1,
        )


def _bash_request(
    tmp_path: Path,
) -> tuple[RuntimeAssemblyRequest, Path, _BashProvider]:
    request, workspace = _request(tmp_path)
    provider = _BashProvider()
    request.profiles.register(
        ProfileDefinition(
            profile_id="bash_visible",
            name="bash visible",
            description="Exact managed profile for the model's bash pytest command",
            executable=sys.executable,
            argv=("-m", "pytest", "-q"),
            cwd=".",
            timeout_seconds=30,
            max_output_bytes=100_000,
            profile_version=1,
            purpose=ProfilePurpose.DEVELOPMENT,
        )
    )
    profile = request.profiles.get("bash_visible")
    ProfileKernel(request.database, request.profiles).trust(
        ProfileKernel(request.database, request.profiles).challenge(
            profile.profile_id, purpose=profile.purpose
        ),
        command_id=uuid4(),
    )
    policy_payload = request.policy.model_dump(mode="python", exclude={"policy_digest"})
    policy_payload.update(
        allowed_development_test_profiles=(
            *request.policy.allowed_development_test_profiles,
            "bash_visible",
        ),
        path_case_sensitive=os.path.normcase("A") != os.path.normcase("a"),
    )
    policy = RepairTaskPolicy.model_validate(policy_payload)
    return replace(request, provider=provider, policy=policy), workspace, provider


@pytest.mark.asyncio
async def test_bash_runtime_approves_recovers_tests_and_publishes_without_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, workspace, provider = _bash_request(tmp_path)
    components = RuntimeComponentFactory().build(
        replace(request, repair_engine=RepairEngineKind.MINI_NATIVE)
    )
    environment = _FakeBashEnvironment(workspace)
    components.runtime._mini_native_environment = environment
    run = _create_product_run(request, workspace)

    waiting = await components.runtime.execute(run.run_id)

    assert waiting.status is RunStatus.WAITING_APPROVAL
    [write_approval] = components.runtime.list_pending_approvals(run.run_id)
    assert write_approval.tool_name == "bash"
    components.runtime.approve(write_approval.approval_id)

    original = AgentForgeMiniNativeHost.execute_approved_bash

    async def interrupt_after_saved_observation(
        self: AgentForgeMiniNativeHost, *args: object, **kwargs: object
    ) -> object:
        await original(self, *args, **kwargs)
        raise RuntimeError("interrupt after approved bash observation")

    monkeypatch.setattr(
        AgentForgeMiniNativeHost,
        "execute_approved_bash",
        interrupt_after_saved_observation,
    )
    with pytest.raises(RuntimeError, match="interrupt after approved bash observation"):
        await components.runtime.resume(run.run_id)

    monkeypatch.undo()
    reopened = RuntimeComponentFactory().build(
        replace(request, repair_engine=RepairEngineKind.MINI_NATIVE)
    )
    reopened.runtime._mini_native_environment = environment.execute
    waiting = await reopened.runtime.resume(run.run_id)

    assert waiting.status is RunStatus.WAITING_APPROVAL
    assert environment.commands.count("sed -i 's/VALUE = 0/VALUE = 2/' src/value.py") == 1
    [test_approval] = reopened.runtime.list_pending_approvals(run.run_id)
    assert test_approval.tool_name == "run_tests"
    reopened.runtime.approve(test_approval.approval_id)

    waiting = await reopened.runtime.resume(run.run_id)

    assert waiting.status is RunStatus.WAITING_APPROVAL
    [candidate_approval] = reopened.runtime.list_pending_approvals(run.run_id)
    assert candidate_approval.tool_name == "publish_candidate_patch"
    reopened.runtime.approve(candidate_approval.approval_id)

    completed = await reopened.runtime.resume(run.run_id)

    assert completed.run_id == run.run_id
    assert completed.status is RunStatus.COMPLETED
    assert (workspace / "src" / "value.py").read_text(encoding="utf-8") == "VALUE = 2\n"
    assert len(provider.requests) == 4
    assert environment.commands == [
        "rg -n VALUE src/value.py",
        "sed -i 's/VALUE = 0/VALUE = 2/' src/value.py",
        "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT",
    ]
    assert len(reopened.test_coordinator.list_executions(run.run_id)) == 1
    events = request.events.list_for_run(run.run_id)
    event_types = {event.event_type for event in events}
    assert {
        EventType.MODEL_REQUESTED,
        EventType.BASH_REQUESTED,
        EventType.APPROVAL_REQUESTED,
        EventType.BASH_COMPLETED,
        EventType.CHECKPOINT_SAVED,
        EventType.RUN_COMPLETED,
    }.issubset(event_types)
    assert [event.sequence_number for event in events] == list(
        range(1, len(events) + 1)
    )
