from collections import Counter
from collections.abc import Sequence
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from agentforge.domain.enums import (
    EvaluationAttemptStatus,
    EvaluationCampaignStatus,
    EvaluationSlotStatus,
)
from agentforge.evaluation.campaign_models import (
    EvaluationCampaign,
    EvaluationPilotAttempt,
    EvaluationSlot,
)
from agentforge.evaluation.metrics import summarize_task
from agentforge.evaluation.models import RepairEvaluationRun, TaskEvaluationSummary
from agentforge.evaluation.protocol import EvaluationProtocol


class EvaluationSelectionError(ValueError):
    pass


class EvaluationSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol_digest: str
    campaign_id: UUID
    task_id: str
    selected_runs: tuple[RepairEvaluationRun, ...]
    superseded_run_ids: tuple[UUID, ...]
    infrastructure_invalid_count: int
    replacement_count: int
    summary: TaskEvaluationSummary


def resolve_effective_runs(
    protocol: EvaluationProtocol,
    campaign: EvaluationCampaign,
    slots: Sequence[EvaluationSlot],
    attempts: Sequence[EvaluationPilotAttempt],
    runs: Sequence[RepairEvaluationRun],
    *,
    require_complete: bool = True,
) -> EvaluationSelection:
    _validate_campaign(protocol, campaign)
    ordered_slots = _validate_slots(protocol, campaign, slots, require_complete)
    by_run_id = _index_runs(protocol, campaign, runs)
    _validate_replacement_graph(protocol, by_run_id)
    by_attempt_id = _index_attempts(protocol, campaign, attempts)
    _validate_attempt_replacement_graph(protocol, by_attempt_id)
    _validate_run_attempt_bindings(by_run_id, by_attempt_id)

    selected: list[RepairEvaluationRun] = []
    selected_ids: set[UUID] = set()
    for slot in ordered_slots:
        if slot.status is not EvaluationSlotStatus.ACCEPTED:
            if require_complete:
                raise EvaluationSelectionError(
                    "Complete selection requires every slot to be accepted"
                )
            continue
        if (
            slot.selected_attempt_id is None
            or slot.selected_evaluation_run_id is None
        ):
            raise EvaluationSelectionError("Accepted slot is missing selected facts")
        if slot.selected_evaluation_run_id in selected_ids:
            raise EvaluationSelectionError(
                "Evaluation run cannot be selected by multiple slots"
            )
        selected_run = by_run_id.get(slot.selected_evaluation_run_id)
        selected_attempt = by_attempt_id.get(slot.selected_attempt_id)
        if selected_run is None or selected_attempt is None:
            raise EvaluationSelectionError("Selected result is missing")
        if (
            selected_run.slot_id != slot.slot_id
            or selected_run.attempt_id != selected_attempt.attempt_id
            or selected_run.evaluation_run_id != selected_attempt.evaluation_run_id
            or selected_run.repetition_index != slot.repetition_index
            or selected_attempt.status is not EvaluationAttemptStatus.COMPLETED
        ):
            raise EvaluationSelectionError("Selected result binding does not match")
        selected_ids.add(selected_run.evaluation_run_id)
        selected.append(selected_run)

    if require_complete and len(selected) != protocol.repetition_count:
        raise EvaluationSelectionError("Complete selection has missing repetitions")
    selected.sort(key=lambda item: item.repetition_index)
    superseded = tuple(
        sorted(
            (
                run.evaluation_run_id
                for run in runs
                if run.evaluation_run_id not in selected_ids
                and run.infrastructure_failure
            ),
            key=str,
        )
    )
    selected_tuple = tuple(selected)
    return EvaluationSelection(
        protocol_digest=protocol.protocol_digest,
        campaign_id=campaign.campaign_id,
        task_id=protocol.task_id,
        selected_runs=selected_tuple,
        superseded_run_ids=superseded,
        infrastructure_invalid_count=sum(
            attempt.status is EvaluationAttemptStatus.INVALID
            and attempt.infrastructure_failure
            for attempt in attempts
        ),
        replacement_count=sum(
            attempt.predecessor_attempt_id is not None for attempt in attempts
        ),
        summary=summarize_task(list(selected_tuple)),
    )


def resolve_available_scored_runs(
    protocol: EvaluationProtocol,
    campaign: EvaluationCampaign,
    slots: Sequence[EvaluationSlot],
    attempts: Sequence[EvaluationPilotAttempt],
    runs: Sequence[RepairEvaluationRun],
) -> EvaluationSelection:
    if campaign.status is not EvaluationCampaignStatus.COMPLETED_WITH_INVALID:
        raise EvaluationSelectionError(
            "Available scored selection requires an infrastructure-gap campaign"
        )
    return resolve_effective_runs(
        protocol,
        campaign,
        slots,
        attempts,
        runs,
        require_complete=False,
    )


