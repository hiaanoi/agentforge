import os
import subprocess
import sys
from pathlib import Path

import pytest

from agentforge.evaluation.environment import (
    build_fixed_test_environment,
)
from agentforge.evaluation.source_provenance import (
    SourceProvenanceCollector,
)


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        shell=False,
    )


def _repository(tmp_path: Path) -> Path:
    root = tmp_path / "repository"
    (root / "src" / "agentforge").mkdir(parents=True)
    (root / "src" / "agentforge" / "__init__.py").write_text(
        'VERSION = "1"\n',
        encoding="utf-8",
    )
    (root / "pyproject.toml").write_text(
        "[project]\nname = \"fixture\"\nversion = \"0.1.0\"\n",
        encoding="utf-8",
    )
    (root / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    _git(root, "init")
    _git(root, "config", "user.email", "agentforge@example.invalid")
    _git(root, "config", "user.name", "AgentForge Test")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "fixture")
    return root


def test_source_provenance_binds_clean_commit_runtime_and_dependencies(
    tmp_path: Path,
) -> None:
    root = _repository(tmp_path)
    collector = SourceProvenanceCollector()

    first = collector.collect(root)
    repeated = collector.collect(root)

    assert first == repeated
    assert first.git_worktree_clean is True
    assert len(first.git_commit_sha) == 40
    assert len(first.runtime_source_digest) == 64
    assert len(first.pyproject_sha256) == 64
    assert len(first.uv_lock_sha256) == 64
    assert len(first.provenance_digest) == 64
    serialized = first.model_dump_json()
    assert str(root) not in serialized


def test_dirty_tracked_or_untracked_state_is_detected(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    collector = SourceProvenanceCollector()
    clean = collector.collect(root)
    source = root / "src" / "agentforge" / "__init__.py"
    source.write_text('VERSION = "2"\n', encoding="utf-8")

    dirty_tracked = collector.collect(root)

    assert dirty_tracked.git_worktree_clean is False
    assert dirty_tracked.git_commit_sha == clean.git_commit_sha
    assert dirty_tracked.runtime_source_digest != clean.runtime_source_digest
    _git(root, "checkout", "--", "src/agentforge/__init__.py")
    (root / "untracked.txt").write_text("untracked\n", encoding="utf-8")
    dirty_untracked = collector.collect(root)
    assert dirty_untracked.git_worktree_clean is False
    assert dirty_untracked.runtime_source_digest == clean.runtime_source_digest


def test_inherited_git_control_variables_cannot_redirect_collection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _repository(tmp_path)
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "missing-git-dir"))
    monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path / "other-worktree"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "host-config"))

    provenance = SourceProvenanceCollector().collect(root)

    assert provenance.git_worktree_clean is True


def test_fixed_test_environment_does_not_inherit_host_secrets_or_arbitrary_values() -> None:
    parent = {
        "OPENAI_API_KEY": "must-not-enter-tests",
        "GITHUB_TOKEN": "must-not-enter-tests",
        "SSH_AUTH_SOCK": "must-not-enter-tests",
        "HTTP_PROXY": "http://user:password@example.invalid",
        "ARBITRARY_PARENT_VALUE": "must-not-enter-tests",
        "PATH": "must-not-enter-tests",
        "SYSTEMROOT": r"C:\Windows",
        "WINDIR": r"C:\Windows",
    }

    posix = build_fixed_test_environment(
        parent,
        platform="POSIX",
    )
    windows = build_fixed_test_environment(
        parent,
        platform="WINDOWS",
    )

    assert posix == {
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONNOUSERSITE": "1",
        "PYTHONUTF8": "1",
        "PYTEST_ADDOPTS": "-p no:cacheprovider",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
    }
    assert windows == {
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONNOUSERSITE": "1",
        "PYTHONUTF8": "1",
        "PYTEST_ADDOPTS": "-p no:cacheprovider",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "SYSTEMROOT": r"C:\Windows",
        "WINDIR": r"C:\Windows",
    }
    combined = repr((posix, windows))
    for forbidden in (
        "must-not-enter-tests",
        "OPENAI",
        "TOKEN",
        "SECRET",
        "SSH",
        "PROXY",
        "ARBITRARY",
        "PATH",
    ):
        assert forbidden not in combined


def test_default_fixed_environment_matches_current_platform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-enter-tests")
    environment = build_fixed_test_environment()

    assert "OPENAI_API_KEY" not in environment
    if sys.platform == "win32":
        assert environment.get("SYSTEMROOT") == os.environ.get("SYSTEMROOT")
        assert environment.get("WINDIR") == os.environ.get("WINDIR")
    else:
        assert "SYSTEMROOT" not in environment
        assert "WINDIR" not in environment


def test_private_evaluation_state_is_gitignored() -> None:
    gitignore = Path(__file__).parents[2] / ".gitignore"

    assert ".agentforge/" in gitignore.read_text(encoding="utf-8").splitlines()
