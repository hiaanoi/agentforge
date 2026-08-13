import json

from agentforge.evaluation.models import RepairEvaluationRun, TaskEvaluationSummary
from agentforge.evaluation.protocol import EvaluationProtocol
from agentforge.evaluation.selection import EvaluationSelection


def render_task_report(
    summary: TaskEvaluationSummary,
    runs: list[RepairEvaluationRun],
) -> str:
    if any(run.task_id != summary.task_id for run in runs):
        raise ValueError("Evaluation report cannot mix task IDs")
    payload = {
        "result_schema_version": 1,
        "summary": summary.model_dump(mode="json"),
        "runs": [
            run.model_dump(
                mode="json",
                exclude={
                    "model_parameters_digest",
                    "system_prompt_digest",
                    "task_prompt_digest",
                    "tool_schema_digest",
                },
            )
            for run in sorted(
                runs,
                key=lambda item: (
                    item.repetition_index,
                    str(item.evaluation_run_id),
                ),
            )
        ],
    }
    return json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )


def render_campaign_report(
    protocol: EvaluationProtocol,
    selection: EvaluationSelection,
) -> str:
    if selection.protocol_digest != protocol.protocol_digest:
        raise ValueError("Selection does not match evaluation protocol")
    payload = {
        "result_schema_version": 1,
        "protocol_digest": protocol.protocol_digest,
        "campaign_id": str(selection.campaign_id),
        "task_id": protocol.task_id,
        "fixture_registry_digest": protocol.fixture_registry_digest,
        "fixture_asset_digest": protocol.fixture_asset_digest,
        "task_policy_digest": protocol.task_policy_digest,
        "tool_schema_digest": protocol.tool_schema_digest,
        "infrastructure_invalid_count": selection.infrastructure_invalid_count,
        "replacement_count": selection.replacement_count,
        "superseded_run_ids": [
            str(run_id) for run_id in selection.superseded_run_ids
        ],
        "summary": selection.summary.model_dump(mode="json"),
        "selected_runs": [
            {
                "evaluation_run_id": str(run.evaluation_run_id),
                "slot_id": str(run.slot_id),
                "attempt_id": str(run.attempt_id),
                "attempt_number": run.attempt_number,
                "repetition_index": run.repetition_index,
                "final_status": run.final_status.value,
                "verified_success": run.verified_success,
                "model_calls": run.model_calls,
                "read_calls": run.read_calls,
                "edit_attempts": run.edit_attempts,
                "test_runs": run.test_runs,
                "completion_corrections": run.completion_corrections,
                "policy_violations": run.policy_violations,
                "wall_time_ms": run.wall_time_ms,
                "token_usage": run.token_usage,
                "final_workspace_digest": run.final_workspace_digest,
                "final_diff_digest": run.final_diff_digest,
                "failure_category": run.failure_category,
            }
            for run in selection.selected_runs
        ],
    }
    return json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
