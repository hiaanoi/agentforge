import sys
from pathlib import Path

from agentforge.application.contracts import ProfilePurpose, VerificationRuntimeMode
from agentforge.domain.enums import ConfigSourceKind
from agentforge.evaluation.formal_fixtures import (
    FormalFixtureLoader,
    FormalFixturePilotWorkspace,
)
from agentforge.tools.paths import WorkspacePathResolver
from agentforge.tools.testing.profiles import TestProfileRegistry as ManagedProfileRegistry

ROOT = Path(__file__).resolve().parents[2]
TASK_ROOT = ROOT / "evaluation" / "fixtures" / "tasks" / "self-durable-double-consumption"


def test_formal_fixture_loader_binds_expected_baseline_failure() -> None:
    manifest = FormalFixtureLoader().load(TASK_ROOT)

    assert manifest.task_id == "self-durable-double-consumption"
    assert manifest.expected_baseline_failure.failed_node_ids == (
        "tests/visible/test_restart_dispatch.py::test_restart_after_dispatch_does_not_store_a_second_dispatch",
    )


def test_each_pilot_uses_fresh_buggy_workspace_without_reference_or_hidden_tests() -> None:
    manifest = FormalFixtureLoader().load(TASK_ROOT)

    with FormalFixturePilotWorkspace.create(manifest) as first:
        first_source = first.root / "workspace" / "parcel_flow" / "store.py"
        original = first_source.read_text(encoding="utf-8")
        first_source.write_text(original + "\n# pilot mutation\n", encoding="utf-8")
        first_root = first.root
        assert (first.root / "tests" / "visible").is_dir()
        assert not (first.root / "tests" / "hidden").exists()
        assert not (first.root / "reference").exists()
        assert first.hidden_test_root.is_dir()
    assert not first_root.exists()

    with FormalFixturePilotWorkspace.create(manifest) as second:
        second_source = second.root / "workspace" / "parcel_flow" / "store.py"
        assert "# pilot mutation" not in second_source.read_text(encoding="utf-8")
        assert second.root != first_root


def test_trusted_profiles_bind_visible_and_external_hidden_paths() -> None:
    manifest = FormalFixtureLoader().load(TASK_ROOT)

    with FormalFixturePilotWorkspace.create(manifest) as pilot:
        visible_definition, hidden_definition = pilot.profile_definitions(
            executable=sys.executable,
            allowed_env={"PYTHONUTF8": "1", "PYTHONNOUSERSITE": "1"},
        )
        registry = ManagedProfileRegistry(WorkspacePathResolver(pilot.root))
        visible = registry.register(visible_definition)
        hidden = registry.register(hidden_definition)

        assert visible.cwd == str(pilot.root.resolve())
        assert visible.argv[1:] == manifest.visible_command
        assert visible.config_source_kind is ConfigSourceKind.FORMAL_MANIFEST
        assert hidden.config_source_kind is ConfigSourceKind.FORMAL_MANIFEST
        assert registry.argv_review(visible) == (
            visible.executable_path,
            "-m",
            "pytest",
            "<redacted>",
            "-q",
        )
        assert "{VERIFIER}" in hidden.argv
        assert Path(hidden.verifier_root).samefile(pilot.hidden_test_root)
        assert hidden.purpose is ProfilePurpose.VERIFICATION
        assert hidden.runtime_mode is VerificationRuntimeMode.SYSTEM_RUNTIME
        assert "PYTHONPATH" not in hidden.allowed_env
        assert pilot.hidden_test_root.parents[2].samefile(pilot.root.parent)
        assert not pilot.hidden_test_root.parents[1].samefile(pilot.root)
