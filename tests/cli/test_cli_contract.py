from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from agentforge.application.config import ProductConfigLoader, UnsafeConfigurationError


def _write_runtime_definition(workspace: Path, verifier: Path) -> None:
    state = workspace / ".agentforge"
    state.mkdir()
    (state / "runtime.toml").write_text(
        "\n".join(
            (
                '[provider]',
                'kind = "mock"',
                (
                    'mock_responses = [{ type = "tool_call", tool = "run_tests", '
                    'arguments = { profile_id = "visible" } }]'
                ),
                '',
                '[policy]',
                'task_id = "cli-contract"',
                'policy_version = 1',
                'difficulty = "ENGINEERING"',
                'budget_profile = "ENGINEERING"',
                'allowed_write_paths = ["src/**"]',
                'forbidden_write_paths = [".git/**"]',
                'protected_paths = ["tests/**"]',
                'allowed_development_test_profiles = ["visible"]',
                'final_verification_profile_id = "verify"',
                'allow_file_creation = true',
                'allowed_create_paths = ["src/**"]',
                'max_created_files = 2',
                'max_changed_files = 4',
                'max_total_changed_bytes = 1048576',
                'max_single_file_changed_bytes = 1048576',
                'path_case_sensitive = false',
                '',
                '[[profiles]]',
                'profile_id = "visible"',
                'name = "Visible tests"',
                'description = "Product development tests"',
                f'executable = {str(Path(sys.executable))!r}',
                'argv = ["-m", "pytest", "{SOURCE}", "-q"]',
                'cwd = "."',
                'timeout_seconds = 10',
                'max_output_bytes = 4096',
                'profile_version = 1',
                'purpose = "development"',
                '',
                '[[profiles]]',
                'profile_id = "verify"',
                'name = "Hidden tests"',
                'description = "Product final verification"',
                f'executable = {str(Path(sys.executable))!r}',
                'argv = ["-m", "pytest", "{VERIFIER}", "-q"]',
                'cwd = "."',
                'timeout_seconds = 10',
                'max_output_bytes = 4096',
                'profile_version = 1',
                'purpose = "verification"',
                f'verifier_root = {str(verifier)!r}',
            )
        ),
        encoding="utf-8",
    )


def _write_product_config(workspace: Path) -> None:
    (workspace / ".agentforge" / "config.toml").write_text(
        "\n".join(
            (
                'database_path = ".agentforge/agentforge.db"',
                'model = "mock"',
                'profile_ids = ["verify", "visible"]',
            )
        ),
        encoding="utf-8",
    )


def test_runtime_definition_is_required_and_binds_exact_profiles(tmp_path: Path) -> None:
    from agentforge.application.bootstrap import ProductRuntimeDefinitionLoader

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    verifier = tmp_path / "verifier"
    verifier.mkdir()
    config = ProductConfigLoader(user_root=tmp_path / "user").load(
        workspace,
        cli={
            "database_path": ".agentforge/agentforge.db",
            "model": "mock",
            "profile_ids": ("verify", "visible"),
        },
    )
    with pytest.raises(UnsafeConfigurationError):
        ProductRuntimeDefinitionLoader().load(workspace, config=config)

    _write_runtime_definition(workspace, verifier)
    definition = ProductRuntimeDefinitionLoader().load(workspace, config=config)

    assert definition.profile_ids == ("verify", "visible")
    assert definition.profiles[1].purpose.value == "verification"
    assert definition.config_source_digest != config.effective_config_digest


def test_runtime_definition_rejects_secret_fields(tmp_path: Path) -> None:
    from agentforge.application.bootstrap import ProductRuntimeDefinitionLoader

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    verifier = tmp_path / "verifier"
    verifier.mkdir()
    _write_runtime_definition(workspace, verifier)
    runtime_path = workspace / ".agentforge" / "runtime.toml"
    runtime_path.write_text(
        runtime_path.read_text(encoding="utf-8") + '\napi_key = "do-not-store"\n',
        encoding="utf-8",
    )
    config = ProductConfigLoader(user_root=tmp_path / "user").load(
        workspace,
        cli={
            "database_path": ".agentforge/agentforge.db",
            "model": "mock",
            "profile_ids": ("verify", "visible"),
        },
    )

    with pytest.raises(UnsafeConfigurationError):
        ProductRuntimeDefinitionLoader().load(workspace, config=config)


