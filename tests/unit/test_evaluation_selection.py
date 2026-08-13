import hashlib
import json
import sys
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from agentforge.domain.enums import (
    EvaluationAttemptStatus,
    EvaluationCampaignStatus,
    EvaluationSlotStatus,
)
from agentforge.domain.repair import (
    BudgetProfile,
    CompletionCorrectionMode,
    RepairCompletionStatus,
)
from agentforge.evaluation.campaign_models import (
    EvaluationCampaign,
    EvaluationPilotAttempt,
    EvaluationSlot,
)
from agentforge.evaluation.models import RepairEvaluationRun
from agentforge.evaluation.protocol import (
    ContextPolicyBinding,
    EvaluationProtocol,
    ModelBudgetBinding,
    PlatformBinding,
    ProviderBinding,
    ReplacementPolicy,
)
from agentforge.evaluation.reports import render_campaign_report
from agentforge.evaluation.selection import (
    EvaluationSelectionError,
    resolve_effective_runs,
)

SHA = "c" * 64


def protocol() -> EvaluationProtocol:
    executable = Path(sys.executable).resolve()
    return EvaluationProtocol(
        protocol_name="selection-protocol",
        execution_mode="OFFLINE_TEST",
        task_id="durable-task",
        fixture_registry_digest=SHA,
        fixture_asset_digest=SHA,
        expected_baseline_fingerprint_digest=SHA,
        task_policy_digest=SHA,
        test_profile_template_digest=SHA,
        provider_binding=ProviderBinding(
            provider="mock",
            model_id="deterministic-model",
            timeout_seconds=30,
            max_retries=1,
            store=False,
            max_output_tokens=2_000,
            multi_tool_response_policy="SEQUENTIAL_READ_ONLY",
            max_function_calls_per_response=8,
        ),
        model_budget=ModelBudgetBinding(
            max_model_requests=10,
            max_retries=1,
            max_total_tokens=50_000,
        ),
        system_prompt_version=1,
        system_prompt="Private system instructions.",
        task_prompt="Private repair task.",
        tool_schema_digest=SHA,
        context_policy=ContextPolicyBinding(
            version="1",
            system_prompt_version="1",
            system_instructions="Private system instructions.",
        ),
        completion_correction_mode="DEFAULT",
        repetition_count=3,
        replacement_policy=ReplacementPolicy(
            max_replacements_per_slot=1,
            replaceable_failure_categories=("MODEL_TIMEOUT",),
        ),
        platform_binding=PlatformBinding(
            os_family="WINDOWS" if sys.platform == "win32" else "POSIX",
            python_implementation=sys.implementation.name,
            python_version=".".join(str(item) for item in sys.version_info[:3]),
            executable_path=str(executable),
            executable_sha256=hashlib.sha256(executable.read_bytes()).hexdigest(),
        ),
        real_model_authorized=False,
    )


def evaluation_run(
    *,
    protocol_digest: str,
    campaign_id: UUID,
    slot_id: UUID,
    attempt_id: UUID,
    attempt_number: int,
    repetition_index: int,
    success: bool,
    infrastructure_failure: bool = False,
    failure_category: str | None = None,
    predecessor_id: UUID | None = None,
    task_id: str = "durable-task",
) -> RepairEvaluationRun:
    if success:
        status = RepairCompletionStatus.VERIFIED_SUCCESS
    elif infrastructure_failure:
        status = RepairCompletionStatus.RUNTIME_FAILURE
    elif failure_category is not None:
        status = RepairCompletionStatus(failure_category)
    else:
        status = RepairCompletionStatus.TESTS_FAILED
    return RepairEvaluationRun(
        protocol_digest=protocol_digest,
        campaign_id=campaign_id,
        slot_id=slot_id,
        attempt_id=attempt_id,
        attempt_number=attempt_number,
        task_id=task_id,
        repetition_index=repetition_index,
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
        verified_success=success,
        model_calls=attempt_number,
        read_calls=1,
        edit_attempts=1,
        test_runs=1,
        completion_corrections=0,
        policy_violations=0,
        wall_time_ms=100,
        final_workspace_digest=SHA if success else None,
        final_diff_digest=SHA if success else None,
        final_verification_execution_id=uuid4() if success else None,
        failure_category=failure_category,
        infrastructure_failure=infrastructure_failure,
        replacement_for_evaluation_run_id=predecessor_id,
    )


