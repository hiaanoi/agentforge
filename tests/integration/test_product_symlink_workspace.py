from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import pytest

from agentforge.application.app import AgentApplication
from agentforge.application.commands import DecideApproval, ResumeRun, StartRun
from agentforge.application.config import ProductConfigLoader
from agentforge.application.contracts import ProfilePurpose, VerificationCapsuleState
from agentforge.application.product_workspace import (
    ProductWorkspaceCapture,
    ProductWorkspaceScanner,
)
from agentforge.application.queries import PendingApprovals, RunDetails
from agentforge.application.run_creation import ProductStartRunAssembler
from agentforge.application.runtime_factory import (
    RuntimeAssemblyRequest,
    RuntimeComponentFactory,
    RuntimeComponents,
    SupervisorIdentity,
)
from agentforge.context.models import ContextPolicy
from agentforge.domain.enums import ApprovalStatus
from agentforge.domain.repair import (
    BudgetProfile,
    RepairDifficulty,
    RepairTaskPolicy,
    fixed_budget,
)
from agentforge.domain.test_execution import TestProfile as DomainTestProfile
from agentforge.evaluation.validators import WorkspaceDiffValidator
from agentforge.models.domain import ModelBudget
from agentforge.models.mock import MockModelProvider
from agentforge.persistence.database import Database
from agentforge.persistence.legacy_evaluator import LegacyEvaluatorEventRepository
from agentforge.persistence.profile_trust import ProfileKernel
from agentforge.persistence.repair_workflow import RepairWorkflow
from agentforge.persistence.verification_capsules import VerificationCapsuleBuilder
from agentforge.process.base import (
    ProcessTreeSupervisor,
    SupervisorOutcome,
    SupervisorStatus,
    empty_captured_stream,
)
from agentforge.tools.paths import WorkspacePathResolver
from agentforge.tools.testing.profiles import (
    TestProfileDefinition as ProfileDefinition,
)
from agentforge.tools.testing.profiles import TestProfileRegistry as ProfileRegistry


def _policy() -> RepairTaskPolicy:
    budget = fixed_budget(BudgetProfile.BASIC)
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
        path_case_sensitive=True,
        **budget.model_dump(),
    )


def test_product_symlink_policy_uses_the_exact_basic_budget() -> None:
    budget = fixed_budget(BudgetProfile.BASIC)
    policy = _policy()
    assert {
        name: getattr(policy, name)
        for name in type(budget).model_fields
    } == budget.model_dump()


class _PassingSupervisor:
    def __init__(self, launched_profiles: list[DomainTestProfile]) -> None:
        self._launched_profiles = launched_profiles

    def run(self, profile: DomainTestProfile) -> SupervisorOutcome:
        self._launched_profiles.append(profile)
        return SupervisorOutcome(
            status=SupervisorStatus.EXITED,
            root_pid=None,
            process_group_id=None,
            job_id=None,
            exit_code=0,
            stdout=empty_captured_stream(),
            stderr=empty_captured_stream(),
            duration_ms=1,
            termination_reason=None,
            termination_result="natural_exit",
            termination_confirmed=True,
        )

    def cancel(self, reason: str = "cancelled") -> bool:
        return True


class _RecordingSupervisorFactory:
    def __init__(self) -> None:
        self.launched_profiles: list[DomainTestProfile] = []

    def __call__(self) -> ProcessTreeSupervisor:
        return _PassingSupervisor(self.launched_profiles)


async def _collect(stream: AsyncIterator[object]) -> list[object]:
    return [event async for event in stream]


