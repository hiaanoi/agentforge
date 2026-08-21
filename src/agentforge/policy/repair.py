import hashlib
import json
from datetime import UTC, datetime

from pydantic import BaseModel

from agentforge.domain.enums import PolicyOutcome, ToolCapability, ToolErrorCode, ToolRisk
from agentforge.domain.models import Run, ToolSpec
from agentforge.domain.mutations import MutationPlan
from agentforge.domain.repair import BudgetKind, RepairCompletionStatus
from agentforge.domain.test_execution import TestExecutionPlan
from agentforge.persistence.event_log import RunLeaseAuthority
from agentforge.persistence.repair_workflow import RepairWorkflow
from agentforge.policy.models import PolicyDecision


class RepairPolicyEnforcer:
    def __init__(self, workflow: RepairWorkflow) -> None:
        self._workflow = workflow

    def evaluate(
        self,
        run: Run,
        tool_name: str,
        spec: ToolSpec,
        arguments: BaseModel,
        *,
        prepared: MutationPlan | TestExecutionPlan | None,
        trusted_final_verification: bool = False,
        authority: RunLeaseAuthority,
    ) -> PolicyDecision | None:
        policy = self._workflow.get_policy(run.run_id)
        state = self._workflow.get_state(run.run_id)
        if state.policy_digest != policy.policy_digest:
            return self._deny(
                run,
                tool_name,
                arguments,
                "policy_digest",
                severe=True,
                authority=authority,
            )
        if datetime.now(UTC) >= state.deadline_at:
            return PolicyDecision(
                decision=PolicyOutcome.DENY,
                reason="Repair task wall-time budget is exhausted",
                matched_rule="repair_wall_time",
                metadata={"error_type": ToolErrorCode.REPAIR_BUDGET_EXCEEDED.value},
            )
        if state.status is not RepairCompletionStatus.RUNNING:
            return PolicyDecision(
                decision=PolicyOutcome.DENY,
                reason="Repair task is no longer executable",
                matched_rule="repair_terminal_state",
                metadata={"error_type": ToolErrorCode.REPAIR_POLICY_DENIED.value},
            )
        if spec.capability is ToolCapability.CANDIDATE_PATCH_PUBLICATION:
            if spec.risk_level is not ToolRisk.WRITE or not spec.requires_approval:
                return self._deny(
                    run,
                    tool_name,
                    arguments,
                    "candidate_patch_capability_binding",
                    severe=True,
                    authority=authority,
                )
            return None
        if spec.risk_level is ToolRisk.READ:
            return (
                self._budget_denied("repair_read_budget")
                if state.read_calls_used >= policy.max_read_calls
                else None
            )
        if spec.risk_level is ToolRisk.WRITE:
            if state.edit_attempts_used >= policy.max_edit_attempts:
                return self._budget_denied("repair_edit_budget")
            if not isinstance(prepared, MutationPlan):
                return self._deny(
                    run,
                    tool_name,
                    arguments,
                    "mutation_plan_missing",
                    severe=True,
                    authority=authority,
                )
            rule = policy.write_rule(
                prepared.target_path,
                creating=not prepared.target_existed,
            )
            if rule != "ALLOWED":
                return self._deny(
                    run,
                    tool_name,
                    arguments,
                    f"repair_path_{rule.casefold()}",
                    severe=rule in {"FORBIDDEN", "PROTECTED"},
                    authority=authority,
                )
            if prepared.bytes_written > policy.max_single_file_changed_bytes:
                return self._deny(
                    run,
                    tool_name,
                    arguments,
                    "repair_single_file_size",
                    severe=False,
                    authority=authority,
                )
            return None
        if spec.risk_level is ToolRisk.DANGEROUS:
            if state.test_runs_used >= policy.max_test_runs:
                return self._budget_denied("repair_test_budget")
            if not isinstance(prepared, TestExecutionPlan):
                return self._deny(
                    run,
                    tool_name,
                    arguments,
                    "test_plan_missing",
                    severe=True,
                    authority=authority,
                )
            if prepared.profile_id == policy.final_verification_profile_id:
                if trusted_final_verification and state.pending_final_verification:
                    return None
                return self._deny(
                    run,
                    tool_name,
                    arguments,
                    "hidden_profile_model_selection",
                    severe=False,
                    authority=authority,
                )
            if not policy.allows_development_profile(prepared.profile_id):
                return self._deny(
                    run,
                    tool_name,
                    arguments,
                    "development_profile_not_allowed",
                    severe=False,
                    authority=authority,
                )
            return None
        return self._deny(
            run,
            tool_name,
            arguments,
            "unsupported_repair_capability",
            severe=True,
            authority=authority,
        )

    def record_started(
        self,
        run: Run,
        tool_name: str,
        spec: ToolSpec,
        arguments: BaseModel,
        *,
        authority: RunLeaseAuthority,
    ) -> PolicyDecision | None:
        if spec.risk_level is not ToolRisk.READ:
            return None
        canonical = json.dumps(
            {
                "run_id": str(run.run_id),
                "step": run.current_step,
                "tool": tool_name,
                "arguments": arguments.model_dump(mode="json"),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        decision = self._workflow.consume_budget(
            run.run_id,
            BudgetKind.READ,
            hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            authority=authority,
        )
        return (
            self._budget_denied("repair_read_budget")
            if not decision.consumed
            else None
        )

    def _deny(
        self,
        run: Run,
        tool_name: str,
        arguments: BaseModel,
        rule: str,
        *,
        severe: bool,
        authority: RunLeaseAuthority,
    ) -> PolicyDecision:
        canonical = json.dumps(
            {
                "run_id": str(run.run_id),
                "step": run.current_step,
                "tool": tool_name,
                "arguments": arguments.model_dump(mode="json"),
                "rule": rule,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        fact_id = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        self._workflow.record_policy_violation(
            run.run_id,
            fact_id=fact_id,
            rule=rule,
            severe=severe,
            authority=authority,
        )
        return PolicyDecision(
            decision=PolicyOutcome.DENY,
            reason="Tool request is not permitted by the bound repair task policy",
            matched_rule=rule,
            metadata={"error_type": ToolErrorCode.REPAIR_POLICY_DENIED.value},
        )

    @staticmethod
    def _budget_denied(rule: str) -> PolicyDecision:
        return PolicyDecision(
            decision=PolicyOutcome.DENY,
            reason="Repair task budget is exhausted for this capability",
            matched_rule=rule,
            metadata={"error_type": ToolErrorCode.REPAIR_BUDGET_EXCEEDED.value},
        )
