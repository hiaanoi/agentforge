import hashlib
import sys
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from agentforge.domain.enums import (
    EvaluationCampaignStatus,
    EvaluationStudyStatus,
)
from agentforge.evaluation.campaign_persistence import (
    EvaluationCampaignRepository,
)
from agentforge.evaluation.protocol import (
    ContextPolicyBinding,
    EvaluationProtocol,
    ModelBudgetBinding,
    PlatformBinding,
    ProviderBinding,
    ReplacementPolicy,
    canonical_digest,
)
from agentforge.evaluation.protocol_persistence import (
    EvaluationProtocolRepository,
)
from agentforge.evaluation.study_models import (
    B2_4_TASK_ORDER,
    EvaluationStudyDefinition,
    RealModelAuthorization,
)
from agentforge.evaluation.study_persistence import (
    EvaluationStudyRepository,
    StudyConflictError,
)
from agentforge.persistence.database import Database

SHA = "e" * 64


def _protocol(task_id: str, index: int) -> EvaluationProtocol:
    executable = Path(sys.executable).resolve()
    system_prompt = "Use only constrained local tools."
    return EvaluationProtocol(
        protocol_name=f"study-{index}-{task_id}",
        execution_mode="OFFLINE_TEST",
        task_id=task_id,
        fixture_registry_digest=SHA,
        fixture_asset_digest=f"{index + 1:x}" * 64,
        expected_baseline_fingerprint_digest=SHA,
        task_policy_digest=SHA,
        test_profile_template_digest=SHA,
        provider_binding=ProviderBinding(
            provider="mock",
            model_id="deterministic-model",
            timeout_seconds=90,
            max_retries=1,
            store=False,
            max_output_tokens=4_000,
            multi_tool_response_policy="SEQUENTIAL_READ_ONLY",
            max_function_calls_per_response=8,
        ),
        model_budget=ModelBudgetBinding(
            max_model_requests=12,
            max_retries=1,
            max_output_tokens_per_request=4_000,
            max_total_input_tokens=50_000,
            max_total_output_tokens=10_000,
            max_total_tokens=60_000,
        ),
        system_prompt_version=1,
        system_prompt=system_prompt,
        task_prompt=f"Repair task {task_id}.",
        tool_schema_digest=SHA,
        context_policy=ContextPolicyBinding(
            max_items=100,
            max_characters=20_000,
            max_utf8_bytes=40_000,
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


def _setup(
    path: Path,
) -> tuple[
    Database,
    EvaluationStudyDefinition,
    tuple[UUID, UUID, UUID, UUID],
]:
    database = Database.from_path(path)
    database.create_schema()
    protocols = tuple(
        _protocol(task_id, index)
        for index, task_id in enumerate(B2_4_TASK_ORDER)
    )
    protocol_repository = EvaluationProtocolRepository(database)
    campaign_repository = EvaluationCampaignRepository(database)
    campaign_ids: list[UUID] = []
    for protocol in protocols:
        protocol_repository.register(protocol)
        campaign_ids.append(
            campaign_repository.create_campaign(protocol).campaign_id
        )
    definition = EvaluationStudyDefinition(
        study_name="persistence-study",
        task_ids=B2_4_TASK_ORDER,
        protocol_digests=tuple(
            protocol.protocol_digest for protocol in protocols
        ),
        provider_configuration_digest=protocols[
            0
        ].provider_binding.configuration_digest,
        model_id=protocols[0].provider_binding.model_id,
        runtime_source_digest=SHA,
        git_commit_sha="a" * 40,
        git_worktree_clean=True,
        pyproject_sha256=SHA,
        uv_lock_sha256=SHA,
        fixture_registry_digest=SHA,
        platform_binding_digest=canonical_digest(protocols[0].platform_binding),
        pricing_snapshot_digest=SHA,
    )
    return database, definition, tuple(campaign_ids)  # type: ignore[return-value]


def _authorization(
    definition: EvaluationStudyDefinition,
) -> RealModelAuthorization:
    return RealModelAuthorization(
        study_definition_digest=definition.definition_digest,
        protocol_digests=definition.protocol_digests,
        model_id=definition.model_id,
        network_access_acknowledged=True,
    )


def test_create_study_atomically_binds_four_campaigns_and_is_idempotent(
    tmp_path: Path,
) -> None:
    database, definition, campaign_ids = _setup(tmp_path / "study.sqlite3")
    repository = EvaluationStudyRepository(database)
    study_id = uuid4()

    created = repository.create_study(
        definition,
        campaign_ids,
        study_id=study_id,
    )
    repeated = repository.create_study(
        definition,
        campaign_ids,
        study_id=study_id,
    )

    assert repeated == created
    assert repository.get_definition(study_id) == definition
    bindings = repository.list_campaign_bindings(study_id)
    assert [binding.task_order for binding in bindings] == [0, 1, 2, 3]
    assert tuple(binding.campaign_id for binding in bindings) == campaign_ids
    assert len(repository.list_events(study_id)) == 1
    database.close()


def test_create_rejects_missing_duplicate_or_rebound_campaigns(
    tmp_path: Path,
) -> None:
    database, definition, campaign_ids = _setup(tmp_path / "study.sqlite3")
    repository = EvaluationStudyRepository(database)
    study_id = uuid4()
    repository.create_study(definition, campaign_ids, study_id=study_id)

    with pytest.raises(StudyConflictError, match="four"):
        repository.create_study(
            definition,
            campaign_ids[:3],  # type: ignore[arg-type]
            study_id=uuid4(),
        )
    duplicate = (
        campaign_ids[0],
        campaign_ids[1],
        campaign_ids[2],
        campaign_ids[0],
    )
    with pytest.raises(StudyConflictError, match="unique"):
        repository.create_study(definition, duplicate, study_id=uuid4())
    with pytest.raises(StudyConflictError, match="identity conflict"):
        repository.create_study(
            definition,
            tuple(reversed(campaign_ids)),  # type: ignore[arg-type]
            study_id=study_id,
        )
    database.close()


def test_authorize_start_and_finish_use_conditional_versions(
    tmp_path: Path,
) -> None:
    database, definition, campaign_ids = _setup(tmp_path / "study.sqlite3")
    repository = EvaluationStudyRepository(database)
    draft = repository.create_study(definition, campaign_ids)
    auth = _authorization(definition)

    with pytest.raises(StudyConflictError, match="version"):
        repository.authorize(
            draft.study_id,
            auth,
            expected_version=draft.record_version + 1,
        )
    authorized = repository.authorize(
        draft.study_id,
        auth,
        expected_version=draft.record_version,
    )
    assert authorized.status is EvaluationStudyStatus.AUTHORIZED
    assert repository.get_authorization(draft.study_id) == auth
    running = repository.start(
        draft.study_id,
        expected_version=authorized.record_version,
    )
    assert running.status is EvaluationStudyStatus.RUNNING
    with pytest.raises(StudyConflictError, match="claim"):
        repository.start(
            draft.study_id,
            expected_version=authorized.record_version,
        )
    completed = repository.finish(
        draft.study_id,
        expected_version=running.record_version,
        status=EvaluationStudyStatus.COMPLETED,
    )
    assert completed.completed_at is not None
    with pytest.raises(StudyConflictError, match="terminal"):
        repository.finish(
            draft.study_id,
            expected_version=completed.record_version,
            status=EvaluationStudyStatus.INDETERMINATE,
        )
    database.close()


def test_campaign_completion_is_ordered_versioned_and_idempotent(
    tmp_path: Path,
) -> None:
    database, definition, campaign_ids = _setup(tmp_path / "study.sqlite3")
    repository = EvaluationStudyRepository(database)
    draft = repository.create_study(definition, campaign_ids)
    authorized = repository.authorize(
        draft.study_id,
        _authorization(definition),
        expected_version=draft.record_version,
    )
    running = repository.start(
        draft.study_id,
        expected_version=authorized.record_version,
    )
    campaigns = EvaluationCampaignRepository(database)
    first_campaign = campaigns.get_campaign(campaign_ids[0])
    started_campaign = campaigns.start_campaign(
        first_campaign.campaign_id,
        expected_version=first_campaign.record_version,
    )
    campaigns.finish_campaign(
        first_campaign.campaign_id,
        expected_version=started_campaign.record_version,
        status=EvaluationCampaignStatus.BLOCKED,
    )

    recorded = repository.record_campaign_completion(
        running.study_id,
        campaign_ids[0],
        campaign_status=EvaluationCampaignStatus.BLOCKED,
        expected_version=running.record_version,
    )
    repeated = repository.record_campaign_completion(
        running.study_id,
        campaign_ids[0],
        campaign_status=EvaluationCampaignStatus.BLOCKED,
        expected_version=running.record_version,
    )

    assert repeated == recorded
    assert recorded.record_version == running.record_version + 1
    bindings = repository.list_campaign_bindings(running.study_id)
    assert bindings[0].campaign_status is EvaluationCampaignStatus.BLOCKED
    assert bindings[0].completed_at is not None
    with pytest.raises(StudyConflictError, match="order"):
        repository.record_campaign_completion(
            running.study_id,
            campaign_ids[2],
            campaign_status=EvaluationCampaignStatus.COMPLETED,
            expected_version=recorded.record_version,
        )
    database.close()


def test_study_and_events_survive_database_reopen_without_sensitive_payloads(
    tmp_path: Path,
) -> None:
    path = tmp_path / "study.sqlite3"
    database, definition, campaign_ids = _setup(path)
    repository = EvaluationStudyRepository(database)
    draft = repository.create_study(definition, campaign_ids)
    authorized = repository.authorize(
        draft.study_id,
        _authorization(definition),
        expected_version=draft.record_version,
    )
    running = repository.start(
        draft.study_id,
        expected_version=authorized.record_version,
    )
    terminal = repository.finish(
        draft.study_id,
        expected_version=running.record_version,
        status=EvaluationStudyStatus.ABORTED_CONFIGURATION,
    )
    serialized_events = " ".join(
        event.model_dump_json()
        for event in repository.list_events(draft.study_id)
    ).casefold()
    assert "api_key" not in serialized_events
    assert "prompt" not in serialized_events
    assert "source_url" not in serialized_events
    database.close()

    reopened = Database.from_path(path)
    reopened.create_schema()
    reopened_repository = EvaluationStudyRepository(reopened)
    assert reopened_repository.get_study(draft.study_id) == terminal
    assert reopened_repository.get_definition(draft.study_id) == definition
    assert len(reopened_repository.list_campaign_bindings(draft.study_id)) == 4
    reopened.close()