def test_runtime_definition_rejects_caller_controlled_profile_provenance(tmp_path: Path) -> None:
    from agentforge.application.bootstrap import ProductRuntimeDefinitionLoader

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    verifier = tmp_path / "verifier"
    verifier.mkdir()
    _write_runtime_definition(workspace, verifier)
    runtime_path = workspace / ".agentforge" / "runtime.toml"
    runtime_path.write_text(
        runtime_path.read_text(encoding="utf-8").replace(
            'purpose = "verification"',
            'purpose = "verification"\nconfig_source_identity = "operator-override"',
        ),
        encoding="utf-8",
    )
    config = ProductConfigLoader(user_root=tmp_path / "user").load(
        workspace,
        cli={
            "database_path": ".agentforge/agentforge.db",
            "model": "mock",
            "profile_ids": ("verify", "visible"),
        },
    )

    with pytest.raises(UnsafeConfigurationError):
        ProductRuntimeDefinitionLoader().load(workspace, config=config)


def test_runtime_definition_is_not_an_authorized_model_write_target(tmp_path: Path) -> None:
    from agentforge.application.bootstrap import ProductRuntimeDefinitionLoader

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    verifier = tmp_path / "verifier"
    verifier.mkdir()
    _write_runtime_definition(workspace, verifier)
    runtime_path = workspace / ".agentforge" / "runtime.toml"
    runtime_path.write_text(
        runtime_path.read_text(encoding="utf-8").replace(
            'allowed_write_paths = ["src/**"]',
            'allowed_write_paths = [".agentforge/**"]',
        ),
        encoding="utf-8",
    )
    config = ProductConfigLoader(user_root=tmp_path / "user").load(
        workspace,
        cli={
            "database_path": ".agentforge/agentforge.db",
            "model": "mock",
            "profile_ids": ("verify", "visible"),
        },
    )

    with pytest.raises(UnsafeConfigurationError):
        ProductRuntimeDefinitionLoader().load(workspace, config=config)


def test_profile_selection_order_is_canonical_for_runtime_binding(tmp_path: Path) -> None:
    from agentforge.application.bootstrap import ProductRuntimeDefinitionLoader

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    verifier = tmp_path / "verifier"
    verifier.mkdir()
    _write_runtime_definition(workspace, verifier)
    config = ProductConfigLoader(user_root=tmp_path / "user").load(
        workspace,
        cli={
            "database_path": ".agentforge/agentforge.db",
            "model": "mock",
            "profile_ids": ("visible", "verify"),
        },
    )

    assert config.profile_ids == ("verify", "visible")
    assert ProductRuntimeDefinitionLoader().load(workspace, config=config).profile_ids == (
        "verify",
        "visible",
    )


@pytest.mark.asyncio
async def test_bootstrapped_application_requires_explicit_profile_trust(tmp_path: Path) -> None:
    from agentforge.application.bootstrap import ProductApplicationFactory
    from agentforge.application.commands import TrustProfile
    from agentforge.application.queries import ProfileTrustDetails
    from agentforge.application.views import ProfileTrustDetailsView

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    verifier = tmp_path / "verifier"
    verifier.mkdir()
    _write_runtime_definition(workspace, verifier)
    config = ProductConfigLoader(user_root=tmp_path / "user").load(
        workspace,
        cli={
            "database_path": ".agentforge/agentforge.db",
            "model": "mock",
            "profile_ids": ("verify", "visible"),
        },
    )

    async with ProductApplicationFactory().build(workspace, config=config) as application:
        view = application.query(
            ProfileTrustDetails(workspace=workspace, profile_id="visible")
        )
        assert isinstance(view, ProfileTrustDetailsView)
        assert view.trusted is False
        events = [
            event
            async for event in application.stream(
                TrustProfile(
                    command_id=uuid4(),
                    workspace_identity=view.workspace_identity,
                    purpose=view.purpose,
                    identity=view.trusted_identity(),
                )
            )
        ]
        assert events[-1].event.value == "profile_trusted"


@pytest.mark.parametrize(
    "name", ["exec", "inspect", "doctor", "approvals", "approve", "reject", "resume", "trust"]
)
def test_core_subcommand_has_help(name: str, capsys: pytest.CaptureFixture[str]) -> None:
    from agentforge.cli.main import main

    assert main((name, "--help")) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert "usage:" in captured.out


