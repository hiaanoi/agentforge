import json
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from agentforge.evaluation.costs import PricingSnapshot
from agentforge.evaluation.pilot_factory import PilotRuntimeFactory
from agentforge.evaluation.protocol import EvaluationExecutionMode
from agentforge.evaluation.provider_factory import MockEvaluationProviderFactory
from agentforge.evaluation.source_provenance import SourceProvenance
from agentforge.evaluation.study_builder import (
    B2_4_REPLACEABLE_FAILURES,
    EvaluationStudyBuilder,
    StudyBuildError,
)
from agentforge.evaluation.study_models import B2_4_TASK_ORDER
from agentforge.persistence.database import Database

ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = ROOT / "evaluation" / "fixtures"


@pytest.fixture
def builder() -> EvaluationStudyBuilder:
    database = Database("sqlite:///:memory:")
    database.create_schema()
    runtime_factory = PilotRuntimeFactory(
        database,
        MockEvaluationProviderFactory([]),
        executable=Path(sys.executable),
        allowed_env={
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "PYTHONHASHSEED": "0",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
        },
    )
    return EvaluationStudyBuilder(runtime_factory, FIXTURE_ROOT)


def source_provenance(*, clean: bool = True) -> SourceProvenance:
    return SourceProvenance(
        git_commit_sha="a" * 40,
        git_worktree_clean=clean,
        runtime_source_digest="b" * 64,
        pyproject_sha256="c" * 64,
        uv_lock_sha256="d" * 64,
    )


def pricing(model_id: str = "gpt-test-exact") -> PricingSnapshot:
    return PricingSnapshot(
        model_id=model_id,
        effective_date=date(2026, 7, 29),
        source_url="https://example.test/pricing",
        input_per_million=Decimal("1.25"),
        cached_input_per_million=Decimal("0.25"),
        output_per_million=Decimal("10.00"),
    )


def test_builds_four_frozen_real_model_protocols(
    builder: EvaluationStudyBuilder,
) -> None:
    built = builder.build(
        model_id="gpt-test-exact",
        response_model_id="gpt-test-exact-2026-03-17",
        source_provenance=source_provenance(),
        pricing_snapshot=pricing(),
    )

    assert tuple(protocol.task_id for protocol in built.protocols) == (
        B2_4_TASK_ORDER
    )
    assert built.definition.task_ids == B2_4_TASK_ORDER
    assert built.definition.model_id == "gpt-test-exact"
    assert built.definition.response_model_id == "gpt-test-exact-2026-03-17"
    assert built.definition.protocol_digests == tuple(
        protocol.protocol_digest for protocol in built.protocols
    )
    assert len(set(built.definition.protocol_digests)) == 4
    assert all(
        protocol.execution_mode is EvaluationExecutionMode.REAL_MODEL
        and protocol.real_model_authorized
        and protocol.repetition_count == 3
        for protocol in built.protocols
    )


def test_applies_exact_provider_and_context_settings(
    builder: EvaluationStudyBuilder,
) -> None:
    built = builder.build(
        model_id="gpt-test-exact",
        response_model_id="gpt-test-exact-2026-03-17",
        source_provenance=source_provenance(),
        pricing_snapshot=pricing(),
    )

    for protocol in built.protocols:
        provider = protocol.provider_binding
        assert provider.provider == "openai"
        assert provider.model_id == "gpt-test-exact"
        assert provider.response_model_id == "gpt-test-exact-2026-03-17"
        assert provider.timeout_seconds == 90
        assert provider.max_retries == 1
        assert provider.store is False
        assert provider.max_output_tokens == 4_000
        assert provider.multi_tool_response_policy == "SEQUENTIAL_READ_ONLY"
        assert provider.max_function_calls_per_response == 8
        assert protocol.completion_correction_mode == "DEFAULT"
        assert protocol.context_policy.max_items == 100
        assert protocol.context_policy.max_characters == 20_000
        assert protocol.context_policy.max_utf8_bytes == 40_000
        assert protocol.replacement_policy.max_replacements_per_slot == 1
        assert (
            protocol.replacement_policy.replaceable_failure_categories
            == B2_4_REPLACEABLE_FAILURES
        )

    assert len(
        {
            protocol.provider_binding.configuration_digest
            for protocol in built.protocols
        }
    ) == 1