def _validate_campaign(
    protocol: EvaluationProtocol,
    campaign: EvaluationCampaign,
) -> None:
    if (
        campaign.protocol_digest != protocol.protocol_digest
        or campaign.task_id != protocol.task_id
        or campaign.repetition_count != protocol.repetition_count
    ):
        raise EvaluationSelectionError("Campaign does not match protocol")
    if campaign.status not in {
        EvaluationCampaignStatus.COMPLETED,
        EvaluationCampaignStatus.COMPLETED_WITH_INVALID,
    }:
        raise EvaluationSelectionError("Campaign is not complete")


def _validate_slots(
    protocol: EvaluationProtocol,
    campaign: EvaluationCampaign,
    slots: Sequence[EvaluationSlot],
    require_complete: bool,
) -> list[EvaluationSlot]:
    ordered = sorted(slots, key=lambda item: item.repetition_index)
    indexes = [slot.repetition_index for slot in ordered]
    if len(set(indexes)) != len(indexes):
        raise EvaluationSelectionError("Evaluation slots contain duplicate repetitions")
    if require_complete and indexes != list(range(protocol.repetition_count)):
        raise EvaluationSelectionError("complete selection requires the fixed slot set")
    for slot in ordered:
        if (
            slot.campaign_id != campaign.campaign_id
            or slot.protocol_digest != protocol.protocol_digest
            or slot.task_id != protocol.task_id
        ):
            raise EvaluationSelectionError("Evaluation slot binding does not match")
    return ordered


def _index_runs(
    protocol: EvaluationProtocol,
    campaign: EvaluationCampaign,
    runs: Sequence[RepairEvaluationRun],
) -> dict[UUID, RepairEvaluationRun]:
    by_id: dict[UUID, RepairEvaluationRun] = {}
    for run in runs:
        if run.evaluation_run_id in by_id:
            raise EvaluationSelectionError("Duplicate evaluation run ID")
        if run.protocol_digest != protocol.protocol_digest:
            raise EvaluationSelectionError("Evaluation run protocol does not match")
        if run.campaign_id != campaign.campaign_id:
            raise EvaluationSelectionError("Evaluation run campaign does not match")
        if run.task_id != protocol.task_id:
            raise EvaluationSelectionError("Evaluation run task does not match")
        if run.repetition_index < 0 or run.repetition_index >= protocol.repetition_count:
            raise EvaluationSelectionError("Evaluation run repetition does not match")
        by_id[run.evaluation_run_id] = run
    return by_id


def _validate_replacement_graph(
    protocol: EvaluationProtocol,
    by_id: dict[UUID, RepairEvaluationRun],
) -> None:
    children = Counter(
        run.replacement_for_evaluation_run_id
        for run in by_id.values()
        if run.replacement_for_evaluation_run_id is not None
    )
    if any(count > 1 for count in children.values()):
        raise EvaluationSelectionError("Replacement graph contains a branch")
    for run in by_id.values():
        _detect_cycle(run, by_id)
    for run in by_id.values():
        predecessor_id = run.replacement_for_evaluation_run_id
        if predecessor_id is None:
            continue
        predecessor = by_id.get(predecessor_id)
        if predecessor is None:
            raise EvaluationSelectionError("Replacement has a missing predecessor")
        if predecessor.task_id != run.task_id:
            raise EvaluationSelectionError("Replacement crosses task binding")
        if predecessor.protocol_digest != run.protocol_digest:
            raise EvaluationSelectionError("Replacement crosses protocol binding")
        if (
            predecessor.campaign_id != run.campaign_id
            or predecessor.slot_id != run.slot_id
        ):
            raise EvaluationSelectionError("Replacement crosses slot binding")
        if predecessor.repetition_index != run.repetition_index:
            raise EvaluationSelectionError("Replacement crosses repetition binding")
        if run.attempt_number <= predecessor.attempt_number:
            raise EvaluationSelectionError(
                "Replacement attempt number must increase"
            )
        if (
            not predecessor.infrastructure_failure
            or predecessor.failure_category
            not in protocol.replacement_policy.replaceable_failure_categories
        ):
            raise EvaluationSelectionError(
                "Replacement predecessor is not an allowed infrastructure failure"
            )
    replacement_counts = Counter(
        run.repetition_index
        for run in by_id.values()
        if run.replacement_for_evaluation_run_id is not None
    )
    if any(
        count > protocol.replacement_policy.max_replacements_per_slot
        for count in replacement_counts.values()
    ):
        raise EvaluationSelectionError("Replacement allowance was exceeded")


