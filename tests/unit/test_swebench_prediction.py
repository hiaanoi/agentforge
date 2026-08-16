from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from agentforge.evaluation.swebench_prediction import (
    SWEbenchInstanceBinding,
    SWEbenchPredictionError,
    SWEbenchPredictionExporter,
    save_swebench_prediction,
)


def _git(repository: Path, *arguments: str) -> str:
    executable = shutil.which("git")
    assert executable is not None
    completed = subprocess.run(
        [executable, *arguments],
        cwd=repository,
        capture_output=True,
        check=True,
        text=True,
    )
    return completed.stdout.strip()


def _repository(tmp_path: Path) -> tuple[Path, str]:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "--quiet")
    _git(repository, "config", "user.email", "agentforge@example.invalid")
    _git(repository, "config", "user.name", "AgentForge Tests")
    (repository / "source.py").write_text("answer = 1\n", encoding="utf-8")
    _git(repository, "add", "source.py")
    _git(repository, "commit", "--quiet", "-m", "fixture")
    return repository, _git(repository, "rev-parse", "HEAD")


def _binding(base_commit: str) -> SWEbenchInstanceBinding:
    return SWEbenchInstanceBinding(
        instance_id="sympy__sympy-20590",
        repo="sympy/sympy",
        base_commit=base_commit,
    )


def test_capture_binds_standard_prediction_to_base_commit(tmp_path: Path) -> None:
    repository, base_commit = _repository(tmp_path)
    (repository / "source.py").write_text("answer = 2\n", encoding="utf-8")

    prediction = SWEbenchPredictionExporter().capture(
        repository,
        binding=_binding(base_commit),
        model_identity="deepseek/account-model",
    )

    assert prediction.instance_id == "sympy__sympy-20590"
    assert prediction.model_name_or_path == "agentforge:deepseek/account-model"
    assert "diff --git a/source.py b/source.py" in prediction.model_patch
    assert prediction.base_commit == base_commit
    assert len(prediction.patch_sha256) == 64
    assert prediction.harness_record() == {
        "instance_id": "sympy__sympy-20590",
        "model_name_or_path": "agentforge:deepseek/account-model",
        "model_patch": prediction.model_patch,
    }


def test_capture_rejects_wrong_head(tmp_path: Path) -> None:
    repository, base_commit = _repository(tmp_path)
    (repository / "source.py").write_text("answer = 2\n", encoding="utf-8")

    with pytest.raises(SWEbenchPredictionError, match="HEAD"):
        SWEbenchPredictionExporter().capture(
            repository,
            binding=_binding("0" * 40 if base_commit != "0" * 40 else "1" * 40),
            model_identity="deepseek/account-model",
        )


def test_capture_rejects_empty_patch(tmp_path: Path) -> None:
    repository, base_commit = _repository(tmp_path)

    with pytest.raises(SWEbenchPredictionError, match="empty"):
        SWEbenchPredictionExporter().capture(
            repository,
            binding=_binding(base_commit),
            model_identity="deepseek/account-model",
        )


def test_capture_rejects_untracked_files(tmp_path: Path) -> None:
    repository, base_commit = _repository(tmp_path)
    (repository / "source.py").write_text("answer = 2\n", encoding="utf-8")
    (repository / "created.py").write_text("created = True\n", encoding="utf-8")

    with pytest.raises(SWEbenchPredictionError, match="untracked"):
        SWEbenchPredictionExporter().capture(
            repository,
            binding=_binding(base_commit),
            model_identity="deepseek/account-model",
        )


def test_capture_includes_staged_new_files(tmp_path: Path) -> None:
    repository, base_commit = _repository(tmp_path)
    (repository / "created.py").write_text("created = True\n", encoding="utf-8")
    _git(repository, "add", "created.py")

    prediction = SWEbenchPredictionExporter().capture(
        repository,
        binding=_binding(base_commit),
        model_identity="deepseek/account-model",
    )

    assert "diff --git a/created.py b/created.py" in prediction.model_patch


def test_capture_rejects_non_repository(tmp_path: Path) -> None:
    workspace = tmp_path / "not-a-repository"
    workspace.mkdir()

    with pytest.raises(SWEbenchPredictionError, match="Git command failed"):
        SWEbenchPredictionExporter().capture(
            workspace,
            binding=_binding("0" * 40),
            model_identity="deepseek/account-model",
        )


def test_capture_rejects_git_output_over_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, base_commit = _repository(tmp_path)
    exporter = SWEbenchPredictionExporter()

    def oversized(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(
            args=["git"],
            returncode=0,
            stdout=b"x" * (1024 * 1024 + 1),
            stderr=b"",
        )

    monkeypatch.setattr(
        "agentforge.evaluation.swebench_prediction.subprocess.run",
        oversized,
    )
    with pytest.raises(SWEbenchPredictionError, match="output exceeded"):
        exporter.capture(
            repository,
            binding=_binding(base_commit),
            model_identity="deepseek/account-model",
        )


def test_capture_rejects_symlinked_workspace(tmp_path: Path) -> None:
    repository, base_commit = _repository(tmp_path)
    linked = tmp_path / "linked"
    try:
        linked.symlink_to(repository, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"Symlinks are unavailable on this platform: {exc}")

    with pytest.raises(SWEbenchPredictionError, match="symlink"):
        SWEbenchPredictionExporter().capture(
            linked,
            binding=_binding(base_commit),
            model_identity="deepseek/account-model",
        )


def test_save_writes_one_standard_jsonl_record_atomically(tmp_path: Path) -> None:
    repository, base_commit = _repository(tmp_path)
    (repository / "source.py").write_text("answer = 2\n", encoding="utf-8")
    prediction = SWEbenchPredictionExporter().capture(
        repository,
        binding=_binding(base_commit),
        model_identity="deepseek/account-model",
    )
    output = tmp_path / "artifacts" / "predictions.jsonl"

    save_swebench_prediction(output, prediction)

    lines = output.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0]) == prediction.harness_record()
    assert not output.with_name(f".{output.name}.tmp").exists()


def test_cli_exports_without_printing_patch(
    tmp_path: Path,
) -> None:
    repository, base_commit = _repository(tmp_path)
    (repository / "source.py").write_text("answer = 2\n", encoding="utf-8")
    output = tmp_path / "predictions.jsonl"

    project_root = Path(__file__).resolve().parents[2]
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(project_root / "src")
    completed = subprocess.run(
        [
            sys.executable,
            str(project_root / "evaluation" / "export_swebench_prediction.py"),
            "--workspace",
            str(repository),
            "--instance-id",
            "sympy__sympy-20590",
            "--repo",
            "sympy/sympy",
            "--base-commit",
            base_commit,
            "--model-identity",
            "deepseek/account-model",
            "--output",
            str(output),
        ],
        cwd=project_root,
        env=environment,
        capture_output=True,
        check=False,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert "prediction_sha256=" in completed.stdout
    assert "output=predictions.jsonl" in completed.stdout
    assert "diff --git" not in completed.stdout
    assert output.is_file()
