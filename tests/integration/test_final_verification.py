import hashlib
import sys
import tempfile
import time
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from threading import get_ident
from uuid import UUID, uuid4

import pytest

from agentforge.application.contracts import RuntimeTrustClass, VerificationCapsuleState
from agentforge.application.kernel_errors import SourceRevisionConflictError
from agentforge.domain.enums import (
    ProcessExecutionStatus,
    ProcessFailureKind,
    RunStatus,
)
from agentforge.domain.errors import ResumeNotAllowedError
from agentforge.domain.models import utc_now
from agentforge.domain.repair import (
    BudgetProfile,
    RepairCompletionStatus,
    RepairDifficulty,
    RepairTaskPolicy,
)
from agentforge.domain.test_execution import ProcessExecutionRecord
from agentforge.evaluation.auto_approval import AutoApprovalHarness, EvaluationWorkspaceHandle
from agentforge.evaluation.persistence import EvaluationWorkspaceRepository
from agentforge.evaluation.validators import WorkspaceDiffValidator
from agentforge.evaluation.workspace import WorkspaceBaselineBuilder
from agentforge.models.mock import MockModelProvider
from agentforge.persistence.approval_workflow import ApprovalWorkflow
from agentforge.persistence.database import Database
from agentforge.persistence.legacy_evaluator import LegacyEvaluatorEventRepository
from agentforge.persistence.product_tables import (
    RunLeaseRow,
    TrustedProfileRow,
    WorkspaceSourceBindingRow,
)
from agentforge.persistence.profile_trust import ProfileKernel, ProfilePurpose
from agentforge.persistence.repair_workflow import RepairWorkflow
from agentforge.persistence.repositories import (
    ApprovalRepository,
    CheckpointRepository,
    EventRepository,
    RunRepository,
)
from agentforge.persistence.run_leases import RunLeaseStore
from agentforge.persistence.source_revisions import WorkspaceDigester
from agentforge.persistence.tables import RunRow
from agentforge.persistence.test_execution_workflow import (
    TestExecutionWorkflow as ManagedExecutionWorkflow,
)
from agentforge.persistence.test_executions import (
    ProcessExecutionRepository,
)
from agentforge.persistence.test_executions import (
    TestApprovalBindingRepository as ManagedApprovalBindingRepository,
)
from agentforge.persistence.verification_capsules import (
    VerificationCapsule,
    VerificationCapsuleBuilder,
)
from agentforge.policy.engine import PolicyEngine
from agentforge.policy.repair import RepairPolicyEnforcer
from agentforge.policy.sensitive import SensitiveFilePolicy
from agentforge.process.base import SupervisorOutcome, SupervisorStatus
from agentforge.process.streaming import CapturedStream
from agentforge.runtime.engine import AgentRuntime
from agentforge.runtime.repair import RepairCoordinator
from agentforge.runtime.test_execution import (
    TestExecutionCoordinator as ManagedExecutionCoordinator,
)
from agentforge.tools.executor import ToolExecutor
from agentforge.tools.paths import WorkspacePathResolver
from agentforge.tools.registry import ToolRegistry
from agentforge.tools.testing.profiles import (
    TestProfileDefinition as ManagedProfileDefinition,
)
from agentforge.tools.testing.profiles import (
    TestProfileRegistry as ManagedProfileRegistry,
)
from agentforge.tools.testing.run_tests import RunTestsTool

SHA = "a" * 64
HIDDEN_OUTPUT = "hidden_test_name and secret assertion detail"


class FinalSupervisor:
    def __init__(
        self, exit_code: int, *, on_run: Callable[[], None] | None = None
    ) -> None:
        self.exit_code = exit_code
        self.calls = 0
        self._on_run = on_run
        self.last_profile: object | None = None

    def run(self, profile: object) -> SupervisorOutcome:
        self.calls += 1
        self.last_profile = profile
        if self._on_run is not None:
            self._on_run()
        stdout = HIDDEN_OUTPUT.encode()
        return SupervisorOutcome(
            status=SupervisorStatus.EXITED,
            root_pid=101,
            process_group_id=None,
            job_id="final-job",
            exit_code=self.exit_code,
            stdout=CapturedStream(
                retained_bytes=stdout,
                summary=HIDDEN_OUTPUT,
                sha256_digest=hashlib.sha256(stdout).hexdigest(),
                size=len(stdout),
                truncated=False,
            ),
            stderr=CapturedStream(
                retained_bytes=b"",
                summary="",
                sha256_digest=hashlib.sha256(b"").hexdigest(),
                size=0,
                truncated=False,
            ),
            duration_ms=5,
            termination_reason=None,
            termination_result="natural_exit",
            termination_confirmed=True,
        )

    def cancel(self, reason: str = "cancelled") -> bool:
        return True


