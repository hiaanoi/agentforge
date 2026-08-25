import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest

from agentforge.domain.enums import ToolCapability, ToolErrorCode
from agentforge.domain.errors import ToolExecutionError
from agentforge.domain.mutations import MutationPlan
from agentforge.policy.sensitive import SensitiveFilePolicy
from agentforge.runtime.candidate_patch import (
    CandidatePatch,
    CandidatePatchEntry,
    CandidatePatchPublisher,
    CandidatePatchPublishTool,
    CandidatePatchStore,
)
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


def test_saved_candidate_patch_can_be_reopened_and_published_after_restart(tmp_path: Path) -> None:
    canonical, candidate, publisher = _candidate_and_publisher(tmp_path)
    (candidate / "src" / "module.py").write_text("value = 2\n", encoding="utf-8")

    patch = publisher.capture(candidate)
    manifest = CandidatePatchStore(canonical).save("run-1", patch)
    reopened = CandidatePatchStore(canonical).load(manifest)

    assert (canonical / "src" / "module.py").read_text(encoding="utf-8") == "value = 1\n"
    publisher.publish(reopened)
    assert (canonical / "src" / "module.py").read_text(encoding="utf-8") == "value = 2\n"


def test_candidate_patch_publish_tool_reads_saved_manifest_by_run_id(tmp_path: Path) -> None:
    canonical, candidate, publisher = _candidate_and_publisher(tmp_path)
    (candidate / "src" / "module.py").write_text("value = 2\n", encoding="utf-8")
    store = CandidatePatchStore(canonical)
    patch = publisher.capture(candidate)
    store.save("00000000-0000-0000-0000-000000000001", patch)
    tool = CandidatePatchPublishTool(publisher, store)

    arguments = tool.input_model.model_validate(
        {
            "run_id": "00000000-0000-0000-0000-000000000001",
            "manifest_digest": patch.manifest_digest,
        }
    )
    result = tool.execute(arguments)

    assert tool.spec.requires_approval is True
    assert tool.spec.capability is ToolCapability.CANDIDATE_PATCH_PUBLICATION
    assert result.success is True
    assert result.output == {"entries": 1, "status": "published"}
    assert (canonical / "src" / "module.py").read_text(encoding="utf-8") == "value = 2\n"


def test_candidate_patch_publish_rejects_unapproved_manifest_digest(tmp_path: Path) -> None:
    canonical, candidate, publisher = _candidate_and_publisher(tmp_path)
    (candidate / "src" / "module.py").write_text("value = 2\n", encoding="utf-8")
    patch = publisher.capture(candidate)
    store = CandidatePatchStore(canonical)
    store.save("00000000-0000-0000-0000-000000000002", patch)
    tool = CandidatePatchPublishTool(publisher, store)
    arguments = tool.input_model.model_validate(
        {
            "run_id": "00000000-0000-0000-0000-000000000002",
            "manifest_digest": "f" * 64,
        }
    )

    with pytest.raises(ToolExecutionError) as raised:
        tool.execute(arguments)

    assert raised.value.code is ToolErrorCode.APPROVAL_CONFLICT
    assert (canonical / "src" / "module.py").read_text(encoding="utf-8") == "value = 1\n"


def test_candidate_patch_publish_revalidates_excluded_paths(tmp_path: Path) -> None:
    canonical, _, _ = _candidate_and_publisher(tmp_path)
    publisher = CandidatePatchPublisher(
        canonical_root=canonical,
        security=MutationSecurityPolicy(
            WorkspacePathResolver(canonical),
            SensitiveFilePolicy(),
            MutationLimits(),
        ),
        excluded_path_prefixes=(".agentforge",),
    )
    data = b'{"private":true}\n'
    path = ".agentforge/runtime-config.json"
    patch = CandidatePatch(
        entries=(
            CandidatePatchEntry(
                target_path=path,
                data=data,
                plan=MutationPlan(
                    tool_name="publish_candidate_patch",
                    target_path=path,
                    target_existed=False,
                    before_sha256=None,
                    expected_after_sha256=hashlib.sha256(data).hexdigest(),
                    bytes_written=len(data),
                ),
            ),
        )
    )

    with pytest.raises(ToolExecutionError) as raised:
        publisher.publish(patch)

    assert raised.value.code is ToolErrorCode.POLICY_DENIED
    assert not (canonical / path).exists()


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
