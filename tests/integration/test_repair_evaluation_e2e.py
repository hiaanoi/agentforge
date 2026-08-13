import hashlib
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from agentforge.application.contracts import ProfilePurpose
from agentforge.domain.enums import EventType, RunStatus
from agentforge.domain.repair import (
    BudgetProfile,
    CompletionCorrectionMode,
    RepairCompletionStatus,
    RepairDifficulty,
    RepairTaskPolicy,
)
from agentforge.evaluation.auto_approval import EvaluationWorkspaceHandle
from agentforge.evaluation.baseline import BaselineExecutionCoordinator
from agentforge.evaluation.baseline_models import ExpectedBaselineFailure
from agentforge.evaluation.baseline_persistence import (
    BaselineExecutionRepository,
    BaselineExecutionWorkflow,
)
from agentforge.evaluation.harness import EvaluationHarness, EvaluationRunMetadata
from agentforge.evaluation.persistence import (
    EvaluationRunRepository,
    EvaluationWorkspaceRepository,
)
from agentforge.evaluation.validators import WorkspaceDiffValidator
from agentforge.evaluation.workspace import WorkspaceBaselineBuilder
from agentforge.models.mock import MockModelProvider
from agentforge.persistence.application_uow import ApplicationUnitOfWork
from agentforge.persistence.approval_workflow import ApprovalWorkflow
from agentforge.persistence.database import Database
from agentforge.persistence.legacy_evaluator import LegacyEvaluatorEventRepository
from agentforge.persistence.mutation_workflow import MutationWorkflow
from agentforge.persistence.mutations import (
    MutationApprovalBindingRepository,
    MutationExecutionRepository,
)
from agentforge.persistence.profile_trust import ProfileKernel
from agentforge.persistence.repair_workflow import RepairWorkflow
from agentforge.persistence.repositories import (
    ApprovalRepository,
    CheckpointRepository,
    RunRepository,
)
from agentforge.persistence.source_revisions import SourceRevisionStore, WorkspaceDigester
from agentforge.persistence.test_execution_workflow import (
    TestExecutionWorkflow as ManagedTestWorkflow,
)
from agentforge.persistence.test_executions import (
    ProcessExecutionRepository,
)
from agentforge.persistence.test_executions import (
    TestApprovalBindingRepository as ManagedTestBindingRepository,
)
from agentforge.policy.engine import PolicyEngine
from agentforge.policy.repair import RepairPolicyEnforcer
from agentforge.policy.sensitive import SensitiveFilePolicy
from agentforge.process.base import SupervisorOutcome, SupervisorStatus
from agentforge.process.managed import ManagedTestExecutionCore
from agentforge.process.streaming import CapturedStream
from agentforge.runtime.engine import AgentRuntime
from agentforge.runtime.mutations import MutationCoordinator
from agentforge.runtime.repair import RepairCoordinator
from agentforge.runtime.test_execution import (
    TestExecutionCoordinator as ManagedTestCoordinator,
)
from agentforge.tools.executor import ToolExecutor
from agentforge.tools.mutation.base import MutationLimits
from agentforge.tools.mutation.edit_file import EditFileTool
from agentforge.tools.mutation.security import MutationSecurityPolicy
from agentforge.tools.paths import WorkspacePathResolver
from agentforge.tools.registry import ToolRegistry
from agentforge.tools.repository.read_file import ReadFileTool
from agentforge.tools.testing.profiles import (
    TestProfileDefinition as ManagedProfileDefinition,
)
from agentforge.tools.testing.profiles import (
    TestProfileRegistry as ManagedProfileRegistry,
)
from agentforge.tools.testing.run_tests import RunTestsTool

SHA = "c" * 64
HIDDEN_OUTPUT = "hidden assertion must remain private"


