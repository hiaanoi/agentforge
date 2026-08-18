from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest

from agentforge.application.product_workspace import (
    ProductWorkspaceCapture,
    ProductWorkspaceScanner,
)
from agentforge.domain.repair import BudgetProfile, RepairDifficulty, RepairTaskPolicy
from agentforge.evaluation.validators import WorkspaceDiffValidator
from agentforge.persistence.verification_capsules import VerificationCapsuleBuilder
from agentforge.tools.paths import WorkspacePathResolver


def _policy() -> RepairTaskPolicy:
    return RepairTaskPolicy(
        task_id="product-symlink-workspace",
        policy_version=1,
        difficulty=RepairDifficulty.BASIC,
        budget_profile=BudgetProfile.BASIC,
        allowed_write_paths=("src/**",),
        protected_paths=("tests/**",),
        allowed_development_test_profiles=("visible",),
        final_verification_profile_id="hidden",
        allow_file_creation=False,
        max_created_files=0,
        max_changed_files=1,
        max_total_changed_bytes=1024,
        max_single_file_changed_bytes=1024,
        max_model_calls=1,
        max_read_calls=1,
        max_edit_attempts=1,
        max_test_runs=1,
        max_completion_corrections=0,
        max_policy_violations=0,
        max_wall_time_seconds=60,
        path_case_sensitive=True,
    )


@pytest.mark.skipif(os.name != "posix", reason="requires real POSIX symlinks")
def test_product_capture_capsule_and_final_diff_keep_internal_symlinks_inert(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    verifier = tmp_path / "verifier"
    (workspace / "src").mkdir(parents=True)
    verifier.mkdir()
    target = workspace / "src" / "module.py"
    target.write_text("value = 1\n", encoding="utf-8")
    (verifier / "test_hidden.py").write_text("# hidden\n", encoding="utf-8")
    links = {
        "django-relative.py": "src/module.py",
        "matplotlib-relative.py": "src/module.py",
        "pylint-relative.py": "src/module.py",
    }
    for name, link_target in links.items():
        (workspace / name).symlink_to(link_target)

    capture = ProductWorkspaceCapture()
    prepared = capture.capture(
        workspace, task_id="product-symlink-workspace", command_id=uuid4()
    )
    assert capture.matches_source(workspace, prepared.source_digest)

    capsule = VerificationCapsuleBuilder((tmp_path / "capsules").resolve()).capture(
        execution_id=uuid4(), source_root=workspace.resolve(), verifier_root=verifier.resolve()
    )
    for name, link_target in links.items():
        copied = capsule.source_root / name
        assert os.path.islink(copied)
        assert os.readlink(copied) == link_target

    target.write_text("value = 2\n", encoding="utf-8")
    assert not capture.matches_source(workspace, prepared.source_digest)
    VerificationCapsuleBuilder((tmp_path / "capsules").resolve()).verify(capsule)

    resolver = WorkspacePathResolver(workspace)
    diff = WorkspaceDiffValidator(
        resolver, scanner=ProductWorkspaceScanner(workspace)
    ).validate(prepared.baseline, _policy())
    assert diff.compliant
    assert diff.modified_files == ("src/module.py",)
