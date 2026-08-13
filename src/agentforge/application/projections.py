from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from datetime import datetime
from uuid import UUID

from pydantic import ValidationError

from agentforge.application.contracts import LifecycleStatus, OutcomeStatus, ProfilePurpose
from agentforge.application.events import (
    ApprovalDecidedPayload,
    ApprovalRequestedPayload,
    ProductEvent,
    ProductEventPayload,
    ProductProgressStage,
    ProfileTrustedPayload,
    ProgressPayload,
    RunFinishedPayload,
    RunStatePayload,
    VerificationCompletedPayload,
)
from agentforge.application.views import (
    ExportRunDetailsView,
    LocalRunDetailsView,
    PendingApprovalsView,
    PendingApprovalView,
    RunProjectionFacts,
)
from agentforge.domain.enums import ApprovalStatus, EventType, RunFailureCode, RunStatus
from agentforge.domain.models import ApprovalRequest, PersistedEvent
from agentforge.domain.repair import RepairCompletionStatus
from agentforge.domain.strict_json import StrictJsonError, canonical_json_size
from agentforge.evaluation.public_artifacts import (
    ForbiddenPublicArtifactError,
    PublicArtifactScanner,
)


class ProductProjectionError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("persisted product fact cannot be safely projected")


_PROFILE_TRUSTED = "PROFILE_TRUSTED"

_SPECIAL_PRODUCT_EVENT_TYPES = frozenset(
    {
        EventType.RUN_CREATED,
        EventType.RUN_STARTED,
        EventType.APPROVAL_REQUESTED,
        EventType.APPROVAL_GRANTED,
        EventType.APPROVAL_REJECTED,
        EventType.RUN_PAUSED,
        EventType.RUN_RESUMED,
        EventType.RUN_FAILED,
        EventType.RUN_COMPLETED,
        EventType.RUN_CANCELLED,
        EventType.FINAL_VERIFICATION_COMPLETED,
        EventType.FINAL_VERIFICATION_FAILED,
    }
)

_PRODUCT_EVENT_STAGE_BY_TYPE: dict[EventType, ProductProgressStage] = {
    EventType.MODEL_REQUESTED: ProductProgressStage.MODEL,
    EventType.MODEL_RESPONDED: ProductProgressStage.MODEL,
    EventType.MODEL_ATTEMPT_PREPARED: ProductProgressStage.MODEL,
    EventType.MODEL_ATTEMPT_DISPATCHING: ProductProgressStage.MODEL,
    EventType.MODEL_ATTEMPT_COMPLETED: ProductProgressStage.MODEL,
    EventType.MODEL_ATTEMPT_FAILED: ProductProgressStage.MODEL,
    EventType.MODEL_ATTEMPT_INDETERMINATE: ProductProgressStage.MODEL,
    EventType.MODEL_RETRY_SCHEDULED: ProductProgressStage.MODEL,
    EventType.MODEL_FAILED: ProductProgressStage.MODEL,
    EventType.MODEL_PROVIDER_DEVIATION: ProductProgressStage.MODEL,
    EventType.MULTI_TOOL_RESPONSE_NORMALIZED: ProductProgressStage.MODEL,
    EventType.CONTEXT_COMPACTED: ProductProgressStage.RUNTIME,
    EventType.LOOP_WARNING: ProductProgressStage.POLICY,
    EventType.LOOP_DETECTED: ProductProgressStage.POLICY,
    EventType.BUDGET_EXCEEDED: ProductProgressStage.POLICY,
    EventType.TOOL_REQUESTED: ProductProgressStage.TOOL,
    EventType.TOOL_STARTED: ProductProgressStage.TOOL,
    EventType.TOOL_COMPLETED: ProductProgressStage.TOOL,
    EventType.TOOL_FAILED: ProductProgressStage.TOOL,
    EventType.CHECKPOINT_SAVED: ProductProgressStage.RUNTIME,
    EventType.MUTATION_REQUESTED: ProductProgressStage.MUTATION,
    EventType.MUTATION_STARTED: ProductProgressStage.MUTATION,
    EventType.MUTATION_COMMITTED: ProductProgressStage.MUTATION,
    EventType.MUTATION_FAILED: ProductProgressStage.MUTATION,
    EventType.MUTATION_INDETERMINATE: ProductProgressStage.MUTATION,
    EventType.TEST_REQUESTED: ProductProgressStage.TEST,
    EventType.TEST_STARTED: ProductProgressStage.TEST,
    EventType.TEST_COMPLETED: ProductProgressStage.TEST,
    EventType.TEST_FAILED: ProductProgressStage.TEST,
    EventType.TEST_TIMEOUT: ProductProgressStage.TEST,
    EventType.TEST_CANCELLED: ProductProgressStage.TEST,
    EventType.TEST_INDETERMINATE: ProductProgressStage.TEST,
    EventType.REPAIR_TASK_STARTED: ProductProgressStage.RUNTIME,
    EventType.REPAIR_POLICY_BOUND: ProductProgressStage.POLICY,
    EventType.REPAIR_BUDGET_CONSUMED: ProductProgressStage.POLICY,
    EventType.REPAIR_POLICY_VIOLATION: ProductProgressStage.POLICY,
    EventType.REPAIR_COMPLETION_REJECTED: ProductProgressStage.POLICY,
    EventType.REPAIR_COMPLETION_CORRECTED: ProductProgressStage.POLICY,
    EventType.WORKSPACE_BASELINE_CREATED: ProductProgressStage.MUTATION,
    EventType.WORKSPACE_DIFF_VALIDATED: ProductProgressStage.MUTATION,
    EventType.WORKSPACE_DIFF_VIOLATION: ProductProgressStage.POLICY,
    EventType.FINAL_VERIFICATION_REQUESTED: ProductProgressStage.TEST,
    EventType.REPAIR_TASK_COMPLETED: ProductProgressStage.RUNTIME,
    EventType.REPAIR_TASK_FAILED: ProductProgressStage.RUNTIME,
}

