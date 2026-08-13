import hashlib
import json
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from agentforge.domain.enums import (
    EvaluationAttemptStatus,
    EvaluationCampaignStatus,
    EvaluationSlotStatus,
)
from agentforge.evaluation.campaign_models import EvaluationCampaign
from agentforge.evaluation.campaign_persistence import (
    CampaignConflictError,
    EvaluationCampaignRepository,
)
from agentforge.evaluation.protocol import (
    ContextPolicyBinding,
    EvaluationProtocol,
    ModelBudgetBinding,
    PlatformBinding,
    ProviderBinding,
    ReplacementPolicy,
)
from agentforge.evaluation.protocol_persistence import EvaluationProtocolRepository
from agentforge.persistence.database import Database

SHA = "a" * 64


def protocol(*, name: str = "campaign-protocol") -> EvaluationProtocol:
    executable = Path(sys.executable).resolve()
    system_prompt = "Use only the constrained repair tools."
    return EvaluationProtocol(
        protocol_name=name,
        execution_mode="OFFLINE_TEST",
        task_id="self-durable-double-consumption",
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
        system_prompt=system_prompt,
        task_prompt="Repair the durable dispatch defect.",
        tool_schema_digest=SHA,
        context_policy=ContextPolicyBinding(
            version="1",
            system_prompt_version="1",
            system_instructions=system_prompt,
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


def setup_repository(
    tmp_path: Path,
) -> tuple[EvaluationCampaignRepository, EvaluationProtocol]:
    database = Database.from_path(tmp_path / "campaign.sqlite3")
    database.create_schema()
    value = protocol()
    EvaluationProtocolRepository(database).register(value)
    return EvaluationCampaignRepository(database), value


def test_create_campaign_is_atomic_fixed_and_idempotent(tmp_path: Path) -> None:
    repository, value = setup_repository(tmp_path)
    campaign_id = uuid4()

    created = repository.create_campaign(value, campaign_id=campaign_id)
    repeated = repository.create_campaign(value, campaign_id=campaign_id)
    slots = repository.list_slots(campaign_id)

    assert repeated == created
    assert created.status is EvaluationCampaignStatus.CREATED
    assert created.repetition_count == 3
    assert [slot.repetition_index for slot in slots] == [0, 1, 2]
    assert all(slot.status is EvaluationSlotStatus.PENDING for slot in slots)
    assert len(repository.list_events(campaign_id)) == 1

    with pytest.raises(CampaignConflictError, match="different facts"):
        repository.create_campaign(
            value.model_copy(update={"repetition_count": 2}),
            campaign_id=campaign_id,
        )


def test_campaign_and_slot_use_versioned_conditional_transitions(
    tmp_path: Path,
) -> None:
    repository, value = setup_repository(tmp_path)
    campaign = repository.create_campaign(value)
    started = repository.start_campaign(
        campaign.campaign_id,
        expected_version=campaign.record_version,
    )
    assert started.status is EvaluationCampaignStatus.RUNNING
    assert (
        repository.start_campaign(
            campaign.campaign_id,
            expected_version=campaign.record_version,
        )
        == started
    )

    slot = repository.list_slots(campaign.campaign_id)[0]
    winner = repository.claim_slot(
        slot.slot_id,
        expected_version=slot.record_version,
    )
    loser = repository.claim_slot(
        slot.slot_id,
        expected_version=slot.record_version,
    )

    assert winner is not None
    assert winner.status is EvaluationSlotStatus.CLAIMED
    assert loser is None


def test_attempt_lifecycle_is_monotonic_and_terminal_facts_are_immutable(
    tmp_path: Path,
) -> None:
    repository, value = setup_repository(tmp_path)
    campaign = repository.create_campaign(value)
    repository.start_campaign(
        campaign.campaign_id,
        expected_version=campaign.record_version,
    )
    pending = repository.list_slots(campaign.campaign_id)[0]
    claimed = repository.claim_slot(
        pending.slot_id,
        expected_version=pending.record_version,
    )
    assert claimed is not None

    attempt = repository.create_attempt(
        claimed.slot_id,
        expected_slot_version=claimed.record_version,
    )
    assert attempt.attempt_number == 1
    assert attempt.status is EvaluationAttemptStatus.CREATED

    with pytest.raises(CampaignConflictError, match="active attempt"):
        repository.create_attempt(
            claimed.slot_id,
            expected_slot_version=claimed.record_version,
        )

    workspace_ready = repository.mark_workspace_ready(
        attempt.attempt_id,
        expected_version=attempt.record_version,
        workspace_lease_id=uuid4(),
        workspace_root_digest="b" * 64,
        initial_workspace_digest="c" * 64,
        workspace_path=tmp_path / "sensitive-workspace",
    )
    runtime_ready = repository.mark_runtime_ready(
        attempt.attempt_id,
        expected_version=workspace_ready.record_version,
        run_id=uuid4(),
        baseline_execution_id=uuid4(),
    )
    running = repository.start_attempt(
        attempt.attempt_id,
        expected_version=runtime_ready.record_version,
    )
    running_slot = repository.get_slot(claimed.slot_id)
    assert running_slot.status is EvaluationSlotStatus.RUNNING
    completed = repository.finish_attempt(
        attempt.attempt_id,
        expected_version=running.record_version,
        status=EvaluationAttemptStatus.COMPLETED,
        evaluation_run_id=uuid4(),
        infrastructure_failure=False,
    )

    assert completed.status is EvaluationAttemptStatus.COMPLETED
    with pytest.raises(CampaignConflictError, match="terminal"):
        repository.finish_attempt(
            attempt.attempt_id,
            expected_version=completed.record_version,
            status=EvaluationAttemptStatus.INVALID,
            failure_category="MODEL_TIMEOUT",
            infrastructure_failure=True,
        )


def test_replacement_attempt_has_monotonic_number_and_predecessor(
    tmp_path: Path,
) -> None:
    repository, value = setup_repository(tmp_path)
    campaign = repository.create_campaign(value)
    repository.start_campaign(
        campaign.campaign_id,
        expected_version=campaign.record_version,
    )
    pending = repository.list_slots(campaign.campaign_id)[0]
    claimed = repository.claim_slot(
        pending.slot_id,
        expected_version=pending.record_version,
    )
    assert claimed is not None
    first = repository.create_attempt(
        claimed.slot_id,
        expected_slot_version=claimed.record_version,
    )
    workspace = repository.mark_workspace_ready(
        first.attempt_id,
        expected_version=first.record_version,
        workspace_lease_id=uuid4(),
        workspace_root_digest="b" * 64,
        initial_workspace_digest="c" * 64,
        workspace_path=tmp_path / "first",
    )
    ready = repository.mark_runtime_ready(
        first.attempt_id,
        expected_version=workspace.record_version,
        run_id=uuid4(),
        baseline_execution_id=uuid4(),
    )
    running = repository.start_attempt(
        first.attempt_id,
        expected_version=ready.record_version,
    )
    invalid = repository.finish_attempt(
        first.attempt_id,
        expected_version=running.record_version,
        status=EvaluationAttemptStatus.INVALID,
        evaluation_run_id=uuid4(),
        failure_category="MODEL_TIMEOUT",
        infrastructure_failure=True,
    )
    running_slot = repository.get_slot(claimed.slot_id)
    replacement_pending = repository.request_replacement(
        claimed.slot_id,
        expected_version=running_slot.record_version,
        predecessor_attempt_id=invalid.attempt_id,
    )
    second = repository.create_attempt(
        claimed.slot_id,
        expected_slot_version=replacement_pending.record_version,
        predecessor_attempt_id=invalid.attempt_id,
        predecessor_evaluation_run_id=invalid.evaluation_run_id,
    )

    assert second.attempt_number == 2
    assert second.predecessor_attempt_id == first.attempt_id
    assert second.predecessor_evaluation_run_id == invalid.evaluation_run_id


def test_accept_slot_and_finish_campaign_are_idempotent_terminal_transitions(
    tmp_path: Path,
) -> None:
    repository, value = setup_repository(tmp_path)
    campaign = repository.create_campaign(value)
    started = repository.start_campaign(
        campaign.campaign_id,
        expected_version=campaign.record_version,
    )
    accepted_slots = []
    for pending in repository.list_slots(campaign.campaign_id):
        claimed = repository.claim_slot(
            pending.slot_id,
            expected_version=pending.record_version,
        )
        assert claimed is not None
        attempt = repository.create_attempt(
            claimed.slot_id,
            expected_slot_version=claimed.record_version,
        )
        workspace = repository.mark_workspace_ready(
            attempt.attempt_id,
            expected_version=attempt.record_version,
            workspace_lease_id=uuid4(),
            workspace_root_digest="b" * 64,
            initial_workspace_digest="c" * 64,
            workspace_path=tmp_path / str(attempt.attempt_id),
        )
        ready = repository.mark_runtime_ready(
            attempt.attempt_id,
            expected_version=workspace.record_version,
            run_id=uuid4(),
            baseline_execution_id=uuid4(),
        )
        running = repository.start_attempt(
            attempt.attempt_id,
            expected_version=ready.record_version,
        )
        completed = repository.finish_attempt(
            attempt.attempt_id,
            expected_version=running.record_version,
            status=EvaluationAttemptStatus.COMPLETED,
            evaluation_run_id=uuid4(),
            infrastructure_failure=False,
        )
        running_slot = repository.get_slot(claimed.slot_id)
        accepted_slots.append(
            repository.accept_slot(
                claimed.slot_id,
                expected_version=running_slot.record_version,
                attempt_id=completed.attempt_id,
                evaluation_run_id=completed.evaluation_run_id,
            )
        )

    finished = repository.finish_campaign(
        campaign.campaign_id,
        expected_version=started.record_version,
        status=EvaluationCampaignStatus.COMPLETED,
    )
    repeated = repository.finish_campaign(
        campaign.campaign_id,
        expected_version=started.record_version,
        status=EvaluationCampaignStatus.COMPLETED,
    )

    assert all(slot.status is EvaluationSlotStatus.ACCEPTED for slot in accepted_slots)
    assert repeated == finished
    with pytest.raises(CampaignConflictError, match="terminal"):
        repository.finish_campaign(
            campaign.campaign_id,
            expected_version=finished.record_version,
            status=EvaluationCampaignStatus.BLOCKED,
        )


def test_pending_slot_cannot_skip_claim_and_become_terminal(
    tmp_path: Path,
) -> None:
    repository, value = setup_repository(tmp_path)
    campaign = repository.create_campaign(value)
    pending = repository.list_slots(campaign.campaign_id)[0]

    with pytest.raises(CampaignConflictError, match="transition"):
        repository.finish_slot(
            pending.slot_id,
            expected_version=pending.record_version,
            status=EvaluationSlotStatus.INVALID,
        )


def test_campaign_audit_is_monotonic_isolated_and_redacted(tmp_path: Path) -> None:
    repository, value = setup_repository(tmp_path)
    first = repository.create_campaign(value)
    second_value = protocol(name="second-campaign-protocol")
    EvaluationProtocolRepository(repository.database).register(second_value)
    second = repository.create_campaign(second_value)

    repository.start_campaign(first.campaign_id, expected_version=first.record_version)
    first_slot = repository.list_slots(first.campaign_id)[0]
    repository.claim_slot(
        first_slot.slot_id,
        expected_version=first_slot.record_version,
    )

    first_events = repository.list_events(first.campaign_id)
    second_events = repository.list_events(second.campaign_id)
    assert [event.sequence_number for event in first_events] == [1, 2, 3]
    assert [event.sequence_number for event in second_events] == [1]
    assert all(event.campaign_id == first.campaign_id for event in first_events)

    serialized = json.dumps(
        [event.model_dump(mode="json") for event in first_events],
        sort_keys=True,
    ).casefold()
    for forbidden in (
        "repair the durable dispatch defect",
        "constrained repair tools",
        "api_key",
        "environment",
        "stdout",
        "stderr",
        str(tmp_path).casefold(),
    ):
        assert forbidden not in serialized


def test_campaign_model_rejects_invalid_repetition_count() -> None:
    with pytest.raises(ValueError):
        EvaluationCampaign(
            campaign_id=uuid4(),
            protocol_digest=SHA,
            task_id="task",
            repetition_count=0,
        )
