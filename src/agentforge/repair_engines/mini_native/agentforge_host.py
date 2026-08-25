from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from time import perf_counter
from typing import Protocol
from uuid import UUID, uuid4

from pydantic import JsonValue

from agentforge.application.run_driver import RunOwnership
from agentforge.domain.enums import EventType, ResumePhase
from agentforge.domain.models import ApprovalRequired, Run, ToolResult
from agentforge.models.base import ModelProvider, ModelRequest, ToolCall
from agentforge.models.domain import ModelResponse, ModelUsage
from agentforge.models.executor import ModelExecutor
from agentforge.persistence.model_workflow import ModelWorkflow
from agentforge.persistence.repositories import CheckpointRepository, EventRepository, RunRepository
from agentforge.repair_engines.mini_native.classifier import CommandKind, classify_command
from agentforge.repair_engines.mini_native.contracts import (
    CandidatePatchResult,
    MiniNativeState,
    RepairAction,
    RepairActionKind,
    RepairActionResult,
    action_history_item,
    to_repair_action,
)
from agentforge.repair_engines.mini_native.environment import BashObservation
from agentforge.repair_engines.mini_native.host import bound_output
from agentforge.runtime.candidate_patch import CandidatePatchPublisher, CandidatePatchStore
from agentforge.runtime.snapshots import RuntimeSnapshotV5
from agentforge.tools.executor import ToolExecutor


class MiniNativeApprovalPaused(RuntimeError):
    """Stops the vendored loop after AgentForge has durably queued an approval."""


class MiniNativeBashEnvironment(Protocol):
    async def execute(
        self,
        command: str,
        *,
        cwd: str,
        timeout_seconds: float | None,
    ) -> BashObservation: ...


BashEnvironmentCallback = Callable[..., Awaitable[BashObservation]]


