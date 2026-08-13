import os
import sys
from pathlib import Path

import pytest
from sqlalchemy import select

from agentforge.application.contracts import ProfilePurpose
from agentforge.application.kernel_errors import IdempotencyConflictError
from agentforge.domain.enums import (
    EvaluationAttemptStatus,
    EvaluationSlotStatus,
    RunStatus,
)
from agentforge.evaluation.campaign_persistence import EvaluationCampaignRepository
from agentforge.evaluation.formal_fixtures import FormalFixtureLoader
from agentforge.evaluation.pilot_factory import (
    PilotRuntimeFactory,
    _bootstrap_formal_pilot_profile_trust,
)
from agentforge.evaluation.pilot_workspace import (
    PilotWorkspaceBinding,
    PilotWorkspaceManager,
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
from agentforge.evaluation.provider_factory import MockEvaluationProviderFactory
from agentforge.persistence.database import Database
from agentforge.persistence.product_tables import (
    ApplicationCommandReceiptRow,
    TrustedProfileRow,
    WorkspaceSourceBindingRow,
)
from agentforge.persistence.source_revisions import WorkspaceDigester
from agentforge.persistence.tables import EventRow
from agentforge.tools.paths import WorkspacePathResolver
from agentforge.tools.testing.profiles import TestProfileRegistry as ProfileRegistry

ROOT = Path(__file__).resolve().parents[2]
TASK_ROOT = (
    ROOT
    / "evaluation"
    / "fixtures"
    / "tasks"
    / "self-durable-double-consumption"
)
FIXTURE_ROOT = ROOT / "evaluation" / "fixtures"
ALLOWED_ENV = {
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTEST_ADDOPTS": "-p no:cacheprovider",
    "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
    "PYTHONHASHSEED": "0",
    "PYTHONIOENCODING": "utf-8",
    **{
        name: os.environ[name]
        for name in ("SYSTEMROOT", "WINDIR")
        if name in os.environ
    },
}


def frozen_protocol(
    factory: PilotRuntimeFactory,
    *,
    task_root: Path = TASK_ROOT,
) -> EvaluationProtocol:
    manifest = FormalFixtureLoader().load(task_root)
    facts = factory.inspect_manifest(manifest)
    return EvaluationProtocol(
        protocol_name="offline-formal-pilot",
        execution_mode="OFFLINE_TEST",
        task_id=manifest.task_id,
        fixture_registry_digest=facts.fixture_registry_digest,
        fixture_asset_digest=facts.fixture_asset_digest,
        expected_baseline_fingerprint_digest=(
            facts.expected_baseline_fingerprint_digest
        ),
        task_policy_digest=facts.task_policy_digest,
        test_profile_template_digest=facts.test_profile_template_digest,
        provider_binding=ProviderBinding(
            provider="mock",
            model_id="deterministic-repair-model",
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
        system_prompt_version=facts.system_prompt_version,
        system_prompt=facts.system_prompt,
        task_prompt=facts.task_prompt,
        tool_schema_digest=facts.tool_schema_digest,
        context_policy=ContextPolicyBinding(
            version="1",
            system_prompt_version=str(facts.system_prompt_version),
            system_instructions=facts.system_prompt,
        ),
        completion_correction_mode="DEFAULT",
        repetition_count=3,
        replacement_policy=ReplacementPolicy(
            max_replacements_per_slot=1,
            replaceable_failure_categories=(
                "MODEL_TIMEOUT",
                "PILOT_PREPARATION_ERROR",
                "WORKSPACE_PREPARATION_ERROR",
            ),
        ),
        platform_binding=PlatformBinding(
            os_family=facts.os_family,
            python_implementation=facts.python_implementation,
            python_version=facts.python_version,
            executable_path=facts.executable_path,
            executable_sha256=facts.executable_sha256,
        ),
        real_model_authorized=False,
    )


def prepare_attempt(
    tmp_path: Path,
    database: Database,
    protocol: EvaluationProtocol,
):
    manifest = FormalFixtureLoader().load(TASK_ROOT)
    EvaluationProtocolRepository(database).register(protocol)
    campaigns = EvaluationCampaignRepository(database)
    campaign = campaigns.create_campaign(protocol)
    started = campaigns.start_campaign(
        campaign.campaign_id,
        expected_version=campaign.record_version,
    )
    pending = campaigns.list_slots(campaign.campaign_id)[0]
    claimed = campaigns.claim_slot(
        pending.slot_id,
        expected_version=pending.record_version,
    )
    assert claimed is not None
    attempt = campaigns.create_attempt(
        claimed.slot_id,
        expected_slot_version=claimed.record_version,
    )
    manager = PilotWorkspaceManager(tmp_path / "pilot-workspaces")
    lease = manager.create(
        manifest,
        PilotWorkspaceBinding(
            campaign_id=campaign.campaign_id,
            slot_id=claimed.slot_id,
            attempt_id=attempt.attempt_id,
            protocol_digest=protocol.protocol_digest,
            fixture_asset_digest=protocol.fixture_asset_digest,
        ),
    )
    workspace_ready = campaigns.mark_workspace_ready(
        attempt.attempt_id,
        expected_version=attempt.record_version,
        workspace_lease_id=lease.lease_id,
        workspace_root_digest=lease.workspace_root_digest,
        initial_workspace_digest=lease.initial_workspace_digest,
        workspace_path=lease.model_workspace,
    )
    return manifest, started, claimed, workspace_ready, lease


def test_factory_assembles_complete_protocol_bound_runtime(tmp_path: Path) -> None:
    database = Database.from_path(tmp_path / "pilot.sqlite3")
    database.create_schema()
    factory = PilotRuntimeFactory(
        database,
        MockEvaluationProviderFactory([]),
        executable=Path(sys.executable),
        allowed_env=ALLOWED_ENV,
    )
    protocol = frozen_protocol(factory)
    manifest, _, _, attempt, lease = prepare_attempt(
        tmp_path,
        database,
        protocol,
    )

    execution = factory.prepare(protocol, manifest, attempt, lease)

    assert execution.run.status is RunStatus.CREATED
    assert execution.attempt_id == attempt.attempt_id
    assert execution.lease_id == lease.lease_id
    assert execution.protocol_digest == protocol.protocol_digest
    assert execution.tool_names == (
        "edit_file",
        "git_log",
        "git_status",
        "list_files",
        "read_file",
        "run_tests",
        "search_text",
    )
    assert execution.profile_ids == (
        f"hidden-{manifest.task_id}",
        f"visible-{manifest.task_id}",
    )
    assert execution.context_policy.system_instructions == protocol.system_prompt
    assert execution.visible_test_plan.profile_id == f"visible-{manifest.task_id}"
    assert execution.hidden_test_plan.profile_id == f"hidden-{manifest.task_id}"
    assert execution.baseline_execution.run_id == execution.run.run_id
    assert (
        execution.baseline_execution.expected_failure.fingerprint_digest
        == protocol.expected_baseline_fingerprint_digest
    )
    assert execution.workspace_baseline.root_digest == lease.initial_workspace_digest
    assert execution.metadata.protocol_digest == protocol.protocol_digest
    assert execution.metadata.campaign_id == attempt.campaign_id
    assert execution.metadata.slot_id == attempt.slot_id
    assert execution.metadata.attempt_id == attempt.attempt_id
    assert execution.metadata.attempt_number == attempt.attempt_number
    assert execution.repair_state.policy_digest == protocol.task_policy_digest
    assert execution.provider.configuration_digest == (
        protocol.provider_binding.configuration_digest
    )

    with database.session() as session:
        trusted_profiles = session.scalars(select(TrustedProfileRow)).all()
        trust_receipts = session.scalars(
            select(ApplicationCommandReceiptRow).where(
                ApplicationCommandReceiptRow.command_type
                == "TRUST_PROFILE"
            )
        ).all()
        trust_events = session.scalars(
            select(EventRow).where(EventRow.event_type == "PROFILE_TRUSTED")
        ).all()
    assert len(trusted_profiles) == len(trust_receipts) == len(trust_events) == 2
    trusted_profile = next(
        profile
        for profile in trusted_profiles
        if profile.profile_id == f"hidden-{manifest.task_id}"
    )
    assert trusted_profile.profile_id == f"hidden-{manifest.task_id}"
    assert trusted_profile.purpose == "verification"
    assert {profile.purpose for profile in trusted_profiles} == {
        "development",
        "verification",
    }
    assert all(receipt.result_scope_type == "WORKSPACE" for receipt in trust_receipts)
    assert all(
        receipt.result_scope_id == trusted_profile.workspace_identity
        for receipt in trust_receipts
    )
    assert all(event.scope_type == "WORKSPACE" for event in trust_events)
    assert all(
        event.scope_id == trusted_profile.workspace_identity for event in trust_events
    )
    persisted_trust = " ".join(
        (
            str([profile.__dict__ for profile in trusted_profiles]),
            str([receipt.__dict__ for receipt in trust_receipts]),
            str([event.payload for event in trust_events]),
        )
    )
    assert "PYTHONPATH" not in persisted_trust
    assert str(lease.hidden_test_root) not in persisted_trust

    events = execution.events.list_for_run(execution.run.run_id)
    assert events[0].payload == {
        "task_digest": protocol.task_prompt_digest,
    }
    assert protocol.task_prompt not in " ".join(
        event.model_dump_json() for event in events
    )

    persisted_attempt = EvaluationCampaignRepository(database).mark_runtime_ready(
        attempt.attempt_id,
        expected_version=attempt.record_version,
        run_id=execution.run.run_id,
        baseline_execution_id=execution.baseline_execution.baseline_execution_id,
    )
    assert persisted_attempt.status is EvaluationAttemptStatus.RUNTIME_READY
    assert (
        EvaluationCampaignRepository(database).get_slot(attempt.slot_id).status
        is EvaluationSlotStatus.CLAIMED
    )
    database.close()


def test_formal_pilot_verification_trust_replays_after_database_reopen(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "pilot.sqlite3"
    database = Database.from_path(database_path)
    database.create_schema()
    factory = PilotRuntimeFactory(
        database,
        MockEvaluationProviderFactory([]),
        executable=Path(sys.executable),
        allowed_env=ALLOWED_ENV,
    )
    protocol = frozen_protocol(factory)
    manifest, _, _, attempt, lease = prepare_attempt(
        tmp_path,
        database,
        protocol,
    )
    execution = factory.prepare(protocol, manifest, attempt, lease)
    database.close()

    reopened = Database.from_path(database_path)
    reopened.validate_product_schema()
    reopened_factory = PilotRuntimeFactory(
        reopened,
        MockEvaluationProviderFactory([]),
        executable=Path(sys.executable),
        allowed_env=ALLOWED_ENV,
    )
    profiles = ProfileRegistry(WorkspacePathResolver(lease.model_workspace))
    _, hidden_definition = reopened_factory._profile_definitions(
        manifest,
        lease.hidden_test_root,
        lease.model_workspace,
    )
    profiles.register(hidden_definition)

    _bootstrap_formal_pilot_profile_trust(
        reopened,
        profiles,
        hidden_definition.profile_id,
        purpose=ProfilePurpose.VERIFICATION,
    )

    mismatched_profiles = ProfileRegistry(
        WorkspacePathResolver(lease.model_workspace)
    )
    mismatched_profiles.register(
        hidden_definition.model_copy(
            update={
                "allowed_env": {
                    **hidden_definition.allowed_env,
                    "SAFE_MODE": "changed",
                }
            }
        )
    )
    with pytest.raises(IdempotencyConflictError):
        _bootstrap_formal_pilot_profile_trust(
            reopened,
            mismatched_profiles,
            hidden_definition.profile_id,
            purpose=ProfilePurpose.VERIFICATION,
        )

    with reopened.session() as session:
        assert len(session.scalars(select(TrustedProfileRow)).all()) == 2
        source = session.get(WorkspaceSourceBindingRow, str(execution.run.run_id))
        assert source is not None
        assert source.workspace_root_identity == str(lease.model_workspace.resolve())
        assert source.initial_source_digest == WorkspaceDigester().digest(
            lease.model_workspace
        )
        assert source.expected_source_digest == source.initial_source_digest
        assert source.source_revision_number == 0
        assert (
            len(
                session.scalars(
                    select(ApplicationCommandReceiptRow).where(
                        ApplicationCommandReceiptRow.command_type
                        == "TRUST_PROFILE"
                    )
                ).all()
            )
            == 2
        )
        assert (
            len(
                session.scalars(
                    select(EventRow).where(
                        EventRow.event_type == "PROFILE_TRUSTED"
                    )
                ).all()
            )
            == 2
        )
    reopened.close()


def test_inspection_is_deterministic_and_matches_formal_registry() -> None:
    database = Database("sqlite:///:memory:")
    database.create_schema()
    factory = PilotRuntimeFactory(
        database,
        MockEvaluationProviderFactory([]),
        executable=Path(sys.executable),
        allowed_env=ALLOWED_ENV,
    )
    manifest = FormalFixtureLoader().load(TASK_ROOT)

    first = factory.inspect_manifest(manifest)
    second = factory.inspect_manifest(manifest)

    assert first == second
    assert first.fixture_asset_digest == manifest.asset_digest
    assert len(first.fixture_registry_digest) == 64
    assert first.test_profile_template_digest == manifest.profile_template_digest(
        executable=sys.executable,
        allowed_env=ALLOWED_ENV,
    )
    assert first.expected_baseline_fingerprint_digest == (
        manifest.expected_baseline_failure.fingerprint_digest
    )
    assert first.task_policy_digest == manifest.to_policy(
        path_case_sensitive=first.path_case_sensitive
    ).policy_digest
    assert FIXTURE_ROOT.is_dir()
    database.close()
