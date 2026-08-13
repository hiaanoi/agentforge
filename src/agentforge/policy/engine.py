from pathlib import Path

from pydantic import BaseModel

from agentforge.domain.enums import (
    PolicyOutcome,
    RunStatus,
    ToolCapability,
    ToolErrorCode,
    ToolRisk,
    ToolSource,
)
from agentforge.domain.errors import ToolRuntimeError
from agentforge.domain.models import Run, ToolSpec
from agentforge.policy.models import PolicyDecision
from agentforge.policy.sensitive import SensitiveFilePolicy
from agentforge.tools.paths import WorkspacePathResolver


class PolicyEngine:
    def __init__(
        self,
        resolver: WorkspacePathResolver,
        sensitive_files: SensitiveFilePolicy,
    ) -> None:
        self._resolver = resolver
        self._sensitive_files = sensitive_files

    def evaluate(
        self,
        run: Run,
        tool_name: str,
        spec: ToolSpec | None,
        arguments: BaseModel | None,
        *,
        arguments_valid: bool,
        approval_granted: bool = False,
    ) -> PolicyDecision:
        if spec is None:
            return self._deny(
                "tool_exists",
                ToolErrorCode.TOOL_NOT_FOUND,
                f"Tool {tool_name!r} is not registered",
            )
        if not arguments_valid or arguments is None:
            return self._deny(
                "arguments_valid",
                ToolErrorCode.INVALID_ARGUMENTS,
                "Tool arguments failed schema validation",
            )
        if run.status is RunStatus.CANCELLED:
            return self._deny(
                "run_not_cancelled",
                ToolErrorCode.POLICY_DENIED,
                "Cancelled runs cannot execute tools",
            )
        if run.tool_call_count >= run.max_tool_calls:
            return self._deny(
                "tool_call_budget",
                ToolErrorCode.TOOL_BUDGET_EXCEEDED,
                "Run tool-call budget is exhausted",
            )
        if spec.source is not ToolSource.LOCAL:
            return self._deny(
                "local_tools_only",
                ToolErrorCode.POLICY_DENIED,
                "This milestone permits local tools only",
            )
        if spec.risk_level is ToolRisk.DANGEROUS:
            if (
                spec.capability is not ToolCapability.TEST_PROFILE_EXECUTION
                or not spec.requires_approval
            ):
                return self._deny(
                    "dangerous_tools_denied",
                    ToolErrorCode.POLICY_DENIED,
                    "Only registered test-profile execution may request DANGEROUS approval",
                )
        if spec.risk_level is ToolRisk.WRITE and not spec.requires_approval:
            return self._deny(
                "write_requires_approval",
                ToolErrorCode.POLICY_DENIED,
                "WRITE tools must require explicit approval",
            )

        path_decision = self._check_path(spec, arguments)
        if path_decision is not None:
            return path_decision
        if spec.requires_approval and not approval_granted:
            return PolicyDecision(
                decision=PolicyOutcome.REQUIRE_APPROVAL,
                reason="Tool requires explicit approval before execution",
                matched_rule="approval_required",
                metadata={"error_type": ToolErrorCode.APPROVAL_REQUIRED.value},
            )

        if spec.risk_level is ToolRisk.WRITE:
            return PolicyDecision(
                decision=PolicyOutcome.ALLOW,
                reason="Approved local WRITE request satisfies mutation policy",
                matched_rule="approved_write_tool_allowed",
            )
        if spec.risk_level is ToolRisk.DANGEROUS:
            return PolicyDecision(
                decision=PolicyOutcome.ALLOW,
                reason="Approved test profile satisfies managed execution policy",
                matched_rule="approved_test_profile_allowed",
            )
        return PolicyDecision(
            decision=PolicyOutcome.ALLOW,
            reason="Tool request satisfies local READ policy",
            matched_rule="read_tool_allowed",
        )

    def _check_path(self, spec: ToolSpec, arguments: BaseModel) -> PolicyDecision | None:
        if spec.path_argument is None:
            return None
        value = getattr(arguments, spec.path_argument, None)
        if not isinstance(value, str):
            return self._deny(
                "path_argument",
                ToolErrorCode.INVALID_ARGUMENTS,
                "Configured path argument is missing or invalid",
            )
        try:
            resolved = (
                self._resolver.resolve_mutation_target(value)
                if spec.risk_level is ToolRisk.WRITE
                else self._resolver.resolve(value, spec.path_kind)
            )
            relative = self._resolver.relative(resolved)
            if spec.protect_sensitive_path:
                self._sensitive_files.require_allowed(relative)
            if spec.allowed_paths and not self._is_allowed_path(relative, spec.allowed_paths):
                return self._deny(
                    "tool_allowed_paths",
                    ToolErrorCode.POLICY_DENIED,
                    "ToolSpec does not permit the requested workspace path",
                )
        except ToolRuntimeError as exc:
            return self._deny("workspace_path", exc.code, exc.safe_message)
        return None

    @staticmethod
    def _is_allowed_path(relative: str, allowed_paths: list[str]) -> bool:
        candidate = Path(relative)
        return any(
            candidate == Path(allowed) or Path(allowed) in candidate.parents
            for allowed in allowed_paths
        )

    @staticmethod
    def _deny(rule: str, code: ToolErrorCode, reason: str) -> PolicyDecision:
        return PolicyDecision(
            decision=PolicyOutcome.DENY,
            reason=reason,
            matched_rule=rule,
            metadata={"error_type": code.value},
        )
