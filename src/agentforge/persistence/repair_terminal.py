"""Session-bound RepairState terminalization for product Run terminal facts."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy.orm import Session

from agentforge.application.kernel_errors import IncompleteRunBundleError
from agentforge.domain.repair import RepairCompletionStatus, RepairTerminationReason
from agentforge.persistence.tables import RepairStateRow


class RepairStateProvenance(StrEnum):
    PRODUCT_BUNDLE = "PRODUCT_BUNDLE"
    EVALUATOR_LEGACY = "EVALUATOR_LEGACY"


def terminalize_repair_in_session(
    session: Session,
    run_id: UUID,
    *,
    status: RepairCompletionStatus,
    reason: RepairTerminationReason,
    at: datetime,
    provenance: RepairStateProvenance = RepairStateProvenance.PRODUCT_BUNDLE,
) -> None:
    """Close RepairState in the caller's Run/event/receipt transaction."""
    repair = session.get(RepairStateRow, str(run_id))
    if repair is None:
        if provenance is RepairStateProvenance.PRODUCT_BUNDLE:
            raise IncompleteRunBundleError()
        return
    if repair.status == status.value and repair.failure_reason == reason.value:
        return
    if repair.status != RepairCompletionStatus.RUNNING.value:
        raise IncompleteRunBundleError()
    repair.status = status.value
    repair.failure_reason = reason.value
    repair.state_version += 1
    repair.updated_at = at
