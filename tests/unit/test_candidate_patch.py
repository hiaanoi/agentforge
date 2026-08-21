import shutil
import subprocess
from pathlib import Path

import pytest

from agentforge.domain.enums import ToolErrorCode
from agentforge.domain.errors import ToolExecutionError
from agentforge.policy.sensitive import SensitiveFilePolicy
from agentforge.runtime.candidate_patch import CandidatePatchPublisher
from agentforge.tools.mutation.base import MutationLimits
from agentforge.tools.mutation.security import MutationSecurityPolicy
from agentforge.tools.paths import WorkspacePathResolver


def test_candidate_patch_stays_unpublished_until_explicit_publish(tmp_path: Path) -> None:
    canonical = tmp_path / "canonical"
    candidate = tmp_path / "candidate"
    canonical.mkdir()
    (canonical / "src").mkdir()
    (canonical / "src" / "module.py").write_text("value = 1\n", encoding="utf-8")
    _git(canonical, "init")
    _git(canonical, "config", "user.email", "test@example.invalid")
    _git(canonical, "config", "user.name", "Test User")
    _git(canonical, "add", ".")
    _git(canonical, "commit", "-m", "baseline")
    shutil.copytree(canonical, candidate)
    (candidate / "src" / "module.py").write_text("value = 2\n", encoding="utf-8")
    (candidate / "src" / "new.py").write_text("created = True\n", encoding="utf-8")

    publisher = CandidatePatchPublisher(
        canonical_root=canonical,
        security=MutationSecurityPolicy(
            WorkspacePathResolver(canonical),
            SensitiveFilePolicy(),
            MutationLimits(),
        ),
    )

    patch = publisher.capture(candidate)

    assert [entry.target_path for entry in patch.entries] == ["src/module.py", "src/new.py"]
    assert (canonical / "src" / "module.py").read_text(encoding="utf-8") == "value = 1\n"
    assert not (canonical / "src" / "new.py").exists()

    publisher.publish(patch)

    assert (canonical / "src" / "module.py").read_text(encoding="utf-8") == "value = 2\n"
    assert (canonical / "src" / "new.py").read_text(encoding="utf-8") == "created = True\n"


def test_candidate_patch_rejects_file_deletion_without_publishing(tmp_path: Path) -> None:
    canonical, candidate, publisher = _candidate_and_publisher(tmp_path)
    (candidate / "src" / "module.py").unlink()

    with pytest.raises(ToolExecutionError) as raised:
        publisher.capture(candidate)

    assert raised.value.code is ToolErrorCode.TOOL_EXECUTION_ERROR
    assert (canonical / "src" / "module.py").read_text(encoding="utf-8") == "value = 1\n"


def _git(root: Path, *arguments: str) -> None:
    completed = subprocess.run(
        ("git", *arguments),
        cwd=root,
        capture_output=True,
        check=False,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


def _candidate_and_publisher(tmp_path: Path) -> tuple[Path, Path, CandidatePatchPublisher]:
    canonical = tmp_path / "canonical"
    candidate = tmp_path / "candidate"
    canonical.mkdir()
    (canonical / "src").mkdir()
    (canonical / "src" / "module.py").write_text("value = 1\n", encoding="utf-8")
    _git(canonical, "init")
    _git(canonical, "config", "user.email", "test@example.invalid")
    _git(canonical, "config", "user.name", "Test User")
    _git(canonical, "add", ".")
    _git(canonical, "commit", "-m", "baseline")
    shutil.copytree(canonical, candidate)
    publisher = CandidatePatchPublisher(
        canonical_root=canonical,
        security=MutationSecurityPolicy(
            WorkspacePathResolver(canonical),
            SensitiveFilePolicy(),
            MutationLimits(),
        ),
    )
    return canonical, candidate, publisher