class SyntheticSupervisor:
    def __init__(self) -> None:
        self.profile_ids: list[str] = []

    def run(self, profile: object) -> SupervisorOutcome:
        profile_id = profile.profile_id  # type: ignore[attr-defined]
        self.profile_ids.append(profile_id)
        baseline = profile_id == "unit" and self.profile_ids.count("unit") == 1
        text = (
            "FAILED tests/visible/test_calc.py::test_addition - AssertionError\n"
            if baseline
            else (HIDDEN_OUTPUT if profile_id == "hidden" else "development tests passed")
        )
        encoded = text.encode()
        return SupervisorOutcome(
            status=SupervisorStatus.EXITED,
            root_pid=123,
            process_group_id=None,
            job_id=f"job-{profile_id}",
            exit_code=1 if baseline else 0,
            stdout=CapturedStream(
                retained_bytes=encoded,
                summary=text,
                sha256_digest=hashlib.sha256(encoded).hexdigest(),
                size=len(encoded),
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


def policy() -> RepairTaskPolicy:
    return RepairTaskPolicy(
        task_id="synthetic-e2e",
        policy_version=1,
        difficulty=RepairDifficulty.BASIC,
        budget_profile=BudgetProfile.BASIC,
        allowed_write_paths=("src/**",),
        protected_paths=("tests/**",),
        allowed_development_test_profiles=("unit",),
        final_verification_profile_id="hidden",
        allow_file_creation=False,
        max_created_files=0,
        max_changed_files=1,
        max_total_changed_bytes=1024,
        max_single_file_changed_bytes=1024,
        path_case_sensitive=False,
    )


@pytest.mark.asyncio
async def test_synthetic_repair_uses_existing_runtime_and_persists_evaluation_facts(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    source = workspace / "src" / "calc.py"
    source.parent.mkdir(parents=True)
    source.write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    resolver = WorkspacePathResolver(workspace)
    sensitive = SensitiveFilePolicy()
    baseline = WorkspaceBaselineBuilder(resolver).build(task_id="synthetic-e2e")
    database = Database.from_path(tmp_path / "evaluation.db")
    database.create_schema()
    runs = RunRepository(database)
    events = LegacyEvaluatorEventRepository(database)
    repairs = RepairWorkflow(database)
    workspace_repository = EvaluationWorkspaceRepository(database)
    workspace_repository.save_baseline(baseline)
    repair_coordinator = RepairCoordinator(
        repairs,
        workspace_repository=workspace_repository,
        diff_validator=WorkspaceDiffValidator(resolver),
    )
    verifier = tmp_path / "hidden-verifier"
    verifier.mkdir()
    (verifier / "test_hidden.py").write_text("def test_hidden(): assert True\n", encoding="utf-8")
    profiles = ManagedProfileRegistry(resolver)
    for profile_id in ("unit", "hidden"):
        profiles.register(
            ManagedProfileDefinition(
                profile_id=profile_id,
                name=profile_id,
                description=f"{profile_id} test profile",
                executable=sys.executable,
                argv=(
                    ("-m", "pytest", "{VERIFIER}")
                    if profile_id == "hidden"
                    else ("-c", "print('ok')")
                ),
                cwd=".",
                allowed_env={},
                timeout_seconds=10,
                max_output_bytes=4096,
                profile_version=1,
                purpose=(
                    ProfilePurpose.VERIFICATION
                    if profile_id == "hidden"
                    else ProfilePurpose.DEVELOPMENT
                ),
                verifier_root=(str(verifier) if profile_id == "hidden" else None),
            )
        )
    profile_kernel = ProfileKernel(database, profiles)
    for profile_id in ("unit", "hidden"):
        registered = profiles.get(profile_id)
        profile_kernel.trust(
            profile_kernel.challenge(profile_id, purpose=registered.purpose),
            command_id=uuid4(),
        )
    supervisor = SyntheticSupervisor()
    test_coordinator = ManagedTestCoordinator(
        ManagedTestBindingRepository(database),
        ProcessExecutionRepository(database),
        ManagedTestWorkflow(database),
        profiles,
        supervisor_factory=lambda: supervisor,
    )
    mutation_security = MutationSecurityPolicy(
        resolver,
        sensitive,
        MutationLimits(),
    )
    mutation_coordinator = MutationCoordinator(
        MutationApprovalBindingRepository(database),
        MutationExecutionRepository(database),
        MutationWorkflow(database),
        mutation_security,
    )
    executor = ToolExecutor(
        ToolRegistry(
            [
                ReadFileTool(resolver, sensitive),
                EditFileTool(mutation_security),
                RunTestsTool(profiles),
            ]
        ),
        PolicyEngine(resolver, sensitive),
        events,
        runs,
        repair_guard=RepairPolicyEnforcer(repairs),
    )
    model = MockModelProvider(
        [
            {
                "type": "tool_call",
                "tool": "read_file",
                "arguments": {"path": "src/calc.py"},
            },
            {
                "type": "tool_call",
                "tool": "edit_file",
                "arguments": {
                    "path": "src/calc.py",
                    "old_text": "return a - b",
                    "new_text": "return a + b",
                    "expected_sha256": before,
                },
            },
            {
                "type": "tool_call",
                "tool": "run_tests",
                "arguments": {"profile_id": "unit"},
            },
            {"type": "final", "answer": "repair complete"},
        ]
    )
    runtime = AgentRuntime(
        runs,
        events,
        CheckpointRepository(database),
        model,
        executor,
        approval_repository=ApprovalRepository(database),
        approval_workflow=ApprovalWorkflow(database),
        mutation_coordinator=mutation_coordinator,
        test_execution_coordinator=test_coordinator,
        repair_coordinator=repair_coordinator,
    )
    run = runtime.create_run("repair synthetic addition")
    bound_policy = policy()
    repairs._evaluator_only_start(
        run.run_id,
        bound_policy,
        baseline.baseline_id,
        baseline.root_digest,
    )
    with ApplicationUnitOfWork(database) as uow:
        SourceRevisionStore()._evaluator_formal_bootstrap(
            uow.session,
            run.run_id,
            workspace_root=workspace,
            expected_initial_digest=WorkspaceDigester().digest(workspace),
            config_digest=bound_policy.policy_digest,
            profile_digest=profiles.get("hidden").profile_digest,
        )
        uow.commit()
    handle = EvaluationWorkspaceHandle.create(
        workspace,
        run_id=run.run_id,
        policy_digest=bound_policy.policy_digest,
    )
    harness = EvaluationHarness(
        runtime,
        repairs,
        events,
        handle,
        EvaluationRunRepository(database),
        baseline_coordinator=BaselineExecutionCoordinator(
            BaselineExecutionRepository(database),
            BaselineExecutionWorkflow(database),
            profiles,
            ManagedTestExecutionCore(supervisor_factory=lambda: supervisor),
        ),
    )
    harness.baseline_coordinator.ensure_created(
        run_id=run.run_id,
        task_id="synthetic-e2e",
        workspace_baseline=baseline,
        profile_id="unit",
        expected_failure=ExpectedBaselineFailure(
            failed_node_ids=("tests/visible/test_calc.py::test_addition",)
        ),
    )

    result = await harness.execute(
        run.run_id,
        EvaluationRunMetadata(
            protocol_digest=SHA,
            campaign_id=uuid4(),
            slot_id=uuid4(),
            attempt_id=uuid4(),
            attempt_number=1,
            task_id="synthetic-e2e",
            repetition_index=0,
            model_id="mock",
            model_parameters_digest=SHA,
            system_prompt_digest=SHA,
            task_prompt_digest=SHA,
            tool_schema_digest=SHA,
            context_policy_version=1,
            initial_workspace_digest=baseline.root_digest,
            task_policy_digest=bound_policy.policy_digest,
            budget_profile=BudgetProfile.BASIC,
            completion_correction_mode=CompletionCorrectionMode.DEFAULT,
        ),
    )

    assert runs.get(run.run_id).status is RunStatus.COMPLETED
    assert result.final_status is RepairCompletionStatus.VERIFIED_SUCCESS
    assert result.verified_success is True
    assert result.model_calls == 4
    assert result.read_calls == 1
    assert result.edit_attempts == 1
    assert result.test_runs == 2
    assert (
        result.baseline_execution_id
        == harness.baseline_coordinator.get(run.run_id).baseline_execution_id
    )
    assert source.read_text(encoding="utf-8").endswith("return a + b\n")
    assert supervisor.profile_ids == ["unit", "unit", "hidden"]
    assert model.requests[0].history[0]["kind"] == "EVALUATION_BASELINE_FAILURE"
    for request in model.requests:
        runtime_items = [
            item
            for item in request.history
            if isinstance(item, dict) and item.get("kind") == "REPAIR_RUNTIME_STATE"
        ]
        assert len(runtime_items) == 1
        assert "BASIC" not in str(runtime_items[0])
    event_text = " ".join(event.model_dump_json() for event in events.list_for_run(run.run_id))
    assert EventType.EVALUATION_RUN_STARTED.value in event_text
    assert EventType.EVALUATION_RUN_COMPLETED.value in event_text
    assert EventType.EVALUATION_BASELINE_VERIFIED.value in event_text
    assert HIDDEN_OUTPUT not in event_text
