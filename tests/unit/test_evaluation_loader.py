import json
from pathlib import Path

import pytest

from agentforge.domain.repair import BudgetProfile, RepairDifficulty
from agentforge.evaluation.loader import EvaluationTaskLoader
from agentforge.evaluation.workspace import EvaluationFixtureWorkspace


def task_payload(fixture: Path) -> dict[str, object]:
    return {
        "schema_version": 1,
        "task_id": "loader-test",
        "title": "Loader test",
        "description": "Repair one synthetic source file.",
        "fixture_path": str(fixture.resolve()),
        "difficulty": RepairDifficulty.BASIC.value,
        "difficulty_rationale": "One edit.",
        "budget_profile": BudgetProfile.BASIC.value,
        "allowed_write_paths": ["src/**"],
        "forbidden_write_paths": [],
        "protected_paths": ["tests/**"],
        "allow_file_creation": False,
        "allowed_create_paths": [],
        "max_created_files": 0,
        "max_changed_files": 1,
        "max_total_changed_bytes": 1024,
        "max_single_file_changed_bytes": 1024,
        "development_test_profile_id": "unit",
        "final_verification_profile_id": "hidden",
        "success_conditions": ["Tests pass."],
        "provenance": {"kind": "synthetic"},
    }


def test_loader_and_fixture_workspace_are_isolated_and_repeatable(
    tmp_path: Path,
) -> None:
    fixture = tmp_path / "fixture"
    source = fixture / "src" / "module.py"
    source.parent.mkdir(parents=True)
    source.write_text("value = 1\n", encoding="utf-8")
    definition_path = tmp_path / "task.json"
    definition_path.write_text(
        json.dumps(task_payload(fixture)),
        encoding="utf-8",
    )

    task = EvaluationTaskLoader().load(definition_path)
    with EvaluationFixtureWorkspace.create(task) as first:
        copied = first.root / "src" / "module.py"
        copied.write_text("value = 2\n", encoding="utf-8")
        first_root = first.root
        assert source.read_text(encoding="utf-8") == "value = 1\n"
    assert not first_root.exists()

    with EvaluationFixtureWorkspace.create(task) as second:
        assert (second.root / "src" / "module.py").read_text(
            encoding="utf-8"
        ) == "value = 1\n"


def test_loader_rejects_unknown_fields_and_non_json_definitions(
    tmp_path: Path,
) -> None:
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    payload = task_payload(fixture)
    payload["unexpected"] = True
    invalid = tmp_path / "invalid.json"
    invalid.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="invalid"):
        EvaluationTaskLoader().load(invalid)
    with pytest.raises(ValueError, match="JSON"):
        EvaluationTaskLoader().load(tmp_path / "task.yaml")


def test_fixture_copy_rejects_symlinks_when_platform_supports_them(
    tmp_path: Path,
) -> None:
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    target = fixture / "real.txt"
    target.write_text("real", encoding="utf-8")
    link = fixture / "link.txt"
    try:
        link.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"Symlinks are unavailable on this platform: {exc}")
    definition = EvaluationTaskLoader().from_mapping(task_payload(fixture))

    with pytest.raises(ValueError, match=r"symlink|reparse"):
        EvaluationFixtureWorkspace.create(definition)
