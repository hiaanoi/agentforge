from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from time import perf_counter
from uuid import UUID, uuid4

from pydantic import JsonValue

from agentforge.application.run_driver import RunOwnership
from agentforge.domain.enums import EventType, ResumePhase
from agentforge.domain.models import ApprovalRequired, Run, ToolResult
from agentforge.models.base import ModelProvider, ModelRequest, ToolCall
from agentforge.models.domain import ModelResponse
from agentforge.models.executor import ModelExecutor
from agentforge.persistence.model_workflow import ModelWorkflow
from agentforge.persistence.repositories import CheckpointRepository, EventRepository, RunRepository
from agentforge.repair_engines.mini_native.contracts import (
    CandidatePatchResult,
    MiniNativeState,
    RepairAction,
    RepairActionKind,
    RepairActionResult,
)
from agentforge.repair_engines.mini_native.host import bound_output
from agentforge.runtime.candidate_patch import CandidatePatchPublisher, CandidatePatchStore
from agentforge.runtime.snapshots import RuntimeSnapshotV4
from agentforge.tools.executor import ToolExecutor


class MiniNativeApprovalPaused(RuntimeError):
    """Stops the vendored loop after AgentForge has durably queued an approval."""


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
        tools: ToolExecutor,
        candidate_publisher: CandidatePatchPublisher,
        candidate_store: CandidatePatchStore,
        workspace: str,
        pause_for_approval: Callable[[ToolCall, ApprovalRequired, list[JsonValue]], None],
        history: list[JsonValue] | None = None,
    ) -> None:
        self._run = run
        self._ownership = ownership
        self._runs = runs
        self._events = events
        self._checkpoints = checkpoints
        self._provider = provider
        self._model_executor = model_executor
        self._tools = tools
        self._candidate_publisher = candidate_publisher
        self._candidate_store = candidate_store
        self._workspace = workspace
        self._pause_for_approval = pause_for_approval
        self._history = list(history or [])

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self._run.current_step += 1
        self._runs.save(self._run, authority=self._ownership.authority)
        bound_request = request.model_copy(update={"step_number": self._run.current_step})
        model_call_id = uuid4()
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
                "provider_metadata": response.sanitized_metadata,
            },
        )
        return response

    async def execute(self, action: RepairAction) -> RepairActionResult:
        if action.run_id != self._run.run_id:
            raise ValueError("Mini-native action belongs to another Run")
        if action.kind is RepairActionKind.FINAL:
            return await self._queue_candidate_publication(action)
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
        return self._result(action, outcome, started)

    async def checkpoint(self, state: MiniNativeState) -> None:
        if state.run_id != self._run.run_id:
            raise ValueError("Mini-native checkpoint belongs to another Run")
        self._history = list(state.history)
        snapshot = RuntimeSnapshotV4(
            run_id=self._run.run_id,
            step_number=self._run.current_step,
            history=self._history,
            resume_phase=ResumePhase.READY_FOR_MODEL,
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

    async def publish(self, run_id: UUID) -> CandidatePatchResult:
        if run_id != self._run.run_id:
            raise ValueError("Candidate publication belongs to another Run")
        patch = self._candidate_publisher.capture(Path(self._workspace))
        self._candidate_store.save(str(run_id), patch)
        digest = hashlib.sha256(
            json.dumps(
                [entry.target_path for entry in patch.entries],
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        return CandidatePatchResult(run_id=run_id, published=False, patch_digest=digest)

    async def _queue_candidate_publication(self, action: RepairAction) -> RepairActionResult:
        await self.publish(action.run_id)
        call = ToolCall(
            type="tool_call",
            call_id=str(action.action_id),
            tool="publish_candidate_patch",
            arguments={"run_id": str(action.run_id)},
            reason="Publish the mini-native candidate patch after approval",
        )
        outcome = await self._tools.execute(
            self._run, call.tool, call.arguments, ownership=self._ownership
        )
        if not isinstance(outcome, ApprovalRequired):
            assert isinstance(outcome, ToolResult)
            return self._result(action, outcome, perf_counter())
        self._pause(action, outcome, call=call)
        raise AssertionError("approval pause must not return")

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
        self._pause_for_approval(queued, required, list(self._history))
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
        if action.kind is RepairActionKind.TEST and isinstance(outcome.output, dict):
            exit_code = outcome.output.get("exit_code")
            if isinstance(exit_code, int):
                returncode = exit_code
        return RepairActionResult(
            returncode=returncode,
            stdout=output,
            duration_ms=max(outcome.duration_ms, int((perf_counter() - started) * 1000)),
            truncated=outcome.truncated or output_truncated,
        )

    def _append_event(self, event_type: EventType, payload: dict[str, JsonValue]) -> None:
        with self._events.database.session() as session:
            EventRepository.append_in_session(
                session, self._ownership.authority, event_type, payload
            )


__all__ = ["AgentForgeMiniNativeHost", "MiniNativeApprovalPaused"]
