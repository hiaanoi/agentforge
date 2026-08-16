import json
from decimal import Decimal

import pytest
from pydantic import ValidationError

from agentforge.domain.enums import (
    EvaluationCampaignStatus,
    EvaluationSlotStatus,
)
from agentforge.domain.repair import RepairCompletionStatus
from agentforge.evaluation.public_artifacts import (
    ForbiddenPublicArtifactError,
    PublicArtifactScanner,
)
from agentforge.evaluation.study_reports import (
    PublicProviderSettings,
    PublicScoredRun,
    PublicSlotStudyRecord,
    PublicTaskStudyReport,
    aggregate_study_summary,
    render_public_study_json,
    render_public_study_markdown,
)


def task_report(
    index: int,
    *,
    scored: int,
    successful: int,
    infrastructure: int = 0,
    indeterminate: int = 0,
    cost_complete: bool = True,
    cost: str = "0.010000",
    successful_indices: tuple[int, ...] | None = None,
) -> PublicTaskStudyReport:
    unexecuted = 3 - scored - infrastructure - indeterminate
    success_indices = set(
        range(successful)
        if successful_indices is None
        else successful_indices
    )
    assert len(success_indices) == successful
    scored_runs = tuple(
        PublicScoredRun(
            repetition_index=repetition_index,
            attempt_number=1,
            final_status=(
                RepairCompletionStatus.VERIFIED_SUCCESS
                if repetition_index in success_indices
                else RepairCompletionStatus.BUDGET_EXHAUSTED
            ),
            verified_success=repetition_index in success_indices,
            failure_category=(
                None
                if repetition_index in success_indices
                else "BUDGET_EXHAUSTED"
            ),
            model_calls=2,
            read_calls=4,
            edit_attempts=0,
            test_runs=0,
            wall_time_ms=100,
            total_tokens=120,
        )
        for repetition_index in range(scored)
    )
    statuses = (
        [EvaluationSlotStatus.ACCEPTED] * scored
        + [EvaluationSlotStatus.INVALID] * infrastructure
        + [EvaluationSlotStatus.INDETERMINATE] * indeterminate
        + [EvaluationSlotStatus.PENDING] * unexecuted
    )
    slots = tuple(
        PublicSlotStudyRecord(
            repetition_index=repetition_index,
            slot_status=status,
            attempt_count=(
                0 if status is EvaluationSlotStatus.PENDING else 1
            ),
            replacement_count=0,
            selected_run=(
                scored_runs[repetition_index]
                if status is EvaluationSlotStatus.ACCEPTED
                else None
            ),
        )
        for repetition_index, status in enumerate(statuses)
    )
    return PublicTaskStudyReport(
        task_id=f"task-{index}",
        task_order=index,
        protocol_digest=str(index + 1) * 64,
        campaign_status=(
            EvaluationCampaignStatus.COMPLETED
            if unexecuted == 0 and infrastructure == 0 and indeterminate == 0
            else None
        ),
        fixture_asset_digest="a" * 64,
        task_policy_digest="b" * 64,
        system_prompt_digest="c" * 64,
        task_prompt_digest="d" * 64,
        tool_schema_digest="e" * 64,
        planned_slots=3,
        scored_slots=scored,
        successful_slots=successful,
        infrastructure_invalid_slots=infrastructure,
        indeterminate_slots=indeterminate,
        unexecuted_slots=unexecuted,
        scoring_coverage=scored / 3,
        scored_success_rate=(
            successful / scored if scored else None
        ),
        planned_slot_success_rate=successful / 3,
        pass_at_1=(successful / scored if scored else None),
        pass_at_3=(1.0 if successful > 0 else 0.0) if scored == 3 else None,
        first_attempt_success=0 in success_indices,
        majority_success=successful >= 2,
        stable_success=successful == 3,
        any_success_in_3=successful > 0,
        logical_model_calls=2 * scored,
        physical_model_requests=3 * scored,
        retry_count=scored,
        read_call_count=4 * scored,
        mutation_committed_count=successful,
        managed_test_completed_count=2 * scored,
        wall_time_ms=100 * scored,
        input_tokens=100 * scored,
        output_tokens=20 * scored,
        total_tokens=120 * scored,
        usage_complete=True,
        estimated_cost_complete=cost_complete,
        estimated_cost=Decimal(cost) if cost_complete else None,
        provider_deviation_count=0,
        normalized_multi_tool_response_count=0,
        mean_logical_model_calls_per_scored_run=(
            2.0 if scored else None
        ),
        mean_physical_model_requests_per_scored_run=(
            3.0 if scored else None
        ),
        mean_retries_per_scored_run=1.0 if scored else None,
        mean_reads_per_scored_run=4.0 if scored else None,
        mean_edits_per_scored_run=0.0 if scored else None,
        mean_development_tests_per_scored_run=0.0 if scored else None,
        median_wall_time_ms=100.0 if scored else None,
        mean_total_tokens_per_scored_run=120.0 if scored else None,
        provider_deviation_rate=0,
        multi_tool_normalization_rate=0,
        raw_infrastructure_attempt_count=infrastructure,
        replacement_count=0,
        policy_block_rate=0,
        budget_exhaustion_rate=0,
        protocol_error_rate=0,
        visible_test_failure_rate=0,
        hidden_test_failure_rate=0,
        model_quality_failure_distribution={},
        infrastructure_failure_distribution=(
            {"MODEL_TIMEOUT": infrastructure}
            if infrastructure
            else {}
        ),
        slots=slots,
        scored_runs=scored_runs,
    )


