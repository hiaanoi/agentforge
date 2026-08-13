import os
from pathlib import Path

import pytest

from agentforge.evaluation.persistence import EvaluationWorkspaceRepository
from agentforge.evaluation.workspace import (
    FileContentKind,
    WorkspaceBaselineBuilder,
)
from agentforge.persistence.database import Database
from agentforge.tools.paths import WorkspacePathResolver


def test_baseline_covers_complete_workspace_with_deterministic_manifest(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "src").mkdir(parents=True)
    (workspace / "src" / "module.py").write_text("answer = 41\n", encoding="utf-8")
    (workspace / "asset.bin").write_bytes(b"\x00\xff")
    resolver = WorkspacePathResolver(workspace)
    builder = WorkspaceBaselineBuilder(resolver)

    first = builder.build(task_id="task-1")
    second = builder.build(task_id="task-1")

    assert first.root_digest == second.root_digest
    assert [entry.relative_path for entry in first.files] == [
        "asset.bin",
        "src/module.py",
    ]
    assert first.files[0].content_kind is FileContentKind.BINARY
    assert first.files[1].content_kind is FileContentKind.TEXT
    assert first.files[1].sha256 != first.files[0].sha256
    assert first.manifest_version == 1


def test_baseline_serialization_contains_metadata_but_not_source_content(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    secret_marker = "unique-source-content-must-not-persist"
    (workspace / "module.py").write_text(secret_marker, encoding="utf-8")

    baseline = WorkspaceBaselineBuilder(WorkspacePathResolver(workspace)).build(task_id="task-2")

    serialized = baseline.model_dump_json()
    assert secret_marker not in serialized
    assert "module.py" in serialized
    assert baseline.files[0].size_bytes == len(secret_marker)


def test_baseline_rejects_symlink_or_reparse_entries(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "real.txt"
    target.write_text("real", encoding="utf-8")
    linked = workspace / "linked.txt"
    try:
        linked.symlink_to(target)
    except OSError as exc:
        if os.name == "nt" and getattr(exc, "winerror", None) == 1314:
            pytest.skip(f"Symlinks are unavailable on this platform: {exc}")
        raise

    with pytest.raises(RuntimeError, match="symlink or reparse"):
        WorkspaceBaselineBuilder(WorkspacePathResolver(workspace)).build(task_id="task-3")


def test_baseline_digest_changes_when_executable_metadata_changes(
    tmp_path: Path,
) -> None:
    if os.name == "nt":
        pytest.skip("Executable bit semantics are POSIX-specific")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    script = workspace / "script.py"
    script.write_text("print('ok')\n", encoding="utf-8")
    builder = WorkspaceBaselineBuilder(WorkspacePathResolver(workspace))
    before = builder.build(task_id="task-4")

    script.chmod(script.stat().st_mode | 0o111)
    after = builder.build(task_id="task-4")

    assert before.root_digest != after.root_digest


def test_baseline_metadata_round_trips_without_file_content(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    marker = "source-content-not-in-database-row"
    (workspace / "module.py").write_text(marker, encoding="utf-8")
    baseline = WorkspaceBaselineBuilder(WorkspacePathResolver(workspace)).build(
        task_id="task-5"
    )
    database = Database.from_path(tmp_path / "baseline.db")
    database.create_schema()
    repository = EvaluationWorkspaceRepository(database)

    repository.save_baseline(baseline)
    loaded = repository.get_baseline(baseline.baseline_id)

    assert loaded == baseline
    assert marker not in loaded.model_dump_json()