REVIEWED_EXCLUDED_EVENT_TYPES = frozenset(
    {
        EventType.EVALUATION_RUN_STARTED,
        EventType.EVALUATION_RUN_COMPLETED,
        EventType.AUTO_APPROVAL_GRANTED,
        EventType.AUTO_APPROVAL_DENIED,
        EventType.EVALUATION_BASELINE_CREATED,
        EventType.EVALUATION_BASELINE_STARTED,
        EventType.EVALUATION_BASELINE_VERIFIED,
        EventType.EVALUATION_BASELINE_BLOCKED,
        EventType.EVALUATION_BASELINE_INDETERMINATE,
        EventType.EVALUATION_BASELINE_CONTEXT_INJECTED,
        EventType.EVALUATION_CAMPAIGN_CREATED,
        EventType.EVALUATION_CAMPAIGN_STARTED,
        EventType.EVALUATION_SLOT_CLAIMED,
        EventType.EVALUATION_ATTEMPT_CREATED,
        EventType.EVALUATION_ATTEMPT_WORKSPACE_READY,
        EventType.EVALUATION_ATTEMPT_RUNTIME_READY,
        EventType.EVALUATION_ATTEMPT_STARTED,
        EventType.EVALUATION_ATTEMPT_COMPLETED,
        EventType.EVALUATION_ATTEMPT_INVALID,
        EventType.EVALUATION_ATTEMPT_INDETERMINATE,
        EventType.EVALUATION_REPLACEMENT_CREATED,
        EventType.EVALUATION_SLOT_ACCEPTED,
        EventType.EVALUATION_SLOT_INVALID,
        EventType.EVALUATION_SLOT_INDETERMINATE,
        EventType.EVALUATION_CAMPAIGN_COMPLETED,
        EventType.EVALUATION_STUDY_CREATED,
        EventType.EVALUATION_STUDY_AUTHORIZED,
        EventType.EVALUATION_STUDY_STARTED,
        EventType.EVALUATION_STUDY_CAMPAIGN_COMPLETED,
        EventType.EVALUATION_STUDY_COMPLETED,
        EventType.EVALUATION_STUDY_ABORTED,
        EventType.EVALUATION_STUDY_INDETERMINATE,
    }
)
REVIEWED_PRODUCT_EVENT_TYPES = frozenset(_PRODUCT_EVENT_STAGE_BY_TYPE) | (
    _SPECIAL_PRODUCT_EVENT_TYPES
)