def test_zero_of_twelve_has_null_scored_rate() -> None:
    summary = aggregate_study_summary(
        tuple(
            task_report(index, scored=0, successful=0)
            for index in range(4)
        )
    )

    assert summary.planned_slots == 12
    assert summary.scored_slots == 0
    assert summary.scored_success_rate is None
    assert summary.scoring_coverage == 0
    assert summary.planned_slot_success_rate == 0
    assert summary.macro_pass_at_1 is None
    assert summary.pass_at_1_eligible_task_count == 0
    assert summary.macro_pass_at_3 is None
    assert summary.pass_at_3_eligible_task_count == 0
    assert summary.first_attempt_success_count == 0


def test_mixed_outcomes_keep_all_denominators_independent() -> None:
    summary = aggregate_study_summary(
        (
            task_report(0, scored=3, successful=2),
            task_report(
                1,
                scored=2,
                successful=1,
                infrastructure=1,
            ),
            task_report(
                2,
                scored=1,
                successful=0,
                indeterminate=1,
            ),
            task_report(3, scored=0, successful=0),
        )
    )

    assert summary.scored_slots == 6
    assert summary.successful_slots == 3
    assert summary.infrastructure_invalid_slots == 1
    assert summary.indeterminate_slots == 1
    assert summary.unexecuted_slots == 4
    assert summary.scoring_coverage == 0.5
    assert summary.scored_success_rate == 0.5
    assert summary.planned_slot_success_rate == 0.25
    assert summary.any_success_in_3_count == 2
    assert summary.macro_pass_at_1 == pytest.approx((2 / 3 + 1 / 2 + 0) / 3)
    assert summary.pass_at_1_eligible_task_count == 3
    assert summary.macro_pass_at_3 is None
    assert summary.pass_at_3_eligible_task_count == 1
    assert summary.stable_success_count == 0


def test_twelve_of_twelve_reconciles_to_full_coverage() -> None:
    summary = aggregate_study_summary(
        tuple(
            task_report(index, scored=3, successful=3)
            for index in range(4)
        )
    )

    assert summary.scored_slots == 12
    assert summary.successful_slots == 12
    assert summary.scoring_coverage == 1
    assert summary.scored_success_rate == 1
    assert summary.planned_slot_success_rate == 1
    assert summary.stable_success_count == 4
    assert summary.macro_pass_at_1 == 1
    assert summary.pass_at_1_eligible_task_count == 4
    assert summary.macro_pass_at_3 == 1
    assert summary.pass_at_3_eligible_task_count == 4