class AgentForgeMiniNativeHost:
    """Control-plane adapter for the vendored mini-native repair loop."""

    def __init__(
        self,
        *,
        run: Run,
        ownership: RunOwnership,
        runs: RunRepository,
        events: EventRepository,
        checkpoints: CheckpointRepository,
        provider: ModelProvider,
        model_executor: ModelExecutor | None,
        model_workflow: ModelWorkflow | None,
        tools: ToolExecutor,
        candidate_publisher: CandidatePatchPublisher,
        candidate_store: CandidatePatchStore,
        workspace: str,
        environment: MiniNativeBashEnvironment | BashEnvironmentCallback,
        test_profile_for_command: Callable[[str], str | None],
        pause_for_approval: Callable[
            [
                ToolCall,
                ApprovalRequired,
                list[JsonValue],
                bool,
                dict[str, JsonValue],
                ModelUsage,
                RepairAction | None,
                RepairActionResult | None,
            ],
            None,
        ],
        history: list[JsonValue] | None = None,
        last_test_passed: bool = False,
        provider_usage_available: bool = False,
        last_provider_metadata: dict[str, JsonValue] | None = None,
        last_model_usage: ModelUsage | None = None,
        pending_action: RepairAction | None = None,
        pending_action_result: RepairActionResult | None = None,
    ) -> None:
        self._run = run
        self._ownership = ownership
        self._runs = runs
        self._events = events
        self._checkpoints = checkpoints
        self._provider = provider
        self._model_executor = model_executor
        self._model_workflow = model_workflow
        self._tools = tools
        self._candidate_publisher = candidate_publisher
        self._candidate_store = candidate_store
        self._workspace = workspace
        self._environment = environment
        self._test_profile_for_command = test_profile_for_command
        self._pause_for_approval = pause_for_approval
        self._history = list(history or [])
        self._last_test_passed = last_test_passed
        self._last_usage = last_model_usage
        self._provider_usage_available = provider_usage_available
        self._last_provider_metadata = dict(last_provider_metadata or {})
        self._pending_action = pending_action
        self._pending_action_result = pending_action_result

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self._run.current_step += 1
        self._runs.save(self._run, authority=self._ownership.authority)
        model_call_id = uuid4()
        bound_request = request.model_copy(
            update={"step_number": self._run.current_step, "model_call_id": model_call_id}
        )
        request_digest = ModelWorkflow.request_digest(bound_request)
        audit_payload: dict[str, JsonValue] = {
            "step_number": self._run.current_step,
            "model_call_id": str(model_call_id),
            "request_digest": request_digest,
        }
        self._append_event(EventType.MODEL_REQUESTED, audit_payload)
        if self._model_executor is not None:
            response = await self._model_executor.generate(
                self._run, bound_request, ownership=self._ownership
            )
        else:
            response = await self._provider.generate(bound_request)
        response = response.model_copy(update={"model_call_id": model_call_id})
        self._last_usage = response.usage
        self._provider_usage_available = response.usage is not None
        self._last_provider_metadata = dict(response.sanitized_metadata)
        if response.usage is not None:
            self._run.total_token_usage += response.usage.total_tokens or 0
            self._runs.save(self._run, authority=self._ownership.authority)
        self._append_event(
            EventType.MODEL_RESPONDED,
            {
                **audit_payload,
                "response_type": response.action.type,
                "provider": response.provider,
                "model": response.model,
                "usage": (
                    response.usage.model_dump(mode="json") if response.usage is not None else None
                ),
                "usage_available": self._provider_usage_available,
                "provider_metadata": self._last_provider_metadata,
            },
        )
        action = to_repair_action(
            response.action,
            run_id=self._run.run_id,
            step=self._run.current_step,
            working_directory=self._workspace,
            parent_model_call_id=response.model_call_id,
        )
        self._history.append(action_history_item(action))
        self._save_checkpoint(mini_native_pending_action=action)
        return response

    async def execute(self, action: RepairAction) -> RepairActionResult:
        if action.run_id != self._run.run_id:
            raise ValueError("Mini-native action belongs to another Run")
        if action.kind is RepairActionKind.FINAL:
            return await self._queue_candidate_publication(action)
        if action.kind is RepairActionKind.BASH:
            return await self._execute_bash(action)
        started = perf_counter()
        outcome = await self._tools.execute(
            self._run,
            action.tool_name,
            action.arguments,
            ownership=self._ownership,
        )
        if isinstance(outcome, ApprovalRequired):
            self._pause(action, outcome)
        assert isinstance(outcome, ToolResult)
        result = self._result(action, outcome, started)
        self._save_checkpoint(
            mini_native_pending_action=action,
            mini_native_pending_action_result=result,
        )
        return result

    async def execute_approved_bash(
        self,
        action: RepairAction,
        *,
        approval_id: UUID,
        approval_digest: str,
    ) -> RepairActionResult:
        """Execute one already-claimed bash write and durably save its observation."""
        if action.run_id != self._run.run_id or action.kind is not RepairActionKind.BASH:
            raise ValueError("Approved bash action identity does not match this Run")
        result = await self._execute_environment(action)
        self._save_checkpoint(
            mini_native_pending_action=action,
            mini_native_pending_action_result=result,
            resume_phase=ResumePhase.AWAITING_APPROVAL,
            pending_approval_id=approval_id,
            tool_call_digest=approval_digest,
        )
        self._append_bash_completed(action, result, executed=True)
        return result

    def complete_managed_bash(
        self,
        action: RepairAction,
        outcome: ToolResult,
    ) -> RepairActionResult:
        """Translate a managed test result back into the upstream bash observation."""
        result = self._result(action, outcome, perf_counter())
        self._run.tool_call_count += 1
        self._runs.save(self._run, authority=self._ownership.authority)
        self._append_bash_completed(action, result, executed=True)
        return result

    def complete_rejected_bash(self, action: RepairAction) -> RepairActionResult:
        result = RepairActionResult(
            returncode=1,
            stderr="AgentForge approval rejected the bash command",
            duration_ms=0,
        )
        self._append_bash_completed(action, result, executed=False)
        return result

    async def _execute_bash(self, action: RepairAction) -> RepairActionResult:
        command = self._command(action)
        kind = classify_command(command)
        self._append_event(
            EventType.BASH_REQUESTED,
            {
                "action_id": str(action.action_id),
                "arguments_digest": action.arguments_digest,
                "command_kind": kind.value,
            },
        )
        if kind is CommandKind.WRITE:
            required = ApprovalRequired(
                tool_name="bash",
                validated_arguments={"command": command},
                sanitized_arguments={
                    "command": "<approved bash write>",
                    "command_digest": action.arguments_digest,
                },
            )
            self._pause(action, required)
        if kind is CommandKind.TEST:
            profile_id = self._test_profile_for_command(command)
            if profile_id is not None:
                outcome = await self._tools.execute(
                    self._run,
                    "run_tests",
                    {"profile_id": profile_id},
                    ownership=self._ownership,
                )
                if isinstance(outcome, ApprovalRequired):
                    self._pause(
                        action,
                        outcome,
                        call=ToolCall(
                            type="tool_call",
                            call_id=str(action.action_id),
                            tool="run_tests",
                            arguments={"profile_id": profile_id},
                            reason="Run the bash test command through its registered profile",
                        ),
                    )
                assert isinstance(outcome, ToolResult)
                result = self.complete_managed_bash(action, outcome)
                self._save_checkpoint(
                    mini_native_pending_action=action,
                    mini_native_pending_action_result=result,
                )
                return result
        result = await self._execute_environment(action)
        self._save_checkpoint(
            mini_native_pending_action=action,
            mini_native_pending_action_result=result,
        )
        self._append_bash_completed(action, result, executed=True)
        return result

    async def _execute_environment(self, action: RepairAction) -> RepairActionResult:
        command = self._command(action)
        executor = (
            self._environment
            if callable(self._environment)
            else self._environment.execute
        )
        observation = await executor(
            command,
            cwd=action.working_directory,
            timeout_seconds=3600.0 if classify_command(command) is CommandKind.TEST else 30.0,
        )
        stderr = observation.exception_info or ""
        result = RepairActionResult(
            returncode=observation.returncode,
            stdout=observation.output,
            stderr=stderr,
            duration_ms=observation.duration_ms,
        )
        self._run.tool_call_count += 1
        self._runs.save(self._run, authority=self._ownership.authority)
        return result

    def _append_bash_completed(
        self,
        action: RepairAction,
        result: RepairActionResult,
        *,
        executed: bool,
    ) -> None:
        self._append_event(
            EventType.BASH_COMPLETED,
            {
                "action_id": str(action.action_id),
                "arguments_digest": action.arguments_digest,
                "returncode": result.returncode,
                "duration_ms": result.duration_ms,
                "truncated": result.truncated,
                "executed": executed,
            },
        )

    @staticmethod
    def _command(action: RepairAction) -> str:
        command = action.arguments.get("command")
        if not isinstance(command, str) or not command.strip():
            raise ValueError("Mini-native bash action requires a non-empty command")
        return command

    async def checkpoint(self, state: MiniNativeState) -> None:
        if state.run_id != self._run.run_id:
            raise ValueError("Mini-native checkpoint belongs to another Run")
        self._history = list(state.history)
        self._last_test_passed = state.last_test_passed
        self._save_checkpoint()

    def _save_checkpoint(
        self,
        *,
        mini_native_pending_action: RepairAction | None = None,
        mini_native_pending_action_result: RepairActionResult | None = None,
        resume_phase: ResumePhase = ResumePhase.READY_FOR_MODEL,
        pending_approval_id: UUID | None = None,
        tool_call_digest: str | None = None,
    ) -> None:
        self._pending_action = mini_native_pending_action
        self._pending_action_result = mini_native_pending_action_result
        model_usage = self._model_usage()
        snapshot = RuntimeSnapshotV5(
            run_id=self._run.run_id,
            step_number=self._run.current_step,
            history=self._history,
            pending_approval_id=pending_approval_id,
            tool_call_digest=tool_call_digest,
            resume_phase=resume_phase,
            model_usage=model_usage,
            model_request_count=(
                self._model_workflow.get_state(self._run.run_id).model_request_count
                if self._model_workflow is not None
                else 0
            ),
            mini_native_pending_action=mini_native_pending_action,
            mini_native_pending_action_result=mini_native_pending_action_result,
            mini_native_last_test_passed=self._last_test_passed,
            provider_usage_available=self._provider_usage_available,
            last_provider_metadata=self._last_provider_metadata,
        )
        checkpoint = self._checkpoints.save(
            self._run.run_id,
            self._run.current_step,
            snapshot.model_dump(mode="json"),
            authority=self._ownership.authority,
        )
        self._append_event(
            EventType.CHECKPOINT_SAVED,
            {"checkpoint_id": str(checkpoint.checkpoint_id), "step_number": checkpoint.step_number},
        )

    def _model_usage(self) -> ModelUsage:
        if self._model_workflow is None:
            return self._last_usage or ModelUsage()
        state = self._model_workflow.get_state(self._run.run_id)
        return ModelUsage(
            input_tokens=state.input_tokens,
            output_tokens=state.output_tokens,
            total_tokens=state.total_tokens,
            cached_input_tokens=state.cached_input_tokens,
            reasoning_tokens=state.reasoning_tokens,
        )

    async def publish(self, run_id: UUID) -> CandidatePatchResult:
        if run_id != self._run.run_id:
            raise ValueError("Candidate publication belongs to another Run")
        patch = self._candidate_publisher.capture(Path(self._workspace))
        self._candidate_store.save(str(run_id), patch)
        candidate = CandidatePatchResult(
            run_id=run_id,
            published=False,
            patch_digest=patch.manifest_digest,
        )
        action = self._pending_action
        if action is None:
            raise ValueError("Candidate publication has no pending bash action")
        if candidate.patch_digest is None:
            raise ValueError("Candidate patch publication is missing its manifest digest")
        call = ToolCall(
            type="tool_call",
            call_id=str(action.action_id),
            tool="publish_candidate_patch",
            arguments={
                "run_id": str(action.run_id),
                "manifest_digest": candidate.patch_digest,
            },
            reason="Publish the mini-native candidate patch after approval",
        )
        outcome = await self._tools.execute(
            self._run, call.tool, call.arguments, ownership=self._ownership
        )
        if isinstance(outcome, ApprovalRequired):
            self._pause(action, outcome, call=call)
        assert isinstance(outcome, ToolResult)
        return candidate.model_copy(update={"published": outcome.success})

    async def _queue_candidate_publication(self, action: RepairAction) -> RepairActionResult:
        self._pending_action = action
        candidate = await self.publish(action.run_id)
        return RepairActionResult(
            returncode=0 if candidate.published else 1,
            stdout="Candidate patch published" if candidate.published else "",
            duration_ms=0,
        )

    def _pause(
        self,
        action: RepairAction,
        required: ApprovalRequired,
        *,
        call: ToolCall | None = None,
    ) -> None:
        queued = call or ToolCall(
            type="tool_call",
            call_id=str(action.action_id),
            tool=action.tool_name,
            arguments=action.arguments,
            reason=f"Mini-native {action.kind.value.lower()} action requires approval",
        )
        self._pause_for_approval(
            queued,
            required,
            list(self._history),
            self._provider_usage_available,
            self._last_provider_metadata,
            self._model_usage(),
            action,
            self._pending_action_result,
        )
        raise MiniNativeApprovalPaused()

    def _result(
        self, action: RepairAction, outcome: ToolResult, started: float
    ) -> RepairActionResult:
        output, output_truncated = bound_output(
            json.dumps(outcome.output, ensure_ascii=False, default=str)
            if outcome.output is not None
            else (outcome.error_message or ""),
        )
        returncode = 0 if outcome.success else 1
        stdout = output
        stderr = ""
        if isinstance(outcome.output, dict):
            exit_code = outcome.output.get("exit_code")
            if isinstance(exit_code, int):
                returncode = exit_code
            stdout_summary = outcome.output.get("stdout_summary")
            stderr_summary = outcome.output.get("stderr_summary")
            if isinstance(stdout_summary, str) or isinstance(stderr_summary, str):
                stdout, stdout_truncated = bound_output(
                    stdout_summary if isinstance(stdout_summary, str) else ""
                )
                stderr, stderr_truncated = bound_output(
                    stderr_summary if isinstance(stderr_summary, str) else ""
                )
                output_truncated = output_truncated or stdout_truncated or stderr_truncated
        return RepairActionResult(
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
            duration_ms=max(outcome.duration_ms, int((perf_counter() - started) * 1000)),
            truncated=outcome.truncated or output_truncated,
        )

    def _append_event(self, event_type: EventType, payload: dict[str, JsonValue]) -> None:
        with self._events.database.session() as session:
            EventRepository.append_in_session(
                session, self._ownership.authority, event_type, payload
            )


__all__ = [
    "AgentForgeMiniNativeHost",
    "BashEnvironmentCallback",
    "MiniNativeApprovalPaused",
    "MiniNativeBashEnvironment",
]