def _detect_cycle(
    start: RepairEvaluationRun,
    by_id: dict[UUID, RepairEvaluationRun],
) -> None:
    seen: set[UUID] = set()
    current = start
    while current.replacement_for_evaluation_run_id is not None:
        if current.evaluation_run_id in seen:
            raise EvaluationSelectionError("Replacement graph contains a cycle")
        seen.add(current.evaluation_run_id)
        predecessor = by_id.get(current.replacement_for_evaluation_run_id)
        if predecessor is None:
            return
        current = predecessor


def _index_attempts(
    protocol: EvaluationProtocol,
    campaign: EvaluationCampaign,
    attempts: Sequence[EvaluationPilotAttempt],
) -> dict[UUID, EvaluationPilotAttempt]:
    by_id: dict[UUID, EvaluationPilotAttempt] = {}
    numbers: set[tuple[UUID, int]] = set()
    for attempt in attempts:
        if attempt.attempt_id in by_id:
            raise EvaluationSelectionError("Duplicate evaluation attempt ID")
        if (
            attempt.protocol_digest != protocol.protocol_digest
            or attempt.campaign_id != campaign.campaign_id
            or attempt.task_id != protocol.task_id
        ):
            raise EvaluationSelectionError("Evaluation attempt binding does not match")
        key = (attempt.slot_id, attempt.attempt_number)
        if key in numbers:
            raise EvaluationSelectionError("Duplicate attempt number in slot")
        numbers.add(key)
        by_id[attempt.attempt_id] = attempt
    return by_id


def _validate_run_attempt_bindings(
    runs: dict[UUID, RepairEvaluationRun],
    attempts: dict[UUID, EvaluationPilotAttempt],
) -> None:
    for run in runs.values():
        attempt = attempts.get(run.attempt_id)
        if attempt is None:
            raise EvaluationSelectionError("Evaluation run attempt is missing")
        if (
            attempt.evaluation_run_id != run.evaluation_run_id
            or attempt.slot_id != run.slot_id
            or attempt.repetition_index != run.repetition_index
            or attempt.attempt_number != run.attempt_number
            or attempt.predecessor_evaluation_run_id
            != run.replacement_for_evaluation_run_id
        ):
            raise EvaluationSelectionError(
                "Evaluation run replacement binding does not match attempt"
            )


def _validate_attempt_replacement_graph(
    protocol: EvaluationProtocol,
    attempts: dict[UUID, EvaluationPilotAttempt],
) -> None:
    children = Counter(
        attempt.predecessor_attempt_id
        for attempt in attempts.values()
        if attempt.predecessor_attempt_id is not None
    )
    if any(count > 1 for count in children.values()):
        raise EvaluationSelectionError("Attempt replacement graph contains a branch")

    replacement_counts: Counter[UUID] = Counter()
    for attempt in attempts.values():
        predecessor_id = attempt.predecessor_attempt_id
        if predecessor_id is None:
            if (
                attempt.attempt_number != 1
                or attempt.predecessor_evaluation_run_id is not None
            ):
                raise EvaluationSelectionError(
                    "Replacement attempt is missing its predecessor"
                )
            continue
        predecessor = attempts.get(predecessor_id)
        if predecessor is None:
            raise EvaluationSelectionError(
                "Attempt replacement has a missing predecessor"
            )
        if (
            predecessor.campaign_id != attempt.campaign_id
            or predecessor.slot_id != attempt.slot_id
            or predecessor.task_id != attempt.task_id
            or predecessor.protocol_digest != attempt.protocol_digest
            or predecessor.repetition_index != attempt.repetition_index
        ):
            raise EvaluationSelectionError(
                "Attempt replacement crosses a frozen binding"
            )
        if (
            predecessor.status is not EvaluationAttemptStatus.INVALID
            or not predecessor.infrastructure_failure
            or predecessor.failure_category is None
            or not protocol.replacement_policy.allows(
                predecessor.failure_category
            )
        ):
            raise EvaluationSelectionError(
                "Attempt replacement predecessor is not an allowed "
                "infrastructure failure"
            )
        if (
            attempt.attempt_number != predecessor.attempt_number + 1
            or attempt.predecessor_evaluation_run_id
            != predecessor.evaluation_run_id
        ):
            raise EvaluationSelectionError(
                "Attempt replacement predecessor binding does not match"
            )
        replacement_counts[attempt.slot_id] += 1

    if any(
        count > protocol.replacement_policy.max_replacements_per_slot
        for count in replacement_counts.values()
    ):
        raise EvaluationSelectionError("Attempt replacement allowance was exceeded")

    for attempt in attempts.values():
        seen: set[UUID] = set()
        current = attempt
        while current.predecessor_attempt_id is not None:
            if current.attempt_id in seen:
                raise EvaluationSelectionError(
                    "Attempt replacement graph contains a cycle"
                )
            seen.add(current.attempt_id)
            predecessor = attempts.get(current.predecessor_attempt_id)
            if predecessor is None:
                break
            current = predecessor
