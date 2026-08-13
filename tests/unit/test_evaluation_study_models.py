from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from agentforge.domain.enums import EvaluationStudyStatus
from agentforge.evaluation.study_models import (
    B2_4_TASK_ORDER,
    EvaluationStudy,
    EvaluationStudyCampaignBinding,
    EvaluationStudyDefinition,
    EvaluationStudySummary,
    validate_study_campaign_bindings,
)

SHA = "a" * 64
GIT_SHA = "b" * 40


def definition(
    *,
    task_ids: tuple[str, str, str, str] = B2_4_TASK_ORDER,
    protocol_digests: tuple[str, str, str, str] = (
        "1" * 64,
        "2" * 64,
        "3" * 64,
        "4" * 64,
    ),
    model_id: str = "gpt-test",
    response_model_id: str | None = None,
) -> EvaluationStudyDefinition:
    values: dict[str, object] = {
        "study_name": "agentforge-b2.4-portfolio-pilot",
        "task_ids": task_ids,
        "protocol_digests": protocol_digests,
        "provider_configuration_digest": SHA,
        "model_id": model_id,
        "runtime_source_digest": SHA,
        "git_commit_sha": GIT_SHA,
        "git_worktree_clean": True,
        "pyproject_sha256": SHA,
        "uv_lock_sha256": SHA,
        "fixture_registry_digest": SHA,
        "platform_binding_digest": SHA,
        "pricing_snapshot_digest": SHA,
    }
    if response_model_id is not None:
        values["response_model_id"] = response_model_id
    return EvaluationStudyDefinition.model_validate(values)


def test_definition_digest_is_deterministic_and_order_sensitive() -> None:
    first = definition()
    repeated = definition()

    assert first == repeated
    assert first.definition_digest == repeated.definition_digest
    assert len(first.definition_digest) == 64
    with pytest.raises(ValidationError, match="task order"):
        definition(
            task_ids=(
                B2_4_TASK_ORDER[1],
                B2_4_TASK_ORDER[0],
                B2_4_TASK_ORDER[2],
                B2_4_TASK_ORDER[3],
            )
        )


def test_definition_requires_four_unique_protocols_and_frozen_slot_count() -> None:
    with pytest.raises(ValidationError, match="unique"):
        definition(
            protocol_digests=(
                "1" * 64,
                "1" * 64,
                "3" * 64,
                "4" * 64,
            )
        )
    with pytest.raises(ValidationError):
        EvaluationStudyDefinition.model_validate(
            {
                **definition().model_dump(mode="json"),
                "planned_scoring_slots": 11,
                "definition_digest": "",
            }
        )


def test_definition_rejects_dirty_source_and_open_ended_secret_fields() -> None:
    data = definition().model_dump(mode="json")
    data.update(
        {
            "git_worktree_clean": False,
            "api_key": "must-not-enter-study",
            "definition_digest": "",
        }
    )

    with pytest.raises(ValidationError):
        EvaluationStudyDefinition.model_validate(data)


def test_definition_rejects_response_model_outside_requested_model_family() -> None:
    with pytest.raises(ValidationError, match="response model"):
        definition(
            model_id="gpt-5.4-mini",
            response_model_id="gpt-5.6-luna",
        )


def test_study_terminal_timestamp_and_status_are_consistent() -> None:
    draft = EvaluationStudy(definition_digest=definition().definition_digest)

    assert draft.status is EvaluationStudyStatus.DRAFT
    assert draft.completed_at is None
    with pytest.raises(ValidationError, match="terminal"):
        EvaluationStudy(
            definition_digest=draft.definition_digest,
            status=EvaluationStudyStatus.COMPLETED,
        )
    completed = EvaluationStudy(
        definition_digest=draft.definition_digest,
        authorization_digest=SHA,
        status=EvaluationStudyStatus.COMPLETED,
        completed_at=datetime.now(UTC),
    )
    assert completed.completed_at is not None


def test_campaign_bindings_require_exact_definition_order_and_uniqueness() -> None:
    value = definition()
    study = EvaluationStudy(definition_digest=value.definition_digest)
    bindings = tuple(
        EvaluationStudyCampaignBinding(
            study_id=study.study_id,
            task_id=task_id,
            task_order=index,
            protocol_digest=value.protocol_digests[index],
            campaign_id=uuid4(),
        )
        for index, task_id in enumerate(value.task_ids)
    )

    assert validate_study_campaign_bindings(value, study, bindings) == bindings
    with pytest.raises(ValueError, match=r"(?i)campaign"):
        validate_study_campaign_bindings(
            value,
            study,
            (
                bindings[0],
                bindings[1],
                bindings[2],
                bindings[3].model_copy(
                    update={"campaign_id": bindings[0].campaign_id}
                ),
            ),
        )
    with pytest.raises(ValueError, match="order"):
        validate_study_campaign_bindings(
            value,
            study,
            (bindings[1], bindings[0], bindings[2], bindings[3]),
        )


