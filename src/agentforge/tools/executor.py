import asyncio
import inspect
import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path, PureWindowsPath
from time import perf_counter

from pydantic import BaseModel, JsonValue, ValidationError

from agentforge.application.run_driver import RunLeaseLostError
from agentforge.domain.digests import compute_tool_call_digest
from agentforge.domain.enums import EventType, PolicyOutcome, ToolErrorCode
from agentforge.domain.errors import ToolNotFoundError, ToolRuntimeError
from agentforge.domain.models import (
    ApprovalAuthorization,
    ApprovalRequired,
    Run,
    ToolResult,
    ToolSpec,
)
from agentforge.domain.mutations import MutationApprovalRequired, MutationPlan
from agentforge.domain.test_execution import TestApprovalRequired, TestExecutionPlan
from agentforge.persistence.event_log import (
    EventLog,
    RunAuthorityProvider,
    RunLeaseAuthority,
)
from agentforge.persistence.repositories import EventRepository, RunRepository
from agentforge.policy.engine import PolicyEngine
from agentforge.policy.models import PolicyDecision
from agentforge.policy.repair import RepairPolicyEnforcer
from agentforge.policy.sensitive import SensitiveFilePolicy
from agentforge.tools.base import Tool
from agentforge.tools.mutation.base import MutationTool
from agentforge.tools.registry import ToolRegistry
from agentforge.tools.testing.base import TestExecutionTool

logger = logging.getLogger(__name__)

_REDACTED_ARGUMENT_KEYS = (
    "content",
    "credential",
    "private_key",
    "query",
    "secret",
    "text",
    "token",
)
_SENSITIVE_FILES = SensitiveFilePolicy()


