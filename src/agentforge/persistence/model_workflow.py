import hashlib
import json
from collections.abc import Callable
from uuid import UUID, uuid4

from pydantic import JsonValue
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from agentforge.domain.enums import (
    EventType,
    ModelAttemptStatus,
    ModelRecoveryAction,
)
from agentforge.domain.models import normalize_utc, utc_now
from agentforge.models.base import ModelRequest
from agentforge.models.domain import (
    ModelAttemptRecord,
    ModelBudget,
    ModelBudgetState,
    ModelErrorCode,
    ModelRecoveryDecision,
    ModelUsage,
    MultiToolResponseInfo,
)
from agentforge.models.errors import ModelAttemptConflictError, ModelRequestError
from agentforge.persistence.database import Database
from agentforge.persistence.event_log import EventLog, RunLeaseAuthority
from agentforge.persistence.run_leases import claim_bound_write
from agentforge.persistence.tables import (
    ModelAttemptRow,
    ModelRuntimeStateRow,
)


class ModelWorkflow:
    def __init__(
        self,
        database: Database,
        *,
        failpoint: Callable[[str], None] | None = None,
    ) -> None:
        self._database = database
        self._failpoint = failpoint or (lambda _: None)

    def _evaluator_only_ensure_state(
        self, run_id: UUID, budget: ModelBudget
    ) -> ModelBudgetState:
        with self._database.session() as session:
            row = session.get(ModelRuntimeStateRow, str(run_id))
            if row is None:
                row = ModelRuntimeStateRow(
                    run_id=str(run_id),
                    model_request_count=0,
                    input_tokens=0,
                    output_tokens=0,
                    total_tokens=0,
                    cached_input_tokens=0,
                    reasoning_tokens=0,
                    max_model_requests=budget.max_model_requests,
                    max_retries=budget.max_retries,
                    max_output_tokens_per_request=budget.max_output_tokens_per_request,
                    max_total_input_tokens=budget.max_total_input_tokens,
                    max_total_output_tokens=budget.max_total_output_tokens,
                    max_total_tokens=budget.max_total_tokens,
                    updated_at=utc_now(),
                )
                session.add(row)
                session.flush()
            return self._to_domain(row)

    def get_state(self, run_id: UUID) -> ModelBudgetState:
        with self._database.session() as session:
            row = session.get(ModelRuntimeStateRow, str(run_id))
            if row is None:
                raise ModelRequestError(
                    ModelErrorCode.MODEL_BUDGET_EXCEEDED,
                    "Model budget state is missing",
                    retryable=False,
                )
            return self._to_domain(row)

    def list_attempts(self, run_id: UUID) -> list[ModelAttemptRecord]:
        with self._database.session() as session:
            rows = session.scalars(
                select(ModelAttemptRow)
                .where(ModelAttemptRow.run_id == str(run_id))
                .order_by(
                    ModelAttemptRow.created_at,
                    ModelAttemptRow.attempt_number,
                    ModelAttemptRow.attempt_id,
                )
            ).all()
            return [self._attempt_to_domain(row) for row in rows]

    def prepare_attempt(
        self,
        run_id: UUID,
        logical_call_id: UUID,
        attempt_number: int,
        *,
        attempt_id: UUID | None = None,
        request: ModelRequest,
        provider_identity: str,
        authority: RunLeaseAuthority,
    ) -> UUID:
        request_hash = self.request_digest(request)
        provider = self._provider_identity(provider_identity)
        exhausted = False
        candidate_id = attempt_id or uuid4()
        with self._database.session() as session:
            claim_bound_write(session, run_id, authority)
            existing = session.get(ModelAttemptRow, str(candidate_id))
            logical_existing = session.scalar(
                select(ModelAttemptRow).where(
                    ModelAttemptRow.run_id == str(run_id),
                    ModelAttemptRow.logical_call_id == str(logical_call_id),
                    ModelAttemptRow.attempt_number == attempt_number,
                )
            )
            if existing is not None or logical_existing is not None:
                persisted = existing or logical_existing
                assert persisted is not None
                if (
                    persisted.attempt_id != str(candidate_id)
                    or persisted.logical_call_id != str(logical_call_id)
                    or persisted.attempt_number != attempt_number
                ):
                    raise ModelAttemptConflictError(
                        "model attempt identity does not match"
                    )
                self._bound_attempt(
                    session, run_id, candidate_id, request_hash, provider
                )
                return candidate_id
            state = session.get(ModelRuntimeStateRow, str(run_id))
            if state is None:
                raise ModelRequestError(
                    ModelErrorCode.MODEL_BUDGET_EXCEEDED,
                    "Model budget state is missing",
                    retryable=False,
                )
            token_exhausted = bool(
                (
                    state.max_total_tokens is not None
                    and state.total_tokens >= state.max_total_tokens
                )
                or (
                    state.max_total_input_tokens is not None
                    and state.input_tokens >= state.max_total_input_tokens
                )
                or (
                    state.max_total_output_tokens is not None
                    and state.output_tokens >= state.max_total_output_tokens
                )
            )
            if state.model_request_count >= state.max_model_requests or token_exhausted:
                self._append_event(
                    session,
                    authority,
                    EventType.BUDGET_EXCEEDED,
                    {
                        "budget": (
                            "model_tokens" if token_exhausted else "model_requests"
                        )
                    },
                )
                exhausted = True
            else:
                budget_hash = self._budget_digest(self._to_domain(state).budget)
                state.model_request_count += 1
                state.updated_at = utc_now()
                session.add(
                    ModelAttemptRow(
                        attempt_id=str(candidate_id),
                        run_id=str(run_id),
                        logical_call_id=str(logical_call_id),
                        attempt_number=attempt_number,
                        status=ModelAttemptStatus.PREPARED.value,
                        request_digest=request_hash,
                        provider_identity=provider,
                        budget_digest=budget_hash,
                        error_type=None,
                        retryable=None,
                        duration_ms=None,
                        usage=None,
                        created_at=utc_now(),
                        dispatched_at=None,
                        completed_at=None,
                    )
                )
                self._append_event(
                    session,
                    authority,
                    EventType.MODEL_ATTEMPT_PREPARED,
                    {
                        "logical_call_id": str(logical_call_id),
                        "attempt": attempt_number,
                        "attempt_id": str(candidate_id),
                        "request_digest": request_hash,
                        "provider_identity": provider,
                        "budget_digest": budget_hash,
                    },
                )
                self._failpoint("before_prepared_commit")
        if exhausted:
            raise ModelRequestError(
                ModelErrorCode.MODEL_BUDGET_EXCEEDED,
                "Model request or token budget is exhausted",
                retryable=False,
            )
        return candidate_id

    def claim_dispatch(
        self,
        run_id: UUID,
        attempt_id: UUID,
        *,
        request: ModelRequest,
        provider_identity: str,
        authority: RunLeaseAuthority,
    ) -> bool:
        request_hash = self.request_digest(request)
        provider = self._provider_identity(provider_identity)
        with self._database.session() as session:
            claim_bound_write(session, run_id, authority)
            attempt = self._bound_attempt(
                session, run_id, attempt_id, request_hash, provider
            )
            now = utc_now()
            changed = self._affected_rows(
                session.execute(
                    update(ModelAttemptRow)
                    .where(
                        ModelAttemptRow.attempt_id == str(attempt_id),
                        ModelAttemptRow.run_id == str(run_id),
                        ModelAttemptRow.status == ModelAttemptStatus.PREPARED.value,
                    )
                    .values(
                        status=ModelAttemptStatus.DISPATCHING.value,
                        dispatched_at=now,
                    )
                    .execution_options(synchronize_session=False)
                )
            )
            if changed != 1:
                session.refresh(attempt)
                return False
            self._append_event(
                session,
                authority,
                EventType.MODEL_ATTEMPT_DISPATCHING,
                {
                    "attempt_id": str(attempt_id),
                    "attempt": attempt.attempt_number,
                    "request_digest": request_hash,
                    "provider_identity": provider,
                },
            )
            return True

    def recover_attempt(
        self,
        run_id: UUID,
        attempt_id: UUID,
        *,
        request: ModelRequest,
        provider_identity: str,
        authority: RunLeaseAuthority,
    ) -> ModelRecoveryDecision:
        request_hash = self.request_digest(request)
        provider = self._provider_identity(provider_identity)
        with self._database.session() as session:
            claim_bound_write(session, run_id, authority)
            attempt = self._bound_attempt(
                session, run_id, attempt_id, request_hash, provider
            )
            status = ModelAttemptStatus(attempt.status)
            if status is ModelAttemptStatus.PREPARED:
                now = utc_now()
                changed = self._affected_rows(
                    session.execute(
                        update(ModelAttemptRow)
                        .where(
                            ModelAttemptRow.attempt_id == str(attempt_id),
                            ModelAttemptRow.run_id == str(run_id),
                            ModelAttemptRow.status == ModelAttemptStatus.PREPARED.value,
                        )
                        .values(
                            status=ModelAttemptStatus.DISPATCHING.value,
                            dispatched_at=now,
                        )
                        .execution_options(synchronize_session=False)
                    )
                )
                if changed == 1:
                    self._append_event(
                        session,
                        authority,
                        EventType.MODEL_ATTEMPT_DISPATCHING,
                        {
                            "attempt_id": str(attempt_id),
                            "attempt": attempt.attempt_number,
                            "request_digest": request_hash,
                            "provider_identity": provider,
                            "recovered": True,
                        },
                    )
                    return ModelRecoveryDecision(
                        action=ModelRecoveryAction.DISPATCH,
                        attempt_number=attempt.attempt_number,
                    )
                session.refresh(attempt)
                status = ModelAttemptStatus(attempt.status)
            if status is ModelAttemptStatus.DISPATCHING:
                completed_at = utc_now()
                changed = self._affected_rows(
                    session.execute(
                        update(ModelAttemptRow)
                        .where(
                            ModelAttemptRow.attempt_id == str(attempt_id),
                            ModelAttemptRow.run_id == str(run_id),
                            ModelAttemptRow.status
                            == ModelAttemptStatus.DISPATCHING.value,
                        )
                        .values(
                            status=ModelAttemptStatus.INDETERMINATE.value,
                            completed_at=completed_at,
                        )
                        .execution_options(synchronize_session=False)
                    )
                )
                if changed == 1:
                    self._append_event(
                        session,
                        authority,
                        EventType.MODEL_ATTEMPT_INDETERMINATE,
                        {
                            "attempt_id": str(attempt_id),
                            "attempt": attempt.attempt_number,
                            "reason": "PROVIDER_DISPATCH_OUTCOME_UNKNOWN",
                        },
                    )
                    return ModelRecoveryDecision(
                        action=ModelRecoveryAction.MARK_INDETERMINATE,
                        attempt_number=attempt.attempt_number,
                    )
            return ModelRecoveryDecision(
                action=ModelRecoveryAction.TERMINAL_REPLAY,
                attempt_number=attempt.attempt_number,
            )

    def complete_attempt(
        self,
        run_id: UUID,
        attempt_id: UUID,
        usage: ModelUsage | None,
        duration_ms: int,
        *,
        authority: RunLeaseAuthority,
    ) -> None:
        with self._database.session() as session:
            claim_bound_write(session, run_id, authority)
            attempt = session.get(ModelAttemptRow, str(attempt_id))
            state = session.get(ModelRuntimeStateRow, str(run_id))
            if attempt is None or state is None:
                raise RuntimeError("Model attempt persistence is missing")
            changed = self._affected_rows(
                session.execute(
                    update(ModelAttemptRow)
                    .where(
                        ModelAttemptRow.attempt_id == str(attempt_id),
                        ModelAttemptRow.run_id == str(run_id),
                        ModelAttemptRow.status
                        == ModelAttemptStatus.DISPATCHING.value,
                    )
                    .values(
                        status=ModelAttemptStatus.COMPLETED.value,
                        duration_ms=duration_ms,
                        usage=usage.model_dump(mode="json") if usage else None,
                        completed_at=utc_now(),
                    )
                    .execution_options(synchronize_session=False)
                )
            )
            if changed != 1:
                raise RuntimeError("Only a DISPATCHING attempt can complete")
            if usage is not None:
                state.input_tokens += usage.input_tokens or 0
                state.output_tokens += usage.output_tokens or 0
                state.total_tokens += usage.total_tokens or 0
                state.cached_input_tokens += usage.cached_input_tokens or 0
                state.reasoning_tokens += usage.reasoning_tokens or 0
            state.updated_at = utc_now()
            self._append_event(
                session,
                authority,
                EventType.MODEL_ATTEMPT_COMPLETED,
                {"attempt_id": str(attempt_id), "attempt": attempt.attempt_number},
            )

    def fail_attempt(
        self,
        run_id: UUID,
        attempt_id: UUID,
        error: ModelRequestError,
        *,
        authority: RunLeaseAuthority,
    ) -> None:
        with self._database.session() as session:
            claim_bound_write(session, run_id, authority)
            attempt = session.get(ModelAttemptRow, str(attempt_id))
            if attempt is None:
                raise RuntimeError("Model attempt persistence is missing")
            changed = self._affected_rows(
                session.execute(
                    update(ModelAttemptRow)
                    .where(
                        ModelAttemptRow.attempt_id == str(attempt_id),
                        ModelAttemptRow.run_id == str(run_id),
                        ModelAttemptRow.status
                        == ModelAttemptStatus.DISPATCHING.value,
                    )
                    .values(
                        status=ModelAttemptStatus.FAILED.value,
                        error_type=error.code.value,
                        retryable=error.retryable,
                        completed_at=utc_now(),
                    )
                    .execution_options(synchronize_session=False)
                )
            )
            if changed != 1:
                raise RuntimeError("Only a DISPATCHING attempt can fail")
            self._append_event(
                session,
                authority,
                EventType.MODEL_ATTEMPT_FAILED,
                {
                    "error_type": error.code.value,
                    "retryable": error.retryable,
                    "attempt": attempt.attempt_number,
                },
            )

    def record_retry(
        self,
        run_id: UUID,
        attempt_number: int,
        delay: float,
        *,
        authority: RunLeaseAuthority,
    ) -> None:
        with self._database.session() as session:
            claim_bound_write(session, run_id, authority)
            self._append_event(
                session,
                authority,
                EventType.MODEL_RETRY_SCHEDULED,
                {"attempt": attempt_number, "retry_delay": delay},
            )

    def record_provider_deviation(
        self,
        run_id: UUID,
        info: MultiToolResponseInfo,
        *,
        authority: RunLeaseAuthority,
    ) -> None:
        with self._database.session() as session:
            claim_bound_write(session, run_id, authority)
            self._append_event(
                session,
                authority,
                EventType.MODEL_PROVIDER_DEVIATION,
                info.audit_payload(),
            )

    @staticmethod
    def _to_domain(row: ModelRuntimeStateRow) -> ModelBudgetState:
        return ModelBudgetState(
            run_id=UUID(row.run_id),
            model_request_count=row.model_request_count,
            input_tokens=row.input_tokens,
            output_tokens=row.output_tokens,
            total_tokens=row.total_tokens,
            cached_input_tokens=row.cached_input_tokens,
            reasoning_tokens=row.reasoning_tokens,
            budget=ModelBudget(
                max_model_requests=row.max_model_requests,
                max_retries=row.max_retries,
                max_output_tokens_per_request=row.max_output_tokens_per_request,
                max_total_input_tokens=row.max_total_input_tokens,
                max_total_output_tokens=row.max_total_output_tokens,
                max_total_tokens=row.max_total_tokens,
            ),
        )

    @staticmethod
    def _attempt_to_domain(row: ModelAttemptRow) -> ModelAttemptRecord:
        duration_ms = row.duration_ms
        if (
            duration_ms is None
            and row.completed_at is not None
            and row.status == ModelAttemptStatus.FAILED.value
        ):
            elapsed = normalize_utc(row.completed_at) - normalize_utc(row.created_at)
            duration_ms = max(0, int(elapsed.total_seconds() * 1000))
        return ModelAttemptRecord(
            attempt_id=UUID(row.attempt_id),
            run_id=UUID(row.run_id),
            logical_call_id=UUID(row.logical_call_id),
            attempt_number=row.attempt_number,
            status=ModelAttemptStatus(row.status),
            request_digest=row.request_digest,
            provider_identity=row.provider_identity,
            budget_digest=row.budget_digest,
            error_type=(
                ModelErrorCode(row.error_type)
                if row.error_type is not None
                else None
            ),
            retryable=row.retryable,
            duration_ms=duration_ms,
            usage=ModelUsage.model_validate(row.usage) if row.usage is not None else None,
            created_at=row.created_at,
            dispatched_at=row.dispatched_at,
            completed_at=row.completed_at,
        )

    @staticmethod
    def request_digest(request: ModelRequest) -> str:
        if type(request) is not ModelRequest:
            raise TypeError("request must be a ModelRequest")
        validated = ModelRequest.model_validate(
            request.model_dump(mode="python", warnings=False), strict=True
        )
        if validated != request:
            raise ValueError("request is not canonically validated")
        canonical = json.dumps(
            validated.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _budget_digest(budget: ModelBudget) -> str:
        canonical = json.dumps(
            budget.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _provider_identity(value: str) -> str:
        if type(value) is not str or not value or value != value.strip() or len(value) > 200:
            raise ValueError("provider identity must be canonical")
        return value

    @staticmethod
    def _bound_attempt(
        session: Session,
        run_id: UUID,
        attempt_id: UUID,
        request_digest: str,
        provider_identity: str,
    ) -> ModelAttemptRow:
        attempt = session.get(ModelAttemptRow, str(attempt_id))
        if attempt is None or attempt.run_id != str(run_id):
            raise ModelAttemptConflictError("model attempt identity does not match")
        if (
            attempt.request_digest != request_digest
            or attempt.provider_identity != provider_identity
        ):
            raise ModelAttemptConflictError("model attempt request does not match")
        state = session.get(ModelRuntimeStateRow, str(run_id))
        if state is None:
            raise ModelAttemptConflictError("model attempt budget identity is missing")
        current_budget_digest = ModelWorkflow._budget_digest(
            ModelWorkflow._to_domain(state).budget
        )
        if attempt.budget_digest != current_budget_digest:
            raise ModelAttemptConflictError("model attempt budget does not match")
        return attempt

    @staticmethod
    def _affected_rows(result: object) -> int:
        value = getattr(result, "rowcount", None)
        return value if isinstance(value, int) else 0

    @staticmethod
    def _append_event(
        session: Session,
        authority: RunLeaseAuthority,
        event_type: EventType,
        payload: dict[str, JsonValue],
    ) -> None:
        EventLog().append(session, authority, event_type, payload)