def test_summary_reconciles_denominators_and_rates() -> None:
    # Realizable task topology: scored=(3, 3, 3, 0), successful=(3, 1, 0, 0).
    summary = EvaluationStudySummary(
        scored_slots=9,
        successful_slots=4,
        infrastructure_invalid_slots=3,
        indeterminate_slots=0,
        scoring_coverage=0.75,
        scored_success_rate=4 / 9,
        planned_slot_success_rate=1 / 3,
        macro_pass_at_1=4 / 9,
        pass_at_1_eligible_task_count=3,
        macro_pass_at_3=None,
        pass_at_3_eligible_task_count=3,
        first_attempt_success_count=1,
        any_success_in_3_count=2,
        stable_success_count=1,
    )

    assert summary.planned_slots == 12
    with pytest.raises(ValidationError, match="reconcile"):
        summary.model_copy(
            update={"infrastructure_invalid_slots": 4}
        ).model_validate(
            {
                **summary.model_dump(mode="json"),
                "infrastructure_invalid_slots": 4,
            }
        )
    with pytest.raises(ValidationError, match="rate"):
        EvaluationStudySummary.model_validate(
            {
                **summary.model_dump(mode="json"),
                "scoring_coverage": 0.5,
            }
        )


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        (
            {
                "scored_slots": 0,
                "successful_slots": 0,
                "scoring_coverage": 0,
                "scored_success_rate": None,
                "planned_slot_success_rate": 0,
                "macro_pass_at_1": 1,
                "pass_at_1_eligible_task_count": 1,
                "macro_pass_at_3": None,
                "pass_at_3_eligible_task_count": 0,
                "first_attempt_success_count": 0,
                "any_success_in_3_count": 0,
                "stable_success_count": 0,
            },
            "pass@1 eligibility exceeds scored slots",
        ),
        (
            {
                "scored_slots": 2,
                "successful_slots": 2,
                "scoring_coverage": 1 / 6,
                "scored_success_rate": 1,
                "planned_slot_success_rate": 1 / 6,
                "pass_at_1_eligible_task_count": 2,
                "macro_pass_at_3": None,
                "pass_at_3_eligible_task_count": 1,
                "stable_success_count": 0,
            },
            "pass@3 eligibility exceeds scored slots",
        ),
    ],
)
def test_summary_rejects_impossible_aggregate_eligibility(
    updates: dict[str, object],
    message: str,
) -> None:
    # Realizable task topology: scored=(3, 3, 3, 3), successful=(3, 1, 0, 0).
    valid = EvaluationStudySummary(
        scored_slots=12,
        successful_slots=4,
        infrastructure_invalid_slots=0,
        indeterminate_slots=0,
        scoring_coverage=1,
        scored_success_rate=1 / 3,
        planned_slot_success_rate=1 / 3,
        macro_pass_at_1=1 / 3,
        pass_at_1_eligible_task_count=4,
        macro_pass_at_3=0.5,
        pass_at_3_eligible_task_count=4,
        first_attempt_success_count=1,
        any_success_in_3_count=2,
        stable_success_count=1,
    )

    with pytest.raises(ValidationError, match=message):
        EvaluationStudySummary.model_validate(
            {**valid.model_dump(mode="json"), **updates}
        )