class ToolExecutor:
    def __init__(
        self,
        registry: ToolRegistry,
        policy_engine: PolicyEngine,
        event_repository: EventRepository,
        run_repository: RunRepository,
        *,
        max_output_chars: int = 20_000,
        repair_guard: RepairPolicyEnforcer | None = None,
    ) -> None:
        if max_output_chars <= 0:
            raise ValueError("max_output_chars must be positive")
        self._registry = registry
        self._policy = policy_engine
        self._events = event_repository
        self._runs = run_repository
        self._max_output_chars = max_output_chars
        self._repair_guard = repair_guard

    @property
    def registry(self) -> ToolRegistry:
        return self._registry

    async def execute(
        self,
        run: Run,
        name: str,
        arguments: dict[str, JsonValue],
        *,
        approval: ApprovalAuthorization | None = None,
        trusted_final_verification: bool = False,
        ownership: RunAuthorityProvider,
    ) -> ToolResult | ApprovalRequired:
        started = perf_counter()
        sanitized_arguments = self._sanitize_arguments(arguments)
        if approval is None:
            self._append_event(
                ownership.authority,
                EventType.TOOL_REQUESTED,
                {"tool_name": name, "sanitized_arguments": sanitized_arguments},
            )

        try:
            tool = self._registry.get(name)
        except ToolNotFoundError:
            decision = self._policy.evaluate(run, name, None, None, arguments_valid=False)
            return self._finish_failure(
                run, name, decision, started, authority=ownership.authority
            )

        try:
            validated = self._registry.validate_arguments(name, arguments)
        except ValidationError:
            decision = self._policy.evaluate(run, name, tool.spec, None, arguments_valid=False)
            return self._finish_failure(
                run, name, decision, started, authority=ownership.authority
            )

        validated_arguments = validated.model_dump(mode="json")
        if approval is not None:
            actual_digest = compute_tool_call_digest(
                tool_name=name,
                validated_arguments=validated_arguments,
                checkpoint_id=approval.checkpoint_id,
                step_number=approval.step_number,
            )
            if actual_digest != approval.request_digest:
                decision = PolicyDecision(
                    decision=PolicyOutcome.DENY,
                    reason="Approval authorization does not match the validated tool call",
                    matched_rule="approval_digest",
                    metadata={"error_type": ToolErrorCode.APPROVAL_CONFLICT.value},
                )
                return self._finish_failure(
                    run, name, decision, started, authority=ownership.authority
                )

        decision = self._policy.evaluate(
            run,
            name,
            tool.spec,
            validated,
            arguments_valid=True,
            approval_granted=approval is not None,
        )
        if decision.decision is PolicyOutcome.REQUIRE_APPROVAL:
            if isinstance(tool, TestExecutionTool):
                try:
                    test_plan = await asyncio.to_thread(tool.prepare, validated)
                except ToolRuntimeError as exc:
                    failed = PolicyDecision(
                        decision=PolicyOutcome.DENY,
                        reason=exc.safe_message,
                        matched_rule="test_profile_preflight",
                        metadata={"error_type": exc.code.value},
                    )
                    return self._finish_failure(
                        run, name, failed, started, authority=ownership.authority
                    )
                repair_denial = self._repair_decision(
                    run,
                    name,
                    tool.spec,
                    validated,
                    prepared=test_plan,
                    trusted_final_verification=trusted_final_verification,
                    authority=ownership.authority,
                )
                if repair_denial is not None:
                    return self._finish_failure(
                        run,
                        name,
                        repair_denial,
                        started,
                        authority=ownership.authority,
                    )
                return TestApprovalRequired(
                    tool_name=name,
                    validated_arguments=validated_arguments,
                    sanitized_arguments=sanitized_arguments,
                    test_plan=test_plan,
                )
            if isinstance(tool, MutationTool):
                try:
                    mutation_plan = await asyncio.to_thread(tool.prepare, validated)
                except ToolRuntimeError as exc:
                    failed = PolicyDecision(
                        decision=PolicyOutcome.DENY,
                        reason=exc.safe_message,
                        matched_rule="mutation_preflight",
                        metadata={"error_type": exc.code.value},
                    )
                    return self._finish_failure(
                        run, name, failed, started, authority=ownership.authority
                    )
                repair_denial = self._repair_decision(
                    run,
                    name,
                    tool.spec,
                    validated,
                    prepared=mutation_plan,
                    trusted_final_verification=trusted_final_verification,
                    authority=ownership.authority,
                )
                if repair_denial is not None:
                    return self._finish_failure(
                        run,
                        name,
                        repair_denial,
                        started,
                        authority=ownership.authority,
                    )
                return MutationApprovalRequired(
                    tool_name=name,
                    validated_arguments=validated_arguments,
                    sanitized_arguments=sanitized_arguments,
                    mutation_plan=mutation_plan,
                )
            repair_denial = self._repair_decision(
                run,
                name,
                tool.spec,
                validated,
                prepared=None,
                trusted_final_verification=trusted_final_verification,
                authority=ownership.authority,
            )
            if repair_denial is not None:
                return self._finish_failure(
                    run,
                    name,
                    repair_denial,
                    started,
                    authority=ownership.authority,
                )
            return ApprovalRequired(
                tool_name=name,
                validated_arguments=validated_arguments,
                sanitized_arguments=sanitized_arguments,
            )
        if decision.decision is not PolicyOutcome.ALLOW:
            return self._finish_failure(
                run, name, decision, started, authority=ownership.authority
            )

        repair_prepared: MutationPlan | TestExecutionPlan | None = None
        if self._repair_guard is not None:
            try:
                if isinstance(tool, TestExecutionTool):
                    repair_prepared = await asyncio.to_thread(tool.prepare, validated)
                elif isinstance(tool, MutationTool):
                    repair_prepared = await asyncio.to_thread(tool.prepare, validated)
            except ToolRuntimeError as exc:
                failed = PolicyDecision(
                    decision=PolicyOutcome.DENY,
                    reason=exc.safe_message,
                    matched_rule="repair_resume_preflight",
                    metadata={"error_type": exc.code.value},
                )
                return self._finish_failure(
                    run, name, failed, started, authority=ownership.authority
                )
        repair_denial = self._repair_decision(
            run,
            name,
            tool.spec,
            validated,
            prepared=repair_prepared,
            trusted_final_verification=trusted_final_verification,
            authority=ownership.authority,
        )
        if repair_denial is not None:
            return self._finish_failure(
                run,
                name,
                repair_denial,
                started,
                authority=ownership.authority,
            )
        if self._repair_guard is not None:
            repair_start_denial = self._repair_guard.record_started(
                run,
                name,
                tool.spec,
                validated,
                authority=ownership.authority,
            )
            if repair_start_denial is not None:
                return self._finish_failure(
                    run,
                    name,
                    repair_start_denial,
                    started,
                    authority=ownership.authority,
                )

        run.tool_call_count += 1
        self._runs.save(run, authority=ownership.authority)
        self._append_event(
            ownership.authority,
            EventType.TOOL_STARTED,
            {
                "tool_name": name,
                "policy_decision": decision.model_dump(mode="json"),
            },
        )

        try:
            async with asyncio.timeout(tool.spec.timeout_seconds):
                result = await self._invoke(tool, validated)
        except asyncio.CancelledError:
            cancelled = ToolResult(
                success=False,
                error_type=ToolErrorCode.TOOL_CANCELLED,
                error_message=f"Tool {name!r} execution was cancelled",
                duration_ms=self._duration_ms(started),
            )
            self._append_event(
                ownership.authority,
                EventType.TOOL_FAILED,
                self._result_event_payload(name, decision, cancelled),
            )
            raise
        except TimeoutError:
            result = ToolResult(
                success=False,
                error_type=ToolErrorCode.TOOL_TIMEOUT,
                error_message=f"Tool {name!r} exceeded its timeout",
            )
        except ToolRuntimeError as exc:
            result = ToolResult(
                success=False,
                error_type=exc.code,
                error_message=exc.safe_message,
            )
        except Exception as exc:
            logger.error(
                "Unexpected tool failure for %s (type=%s)",
                name,
                type(exc).__name__,
            )
            result = ToolResult(
                success=False,
                error_type=ToolErrorCode.TOOL_EXECUTION_ERROR,
                error_message="Tool execution failed unexpectedly",
            )

        duration_ms = self._duration_ms(started)
        result = result.model_copy(update={"duration_ms": duration_ms})
        result = self._truncate_output(result)
        self._append_event(
            ownership.authority,
            EventType.TOOL_COMPLETED if result.success else EventType.TOOL_FAILED,
            self._result_event_payload(name, decision, result),
        )
        return result

    async def execute_managed(
        self,
        run: Run,
        name: str,
        arguments: dict[str, JsonValue],
        *,
        approval: ApprovalAuthorization,
        operation: Callable[[BaseModel], Awaitable[ToolResult]],
        trusted_final_verification: bool = False,
        ownership: RunAuthorityProvider,
    ) -> ToolResult:
        started = perf_counter()
        try:
            tool = self._registry.get(name)
        except ToolNotFoundError:
            decision = self._policy.evaluate(
                run,
                name,
                None,
                None,
                arguments_valid=False,
            )
            return self._finish_failure(
                run, name, decision, started, authority=ownership.authority
            )
        if not isinstance(tool, TestExecutionTool):
            decision = PolicyDecision(
                decision=PolicyOutcome.DENY,
                reason="Tool is not a managed test execution capability",
                matched_rule="managed_test_tool",
                metadata={"error_type": ToolErrorCode.POLICY_DENIED.value},
            )
            return self._finish_failure(
                run, name, decision, started, authority=ownership.authority
            )
        try:
            validated = self._registry.validate_arguments(name, arguments)
        except ValidationError:
            decision = self._policy.evaluate(
                run,
                name,
                tool.spec,
                None,
                arguments_valid=False,
            )
            return self._finish_failure(
                run, name, decision, started, authority=ownership.authority
            )
        validated_arguments = validated.model_dump(mode="json")
        actual_digest = compute_tool_call_digest(
            tool_name=name,
            validated_arguments=validated_arguments,
            checkpoint_id=approval.checkpoint_id,
            step_number=approval.step_number,
        )
        if actual_digest != approval.request_digest:
            decision = PolicyDecision(
                decision=PolicyOutcome.DENY,
                reason="Approval authorization does not match the managed test call",
                matched_rule="approval_digest",
                metadata={"error_type": ToolErrorCode.APPROVAL_CONFLICT.value},
            )
            return self._finish_failure(
                run, name, decision, started, authority=ownership.authority
            )
        decision = self._policy.evaluate(
            run,
            name,
            tool.spec,
            validated,
            arguments_valid=True,
            approval_granted=True,
        )
        if decision.decision is not PolicyOutcome.ALLOW:
            return self._finish_failure(
                run, name, decision, started, authority=ownership.authority
            )
        try:
            before = ownership.authority
            prepared = await asyncio.to_thread(tool.prepare, validated)
            after = ownership.authority
            if (
                after.run_id != before.run_id
                or after.owner_id != before.owner_id
                or after.fencing_token != before.fencing_token
            ):
                raise RunLeaseLostError()
        except ToolRuntimeError as exc:
            result = ToolResult(
                success=False,
                error_type=exc.code,
                error_message=exc.safe_message,
                duration_ms=self._duration_ms(started),
            )
            self._append_event(
                ownership.authority,
                EventType.TOOL_FAILED,
                self._result_event_payload(name, decision, result),
            )
            return result
        repair_denial = self._repair_decision(
            run,
            name,
            tool.spec,
            validated,
            prepared=prepared,
            trusted_final_verification=trusted_final_verification,
            authority=ownership.authority,
        )
        if repair_denial is not None:
            return self._finish_failure(
                run,
                name,
                repair_denial,
                started,
                authority=ownership.authority,
            )
        return await operation(validated)

    def _repair_decision(
        self,
        run: Run,
        name: str,
        spec: ToolSpec,
        arguments: BaseModel,
        *,
        prepared: MutationPlan | TestExecutionPlan | None,
        trusted_final_verification: bool,
        authority: RunLeaseAuthority,
    ) -> PolicyDecision | None:
        if self._repair_guard is None:
            return None
        return self._repair_guard.evaluate(
            run,
            name,
            spec,
            arguments,
            prepared=prepared,
            trusted_final_verification=trusted_final_verification,
            authority=authority,
        )

    async def _invoke(self, tool: Tool, arguments: BaseModel) -> ToolResult:
        if isinstance(tool, MutationTool):
            returned = tool.execute(arguments)
        else:
            returned = await asyncio.to_thread(tool.execute, arguments)
        if inspect.isawaitable(returned):
            return await returned
        return returned

    def _finish_failure(
        self,
        run: Run,
        name: str,
        decision: PolicyDecision,
        started: float,
        *,
        authority: RunLeaseAuthority,
    ) -> ToolResult:
        error_type = self._decision_error_type(decision)
        result = ToolResult(
            success=False,
            error_type=error_type,
            error_message=decision.reason,
            duration_ms=self._duration_ms(started),
        )
        self._append_event(
            authority,
            EventType.TOOL_FAILED,
            self._result_event_payload(name, decision, result),
        )
        return result

    def _append_event(
        self,
        authority: RunLeaseAuthority,
        event_type: EventType,
        payload: dict[str, JsonValue],
    ) -> None:
        with self._runs.database.session() as session:
            EventLog().append(session, authority, event_type, payload)

    def _truncate_output(self, result: ToolResult) -> ToolResult:
        if result.output is None:
            return result
        serialized = json.dumps(result.output, ensure_ascii=False, sort_keys=True)
        if len(serialized) <= self._max_output_chars:
            return result
        metadata = dict(result.metadata)
        metadata["original_output_chars"] = len(serialized)
        preview = serialized[: self._max_output_chars]
        return result.model_copy(
            update={"output": preview, "truncated": True, "metadata": metadata}
        )

    @staticmethod
    def _decision_error_type(decision: PolicyDecision) -> ToolErrorCode:
        raw = decision.metadata.get("error_type")
        if isinstance(raw, str):
            try:
                return ToolErrorCode(raw)
            except ValueError:
                pass
        if decision.decision is PolicyOutcome.REQUIRE_APPROVAL:
            return ToolErrorCode.APPROVAL_REQUIRED
        return ToolErrorCode.POLICY_DENIED

    @classmethod
    def _sanitize_arguments(cls, arguments: dict[str, JsonValue]) -> dict[str, JsonValue]:
        return {key: cls._sanitize_value(key, value) for key, value in arguments.items()}

    @classmethod
    def _sanitize_value(cls, key: str, value: JsonValue) -> JsonValue:
        lowered = key.casefold()
        if any(marker in lowered for marker in _REDACTED_ARGUMENT_KEYS):
            return "<redacted>"
        if isinstance(value, str):
            if lowered.endswith("path") and cls._path_requires_redaction(value):
                return "<redacted>"
            return value[:200]
        if isinstance(value, list):
            return [cls._sanitize_value("", item) for item in value]
        if isinstance(value, dict):
            return {
                nested_key: cls._sanitize_value(nested_key, nested_value)
                for nested_key, nested_value in value.items()
            }
        return value

    @staticmethod
    def _path_requires_redaction(value: str) -> bool:
        raw = value.strip()
        windows_path = PureWindowsPath(raw)
        if (
            Path(raw).is_absolute()
            or windows_path.is_absolute()
            or bool(windows_path.drive)
            or raw.startswith(("\\\\", "//"))
        ):
            return True
        return _SENSITIVE_FILES.match(raw.replace("\\", "/")) is not None

    @staticmethod
    def _result_event_payload(
        name: str,
        decision: PolicyDecision,
        result: ToolResult,
    ) -> dict[str, JsonValue]:
        return {
            "tool_name": name,
            "policy_decision": decision.model_dump(mode="json"),
            "duration_ms": result.duration_ms,
            "success": result.success,
            "error_type": result.error_type.value if result.error_type else None,
            "truncated": result.truncated,
        }

    @staticmethod
    def _duration_ms(started: float) -> int:
        return max(0, int((perf_counter() - started) * 1000))