_FAILED_CODE_OUTCOMES: dict[RunFailureCode, OutcomeStatus] = {
    RunFailureCode.APPROVAL_REJECTED: OutcomeStatus.FAILED,
    RunFailureCode.APPROVAL_INDETERMINATE: OutcomeStatus.UNKNOWN,
    RunFailureCode.COMMAND_FAILED: OutcomeStatus.FAILED,
    RunFailureCode.COMMAND_INDETERMINATE: OutcomeStatus.UNKNOWN,
    RunFailureCode.MUTATION_INDETERMINATE: OutcomeStatus.UNKNOWN,
    RunFailureCode.TEST_PROFILE_MISMATCH: OutcomeStatus.FAILED,
    RunFailureCode.TEST_EXECUTION_INDETERMINATE: OutcomeStatus.UNKNOWN,
    RunFailureCode.TESTS_FAILED: OutcomeStatus.FAILED,
    RunFailureCode.FINAL_VERIFICATION_FAILED: OutcomeStatus.FAILED,
    RunFailureCode.UNVERIFIED_FINAL: OutcomeStatus.UNVERIFIED,
    RunFailureCode.BUDGET_EXHAUSTED: OutcomeStatus.FAILED,
    RunFailureCode.POLICY_BLOCKED: OutcomeStatus.FAILED,
    RunFailureCode.DIFF_POLICY_VIOLATION: OutcomeStatus.FAILED,
    RunFailureCode.LOOP_DETECTED: OutcomeStatus.FAILED,
    RunFailureCode.MODEL_FAILURE: OutcomeStatus.FAILED,
    RunFailureCode.MODEL_PROTOCOL_ERROR: OutcomeStatus.FAILED,
    RunFailureCode.MODEL_TOOL_FAILED: OutcomeStatus.FAILED,
    RunFailureCode.RUNTIME_FAILURE: OutcomeStatus.FAILED,
    RunFailureCode.INDETERMINATE: OutcomeStatus.UNKNOWN,
    RunFailureCode.CANCELLED: OutcomeStatus.FAILED,
}

_TERMINAL_REPAIR_STATUS_BY_RUN_STATUS: dict[RunStatus, frozenset[RepairCompletionStatus]] = {
    RunStatus.COMPLETED: frozenset(
        {RepairCompletionStatus.VERIFIED_SUCCESS, RepairCompletionStatus.UNVERIFIED_FINAL}
    ),
    RunStatus.FAILED: frozenset(
        set(RepairCompletionStatus)
        - {
            RepairCompletionStatus.RUNNING,
            RepairCompletionStatus.VERIFIED_SUCCESS,
            RepairCompletionStatus.CANCELLED,
        }
    ),
    RunStatus.CANCELLED: frozenset({RepairCompletionStatus.CANCELLED}),
}