class ReplaceExecutableBeforeLaunch(VerificationCapsuleBuilder):
    def __init__(self, executable: Path) -> None:
        super().__init__(executable.parent / ".capsules")
        self._executable = executable

    def launch_profile(
        self, profile: object, capsule: VerificationCapsule
    ) -> object:
        self._executable.write_bytes(b"replaced after trust")
        return super().launch_profile(profile, capsule)  # type: ignore[arg-type,return-value]


class SlowCapsuleBuilder(VerificationCapsuleBuilder):
    def __init__(self, store_root: Path) -> None:
        super().__init__(store_root)
        self.thread_ids: list[int] = []

    def capture(self, **kwargs: object) -> VerificationCapsule:
        self.thread_ids.append(get_ident())
        time.sleep(0.15)
        return super().capture(**kwargs)  # type: ignore[arg-type]

    def verify(self, capsule: VerificationCapsule) -> None:
        self.thread_ids.append(get_ident())
        time.sleep(0.15)
        super().verify(capsule)


def task_policy() -> RepairTaskPolicy:
    return RepairTaskPolicy(
        task_id="final-verification",
        policy_version=1,
        difficulty=RepairDifficulty.BASIC,
        budget_profile=BudgetProfile.BASIC,
        allowed_write_paths=("src/**",),
        protected_paths=("tests/**",),
        allowed_development_test_profiles=("unit",),
        final_verification_profile_id="hidden",
        allow_file_creation=False,
        max_created_files=0,
        max_changed_files=2,
        max_total_changed_bytes=1024,
        max_single_file_changed_bytes=1024,
        path_case_sensitive=False,
    )


def development_result(run_id: object) -> ProcessExecutionRecord:
    from uuid import UUID

    assert isinstance(run_id, UUID)
    now = utc_now()
    return ProcessExecutionRecord(
        run_id=run_id,
        approval_id=uuid4(),
        tool_call_digest=SHA,
        attempt_number=1,
        profile_id="unit",
        profile_version=1,
        profile_digest=SHA,
        executable_path=str(Path(sys.executable).resolve()),
        argv_digest=SHA,
        cwd=str(Path(tempfile.gettempdir()).resolve()),
        environment_digest=SHA,
        status=ProcessExecutionStatus.COMPLETED,
        exit_code=0,
        stdout_digest=SHA,
        stderr_digest=SHA,
        created_at=now,
        updated_at=now,
    )


