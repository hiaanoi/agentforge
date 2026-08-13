from pathlib import Path

from agentforge.domain.repair import (
    BudgetProfile,
    DiffViolationKind,
    RepairDifficulty,
    RepairTaskPolicy,
)
from agentforge.evaluation.validators import WorkspaceDiffValidator
from agentforge.evaluation.workspace import WorkspaceBaselineBuilder
from agentforge.tools.paths import WorkspacePathResolver


def make_policy(**overrides: object) -> RepairTaskPolicy:
    values: dict[str, object] = {
        "task_id": "diff-task",
        "policy_version": 1,
        "difficulty": RepairDifficulty.BASIC,
        "budget_profile": BudgetProfile.BASIC,
        "allowed_write_paths": ("src/**",),
        "forbidden_write_paths": ("src/generated/**",),
        "protected_paths": ("tests/**", "pyproject.toml"),
        "allowed_development_test_profiles": ("unit",),
        "final_verification_profile_id": "hidden",
        "allow_file_creation": True,
        "allowed_create_paths": ("src/**",),
        "max_created_files": 2,
        "max_changed_files": 3,
        "max_total_changed_bytes": 1000,
        "max_single_file_changed_bytes": 500,
        "path_case_sensitive": False,
    }
    values.update(overrides)
    return RepairTaskPolicy.model_validate(values)


def baseline_workspace(tmp_path: Path) -> tuple[Path, WorkspacePathResolver, object]:
    workspace = tmp_path / "workspace"
    (workspace / "src" / "generated").mkdir(parents=True)
    (workspace / "tests").mkdir()
    (workspace / "src" / "module.py").write_text("value = 1\n", encoding="utf-8")
    (workspace / "tests" / "test_module.py").write_text(
        "def test_value(): pass\n", encoding="utf-8"
    )
    (workspace / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
    resolver = WorkspacePathResolver(workspace)
    baseline = WorkspaceBaselineBuilder(resolver).build(task_id="diff-task")
    return workspace, resolver, baseline


def violation_kinds(result: object) -> set[DiffViolationKind]:
    return {item.kind for item in result.violations}  # type: ignore[attr-defined]


def test_allowed_modification_produces_compliant_deterministic_diff(tmp_path: Path) -> None:
    workspace, resolver, baseline = baseline_workspace(tmp_path)
    (workspace / "src" / "module.py").write_text("value = 2\n", encoding="utf-8")
    validator = WorkspaceDiffValidator(resolver)

    first = validator.validate(baseline, make_policy())
    second = validator.validate(baseline, make_policy())

    assert first.compliant
    assert first.modified_files == ("src/module.py",)
    assert first.diff_digest == second.diff_digest
    assert first.changed_file_count == 1


def test_forbidden_and_protected_changes_override_allowed_rules(tmp_path: Path) -> None:
    workspace, resolver, baseline = baseline_workspace(tmp_path)
    (workspace / "src" / "generated" / "schema.py").write_text("bad = 1\n", encoding="utf-8")
    (workspace / "tests" / "test_module.py").write_text("def test_value(): pass\n# changed\n")

    result = WorkspaceDiffValidator(resolver).validate(
        baseline,
        make_policy(allowed_write_paths=("src/**", "tests/**")),
    )

    assert not result.compliant
    assert DiffViolationKind.FORBIDDEN_PATH_MODIFIED in violation_kinds(result)
    assert DiffViolationKind.PROTECTED_FILE_MODIFIED in violation_kinds(result)
    assert DiffViolationKind.TEST_INFRASTRUCTURE_MODIFIED in violation_kinds(result)


def test_creation_deletion_and_rename_like_changes_are_reported(tmp_path: Path) -> None:
    workspace, resolver, baseline = baseline_workspace(tmp_path)
    original = workspace / "src" / "module.py"
    renamed = workspace / "src" / "renamed.py"
    original.rename(renamed)

    result = WorkspaceDiffValidator(resolver).validate(baseline, make_policy())

    assert not result.compliant
    assert result.deleted_files == ("src/module.py",)
    assert result.created_files == ("src/renamed.py",)
    assert result.renamed_files == (("src/module.py", "src/renamed.py"),)
    assert DiffViolationKind.FILE_DELETED in violation_kinds(result)
    assert DiffViolationKind.FILE_RENAMED in violation_kinds(result)


def test_change_limits_and_sensitive_file_creation_fail_closed(tmp_path: Path) -> None:
    workspace, resolver, baseline = baseline_workspace(tmp_path)
    (workspace / "src" / "large.py").write_text("x" * 600, encoding="utf-8")
    (workspace / ".env").write_text("DO_NOT_LOG=secret", encoding="utf-8")

    result = WorkspaceDiffValidator(resolver).validate(baseline, make_policy())

    assert not result.compliant
    assert DiffViolationKind.SINGLE_FILE_CHANGE_TOO_LARGE in violation_kinds(result)
    assert DiffViolationKind.SENSITIVE_FILE_TOUCHED in violation_kinds(result)
    assert "DO_NOT_LOG" not in result.model_dump_json()
