import sys
from pathlib import Path

import pytest

from agentforge.evaluation.formal_fixtures import (
    FormalFixtureLoader,
    compute_fixture_registry_digest,
)
from agentforge.evaluation.prompts import (
    INVARIANT_GUIDANCE_PROMPT_VARIANT,
    TASK_CONTRACT_GUIDANCE_PROMPT_VARIANT,
    TASK_DIAGNOSTIC_PROMPT_VARIANT,
    build_formal_prompt_bundle,
)

ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = ROOT / "evaluation" / "fixtures"
TASK_ROOT = FIXTURE_ROOT / "tasks"
TASK_IDS = (
    "bugsinpy-black-21",
    "quixbugs-shortest-path-length",
    "self-durable-double-consumption",
    "swebench-pytest-10051",
)


@pytest.mark.parametrize("task_id", TASK_IDS)
def test_formal_fixture_binds_safe_prompt_and_complete_asset_digest(
    task_id: str,
) -> None:
    manifest = FormalFixtureLoader().load(TASK_ROOT / task_id)

    assert manifest.repair_prompt.title
    assert manifest.repair_prompt.description
    assert manifest.repair_prompt.success_conditions
    assert len(manifest.asset_digest) == 64
    assert manifest.asset_digest == FormalFixtureLoader().load(
        TASK_ROOT / task_id
    ).asset_digest
    serialized = manifest.repair_prompt.model_dump_json().casefold()
    assert "reference/fixed_files" not in serialized
    assert "tests/hidden" not in serialized


def test_primary_demo_prompt_exposes_symptom_without_solution_or_private_matrix() -> None:
    manifest = FormalFixtureLoader().load(
        TASK_ROOT / "self-durable-double-consumption"
    )
    prompt = manifest.repair_prompt.model_dump_json().casefold()

    assert "duplicate dispatch" in prompt
    assert "restart" in prompt
    for forbidden in (
        "idempotent",
        "exactly once",
        "double consumption",
        "compare-and-set",
        "crash window",
        "concurrent",
        "receipts table",
    ):
        assert forbidden not in prompt


def test_formal_prompt_policy_and_profile_template_are_deterministic() -> None:
    manifest = FormalFixtureLoader().load(
        TASK_ROOT / "self-durable-double-consumption"
    )
    policy = manifest.to_policy(path_case_sensitive=False)
    bundle = build_formal_prompt_bundle(
        manifest,
        policy,
        tool_schema={"tools": ["read_file", "edit_file", "run_tests"]},
    )
    env = {
        "PYTHONNOUSERSITE": "1",
        "PYTHONUTF8": "1",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
    }

    assert policy.policy_digest == manifest.to_policy(
        path_case_sensitive=False
    ).policy_digest
    assert bundle == build_formal_prompt_bundle(
        manifest,
        policy,
        tool_schema={"tools": ["read_file", "edit_file", "run_tests"]},
    )
    assert manifest.repair_prompt.description in bundle.task_prompt
    assert "ENGINEERING" not in bundle.task_prompt
    first = manifest.profile_template_digest(
        executable=sys.executable,
        allowed_env=env,
    )
    second = manifest.profile_template_digest(
        executable=sys.executable,
        allowed_env=dict(reversed(tuple(env.items()))),
    )
    assert first == second
    assert first != manifest.profile_template_digest(
        executable=sys.executable,
        allowed_env={**env, "PYTHONHASHSEED": "0"},
    )


def test_fixture_registry_digest_binds_all_admitted_assets() -> None:
    first = compute_fixture_registry_digest(FIXTURE_ROOT)
    second = compute_fixture_registry_digest(FIXTURE_ROOT)

    assert first == second
    assert len(first) == 64


def test_invariant_guidance_is_an_explicit_non_baseline_prompt_variant() -> None:
    manifest = FormalFixtureLoader().load(
        TASK_ROOT / "self-durable-double-consumption"
    )
    policy = manifest.to_policy(path_case_sensitive=False)
    baseline = build_formal_prompt_bundle(
        manifest,
        policy,
        tool_schema={"tools": []},
    )
    guided = build_formal_prompt_bundle(
        manifest,
        policy,
        tool_schema={"tools": []},
        prompt_variant=INVARIANT_GUIDANCE_PROMPT_VARIANT,
    )

    assert baseline.system_prompt_version == 1
    assert guided.system_prompt_version == 2
    assert baseline.system_prompt != guided.system_prompt
    assert "object" in guided.system_prompt
    assert "identity" in guided.system_prompt
    assert "tests/hidden" not in guided.system_prompt.casefold()


@pytest.mark.parametrize(
    ("task_id", "required_terms"),
    [
        (
            "swebench-pytest-10051",
            ("existing collection", "in place", "active phase"),
        ),
        (
            "self-durable-double-consumption",
            ("durable state transitions", "unique identity", "stale"),
        ),
    ],
)
def test_task_diagnostic_guidance_is_explicit_and_not_private_fixture_data(
    task_id: str, required_terms: tuple[str, ...]
) -> None:
    manifest = FormalFixtureLoader().load(TASK_ROOT / task_id)
    policy = manifest.to_policy(path_case_sensitive=False)
    diagnostic = build_formal_prompt_bundle(
        manifest,
        policy,
        tool_schema={"tools": []},
        prompt_variant=TASK_DIAGNOSTIC_PROMPT_VARIANT,
    )

    assert diagnostic.system_prompt_version == 3
    diagnostic_prompt = (
        diagnostic.system_prompt + "\n" + diagnostic.task_prompt
    ).casefold()
    assert all(term in diagnostic_prompt for term in required_terms)
    for forbidden in ("tests/hidden", "reference/fixed_files", "test_phase_lifecycle"):
        assert forbidden not in diagnostic_prompt


@pytest.mark.parametrize(
    ("task_id", "required_terms"),
    [
        (
            "swebench-pytest-10051",
            ("distinct records list", "current phase", "previous phases"),
        ),
        (
            "self-durable-double-consumption",
            ("no durable dispatch", "must not replace", "reuse"),
        ),
    ],
)
def test_task_contract_guidance_states_confirmed_invariants(
    task_id: str, required_terms: tuple[str, ...]
) -> None:
    manifest = FormalFixtureLoader().load(TASK_ROOT / task_id)
    policy = manifest.to_policy(path_case_sensitive=False)
    guided = build_formal_prompt_bundle(
        manifest,
        policy,
        tool_schema={"tools": []},
        prompt_variant=TASK_CONTRACT_GUIDANCE_PROMPT_VARIANT,
    )

    assert guided.system_prompt_version == 4
    prompt = (guided.system_prompt + "\n" + guided.task_prompt).casefold()
    assert all(term in prompt for term in required_terms)
    assert "tests/hidden" not in prompt
    assert "reference/fixed_files" not in prompt