def _product_runtime(
    tmp_path: Path, workspace: Path, verifier: Path
) -> tuple[AgentApplication, Database, RuntimeComponents, _RecordingSupervisorFactory]:
    config = ProductConfigLoader(user_root=tmp_path / "user").load(
        workspace,
        cli={
            "database_path": ".agentforge/agentforge.db",
            "model": "mock",
            "profile_ids": ("hidden", "visible"),
        },
    )
    database = Database.from_path(
        config.database_path,
        artifact_root=(tmp_path / "verification-artifacts").resolve(),
    )
    database.create_schema()
    profiles = ProfileRegistry(WorkspacePathResolver(workspace))
    for profile_id, purpose, argv, verifier_root in (
        ("visible", ProfilePurpose.DEVELOPMENT, ("-c", "pass"), None),
        ("hidden", ProfilePurpose.VERIFICATION, ("-m", "pytest", "{VERIFIER}"), str(verifier)),
    ):
        profiles.register(
            ProfileDefinition(
                profile_id=profile_id,
                name=profile_id,
                description=f"{profile_id} profile",
                executable=sys.executable,
                argv=argv,
                cwd=".",
                timeout_seconds=10,
                max_output_bytes=4096,
                profile_version=1,
                purpose=purpose,
                verifier_root=verifier_root,
            )
        )
    kernel = ProfileKernel(database, profiles)
    for profile in profiles.list_enabled():
        kernel.trust(
            kernel.challenge(profile.profile_id, purpose=profile.purpose),
            command_id=uuid4(),
        )
    provider = MockModelProvider(
        [
            {
                "type": "tool_call",
                "tool": "edit_file",
                "arguments": {
                    "path": "src/module.py",
                    "old_text": "value = 1\n",
                    "new_text": "value = 2\n",
                },
            },
            {"type": "final", "answer": "verified repair"},
        ]
    )
    workflow = RepairWorkflow(database)
    supervisor_factory = _RecordingSupervisorFactory()
    request = RuntimeAssemblyRequest(
        database=database,
        workspace=workspace,
        provider=provider,
        policy=_policy(),
        context_policy=ContextPolicy(system_instructions="product symlink workspace"),
        model_budget=ModelBudget(max_model_requests=3, max_retries=0),
        profiles=profiles,
        events=LegacyEvaluatorEventRepository(database),
        repair_workflow=workflow,
        max_output_chars=20_000,
        supervisor_factory=supervisor_factory,
        supervisor_identity=SupervisorIdentity(
            implementation="tests.product_symlink_workspace.PassingSupervisor",
            implementation_version="1",
            config_digest="a" * 64,
        ),
    )
    components = RuntimeComponentFactory().build(request)
    assembler = ProductStartRunAssembler(
        config=config,
        workspace=workspace,
        components=components,
        profiles=profiles,
        repair_policy=request.policy,
        model_budget=request.model_budget,
    )
    return (
        AgentApplication(
            database,
            components.runtime,
            start_assembler=assembler,
            profiles=profiles,
        ),
        database,
        components,
        supervisor_factory,
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


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="requires real POSIX symlinks")
async def test_product_runtime_executes_final_capsule_after_regular_mutation(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    verifier = tmp_path / "verifier"
    (workspace / "src").mkdir(parents=True)
    verifier.mkdir()
    target = workspace / "src" / "module.py"
    target.write_text("value = 1\n", encoding="utf-8")
    (verifier / "test_hidden.py").write_text("def test_hidden(): pass\n", encoding="utf-8")
    links = {
        "django-relative.py": "src/module.py",
        "matplotlib-relative.py": "src/module.py",
        "pylint-relative.py": "src/module.py",
    }
    for name, link_target in links.items():
        (workspace / name).symlink_to(link_target)
    for command in (
        ("git", "init", "-q"),
        ("git", "config", "user.email", "fixture@example.invalid"),
        ("git", "config", "user.name", "AgentForge Fixture"),
        ("git", "add", "."),
        ("git", "commit", "-qm", "fixture"),
    ):
        await asyncio.to_thread(subprocess.run, command, cwd=workspace, check=True)

    capture = ProductWorkspaceCapture()
    initial = capture.capture(
        workspace, task_id="product-symlink-workspace", command_id=uuid4()
    )
    app, database, components, supervisor_factory = _product_runtime(
        tmp_path, workspace, verifier
    )
    try:
        started = await _collect(
            app.stream(StartRun(command_id=uuid4(), task="repair", workspace=workspace))
        )
        run_id = started[0].run_id  # type: ignore[attr-defined]
        pending = app.query(PendingApprovals(run_id=run_id)).approvals
        assert len(pending) == 1
        await _collect(
            app.stream(
                DecideApproval(
                    command_id=uuid4(),
                    approval_id=pending[0].approval_id,
                    status=ApprovalStatus.APPROVED,
                )
            )
        )
        await _collect(app.stream(ResumeRun(command_id=uuid4(), run_id=run_id)))
        assert target.read_text(encoding="utf-8") == "value = 2\n"
        assert not capture.matches_source(workspace, initial.source_digest)

        pending = app.query(PendingApprovals(run_id=run_id)).approvals
        assert len(pending) == 1
        await _collect(
            app.stream(
                DecideApproval(
                    command_id=uuid4(),
                    approval_id=pending[0].approval_id,
                    status=ApprovalStatus.APPROVED,
                )
            )
        )
        await _collect(app.stream(ResumeRun(command_id=uuid4(), run_id=run_id)))

        executions = components.test_coordinator.list_executions(run_id)
        assert len(executions) == 1
        assert executions[0].capsule_state is VerificationCapsuleState.SEALED
        assert len(supervisor_factory.launched_profiles) == 1
        launched = supervisor_factory.launched_profiles[0]
        capsule_source = Path(launched.cwd)
        assert capsule_source.name == "source"
        assert Path(launched.argv[-1]) == capsule_source.parent / "verifier"
        assert app.query(RunDetails(run_id=run_id)).outcome_status.value == "VERIFIED"
        for name, link_target in links.items():
            assert os.readlink(workspace / name) == link_target

        diff = WorkspaceDiffValidator(
            WorkspacePathResolver(workspace), scanner=ProductWorkspaceScanner(workspace)
        ).validate(initial.baseline, _policy())
        assert diff.compliant
        assert diff.modified_files == ("src/module.py",)
    finally:
        database.close()