def test_derives_physical_budget_from_fixture_difficulty(
    builder: EvaluationStudyBuilder,
) -> None:
    built = builder.build(
        model_id="gpt-test-exact",
        source_provenance=source_provenance(),
        pricing_snapshot=pricing(),
    )
    budgets = {
        protocol.task_id: protocol.model_budget
        for protocol in built.protocols
    }

    for task_id in B2_4_TASK_ORDER[:2]:
        budget = budgets[task_id]
        assert budget.max_model_requests == 12
        assert budget.max_total_input_tokens == 50_000
        assert budget.max_total_output_tokens == 10_000
        assert budget.max_total_tokens == 60_000
        assert budget.max_output_tokens_per_request == 4_000
    for task_id in B2_4_TASK_ORDER[2:]:
        budget = budgets[task_id]
        assert budget.max_model_requests == 20
        assert budget.max_total_input_tokens == 85_000
        assert budget.max_total_output_tokens == 15_000
        assert budget.max_total_tokens == 100_000
        assert budget.max_output_tokens_per_request == 4_000


def test_binds_source_pricing_fixture_and_platform(
    builder: EvaluationStudyBuilder,
) -> None:
    source = source_provenance()
    snapshot = pricing()
    built = builder.build(
        model_id="gpt-test-exact",
        source_provenance=source,
        pricing_snapshot=snapshot,
    )
    definition = built.definition

    assert definition.runtime_source_digest == source.runtime_source_digest
    assert definition.git_commit_sha == source.git_commit_sha
    assert definition.pyproject_sha256 == source.pyproject_sha256
    assert definition.uv_lock_sha256 == source.uv_lock_sha256
    assert definition.pricing_snapshot_digest == snapshot.pricing_digest
    assert definition.fixture_registry_digest == (
        built.protocols[0].fixture_registry_digest
    )
    assert all(
        protocol.fixture_registry_digest
        == definition.fixture_registry_digest
        for protocol in built.protocols
    )
    assert len(definition.platform_binding_digest) == 64


def test_serialized_plan_contains_no_secret_or_hidden_assets(
    builder: EvaluationStudyBuilder,
) -> None:
    built = builder.build(
        model_id="gpt-test-exact",
        source_provenance=source_provenance(),
        pricing_snapshot=pricing(),
    )
    serialized = json.dumps(
        {
            "definition": built.definition.model_dump(mode="json"),
            "protocols": [
                protocol.model_dump(mode="json")
                for protocol in built.protocols
            ],
        },
        sort_keys=True,
    ).lower()

    assert "api_key" not in serialized
    assert "openai_api_key" not in serialized
    assert "tests/hidden" not in serialized
    assert "reference/fixed_files" not in serialized
    for protocol in built.protocols:
        assert "hidden-" not in protocol.task_prompt.lower()
        assert "reference/" not in protocol.task_prompt.lower()


def test_rejects_dirty_source_or_pricing_model_drift(
    builder: EvaluationStudyBuilder,
) -> None:
    with pytest.raises(StudyBuildError, match="clean"):
        builder.build(
            model_id="gpt-test-exact",
            source_provenance=source_provenance(clean=False),
            pricing_snapshot=pricing(),
        )

    with pytest.raises(StudyBuildError, match="pricing"):
        builder.build(
            model_id="gpt-test-exact",
            source_provenance=source_provenance(),
            pricing_snapshot=pricing("different-model"),
        )


def test_build_is_deterministic(builder: EvaluationStudyBuilder) -> None:
    first = builder.build(
        model_id="gpt-test-exact",
        source_provenance=source_provenance(),
        pricing_snapshot=pricing(),
    )
    second = builder.build(
        model_id="gpt-test-exact",
        source_provenance=source_provenance(),
        pricing_snapshot=pricing(),
    )

    assert first.definition == second.definition
    assert first.protocols == second.protocols