def test_aggregate_reports_macro_pass_at_k_and_first_attempts() -> None:
    summary = aggregate_study_summary(
        tuple(task_report(i, scored=3, successful=i) for i in range(4))
    )

    assert summary.macro_pass_at_1 == pytest.approx((0 + 1 / 3 + 2 / 3 + 1) / 4)
    assert summary.macro_pass_at_3 == pytest.approx(3 / 4)
    assert summary.first_attempt_success_count == 3


def test_first_attempt_count_is_distinct_from_any_success_in_three() -> None:
    summary = aggregate_study_summary(
        (
            task_report(0, scored=3, successful=0),
            task_report(1, scored=3, successful=1, successful_indices=(1,)),
            task_report(2, scored=3, successful=2),
            task_report(3, scored=3, successful=3),
        )
    )

    assert summary.first_attempt_success_count == 2
    assert summary.any_success_in_3_count == 3


def test_partial_pass_at_1_averages_only_eligible_task_values() -> None:
    summary = aggregate_study_summary(
        (
            task_report(0, scored=0, successful=0),
            task_report(1, scored=1, successful=1),
            task_report(2, scored=2, successful=1),
            task_report(3, scored=0, successful=0),
        )
    )

    assert summary.macro_pass_at_1 == pytest.approx((1 + 1 / 2) / 2)
    assert summary.pass_at_1_eligible_task_count == 2
    assert summary.macro_pass_at_3 is None
    assert summary.pass_at_3_eligible_task_count == 0


def test_task_metrics_use_standard_estimator() -> None:
    task = task_report(0, scored=3, successful=2, successful_indices=(1, 2))

    assert task.pass_at_1 == pytest.approx(2 / 3)
    assert task.pass_at_3 == pytest.approx(1.0)
    assert task.first_attempt_success is False
    assert task.any_success_in_3 is True


def test_incomplete_task_has_no_pass_at_3() -> None:
    task = task_report(0, scored=2, successful=1)

    assert task.pass_at_1 == pytest.approx(0.5)
    assert task.pass_at_3 is None


def test_task_with_no_scored_slots_has_no_pass_at_one() -> None:
    task = task_report(0, scored=0, successful=0)

    assert task.pass_at_1 is None


def test_task_rejects_success_count_that_disagrees_with_scored_runs() -> None:
    payload = task_report(0, scored=3, successful=1).model_dump()
    payload.update(
        successful_slots=0,
        scored_success_rate=0,
        planned_slot_success_rate=0,
        pass_at_1=0,
        pass_at_3=0,
        any_success_in_3=False,
        majority_success=False,
        stable_success=False,
        mutation_committed_count=0,
    )

    with pytest.raises(
        ValidationError,
        match="Task successful slots do not reconcile with scored runs",
    ):
        PublicTaskStudyReport.model_validate(payload)


def test_incomplete_task_cost_makes_study_cost_incomplete() -> None:
    summary = aggregate_study_summary(
        (
            task_report(0, scored=3, successful=1),
            task_report(
                1,
                scored=3,
                successful=1,
                cost_complete=False,
            ),
            task_report(2, scored=3, successful=1),
            task_report(3, scored=3, successful=1),
        )
    )

    assert summary.estimated_cost_complete is False
    assert summary.estimated_cost is None