def test_exec_pause_prints_only_stable_identifiers(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from agentforge.cli.exit_codes import ExitCode
    from agentforge.cli.main import main
    from agentforge.evaluation.public_artifacts import PublicArtifactScanner

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "src").mkdir()
    (workspace / "src" / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    verifier = tmp_path / "verifier"
    verifier.mkdir()
    _write_runtime_definition(workspace, verifier)
    _write_product_config(workspace)

    assert main(("trust", "--workspace", str(workspace), "visible", "--yes")) == ExitCode.OK
    assert main(("trust", "--workspace", str(workspace), "verify", "--yes")) == ExitCode.OK
    capsys.readouterr()

    result = main(("exec", "--workspace", str(workspace), "fix the defect"))
    assert result == ExitCode.APPROVAL_REQUIRED
    captured = capsys.readouterr()
    assert "run_id=" in captured.out
    assert "approval_id=" in captured.out
    assert captured.err == ""
    PublicArtifactScanner().validate(captured.out)


def test_configuration_failure_uses_stable_exit_and_stderr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from agentforge.cli.exit_codes import ExitCode
    from agentforge.cli.main import main
    from agentforge.evaluation.public_artifacts import PublicArtifactScanner

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    assert main(("doctor", "--workspace", str(workspace))) == ExitCode.CONFIGURATION_ERROR
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "configuration_error\n"
    PublicArtifactScanner().validate(captured.err)


def test_doctor_never_materializes_a_missing_database(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from agentforge.cli.main import main

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    verifier = tmp_path / "verifier"
    verifier.mkdir()
    _write_runtime_definition(workspace, verifier)
    _write_product_config(workspace)
    database_path = workspace / ".agentforge" / "agentforge.db"

    assert not database_path.exists()
    assert main(("doctor", "--workspace", str(workspace))) == 0
    assert not database_path.exists()
    captured = capsys.readouterr()
    assert "check=schema status=FAIL" in captured.out
    assert captured.err == ""


def test_ctrl_c_prints_accepted_run_id_and_detaches(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentforge.application.contracts import LifecycleStatus
    from agentforge.application.events import ProductEvent, RunStatePayload
    from agentforge.cli.exit_codes import ExitCode
    from agentforge.cli.main import main

    run_id = uuid4()

    class _InterruptedApplication:
        async def stream(self, _: object, *, after_cursor: int | None = None):
            del after_cursor
            yield ProductEvent(
                event_id=uuid4(),
                scope_type="RUN",
                scope_id=str(run_id),
                cursor=1,
                run_id=run_id,
                sequence_number=1,
                occurred_at=datetime.now(UTC),
                payload=RunStatePayload(
                    lifecycle_status=LifecycleStatus.RUNNING,
                    outcome_status=None,
                ),
            )
            raise KeyboardInterrupt

        async def aclose(self) -> None:
            return None

    import agentforge.cli.main as cli_main

    monkeypatch.setattr(cli_main, "build_application", lambda _: _InterruptedApplication())
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    assert main(("exec", "--workspace", str(workspace), "fix it")) == ExitCode.PAUSED
    captured = capsys.readouterr()
    assert f"run_id={run_id} detached=true" in captured.err
    assert captured.err.count("detached=true") == 1


def test_trust_requires_explicit_review_confirmation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from agentforge.cli.exit_codes import ExitCode
    from agentforge.cli.main import main

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    verifier = tmp_path / "verifier"
    verifier.mkdir()
    _write_runtime_definition(workspace, verifier)
    _write_product_config(workspace)

    assert main(("trust", "--workspace", str(workspace), "visible", "--show")) == ExitCode.OK
    shown = capsys.readouterr()
    assert "profile_id=visible" in shown.out
    assert "argv_digest=" in shown.out
    assert str(verifier) not in shown.out
    assert shown.err == ""

    assert main(("trust", "--workspace", str(workspace), "visible")) == ExitCode.USAGE
    missing_confirmation = capsys.readouterr()
    assert "code=INVALID_REQUEST" in missing_confirmation.err

    assert main(("trust", "--workspace", str(workspace), "visible", "--yes")) == ExitCode.OK
