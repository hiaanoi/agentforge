import json
from uuid import uuid4

import pytest

from agentforge.domain.repair import (
    BudgetProfile,
    CompletionCorrectionMode,
    RepairCompletionStatus,
)
from agentforge.evaluation.metrics import summarize_task
from agentforge.evaluation.models import RepairEvaluationRun
from agentforge.evaluation.reports import render_task_report

SHA = "b" * 64


def result(
    index: int,
    status: RepairCompletionStatus,
    *,
    infrastructure_failure: bool = False,
) -> RepairEvaluationRun:
    return RepairEvaluationRun(
        protocol_digest=SHA,
        campaign_id=uuid4(),
        slot_id=uuid4(),
        attempt_id=uuid4(),
        attempt_number=1,
        task_id="task",
        repetition_index=index,
        model_id="mock",
        model_parameters_digest=SHA,
        system_prompt_digest=SHA,
        task_prompt_digest=SHA,
        tool_schema_digest=SHA,
        context_policy_version=1,
        initial_workspace_digest=SHA,
        task_policy_digest=SHA,
        budget_profile=BudgetProfile.BASIC,
        completion_correction_mode=CompletionCorrectionMode.DEFAULT,
        run_id=uuid4(),
        final_status=status,
        verified_success=status is RepairCompletionStatus.VERIFIED_SUCCESS,
        model_calls=index + 1,
        read_calls=2,
        edit_attempts=index,
        test_runs=index + 1,
        completion_corrections=1 if index == 1 else 0,
        policy_violations=0,
        wall_time_ms=(100, 300, 200)[index],
        infrastructure_failure=infrastructure_failure,
        failure_category=(
            None
            if status is RepairCompletionStatus.VERIFIED_SUCCESS
            else status.value
        ),
        final_workspace_digest=(
            SHA if status is RepairCompletionStatus.VERIFIED_SUCCESS else None
        ),
        final_diff_digest=(
            SHA if status is RepairCompletionStatus.VERIFIED_SUCCESS else None
        ),
        final_verification_execution_id=(
            uuid4() if status is RepairCompletionStatus.VERIFIED_SUCCESS else None
        ),
    )


def test_task_metrics_separate_outcomes_and_infrastructure_failures() -> None:
    summary = summarize_task(
        [
            result(0, RepairCompletionStatus.VERIFIED_SUCCESS),
            result(1, RepairCompletionStatus.BUDGET_EXHAUSTED),
            result(
                2,
                RepairCompletionStatus.RUNTIME_FAILURE,
                infrastructure_failure=True,
            ),
        ]
    )

    assert summary.repetitions == 3
    assert summary.verified_success_count == 1
    assert summary.run_level_success_rate == pytest.approx(1 / 3)
    assert summary.majority_success is False
    assert summary.stable_success is False
    assert summary.any_success is True
    assert summary.mean_model_calls == 2
    assert summary.mean_edit_attempts == 1
    assert summary.mean_test_runs == 2
    assert summary.median_wall_time_ms == 200
    assert summary.completion_correction_rate == pytest.approx(1 / 3)
    assert summary.policy_block_rate == 0
    assert summary.budget_exhaustion_rate == pytest.approx(1 / 3)
    assert summary.infrastructure_failure_rate == pytest.approx(1 / 3)
    assert summary.failure_distribution == {
        "BUDGET_EXHAUSTED": 1,
        "RUNTIME_FAILURE": 1,
    }


def test_task_metrics_require_one_task_and_unique_repetition_slots() -> None:
    duplicate = result(0, RepairCompletionStatus.VERIFIED_SUCCESS)
    with pytest.raises(ValueError, match="repetition"):
        summarize_task([duplicate, duplicate.model_copy(update={"run_id": uuid4()})])
    with pytest.raises(ValueError, match="task"):
        summarize_task(
            [
                duplicate,
                duplicate.model_copy(
                    update={"task_id": "other", "repetition_index": 1, "run_id": uuid4()}
                ),
            ]
        )


def test_report_is_deterministic_and_contains_only_structured_evaluation_facts() -> None:
    runs = [
        result(0, RepairCompletionStatus.VERIFIED_SUCCESS),
        result(1, RepairCompletionStatus.BUDGET_EXHAUSTED),
    ]
    summary = summarize_task(runs)

    rendered = render_task_report(summary, runs)
    parsed = json.loads(rendered)

    assert rendered == render_task_report(summary, runs)
    assert parsed["summary"]["task_id"] == "task"
    assert len(parsed["runs"]) == 2
    assert "prompt" not in rendered.casefold()
    assert "stdout" not in rendered.casefold()