def build_runtime(
    tmp_path: Path,
    exit_code: int,
    *,
    trust_profiles: bool = True,
    hidden_trust_purpose: ProfilePurpose = ProfilePurpose.VERIFICATION,
    hidden_definition_purpose: ProfilePurpose = ProfilePurpose.VERIFICATION,
    digester: WorkspaceDigester | None = None,
    executable: Path | None = None,
    capsule_builder: VerificationCapsuleBuilder | None = None,
) -> tuple[
    AgentRuntime,
    RepairWorkflow,
    EventRepository,
    CheckpointRepository,
    FinalSupervisor,
    EvaluationWorkspaceHandle,
    Database,
    UUID,
]:
    workspace = tmp_path / "workspace"
    verifier = tmp_path / "verifier"
    (workspace / "src").mkdir(parents=True)
    verifier.mkdir()
    (verifier / "hidden_test.py").write_text("# hidden\n", encoding="utf-8")
    (workspace / "src" / "module.py").write_text("value = 1\n", encoding="utf-8")
    resolver = WorkspacePathResolver(workspace)
    baseline = WorkspaceBaselineBuilder(resolver).build(task_id="final-verification")
    database = Database.from_path(tmp_path / "final.db")
    database.create_schema()
    runs = RunRepository(database)
    events = LegacyEvaluatorEventRepository(database)
    checkpoints = CheckpointRepository(database)
    approvals = ApprovalRepository(database)
    repair_workflow = RepairWorkflow(database)
    workspace_repository = EvaluationWorkspaceRepository(database)
    workspace_repository.save_baseline(baseline)
    repair = RepairCoordinator(
        repair_workflow,
        workspace_repository=workspace_repository,
        diff_validator=WorkspaceDiffValidator(resolver),
    )
    profiles = ManagedProfileRegistry(resolver)
    for profile_id in ("unit", "hidden"):
        profiles.register(
            ManagedProfileDefinition(
                profile_id=profile_id,
                name=profile_id,
                description=f"{profile_id} profile",
                executable=str(executable) if executable is not None else sys.executable,
                argv=(
                    ("-m", "pytest", "{VERIFIER}")
                    if profile_id == "hidden"
                    else ("-c", "print('test')")
                ),
                cwd=".",
                allowed_env={},
                timeout_seconds=10,
                max_output_bytes=4096,
                profile_version=1,
                purpose=(
                    hidden_definition_purpose
                    if profile_id == "hidden"
                    else ProfilePurpose.DEVELOPMENT
                ),
                verifier_root=(str(verifier) if profile_id == "hidden" else None),
            )
        )
    if trust_profiles:
        profile_kernel = ProfileKernel(database, profiles)
        for profile_id, purpose in (
            ("unit", ProfilePurpose.DEVELOPMENT),
            ("hidden", hidden_trust_purpose),
        ):
            registered_purpose = profiles.get(profile_id).purpose
            profile_kernel.trust(
                profile_kernel.challenge(profile_id, purpose=registered_purpose),
                command_id=uuid4(),
            )
            if purpose is not registered_purpose:
                with database.session() as session:
                    row = session.query(TrustedProfileRow).filter_by(
                        workspace_identity=profiles.workspace_identity,
                        profile_id=profile_id,
                    ).one()
                    row.purpose = purpose.value
    supervisor = FinalSupervisor(exit_code)
    test_coordinator = ManagedExecutionCoordinator(
        ManagedApprovalBindingRepository(database),
        ProcessExecutionRepository(database),
        ManagedExecutionWorkflow(database),
        profiles,
        supervisor_factory=lambda: supervisor,
        capsule_builder=capsule_builder,
    )
    if digester is not None:
        test_coordinator._digester = digester
    executor = ToolExecutor(
        ToolRegistry([RunTestsTool(profiles)]),
        PolicyEngine(resolver, SensitiveFilePolicy()),
        events,
        runs,
        repair_guard=RepairPolicyEnforcer(repair_workflow),
    )
    runtime = AgentRuntime(
        runs,
        events,
        checkpoints,
        MockModelProvider([{"type": "final", "answer": "verified repair"}]),
        executor,
        approval_repository=approvals,
        approval_workflow=ApprovalWorkflow(database),
        test_execution_coordinator=test_coordinator,
        repair_coordinator=repair,
    )
    run = runtime.create_run("repair fixture")
    leases = RunLeaseStore(database)
    lease = leases.acquire(
        run.run_id, owner_id="final-verification-setup", ttl=timedelta(seconds=30)
    )
    repair_workflow._start(
        run.run_id,
        task_policy(),
        baseline.baseline_id,
        baseline.root_digest,
        authority=lease.authority,
    )
    repair.observe_test(development_result(run.run_id), authority=lease.authority)
    leases.release(lease.authority)
    return (
        runtime,
        repair_workflow,
        events,
        checkpoints,
        supervisor,
        EvaluationWorkspaceHandle.create(
            workspace,
            run_id=run.run_id,
            policy_digest=task_policy().policy_digest,
        ),
        database,
        run.run_id,
    )