def complete_facts() -> tuple[
    EvaluationProtocol,
    EvaluationCampaign,
    list[EvaluationSlot],
    list[EvaluationPilotAttempt],
    list[RepairEvaluationRun],
]:
    bound_protocol = protocol()
    campaign = EvaluationCampaign(
        protocol_digest=bound_protocol.protocol_digest,
        task_id=bound_protocol.task_id,
        repetition_count=3,
        status=EvaluationCampaignStatus.COMPLETED,
    )
    slots: list[EvaluationSlot] = []
    attempts: list[EvaluationPilotAttempt] = []
    runs: list[RepairEvaluationRun] = []
    for index in range(3):
        slot_id = uuid4()
        if index == 0:
            first_attempt_id = uuid4()
            first = evaluation_run(
                protocol_digest=bound_protocol.protocol_digest,
                campaign_id=campaign.campaign_id,
                slot_id=slot_id,
                attempt_id=first_attempt_id,
                attempt_number=1,
                repetition_index=index,
                success=False,
                infrastructure_failure=True,
                failure_category="MODEL_TIMEOUT",
            )
            replacement_attempt_id = uuid4()
            replacement = evaluation_run(
                protocol_digest=bound_protocol.protocol_digest,
                campaign_id=campaign.campaign_id,
                slot_id=slot_id,
                attempt_id=replacement_attempt_id,
                attempt_number=2,
                repetition_index=index,
                success=True,
                predecessor_id=first.evaluation_run_id,
            )
            attempts.extend(
                [
                    EvaluationPilotAttempt(
                        attempt_id=first_attempt_id,
                        campaign_id=campaign.campaign_id,
                        slot_id=slot_id,
                        protocol_digest=bound_protocol.protocol_digest,
                        task_id=bound_protocol.task_id,
                        repetition_index=index,
                        attempt_number=1,
                        status=EvaluationAttemptStatus.INVALID,
                        evaluation_run_id=first.evaluation_run_id,
                        failure_category="MODEL_TIMEOUT",
                        infrastructure_failure=True,
                    ),
                    EvaluationPilotAttempt(
                        attempt_id=replacement_attempt_id,
                        campaign_id=campaign.campaign_id,
                        slot_id=slot_id,
                        protocol_digest=bound_protocol.protocol_digest,
                        task_id=bound_protocol.task_id,
                        repetition_index=index,
                        attempt_number=2,
                        status=EvaluationAttemptStatus.COMPLETED,
                        predecessor_attempt_id=first_attempt_id,
                        predecessor_evaluation_run_id=first.evaluation_run_id,
                        evaluation_run_id=replacement.evaluation_run_id,
                    ),
                ]
            )
            runs.extend([first, replacement])
            selected_attempt_id = replacement_attempt_id
            selected_run_id = replacement.evaluation_run_id
        else:
            attempt_id = uuid4()
            selected = evaluation_run(
                protocol_digest=bound_protocol.protocol_digest,
                campaign_id=campaign.campaign_id,
                slot_id=slot_id,
                attempt_id=attempt_id,
                attempt_number=1,
                repetition_index=index,
                success=index == 1,
                failure_category=(
                    None if index == 1 else RepairCompletionStatus.BUDGET_EXHAUSTED.value
                ),
            )
            if index == 2:
                selected = selected.model_copy(
                    update={
                        "final_status": RepairCompletionStatus.BUDGET_EXHAUSTED,
                    }
                )
            attempts.append(
                EvaluationPilotAttempt(
                    attempt_id=attempt_id,
                    campaign_id=campaign.campaign_id,
                    slot_id=slot_id,
                    protocol_digest=bound_protocol.protocol_digest,
                    task_id=bound_protocol.task_id,
                    repetition_index=index,
                    attempt_number=1,
                    status=EvaluationAttemptStatus.COMPLETED,
                    evaluation_run_id=selected.evaluation_run_id,
                )
            )
            runs.append(selected)
            selected_attempt_id = attempt_id
            selected_run_id = selected.evaluation_run_id
        slots.append(
            EvaluationSlot(
                slot_id=slot_id,
                campaign_id=campaign.campaign_id,
                protocol_digest=bound_protocol.protocol_digest,
                task_id=bound_protocol.task_id,
                repetition_index=index,
                status=EvaluationSlotStatus.ACCEPTED,
                selected_attempt_id=selected_attempt_id,
                selected_evaluation_run_id=selected_run_id,
            )
        )
    return bound_protocol, campaign, slots, attempts, runs


def resolve(
    facts: tuple[
        EvaluationProtocol,
        EvaluationCampaign,
        list[EvaluationSlot],
        list[EvaluationPilotAttempt],
        list[RepairEvaluationRun],
    ],
):
    return resolve_effective_runs(*facts)


