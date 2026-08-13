from pathlib import Path

import pytest
from pydantic import ValidationError

from agentforge.domain.repair import (
    BudgetProfile,
    RepairDifficulty,
    RepairTaskPolicy,
    fixed_budget,
)


def policy_for(
    profile: BudgetProfile = BudgetProfile.BASIC,
    **overrides: object,
) -> RepairTaskPolicy:
    values: dict[str, object] = {
        "task_id": "repair-001",
        "policy_version": 1,
        "difficulty": RepairDifficulty.BASIC,
        "budget_profile": profile,
        "allowed_write_paths": ("src/**",),
        "forbidden_write_paths": ("src/generated/**",),
        "protected_paths": ("tests/**", "pyproject.toml"),
        "allowed_development_test_profiles": ("unit",),
        "final_verification_profile_id": "hidden",
        "allow_file_creation": True,
        "allowed_create_paths": ("src/**",),
        "max_created_files": 2,
        "max_changed_files": 3,
        "max_total_changed_bytes": 4096,
        "max_single_file_changed_bytes": 2048,
        "path_case_sensitive": False,
    }
    values.update(overrides)
    return RepairTaskPolicy.model_validate(values)


@pytest.mark.parametrize(
    ("profile", "expected"),
    [
        (BudgetProfile.BASIC, (6, 20, 2, 3, 1, 2, 300)),
        (BudgetProfile.ENGINEERING, (10, 35, 4, 5, 1, 2, 600)),
        (BudgetProfile.CHALLENGE, (14, 50, 6, 7, 1, 2, 900)),
    ],
)
def test_fixed_budget_profiles_cannot_drift(
    profile: BudgetProfile,
    expected: tuple[int, int, int, int, int, int, int],
) -> None:
    budget = fixed_budget(profile)

    assert (
        budget.max_model_calls,
        budget.max_read_calls,
        budget.max_edit_attempts,
        budget.max_test_runs,
        budget.max_completion_corrections,
        budget.max_policy_violations,
        budget.max_wall_time_seconds,
    ) == expected


def test_policy_is_immutable_and_digest_is_order_independent() -> None:
    first = policy_for(
        allowed_write_paths=("src/b/**", "src/a/**"),
        protected_paths=("pyproject.toml", "tests/**"),
    )
    second = policy_for(
        allowed_write_paths=("src/a/**", "src/b/**"),
        protected_paths=("tests/**", "pyproject.toml"),
    )

    assert first.policy_digest == second.policy_digest
    assert first.allowed_write_paths == ("src/a/**", "src/b/**")
    with pytest.raises(ValidationError):
        first.max_model_calls = 99


def test_forbidden_and_protected_paths_override_allowed_paths() -> None:
    policy = policy_for(
        allowed_write_paths=("src/**", "tests/**"),
        forbidden_write_paths=("src/generated/**",),
        protected_paths=("tests/**",),
    )

    assert policy.allows_write("src/agentforge/runtime.py", creating=False)
    assert not policy.allows_write("src/generated/schema.py", creating=False)
    assert not policy.allows_write("tests/test_runtime.py", creating=False)
    assert not policy.allows_write("README.md", creating=False)


def test_creation_requires_both_creation_switch_and_allowed_create_path() -> None:
    enabled = policy_for(allowed_write_paths=("src/**",), allowed_create_paths=("src/new/**",))
    disabled = policy_for(allow_file_creation=False, allowed_create_paths=())

    assert enabled.allows_write("src/new/module.py", creating=True)
    assert not enabled.allows_write("src/existing/module.py", creating=True)
    assert not disabled.allows_write("src/module.py", creating=True)


@pytest.mark.parametrize(
    "invalid_path",
    ["../outside.py", "/absolute.py", "C:/absolute.py", "", "src/../tests/test_x.py"],
)
def test_policy_rejects_unsafe_path_patterns(invalid_path: str) -> None:
    with pytest.raises(ValidationError):
        policy_for(allowed_write_paths=(invalid_path,))


def test_policy_rejects_budget_override() -> None:
    with pytest.raises(ValidationError, match="fixed budget"):
        policy_for(max_model_calls=999)


def test_policy_matching_is_case_folded_when_configured() -> None:
    insensitive = policy_for(allowed_write_paths=("src/**",), path_case_sensitive=False)
    sensitive = policy_for(allowed_write_paths=("src/**",), path_case_sensitive=True)

    assert insensitive.allows_write("SRC/Module.py", creating=False)
    assert not sensitive.allows_write("SRC/Module.py", creating=False)


def test_policy_digest_does_not_depend_on_workspace_location(tmp_path: Path) -> None:
    first = policy_for()
    second = policy_for()

    assert str(tmp_path) not in first.model_dump_json()
    assert first.policy_digest == second.policy_digest
