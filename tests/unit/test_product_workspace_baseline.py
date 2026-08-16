import os
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from agentforge.application.kernel_errors import WorkspaceDigestError
from agentforge.application.product_workspace import (
    ProductWorkspaceCapture,
    ProductWorkspaceScanner,
    WorkspaceBaselineStore,
)
from agentforge.application.run_creation import (
    RunCreationFailpoint,
    RunCreationWorkflow,
    SimulatedProcessCrash,
    StartRun,
)
from agentforge.domain.repair import BudgetProfile, RepairDifficulty, RepairTaskPolicy
from agentforge.evaluation.validators import WorkspaceDiffValidator
from agentforge.evaluation.workspace import WorkspaceBaseline
from agentforge.models.domain import ModelBudget
from agentforge.persistence.application_uow import ApplicationUnitOfWork
from agentforge.persistence.database import Database
from agentforge.persistence.source_revisions import WorkspaceDigester, WorkspaceDigestLimits
from agentforge.persistence.tables import WorkspaceBaselineFileRow, WorkspaceBaselineRow
from agentforge.tools.paths import WorkspacePathResolver


def test_capture_is_deterministic_and_separates_source_from_manifest(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "src").mkdir()
    (root / "src" / "main.py").write_text("answer = 42\n", encoding="utf-8")
    command_id = UUID("11111111-1111-1111-1111-111111111111")

    first = ProductWorkspaceCapture().capture(root, task_id="repair", command_id=command_id)
    second = ProductWorkspaceCapture().capture(root, task_id="repair", command_id=command_id)

    assert first.source_digest == second.source_digest
    assert first.baseline.root_digest == second.baseline.root_digest
    assert first.baseline.baseline_id == second.baseline.baseline_id
    assert first.baseline.manifest_version == 2
    assert first.source_digest != first.baseline.root_digest
    assert first.source_digest == WorkspaceDigester().digest(root)


def test_capture_excludes_only_root_agentforge_directory(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / ".agentforge").mkdir()
    (root / ".agentforge" / "agentforge.db").write_bytes(b"sqlite")
    (root / ".agentforge" / "config.toml").write_text("runtime = true", encoding="utf-8")
    (root / "nested" / ".agentforge").mkdir(parents=True)
    (root / "nested" / ".agentforge" / "kept.txt").write_text("kept", encoding="utf-8")

    prepared = ProductWorkspaceCapture().capture(root, task_id="repair", command_id=uuid4())

    assert [item.relative_path for item in prepared.baseline.files] == [
        "nested/.agentforge/kept.txt"
    ]

    (root / ".agentforge" / "agentforge.db-wal").write_bytes(b"new-wal")
    after_runtime_churn = ProductWorkspaceCapture().capture(
        root, task_id="repair", command_id=prepared.baseline.baseline_id
    )
    assert after_runtime_churn.baseline.root_digest == prepared.baseline.root_digest


def test_capture_skips_git_metadata_before_bounded_file_reads(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "a.py").write_text("ok\n", encoding="utf-8")
    (root / ".git" / "objects").mkdir(parents=True)
    (root / ".git" / "objects" / "large.pack").write_bytes(b"12345")
    capture = ProductWorkspaceCapture(
        WorkspaceDigester(
            limits=WorkspaceDigestLimits(
                max_entries=20,
                max_files=10,
                max_file_bytes=4,
                max_total_bytes=20,
            )
        )
    )

    prepared = capture.capture(root, task_id="repair", command_id=uuid4())

    assert [item.relative_path for item in prepared.baseline.files] == ["a.py"]
    assert capture.matches_source(root, prepared.source_digest)


