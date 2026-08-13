from collections import Counter
from statistics import fmean, median

from agentforge.domain.repair import RepairCompletionStatus
from agentforge.evaluation.models import RepairEvaluationRun, TaskEvaluationSummary


def summarize_task(runs: list[RepairEvaluationRun]) -> TaskEvaluationSummary:
    if not runs:
        raise ValueError("Task metrics require at least one evaluation run")
    task_ids = {run.task_id for run in runs}
    if len(task_ids) != 1:
        raise ValueError("Task metrics cannot mix task IDs")
    repetitions = [run.repetition_index for run in runs]
    if len(set(repetitions)) != len(repetitions):
        raise ValueError("Task metrics require unique repetition slots")
    total = len(runs)
    successes = sum(run.verified_success for run in runs)
    failures = Counter(
        run.failure_category or run.final_status.value
        for run in runs
        if not run.verified_success
    )
    return TaskEvaluationSummary(
        task_id=runs[0].task_id,
        repetitions=total,
        verified_success_count=successes,
        run_level_success_rate=successes / total,
        majority_success=successes > total / 2,
        stable_success=successes == total,
        any_success=successes > 0,
        mean_model_calls=fmean(run.model_calls for run in runs),
        mean_edit_attempts=fmean(run.edit_attempts for run in runs),
        mean_test_runs=fmean(run.test_runs for run in runs),
        median_wall_time_ms=median(run.wall_time_ms for run in runs),
        completion_correction_rate=(
            sum(run.completion_corrections > 0 for run in runs) / total
        ),
        policy_block_rate=(
            sum(run.final_status is RepairCompletionStatus.POLICY_BLOCKED for run in runs)
            / total
        ),
        budget_exhaustion_rate=(
            sum(
                run.final_status is RepairCompletionStatus.BUDGET_EXHAUSTED
                for run in runs
            )
            / total
        ),
        infrastructure_failure_rate=(
            sum(run.infrastructure_failure for run in runs) / total
        ),
        failure_distribution=dict(sorted(failures.items())),
    )