def test_resolves_selected_runs_and_excludes_replaced_infrastructure_failure() -> None:
    facts = complete_facts()

    selection = resolve(facts)

    assert [run.repetition_index for run in selection.selected_runs] == [0, 1, 2]
    assert selection.superseded_run_ids == (facts[4][0].evaluation_run_id,)
    assert selection.infrastructure_invalid_count == 1
    assert selection.replacement_count == 1
    assert selection.summary.repetitions == 3
    assert selection.summary.verified_success_count == 2
    assert selection.summary.infrastructure_failure_rate == 0


def test_rejects_replacement_of_normal_model_failure() -> None:
    protocol_value, campaign, slots, attempts, runs = complete_facts()
    runs[0] = runs[0].model_copy(
        update={"infrastructure_failure": False, "failure_category": "MODEL_FAILURE"}
    )

    with pytest.raises(EvaluationSelectionError, match="infrastructure"):
        resolve_effective_runs(protocol_value, campaign, slots, attempts, runs)


def test_rejects_result_replacement_binding_drift_from_attempt() -> None:
    protocol_value, campaign, slots, attempts, runs = complete_facts()
    runs[1] = runs[1].model_copy(
        update={"replacement_for_evaluation_run_id": None}
    )

    with pytest.raises(EvaluationSelectionError, match="replacement binding"):
        resolve_effective_runs(protocol_value, campaign, slots, attempts, runs)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("missing", "missing predecessor"),
        ("cross_task", "task"),
        ("cross_protocol", "protocol"),
        ("cross_repetition", "repetition"),
        ("attempt_regression", "attempt number"),
        ("cycle", "cycle"),
        ("branch", "branch"),
    ],
)
def test_rejects_malformed_replacement_chains(
    mutation: str,
    message: str,
) -> None:
    protocol_value, campaign, slots, attempts, runs = complete_facts()
    first, replacement = runs[0], runs[1]
    if mutation == "missing":
        runs[1] = replacement.model_copy(
            update={"replacement_for_evaluation_run_id": uuid4()}
        )
    elif mutation == "cross_task":
        runs[1] = replacement.model_copy(update={"task_id": "other-task"})
    elif mutation == "cross_protocol":
        runs[1] = replacement.model_copy(update={"protocol_digest": "d" * 64})
    elif mutation == "cross_repetition":
        runs[1] = replacement.model_copy(update={"repetition_index": 1})
    elif mutation == "attempt_regression":
        runs[1] = replacement.model_copy(update={"attempt_number": 1})
    elif mutation == "cycle":
        runs[0] = first.model_copy(
            update={"replacement_for_evaluation_run_id": replacement.evaluation_run_id}
        )
    elif mutation == "branch":
        branch = replacement.model_copy(
            update={
                "evaluation_run_id": uuid4(),
                "attempt_id": uuid4(),
                "run_id": uuid4(),
            }
        )
        runs.append(branch)

    with pytest.raises(EvaluationSelectionError, match=message):
        resolve_effective_runs(protocol_value, campaign, slots, attempts, runs)


def test_rejects_duplicate_selection_and_incomplete_slot_set() -> None:
    protocol_value, campaign, slots, attempts, runs = complete_facts()
    slots[1] = slots[1].model_copy(
        update={"selected_evaluation_run_id": slots[0].selected_evaluation_run_id}
    )
    with pytest.raises(EvaluationSelectionError, match="selected"):
        resolve_effective_runs(protocol_value, campaign, slots, attempts, runs)

    protocol_value, campaign, slots, attempts, runs = complete_facts()
    with pytest.raises(EvaluationSelectionError, match="complete"):
        resolve_effective_runs(protocol_value, campaign, slots[:-1], attempts, runs)


def test_campaign_report_contains_safe_counts_and_digests_only() -> None:
    facts = complete_facts()
    selection = resolve(facts)

    rendered = render_campaign_report(facts[0], selection)
    payload = json.loads(rendered)

    assert payload["protocol_digest"] == facts[0].protocol_digest
    assert payload["fixture_asset_digest"] == SHA
    assert payload["infrastructure_invalid_count"] == 1
    assert payload["replacement_count"] == 1
    assert len(payload["selected_runs"]) == 3
    for forbidden in (
        "private repair task",
        "private system instructions",
        "stdout",
        "stderr",
        "environment",
        str(Path(sys.executable).parent).casefold(),
    ):
        assert forbidden not in rendered.casefold()