def bind_current_source(
    database: Database, *, run_id: UUID, workspace: Path
) -> str:
    source_digest = WorkspaceDigester().digest(workspace)
    now = utc_now()
    with database.session() as session:
        session.add(
            WorkspaceSourceBindingRow(
                run_id=str(run_id),
                workspace_root_identity=str(workspace),
                git_head=None,
                initial_source_digest=source_digest,
                expected_source_digest=source_digest,
                source_revision_number=0,
                digest_algorithm_version=1,
                config_digest=SHA,
                profile_digest=SHA,
                created_at=now,
                updated_at=now,
            )
        )
    return source_digest


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exit_code", "run_status", "repair_status"),
    [
        (0, RunStatus.COMPLETED, RepairCompletionStatus.VERIFIED_SUCCESS),
        (1, RunStatus.FAILED, RepairCompletionStatus.FINAL_VERIFICATION_FAILED),
    ],
)
async def test_hidden_final_verification_uses_m6_chain_and_never_leaks_output(
    tmp_path: Path,
    exit_code: int,
    run_status: RunStatus,
    repair_status: RepairCompletionStatus,
) -> None:
    (
        runtime,
        workflow,
        events,
        checkpoints,
        supervisor,
        handle,
        database,
        run_id,
    ) = build_runtime(tmp_path, exit_code)
    bind_current_source(database, run_id=run_id, workspace=Path(handle.workspace_root))
    summaries = runtime.list_test_profiles()
    assert all("argv" not in item.model_dump_json() for item in summaries)
    with pytest.raises(ValueError):
        EvaluationWorkspaceHandle.create(
            Path.cwd(),
            run_id=run_id,
            policy_digest=workflow.get_policy(run_id).policy_digest,
        )

    paused = await runtime.execute(run_id)
    pending = runtime.list_pending_approvals(run_id)

    assert paused.status is RunStatus.WAITING_APPROVAL
    assert len(pending) == 1
    assert pending[0].tool_name == "run_tests"
    assert pending[0].sanitized_arguments == {"profile_id": "hidden"}

    finished = await AutoApprovalHarness(runtime, events, handle, workflow).process(run_id)

    assert finished.status is run_status
    assert workflow.get_state(run_id).status is repair_status
    assert supervisor.calls == 1
    executions = runtime.list_process_executions(run_id)
    assert len(executions) == 1
    serialized_events = " ".join(
        event.model_dump_json() for event in events.list_for_run(run_id)
    )
    serialized_checkpoint = checkpoints.latest(run_id).model_dump_json()  # type: ignore[union-attr]
    assert HIDDEN_OUTPUT not in serialized_events
    assert HIDDEN_OUTPUT not in serialized_checkpoint
    assert "repair_feedback" not in serialized_checkpoint
    if exit_code == 0:
        source_digest = WorkspaceDigester().digest(Path(handle.workspace_root))
        assert executions[0].source_digest_at_start == source_digest
        assert executions[0].capsule_state is VerificationCapsuleState.SEALED
        assert executions[0].source_snapshot_digest == source_digest
        assert executions[0].verifier_artifact_digest is not None
        assert executions[0].executable_artifact_digest is not None
        assert executions[0].runtime_trust_class is RuntimeTrustClass.NON_HERMETIC

    with database.session() as session:
        row = session.get(RunRow, str(run_id))
        assert row is not None
        row.status = RunStatus.PAUSED.value
    recovered = await runtime.resume(run_id)

    assert recovered.status is run_status
    assert supervisor.calls == 1
    assert len(runtime.list_process_executions(run_id)) == 1
    with pytest.raises(ResumeNotAllowedError):
        await runtime.resume(run_id)


@pytest.mark.asyncio
async def test_final_verification_attests_snapshot_when_live_source_changes_after_capture(
    tmp_path: Path,
) -> None:
    runtime, workflow, events, _, supervisor, handle, database, run_id = build_runtime(
        tmp_path, 0
    )
    workspace = Path(handle.workspace_root)
    expected = bind_current_source(database, run_id=run_id, workspace=workspace)
    supervisor._on_run = lambda: (workspace / "src" / "module.py").write_text(
        "value = 2\n", encoding="utf-8"
    )

    await runtime.execute(run_id)
    finished = await AutoApprovalHarness(runtime, events, handle, workflow).process(run_id)

    record = runtime.list_process_executions(run_id)[0]
    assert record.status is ProcessExecutionStatus.COMPLETED
    assert record.source_snapshot_digest == expected
    assert record.capsule_state is VerificationCapsuleState.SEALED
    assert workflow.get_state(run_id).status is RepairCompletionStatus.VERIFIED_SUCCESS
    assert finished.status is RunStatus.COMPLETED
    database.close()


@pytest.mark.asyncio
async def test_final_verification_detects_sealed_source_mutation_during_process(
    tmp_path: Path,
) -> None:
    runtime, workflow, events, _, supervisor, handle, database, run_id = build_runtime(
        tmp_path, 0
    )
    expected = bind_current_source(
        database, run_id=run_id, workspace=Path(handle.workspace_root)
    )

    def mutate_capsule() -> None:
        profile = supervisor.last_profile
        assert profile is not None
        target = Path(profile.cwd) / "src" / "module.py"  # type: ignore[attr-defined]
        target.chmod(0o600)
        target.write_text("value = 99\n", encoding="utf-8")

    supervisor._on_run = mutate_capsule
    await runtime.execute(run_id)
    finished = await AutoApprovalHarness(runtime, events, handle, workflow).process(run_id)

    record = runtime.list_process_executions(run_id)[0]
    assert record.source_snapshot_digest == expected
    assert record.status is ProcessExecutionStatus.INDETERMINATE
    assert workflow.get_state(run_id).status is RepairCompletionStatus.INDETERMINATE
    assert finished.status is not RunStatus.COMPLETED
    database.close()


