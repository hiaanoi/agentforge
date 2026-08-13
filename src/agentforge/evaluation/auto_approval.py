import os
import tempfile
from pathlib import Path
from uuid import UUID, uuid4

from anyio import Path as AsyncPath
from pydantic import BaseModel, ConfigDict, JsonValue

from agentforge.domain.enums import EventType, RunStatus
from agentforge.domain.models import ApprovalRequest, Run, normalize_utc, utc_now
from agentforge.domain.repair import RepairCompletionStatus, RepairTaskPolicy
from agentforge.persistence.legacy_evaluator import LegacyEvaluatorEventRepository
from agentforge.persistence.repair_workflow import RepairWorkflow
from agentforge.runtime.engine import AgentRuntime


class EvaluationWorkspaceHandle(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workspace_root: str
    run_id: UUID
    policy_digest: str
    nonce: UUID

    @classmethod
    def create(
        cls,
        workspace: Path,
        *,
        run_id: UUID,
        policy_digest: str,
    ) -> "EvaluationWorkspaceHandle":
        resolved = workspace.resolve(strict=True)
        temporary_root = Path(tempfile.gettempdir()).resolve(strict=True)
        try:
            common = Path(os.path.commonpath((resolved, temporary_root)))
        except ValueError as exc:
            raise ValueError("Evaluation workspace must be under the system temp root") from exc
        if os.path.normcase(str(common)) != os.path.normcase(str(temporary_root)):
            raise ValueError("Evaluation workspace must be under the system temp root")
        if not resolved.is_dir():
            raise ValueError("Evaluation workspace must be a directory")
        return cls(
            workspace_root=str(resolved),
            run_id=run_id,
            policy_digest=policy_digest,
            nonce=uuid4(),
        )


class AutoApprovalHarness:
    def __init__(
        self,
        runtime: AgentRuntime,
        events: LegacyEvaluatorEventRepository,
        workspace: EvaluationWorkspaceHandle,
        repairs: RepairWorkflow,
    ) -> None:
        self._runtime = runtime
        self._events = events
        self._workspace = workspace
        self._repairs = repairs

    async def process(self, run_id: UUID) -> Run:
        root = AsyncPath(self._workspace.workspace_root)
        if not await root.is_dir():
            self._deny(run_id, "evaluation_workspace_missing")
        policy = self._repairs.get_policy(run_id)
        state = self._repairs.get_state(run_id)
        if (
            self._workspace.run_id != run_id
            or self._workspace.policy_digest != policy.policy_digest
            or state.policy_digest != policy.policy_digest
        ):
            self._deny(run_id, "evaluation_binding_mismatch")
        if (
            state.status is not RepairCompletionStatus.RUNNING
            or utc_now() > normalize_utc(state.deadline_at)
        ):
            self._deny(run_id, "repair_state_not_approvable")
        pending = self._runtime.list_pending_approvals(run_id)
        if len(pending) != 1:
            self._deny(
                run_id,
                "expected_one_pending_approval",
                {"count": len(pending)},
            )
        approval = pending[0]
        if (
            approval.tool_name in {"edit_file", "write_file"}
            and state.edit_attempts_used >= policy.max_edit_attempts
        ) or (
            approval.tool_name == "run_tests"
            and state.test_runs_used >= policy.max_test_runs
        ):
            self._deny(run_id, "side_effect_budget")
        if approval.run_id != run_id or not self._eligible(approval, policy, state):
            self._deny(
                run_id,
                "request_not_policy_eligible",
                {"tool_name": approval.tool_name},
            )
        self._runtime.approve(approval.approval_id, note="evaluation_harness")
        self._events.append(
            run_id,
            EventType.AUTO_APPROVAL_GRANTED,
            {
                "approval_id": str(approval.approval_id),
                "tool_name": approval.tool_name,
            },
        )
        result = await self._runtime.resume_evaluator(run_id)
        if result.status is RunStatus.WAITING_APPROVAL:
            return await self.process(run_id)
        return result

    @staticmethod
    def _eligible(
        approval: ApprovalRequest,
        policy: RepairTaskPolicy,
        state: object,
    ) -> bool:
        from agentforge.domain.repair import RepairState

        if not isinstance(state, RepairState):
            return False
        arguments = approval.sanitized_arguments
        if approval.tool_name in {"edit_file", "write_file"}:
            path = arguments.get("path")
            if not isinstance(path, str) or state.edit_attempts_used >= policy.max_edit_attempts:
                return False
            if approval.tool_name == "write_file":
                mode = arguments.get("mode")
                if mode not in {"CREATE_ONLY", "EXPECTED_HASH_REPLACE"}:
                    return False
                creating = mode == "CREATE_ONLY"
            else:
                creating = False
            return policy.allows_write(path, creating=creating)
        if approval.tool_name == "run_tests":
            profile_id = arguments.get("profile_id")
            if not isinstance(profile_id, str) or state.test_runs_used >= policy.max_test_runs:
                return False
            if profile_id == policy.final_verification_profile_id:
                return state.pending_final_verification
            return policy.allows_development_profile(profile_id)
        return False

    def _deny(
        self,
        run_id: UUID,
        reason: str,
        metadata: dict[str, JsonValue] | None = None,
    ) -> None:
        payload: dict[str, JsonValue] = {"reason": reason}
        if metadata:
            payload.update(metadata)
        self._events.append(
            run_id,
            EventType.AUTO_APPROVAL_DENIED,
            payload,
        )
        if reason == "evaluation_binding_mismatch":
            raise RuntimeError("AutoApproval evaluation binding is invalid")
        if reason.endswith("budget"):
            raise RuntimeError("AutoApproval budget is exhausted")
        if reason == "request_not_policy_eligible":
            raise RuntimeError("Approval request is not policy eligible")
        raise RuntimeError("AutoApproval precondition failed")