def test_public_rendering_is_deterministic_and_scanned() -> None:
    tasks = tuple(
        task_report(index, scored=3, successful=index)
        for index in range(4)
    )
    summary = aggregate_study_summary(tasks)
    scanner = PublicArtifactScanner()

    first_json = render_public_study_json(summary, tasks, scanner=scanner)
    second_json = render_public_study_json(summary, tasks, scanner=scanner)
    markdown = render_public_study_markdown(
        summary,
        tasks,
        scanner=scanner,
    )

    assert first_json == second_json
    payload = json.loads(first_json)
    assert payload["report_schema_version"] == 2
    assert payload["summary"]["planned_slots"] == 12
    assert payload["summary"]["successful_slots"] == 6
    assert payload["summary"]["macro_pass_at_1"] == pytest.approx(0.5)
    assert payload["summary"]["macro_pass_at_3"] == pytest.approx(0.75)
    assert payload["summary"]["first_attempt_success_count"] == 3
    assert "task_pass_at_1_count" not in payload["summary"]
    assert payload["tasks"][0]["pass_at_1"] == 0.0
    assert payload["tasks"][0]["pass_at_3"] == 0.0
    assert payload["tasks"][1]["pass_at_1"] == pytest.approx(1 / 3)
    assert payload["tasks"][1]["pass_at_3"] == 1.0
    assert "Planned slots: 12" in markdown
    assert "Macro pass@1: 50.00% (4 eligible tasks)" in markdown
    assert "Macro pass@3: 75.00% (4 eligible tasks)" in markdown
    assert "First-attempt success count: 3/4" in markdown
    assert "0/3" in markdown
    assert "score-eligible" in markdown
    assert "non-official" in markdown


@pytest.mark.parametrize("timeout_seconds", [float("nan"), float("inf"), float("-inf")])
def test_provider_settings_reject_nonfinite_timeout(
    timeout_seconds: float,
) -> None:
    with pytest.raises(ValidationError):
        PublicProviderSettings(
            timeout_seconds=timeout_seconds,
            max_retries=0,
            max_output_tokens=1,
            multi_tool_response_policy="SEQUENTIAL",
            max_function_calls_per_response=1,
            configuration_digest="a" * 64,
        )


def test_public_renderers_reject_summary_that_disagrees_with_tasks() -> None:
    summary_tasks = tuple(
        task_report(index, scored=3, successful=3) for index in range(4)
    )
    contradictory_tasks = tuple(
        task_report(index, scored=3, successful=0) for index in range(4)
    )
    summary = aggregate_study_summary(summary_tasks)
    scanner = PublicArtifactScanner()

    with pytest.raises(
        ValueError,
        match="Study report summary does not reconcile with tasks",
    ):
        render_public_study_json(summary, contradictory_tasks, scanner=scanner)
    with pytest.raises(
        ValueError,
        match="Study report summary does not reconcile with tasks",
    ):
        render_public_study_markdown(
            summary,
            contradictory_tasks,
            scanner=scanner,
        )


@pytest.mark.parametrize(
    "artifact",
    [
        '{"api_key":"secret-value"}',
        '{"provider_request_id":"req_private"}',
        '{"tool_arguments":{"path":"x"}}',
        '{"tool_output":"source text"}',
        r'{"path":"C:\\Users\\private\\workspace"}',
        '{"path":"C:/Users/private/workspace"}',
        '{"path":"//server/share/private/workspace"}',
        '{"path":"/home/private/workspace"}',
        '{"path":"/etc/private-config"}',
        '{"marker":"tests/hidden/test_oracle.py"}',
        '{"marker":"reference/fixed_files/answer.py"}',
        'DEEPSEEK_API_KEY="definitely-not-a-placeholder"',
        '{"deepseek_api_key":"definitely-not-a-placeholder"}',
    ],
)
def test_public_scanner_rejects_forbidden_content(artifact: str) -> None:
    with pytest.raises(ForbiddenPublicArtifactError):
        PublicArtifactScanner().validate(artifact)


def test_public_scanner_rejects_supplied_secret_and_prompt() -> None:
    scanner = PublicArtifactScanner(
        forbidden_values=("runtime-secret-value",),
        forbidden_prompt_texts=("exact private prompt body",),
    )

    with pytest.raises(ForbiddenPublicArtifactError):
        scanner.validate("cost: runtime-secret-value")
    with pytest.raises(ForbiddenPublicArtifactError):
        scanner.validate("text: exact private prompt body")


def test_public_scanner_allows_deepseek_environment_name_without_value() -> None:
    PublicArtifactScanner().validate(
        "Configure DEEPSEEK_API_KEY with hidden terminal input."
    )