def test_summary_rejects_inconsistent_complete_pass_at_3_macro() -> None:
    with pytest.raises(ValidationError, match="pass@3 macro does not reconcile"):
        EvaluationStudySummary(
            scored_slots=12,
            successful_slots=0,
            infrastructure_invalid_slots=0,
            indeterminate_slots=0,
            scoring_coverage=1,
            scored_success_rate=0,
            planned_slot_success_rate=0,
            macro_pass_at_1=0,
            pass_at_1_eligible_task_count=4,
            macro_pass_at_3=1,
            pass_at_3_eligible_task_count=4,
            first_attempt_success_count=0,
            any_success_in_3_count=0,
            stable_success_count=0,
        )


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        (
            {
                "first_attempt_success_count": 3,
                "any_success_in_3_count": 3,
                "macro_pass_at_3": 0.75,
            },
            "First-attempt successes exceed successful slots",
        ),
        (
            {"any_success_in_3_count": 4, "macro_pass_at_3": 1},
            "Any-success tasks exceed successful slots",
        ),
        (
            {"stable_success_count": 2},
            "Stable-success tasks require three successful slots each",
        ),
    ],
)
def test_summary_rejects_impossible_aggregate_outcome_counts(
    updates: dict[str, object],
    message: str,
) -> None:
    # Realizable task topology: scored=(3, 3, 3, 3), successful=(1, 1, 0, 0).
    valid = EvaluationStudySummary(
        scored_slots=12,
        successful_slots=2,
        infrastructure_invalid_slots=0,
        indeterminate_slots=0,
        scoring_coverage=1,
        scored_success_rate=1 / 6,
        planned_slot_success_rate=1 / 6,
        macro_pass_at_1=1 / 6,
        pass_at_1_eligible_task_count=4,
        macro_pass_at_3=0.5,
        pass_at_3_eligible_task_count=4,
        first_attempt_success_count=1,
        any_success_in_3_count=2,
        stable_success_count=0,
    )

    with pytest.raises(ValidationError, match=message):
        EvaluationStudySummary.model_validate(
            {**valid.model_dump(mode="json"), **updates}
        )


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (
            {
                "scored_slots": 1,
                "successful_slots": 0,
                "infrastructure_invalid_slots": 0,
                "indeterminate_slots": 0,
                "scoring_coverage": 1 / 12,
                "scored_success_rate": 0,
                "planned_slot_success_rate": 0,
                "macro_pass_at_1": None,
                "pass_at_1_eligible_task_count": 0,
                "macro_pass_at_3": None,
                "pass_at_3_eligible_task_count": 0,
                "first_attempt_success_count": 0,
                "any_success_in_3_count": 0,
                "stable_success_count": 0,
            },
            "Scored slots exceed aggregate task eligibility topology",
        ),
        (
            {
                "scored_slots": 12,
                "successful_slots": 0,
                "infrastructure_invalid_slots": 0,
                "indeterminate_slots": 0,
                "scoring_coverage": 1,
                "scored_success_rate": 0,
                "planned_slot_success_rate": 0,
                "macro_pass_at_1": 0,
                "pass_at_1_eligible_task_count": 3,
                "macro_pass_at_3": 0,
                "pass_at_3_eligible_task_count": 4,
                "first_attempt_success_count": 0,
                "any_success_in_3_count": 0,
                "stable_success_count": 0,
            },
            "Scored slots exceed aggregate task eligibility topology",
        ),
        (
            {
                "scored_slots": 4,
                "successful_slots": 0,
                "infrastructure_invalid_slots": 0,
                "indeterminate_slots": 0,
                "scoring_coverage": 1 / 3,
                "scored_success_rate": 0,
                "planned_slot_success_rate": 0,
                "macro_pass_at_1": 1,
                "pass_at_1_eligible_task_count": 4,
                "macro_pass_at_3": None,
                "pass_at_3_eligible_task_count": 0,
                "first_attempt_success_count": 0,
                "any_success_in_3_count": 0,
                "stable_success_count": 0,
            },
            "Zero successful slots require a zero macro pass@1",
        ),
        (
            {
                "scored_slots": 7,
                "successful_slots": 6,
                "infrastructure_invalid_slots": 0,
                "indeterminate_slots": 0,
                "scoring_coverage": 7 / 12,
                "scored_success_rate": 6 / 7,
                "planned_slot_success_rate": 0.5,
                "macro_pass_at_1": 0.5,
                "pass_at_1_eligible_task_count": 3,
                "macro_pass_at_3": None,
                "pass_at_3_eligible_task_count": 1,
                "first_attempt_success_count": 0,
                "any_success_in_3_count": 2,
                "stable_success_count": 2,
            },
            "Stable-success tasks exceed pass@3-eligible tasks",
        ),
        (
            {
                "scored_slots": 5,
                "successful_slots": 3,
                "infrastructure_invalid_slots": 0,
                "indeterminate_slots": 0,
                "scoring_coverage": 5 / 12,
                "scored_success_rate": 0.6,
                "planned_slot_success_rate": 0.25,
                "macro_pass_at_1": 0.5,
                "pass_at_1_eligible_task_count": 3,
                "macro_pass_at_3": None,
                "pass_at_3_eligible_task_count": 1,
                "first_attempt_success_count": 0,
                "any_success_in_3_count": 2,
                "stable_success_count": 1,
            },
            "Successful slots fall below aggregate outcome topology",
        ),
        (
            {
                "scored_slots": 5,
                "successful_slots": 3,
                "infrastructure_invalid_slots": 0,
                "indeterminate_slots": 0,
                "scoring_coverage": 5 / 12,
                "scored_success_rate": 0.6,
                "planned_slot_success_rate": 0.25,
                "macro_pass_at_1": 0.5,
                "pass_at_1_eligible_task_count": 3,
                "macro_pass_at_3": None,
                "pass_at_3_eligible_task_count": 1,
                "first_attempt_success_count": 0,
                "any_success_in_3_count": 1,
                "stable_success_count": 0,
            },
            "Successful slots exceed aggregate outcome topology",
        ),
    ],
)
def test_summary_rejects_impossible_aggregate_topology(
    payload: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        EvaluationStudySummary(**payload)