def test_capture_rechecks_the_same_source_digest_before_driver_start(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    source = root / "a.py"
    source.write_text("before", encoding="utf-8")
    capture = ProductWorkspaceCapture()
    prepared = capture.capture(root, task_id="repair", command_id=uuid4())
    source.write_text("after", encoding="utf-8")

    assert not capture.matches_source(root, prepared.source_digest)


def test_capture_rejects_links_with_the_source_digest_hardening(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "target.txt"
    target.write_text("data", encoding="utf-8")
    linked = root / "link.txt"
    try:
        linked.symlink_to(target)
    except OSError as exc:
        if os.name == "nt" and getattr(exc, "winerror", None) == 1314:
            pytest.skip("symlinks unavailable")
        raise

    with pytest.raises(WorkspaceDigestError):
        ProductWorkspaceCapture().capture(root, task_id="repair", command_id=uuid4())


def test_session_store_rejects_missing_extra_and_tampered_rows(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "a.py").write_text("a", encoding="utf-8")
    prepared = ProductWorkspaceCapture().capture(root, task_id="repair", command_id=uuid4())
    database = Database.from_path(tmp_path / "app.db")
    database.create_schema()
    store = WorkspaceBaselineStore()
    with ApplicationUnitOfWork(database) as uow:
        store.put(uow.session, prepared.baseline)
        uow.commit()

    with ApplicationUnitOfWork(database) as uow:
        stored = uow.session.query(WorkspaceBaselineFileRow).one()
        row = uow.session.get(WorkspaceBaselineFileRow, stored.file_id)
        assert row is not None
        row.sha256 = "0" * 64
        uow.commit()
    with ApplicationUnitOfWork(database) as uow:
        with pytest.raises(RuntimeError, match="conflict"):
            store.put(uow.session, prepared.baseline)

    with ApplicationUnitOfWork(database) as uow:
        parent = uow.session.get(WorkspaceBaselineRow, str(prepared.baseline.baseline_id))
        assert parent is not None
        uow.session.delete(parent)
        uow.commit()


def test_session_store_rejects_a_forged_manifest_before_any_insert(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "a.py").write_text("a", encoding="utf-8")
    prepared = ProductWorkspaceCapture().capture(root, task_id="repair", command_id=uuid4())
    forged = WorkspaceBaseline.model_construct(
        baseline_id=prepared.baseline.baseline_id,
        task_id=prepared.baseline.task_id,
        workspace_root=prepared.baseline.workspace_root,
        root_digest="0" * 64,
        manifest_version=prepared.baseline.manifest_version,
        created_at=prepared.baseline.created_at,
        files=prepared.baseline.files,
    )
    database = Database.from_path(tmp_path / "app.db")
    database.create_schema()

    with ApplicationUnitOfWork(database) as uow:
        with pytest.raises(RuntimeError, match="conflict"):
            WorkspaceBaselineStore().put(uow.session, forged)
        assert uow.session.query(WorkspaceBaselineRow).count() == 0
        assert uow.session.query(WorkspaceBaselineFileRow).count() == 0


def test_run_creation_persists_prepared_baseline_in_its_same_uow(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "src.py").write_text("x = 1\n", encoding="utf-8")
    command_id = uuid4()
    prepared = ProductWorkspaceCapture().capture(root, task_id="repair", command_id=command_id)
    policy = RepairTaskPolicy(
        task_id="repair", policy_version=1, difficulty=RepairDifficulty.BASIC,
        budget_profile=BudgetProfile.BASIC, allowed_write_paths=("**",), protected_paths=(),
        allowed_development_test_profiles=("unit",), final_verification_profile_id="final",
        allow_file_creation=False, max_created_files=0, max_changed_files=2,
        max_total_changed_bytes=1024, max_single_file_changed_bytes=1024,
        path_case_sensitive=False,
    )
    command = StartRun(
        command_id=command_id, task="repair", max_steps=1, max_tool_calls=1,
        model_provider="test", model_budget=ModelBudget(max_model_requests=1),
        workspace_root_identity=str(root.resolve()), git_head=None,
        initial_source_digest=prepared.source_digest, digest_algorithm_version=1,
        config_digest="a" * 64, profile_digest="b" * 64, repair_policy=policy,
        baseline_id=prepared.baseline.baseline_id, baseline_digest=prepared.baseline.root_digest,
    )
    database = Database.from_path(tmp_path / "app.db")
    database.create_schema()

    result = RunCreationWorkflow(database).create(command, prepared_workspace=prepared)

    with database.session() as session:
        assert session.get(WorkspaceBaselineRow, str(prepared.baseline.baseline_id)) is not None
        assert session.query(WorkspaceBaselineFileRow).count() == 1
    assert result.run_id


def test_baseline_failpoint_rolls_back_the_baseline(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "src.py").write_text("x = 1\n", encoding="utf-8")
    command_id = uuid4()
    prepared = ProductWorkspaceCapture().capture(root, task_id="repair", command_id=command_id)
    policy = RepairTaskPolicy(
        task_id="repair", policy_version=1, difficulty=RepairDifficulty.BASIC,
        budget_profile=BudgetProfile.BASIC, allowed_write_paths=("**",), protected_paths=(),
        allowed_development_test_profiles=("unit",), final_verification_profile_id="final",
        allow_file_creation=False, max_created_files=0, max_changed_files=2,
        max_total_changed_bytes=1024, max_single_file_changed_bytes=1024,
        path_case_sensitive=False,
    )
    command = StartRun(
        command_id=command_id, task="repair", max_steps=1, max_tool_calls=1,
        model_provider="test", model_budget=ModelBudget(max_model_requests=1),
        workspace_root_identity=str(root.resolve()), git_head=None,
        initial_source_digest=prepared.source_digest, digest_algorithm_version=1,
        config_digest="a" * 64, profile_digest="b" * 64, repair_policy=policy,
        baseline_id=prepared.baseline.baseline_id, baseline_digest=prepared.baseline.root_digest,
    )
    database = Database.from_path(tmp_path / "app.db")
    database.create_schema()

    with pytest.raises(SimulatedProcessCrash):
        RunCreationWorkflow(database).create(
            command, prepared_workspace=prepared, failpoint=RunCreationFailpoint.AFTER_BASELINE
        )

    with database.session() as session:
        assert session.get(WorkspaceBaselineRow, str(prepared.baseline.baseline_id)) is None


def test_product_diff_scanner_uses_the_recorded_runtime_exclusion(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    (root / "src").mkdir(parents=True)
    (root / "src" / "main.py").write_text("answer = 1\n", encoding="utf-8")
    prepared = ProductWorkspaceCapture().capture(root, task_id="repair", command_id=uuid4())
    (root / ".agentforge").mkdir()
    (root / ".agentforge" / "agentforge.db-wal").write_bytes(b"runtime")
    policy = RepairTaskPolicy(
        task_id="repair", policy_version=1, difficulty=RepairDifficulty.BASIC,
        budget_profile=BudgetProfile.BASIC, allowed_write_paths=("src/**",), protected_paths=(),
        allowed_development_test_profiles=("unit",), final_verification_profile_id="final",
        allow_file_creation=False, max_created_files=0, max_changed_files=2,
        max_total_changed_bytes=1024, max_single_file_changed_bytes=1024,
        path_case_sensitive=False,
    )

    result = WorkspaceDiffValidator(
        WorkspacePathResolver(root), scanner=ProductWorkspaceScanner(root)
    ).validate(prepared.baseline, policy)

    assert result.compliant