@pytest.mark.asyncio
async def test_final_verification_revalidates_executable_immediately_before_launch(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "runner.bin"
    executable.write_bytes(b"trusted executable")
    capsules = ReplaceExecutableBeforeLaunch(executable)
    runtime, workflow, events, _, supervisor, handle, database, run_id = build_runtime(
        tmp_path, 0, executable=executable, capsule_builder=capsules
    )
    bind_current_source(database, run_id=run_id, workspace=Path(handle.workspace_root))

    await runtime.execute(run_id)
    finished = await AutoApprovalHarness(runtime, events, handle, workflow).process(run_id)

    record = runtime.list_process_executions(run_id)[0]
    assert supervisor.calls == 0
    assert record.status is ProcessExecutionStatus.INDETERMINATE
    assert record.failure_kind is ProcessFailureKind.INDETERMINATE
    assert workflow.get_state(run_id).status is RepairCompletionStatus.INDETERMINATE
    assert finished.status is not RunStatus.COMPLETED
    database.close()


@pytest.mark.asyncio
async def test_slow_source_digests_do_not_starve_run_lease_heartbeats(
    tmp_path: Path,
) -> None:
    capsules = SlowCapsuleBuilder(tmp_path / ".slow-capsules")
    event_loop_thread = get_ident()
    runtime, workflow, events, _, supervisor, handle, database, run_id = build_runtime(
        tmp_path, 0, capsule_builder=capsules
    )
    bind_current_source(database, run_id=run_id, workspace=Path(handle.workspace_root))
    runtime._lease_ttl = timedelta(seconds=30)
    runtime._heartbeat_interval = timedelta(milliseconds=100)
    await runtime.execute(run_id)
    with database.session() as session:
        before = session.get(RunLeaseRow, str(run_id))
        assert before is not None
        baseline_version = before.version

    finished = await AutoApprovalHarness(runtime, events, handle, workflow).process(run_id)

    with database.session() as session:
        after = session.get(RunLeaseRow, str(run_id))
        assert after is not None
        assert after.version >= baseline_version + 3
    assert finished.status is RunStatus.COMPLETED
    assert supervisor.calls == 1
    assert len(capsules.thread_ids) >= 3
    assert all(thread_id != event_loop_thread for thread_id in capsules.thread_ids)
    database.close()


@pytest.mark.asyncio
async def test_slow_executable_hashes_do_not_starve_run_lease_heartbeats(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    thread_ids: list[int] = []
    event_loop_thread = get_ident()
    original = ManagedProfileRegistry.executable_digest

    def slow_digest(
        registry: ManagedProfileRegistry, profile: object
    ) -> str:
        thread_ids.append(get_ident())
        time.sleep(0.15)
        return original(registry, profile)  # type: ignore[arg-type]

    monkeypatch.setattr(ManagedProfileRegistry, "executable_digest", slow_digest)
    runtime, workflow, events, _, supervisor, handle, database, run_id = build_runtime(
        tmp_path, 0
    )
    bind_current_source(database, run_id=run_id, workspace=Path(handle.workspace_root))
    await runtime.execute(run_id)
    runtime._lease_ttl = timedelta(seconds=30)
    runtime._heartbeat_interval = timedelta(milliseconds=100)
    thread_ids.clear()
    with database.session() as session:
        before = session.get(RunLeaseRow, str(run_id))
        assert before is not None
        baseline_version = before.version

    finished = await AutoApprovalHarness(runtime, events, handle, workflow).process(run_id)

    with database.session() as session:
        after = session.get(RunLeaseRow, str(run_id))
        assert after is not None
        assert after.version >= baseline_version + 3
    assert finished.status is RunStatus.COMPLETED
    assert supervisor.calls == 1
    assert len(thread_ids) >= 2
    assert all(thread_id != event_loop_thread for thread_id in thread_ids)
    database.close()


@pytest.mark.asyncio
async def test_final_verification_rejects_source_change_before_launch(
    tmp_path: Path,
) -> None:
    runtime, workflow, events, _, supervisor, handle, database, run_id = build_runtime(
        tmp_path, 0
    )
    workspace = Path(handle.workspace_root)
    bind_current_source(database, run_id=run_id, workspace=workspace)
    await runtime.execute(run_id)
    (workspace / "src" / "module.py").write_text("value = 3\n", encoding="utf-8")

    finished = await AutoApprovalHarness(runtime, events, handle, workflow).process(run_id)

    record = runtime.list_process_executions(run_id)[0]
    assert supervisor.calls == 0
    assert record.status is ProcessExecutionStatus.INDETERMINATE
    assert record.termination_result == "SOURCE_REVISION_MISMATCH"
    assert workflow.get_state(run_id).status is RepairCompletionStatus.INDETERMINATE
    assert finished.status is not RunStatus.COMPLETED
    database.close()


@pytest.mark.asyncio
async def test_final_verification_requires_durable_profile_trust(
    tmp_path: Path,
) -> None:
    runtime, workflow, events, _, supervisor, handle, database, run_id = build_runtime(
        tmp_path, 0, trust_profiles=False
    )
    bind_current_source(
        database, run_id=run_id, workspace=Path(handle.workspace_root)
    )
    await runtime.execute(run_id)

    finished = await AutoApprovalHarness(runtime, events, handle, workflow).process(run_id)

    record = runtime.list_process_executions(run_id)[0]
    assert supervisor.calls == 0
    assert record.status is ProcessExecutionStatus.FAILED
    assert record.failure_kind is ProcessFailureKind.PROFILE_MISMATCH
    assert finished.status is RunStatus.FAILED
    database.close()


@pytest.mark.asyncio
async def test_final_verification_rejects_development_purpose_trust(
    tmp_path: Path,
) -> None:
    runtime, workflow, events, _, supervisor, handle, database, run_id = build_runtime(
        tmp_path,
        0,
        hidden_trust_purpose=ProfilePurpose.DEVELOPMENT,
    )
    bind_current_source(
        database, run_id=run_id, workspace=Path(handle.workspace_root)
    )
    await runtime.execute(run_id)

    finished = await AutoApprovalHarness(runtime, events, handle, workflow).process(run_id)

    record = runtime.list_process_executions(run_id)[0]
    assert supervisor.calls == 0
    assert record.status is ProcessExecutionStatus.FAILED
    assert record.failure_kind is ProcessFailureKind.PROFILE_MISMATCH
    assert workflow.get_state(run_id).status is RepairCompletionStatus.FINAL_VERIFICATION_FAILED
    assert finished.status is RunStatus.FAILED
    database.close()


@pytest.mark.asyncio
async def test_final_verification_without_source_binding_fails_closed(
    tmp_path: Path,
) -> None:
    runtime, workflow, events, _, supervisor, handle, database, run_id = build_runtime(
        tmp_path, 0
    )
    await runtime.execute(run_id)

    finished = await AutoApprovalHarness(runtime, events, handle, workflow).process(run_id)

    record = runtime.list_process_executions(run_id)[0]
    assert supervisor.calls == 0
    assert record.status is ProcessExecutionStatus.INDETERMINATE
    assert record.termination_result == "SOURCE_REVISION_MISMATCH"
    assert workflow.get_state(run_id).status is RepairCompletionStatus.INDETERMINATE
    assert finished.status is not RunStatus.COMPLETED
    database.close()


def test_product_source_binding_lookup_rejects_missing_binding(tmp_path: Path) -> None:
    runtime, _, _, _, _, _, database, run_id = build_runtime(tmp_path, 0)
    del runtime

    with pytest.raises(SourceRevisionConflictError):
        ManagedExecutionWorkflow(database).source_binding(run_id)

    database.close()


@pytest.mark.asyncio
async def test_final_verification_rejects_development_profile_label(
    tmp_path: Path,
) -> None:
    runtime, workflow, events, _, supervisor, handle, database, run_id = build_runtime(
        tmp_path,
        0,
        hidden_trust_purpose=ProfilePurpose.DEVELOPMENT,
        hidden_definition_purpose=ProfilePurpose.DEVELOPMENT,
    )
    bind_current_source(
        database, run_id=run_id, workspace=Path(handle.workspace_root)
    )
    await runtime.execute(run_id)

    finished = await AutoApprovalHarness(runtime, events, handle, workflow).process(run_id)

    record = runtime.list_process_executions(run_id)[0]
    assert supervisor.calls == 0
    assert record.status is ProcessExecutionStatus.FAILED
    assert record.failure_kind is ProcessFailureKind.PROFILE_MISMATCH
    assert workflow.get_state(run_id).status is RepairCompletionStatus.FINAL_VERIFICATION_FAILED
    assert finished.status is RunStatus.FAILED
    database.close()