class ProductProjector:
    """One deterministic, fail-closed projection from durable facts to safe DTOs."""

    def event(self, persisted: PersistedEvent) -> ProductEvent:
        if (
            type(persisted.schema_version) is not int
            or persisted.schema_version != 1
        ):
            raise ProductProjectionError()
        try:
            canonical_json_size(persisted.payload)
            event_type: EventType | str = (
                _PROFILE_TRUSTED
                if persisted.event_type == _PROFILE_TRUSTED
                else EventType(persisted.event_type)
            )
            payload = self._event_payload(event_type, persisted.payload)
            projected = ProductEvent(
                event_id=persisted.event_id,
                scope_type=persisted.scope_type,
                scope_id=persisted.scope_id,
                cursor=persisted.global_cursor,
                run_id=persisted.run_id,
                sequence_number=persisted.sequence_number,
                occurred_at=persisted.created_at,
                payload=payload,
            )
            self._scan(projected.model_dump_json())
            return projected
        except ProductProjectionError:
            raise
        except StrictJsonError:
            raise ProductProjectionError() from None
        except (
            TypeError,
            ValueError,
            ValidationError,
            ForbiddenPublicArtifactError,
        ):
            raise ProductProjectionError() from None

    def events(self, persisted: Iterable[PersistedEvent]) -> tuple[ProductEvent, ...]:
        try:
            facts: list[PersistedEvent] = []
            event_ids: set[UUID] = set()
            cursors: set[int] = set()
            for fact in persisted:
                if len(facts) >= 10_000 or type(fact) is not PersistedEvent:
                    raise ProductProjectionError()
                if (
                    type(fact.event_id) is not UUID
                    or type(fact.global_cursor) is not int
                    or fact.global_cursor <= 0
                    or fact.event_id in event_ids
                    or fact.global_cursor in cursors
                ):
                    raise ProductProjectionError()
                event_ids.add(fact.event_id)
                cursors.add(fact.global_cursor)
                facts.append(fact)
            ordered = sorted(facts, key=lambda fact: fact.global_cursor)
            previous_sequence: dict[UUID, int] = {}
            for fact in ordered:
                if fact.scope_type == "RUN":
                    if type(fact.run_id) is not UUID or type(fact.sequence_number) is not int:
                        raise ProductProjectionError()
                    previous = previous_sequence.get(fact.run_id)
                    if previous is not None and fact.sequence_number <= previous:
                        raise ProductProjectionError()
                    previous_sequence[fact.run_id] = fact.sequence_number
            return tuple(self.event(fact) for fact in ordered)
        except ProductProjectionError:
            raise
        except Exception:
            raise ProductProjectionError() from None

    def run_details(self, facts: RunProjectionFacts) -> LocalRunDetailsView:
        self._validate_run_facts(facts)
        lifecycle, outcome = self._statuses(facts)
        return LocalRunDetailsView(
            run_id=facts.run.run_id,
            lifecycle_status=lifecycle,
            outcome_status=outcome,
            current_step=facts.run.current_step,
            tool_call_count=facts.run.tool_call_count,
            event_count=facts.event_count,
            last_cursor=facts.last_cursor,
            created_at=facts.run.created_at,
            updated_at=facts.run.updated_at,
        )

    def export_run(self, facts: RunProjectionFacts) -> ExportRunDetailsView:
        local = self.run_details(facts)
        exported = ExportRunDetailsView(
            **local.model_dump(),
            source_digest=facts.source_digest,
            config_digest=facts.config_digest,
            profile_digest=facts.profile_digest,
        )
        try:
            self._scan(exported.model_dump_json())
        except ForbiddenPublicArtifactError:
            raise ProductProjectionError() from None
        return exported

    def pending_approvals(
        self, approvals: Iterable[ApprovalRequest]
    ) -> PendingApprovalsView:
        try:
            items = tuple(
                PendingApprovalView(
                    approval_id=approval.approval_id,
                    run_id=approval.run_id,
                    tool_name=approval.tool_name,
                    status=approval.status,
                    requested_at=approval.requested_at,
                )
                for approval in sorted(
                    approvals,
                    key=lambda value: (value.requested_at, str(value.approval_id)),
                )
                if approval.status is ApprovalStatus.PENDING
            )
            view = PendingApprovalsView(approvals=items)
            self._scan(view.model_dump_json())
            return view
        except (TypeError, ValueError, ValidationError, ForbiddenPublicArtifactError):
            raise ProductProjectionError() from None

    @staticmethod
    def _statuses(
        facts: RunProjectionFacts,
    ) -> tuple[LifecycleStatus, OutcomeStatus | None]:
        run_status = facts.run.status
        repair_status = facts.repair_status
        if run_status is RunStatus.CREATED:
            if repair_status is not RepairCompletionStatus.RUNNING:
                raise ProductProjectionError()
            return LifecycleStatus.CREATED, None
        if run_status is RunStatus.RUNNING:
            if repair_status is not RepairCompletionStatus.RUNNING:
                raise ProductProjectionError()
            return LifecycleStatus.RUNNING, None
        if run_status in {RunStatus.WAITING_APPROVAL, RunStatus.PAUSED}:
            if repair_status is not RepairCompletionStatus.RUNNING:
                raise ProductProjectionError()
            return LifecycleStatus.PAUSED, OutcomeStatus.UNVERIFIED
        if repair_status is RepairCompletionStatus.RUNNING:
            raise ProductProjectionError()
        if repair_status not in _TERMINAL_REPAIR_STATUS_BY_RUN_STATUS.get(
            run_status, frozenset()
        ):
            raise ProductProjectionError()
        if repair_status is RepairCompletionStatus.INDETERMINATE:
            outcome = OutcomeStatus.UNKNOWN
        elif repair_status is RepairCompletionStatus.VERIFIED_SUCCESS:
            outcome = OutcomeStatus.VERIFIED
        elif repair_status is RepairCompletionStatus.UNVERIFIED_FINAL:
            outcome = OutcomeStatus.UNVERIFIED
        else:
            outcome = OutcomeStatus.FAILED
        return LifecycleStatus.TERMINAL, outcome

    def _event_payload(
        self, event_type: EventType | str, raw: Mapping[str, object]
    ) -> ProductEventPayload:
        if not isinstance(raw, Mapping):
            raise ProductProjectionError()
        if event_type == _PROFILE_TRUSTED:
            return ProfileTrustedPayload(
                profile_id=self._string(raw, "profile_id"),
                profile_version=self._integer(raw, "profile_version"),
                profile_digest=self._string(raw, "profile_digest"),
                purpose=ProfilePurpose(self._string(raw, "purpose")),
            )
        if not isinstance(event_type, EventType):
            raise ProductProjectionError()
        if event_type is EventType.RUN_CREATED:
            return RunStatePayload(
                lifecycle_status=LifecycleStatus.CREATED, outcome_status=None
            )
        if event_type in {EventType.RUN_STARTED, EventType.RUN_RESUMED}:
            return RunStatePayload(
                lifecycle_status=LifecycleStatus.RUNNING, outcome_status=None
            )
        if event_type is EventType.RUN_PAUSED:
            return RunStatePayload(
                lifecycle_status=LifecycleStatus.PAUSED,
                outcome_status=OutcomeStatus.UNVERIFIED,
            )
        if event_type is EventType.RUN_COMPLETED:
            if set(raw) == {"repair_status"}:
                if (
                    raw["repair_status"]
                    != RepairCompletionStatus.VERIFIED_SUCCESS.value
                ):
                    raise ProductProjectionError()
                outcome = OutcomeStatus.VERIFIED
            elif set(raw) == {"outcome"}:
                if raw["outcome"] != "COMPLETED":
                    raise ProductProjectionError()
                outcome = OutcomeStatus.UNVERIFIED
            elif set(raw) == {"final_output"}:
                if type(raw["final_output"]) is not str:
                    raise ProductProjectionError()
                outcome = OutcomeStatus.UNVERIFIED
            else:
                raise ProductProjectionError()
            return RunFinishedPayload(
                lifecycle_status=LifecycleStatus.TERMINAL,
                outcome_status=outcome,
            )
        if event_type is EventType.RUN_FAILED:
            if set(raw) != {"code"} or type(raw["code"]) is not str:
                raise ProductProjectionError()
            try:
                failed_outcome = _FAILED_CODE_OUTCOMES[RunFailureCode(raw["code"])]
            except (KeyError, ValueError):
                raise ProductProjectionError() from None
            return RunFinishedPayload(
                lifecycle_status=LifecycleStatus.TERMINAL,
                outcome_status=failed_outcome,
            )
        if event_type is EventType.RUN_CANCELLED:
            return RunFinishedPayload(
                lifecycle_status=LifecycleStatus.TERMINAL,
                outcome_status=OutcomeStatus.FAILED,
            )
        if event_type is EventType.APPROVAL_REQUESTED:
            return ApprovalRequestedPayload(
                approval_id=self._uuid(raw, "approval_id"),
                tool_name=self._string(raw, "tool_name"),
            )
        if event_type in {EventType.APPROVAL_GRANTED, EventType.APPROVAL_REJECTED}:
            return ApprovalDecidedPayload(
                approval_id=self._uuid(raw, "approval_id"),
                status=(
                    ApprovalStatus.APPROVED
                    if event_type is EventType.APPROVAL_GRANTED
                    else ApprovalStatus.REJECTED
                ),
            )
        if event_type is EventType.FINAL_VERIFICATION_COMPLETED:
            if raw.get("passed") is not True:
                raise ProductProjectionError()
            return VerificationCompletedPayload(outcome_status=OutcomeStatus.VERIFIED)
        if event_type is EventType.FINAL_VERIFICATION_FAILED:
            if raw.get("passed") is not False:
                raise ProductProjectionError()
            return VerificationCompletedPayload(outcome_status=OutcomeStatus.FAILED)
        return ProgressPayload(stage=self._stage(event_type))

    @staticmethod
    def _stage(event_type: EventType) -> ProductProgressStage:
        try:
            return _PRODUCT_EVENT_STAGE_BY_TYPE[event_type]
        except KeyError:
            raise ProductProjectionError() from None

    @classmethod
    def _validate_run_facts(cls, facts: RunProjectionFacts) -> None:
        run = facts.run
        if (
            type(run.run_id) is not UUID
            or type(run.task) is not str
            or not run.task
            or type(run.status) is not RunStatus
            or type(run.current_step) is not int
            or run.current_step < 0
            or type(run.max_steps) is not int
            or run.max_steps <= 0
            or run.current_step > run.max_steps
            or type(run.tool_call_count) is not int
            or run.tool_call_count < 0
            or type(run.max_tool_calls) is not int
            or run.max_tool_calls < 0
            or run.tool_call_count > run.max_tool_calls
            or type(run.model_provider) is not str
            or not run.model_provider
            or type(run.total_token_usage) is not int
            or run.total_token_usage < 0
            or type(run.estimated_cost) is not float
            or not math.isfinite(run.estimated_cost)
            or run.estimated_cost < 0
            or type(run.created_at) is not datetime
            or run.created_at.tzinfo is None
            or run.created_at.utcoffset() is None
            or type(run.updated_at) is not datetime
            or run.updated_at.tzinfo is None
            or run.updated_at.utcoffset() is None
            or run.updated_at < run.created_at
            or (run.error_message is not None and type(run.error_message) is not str)
            or (run.final_output is not None and type(run.final_output) is not str)
            or type(facts.repair_status) is not RepairCompletionStatus
            or type(facts.event_count) is not int
            or facts.event_count < 0
            or (
                facts.last_cursor is not None
                and (type(facts.last_cursor) is not int or facts.last_cursor <= 0)
            )
            or ((facts.event_count == 0) != (facts.last_cursor is None))
        ):
            raise ProductProjectionError()
        for digest in (
            facts.source_digest,
            facts.config_digest,
            facts.profile_digest,
        ):
            if digest is not None and not cls._sha256(digest):
                raise ProductProjectionError()

    @staticmethod
    def _sha256(value: object) -> bool:
        return (
            type(value) is str
            and len(value) == 64
            and all(character in "0123456789abcdef" for character in value)
        )

    @staticmethod
    def _string(raw: Mapping[str, object], name: str) -> str:
        value = raw.get(name)
        if type(value) is not str or not value:
            raise ProductProjectionError()
        return value

    @classmethod
    def _uuid(cls, raw: Mapping[str, object], name: str) -> UUID:
        value = cls._string(raw, name)
        parsed = UUID(value)
        if str(parsed) != value:
            raise ProductProjectionError()
        return parsed

    @staticmethod
    def _integer(raw: Mapping[str, object], name: str) -> int:
        value = raw.get(name)
        if type(value) is not int:
            raise ProductProjectionError()
        return value

    @staticmethod
    def _scan(serialized: str) -> None:
        PublicArtifactScanner().validate(serialized)
