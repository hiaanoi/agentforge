import hashlib
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from agentforge.domain.enums import RunStatus
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
from agentforge.evaluation.workspace import WorkspaceBaselineBuilder
from agentforge.models.mock import MockModelProvider
from agentforge.persistence.database import Database
from agentforge.persistence.legacy_evaluator import LegacyEvaluatorEventRepository
from agentforge.persistence.repair_workflow import RepairWorkflow
from agentforge.persistence.repositories import (
    CheckpointRepository,
    RunRepository,
)
from agentforge.policy.engine import PolicyEngine
from agentforge.policy.sensitive import SensitiveFilePolicy
from agentforge.process.base import SupervisorOutcome, SupervisorStatus
from agentforge.process.managed import ManagedTestExecutionCore
from agentforge.process.streaming import CapturedStream
from agentforge.runtime.engine import AgentRuntime
from agentforge.tools.executor import ToolExecutor
from agentforge.tools.paths import WorkspacePathResolver
from agentforge.tools.registry import ToolRegistry
from agentforge.tools.testing.profiles import TestProfileDefinition as ManagedProfileDefinition
from agentforge.tools.testing.profiles import TestProfileRegistry as ManagedProfileRegistry

SHA = "a" * 64


class PassingSupervisor:
    def run(self, profile: object) -> SupervisorOutcome:
        del profile
        data = b"1 passed in 0.01s\n"
        output = CapturedStream(
            retained_bytes=data,
            summary=data.decode(),
            sha256_digest=hashlib.sha256(data).hexdigest(),
            size=len(data),
            truncated=False,
        )
        return SupervisorOutcome(
            status=SupervisorStatus.EXITED,
            root_pid=1,
            process_group_id=None,
            job_id="baseline-pass",
            exit_code=0,
            stdout=output,
            stderr=CapturedStream(
                retained_bytes=b"",
                summary="",
                sha256_digest=hashlib.sha256(b"").hexdigest(),
                size=0,
                truncated=False,
            ),
            duration_ms=1,
            termination_reason=None,
            termination_result="natural_exit",
            termination_confirmed=True,
        )

    def cancel(self, reason: str = "cancelled") -> bool:
        del reason
        return True


@pytest.mark.asyncio
async def test_unexpected_baseline_pass_terminates_before_model_request(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "source.py").write_text("value = 1\n", encoding="utf-8")
    resolver = WorkspacePathResolver(workspace)
    baseline = WorkspaceBaselineBuilder(resolver).build(task_id="baseline-gate")
    database = Database.from_path(tmp_path / "evaluation.db")
    database.create_schema()
    runs = RunRepository(database)
    events = LegacyEvaluatorEventRepository(database)
    repairs = RepairWorkflow(database)
    EvaluationWorkspaceRepository(database).save_baseline(baseline)
    model = MockModelProvider([{"type": "final", "answer": "must not run"}])
    runtime = AgentRuntime(
        runs,
        events,
        CheckpointRepository(database),
        model,
        ToolExecutor(
            ToolRegistry([]),
            PolicyEngine(resolver, SensitiveFilePolicy()),
            events,
            runs,
        ),
    )
    run = runtime.create_run("repair")
    policy = RepairTaskPolicy(
        task_id="baseline-gate",
        policy_version=1,
        difficulty=RepairDifficulty.BASIC,
        budget_profile=BudgetProfile.BASIC,
        allowed_write_paths=("source.py",),
        protected_paths=(),
        allowed_development_test_profiles=("visible",),
        final_verification_profile_id="hidden",
        allow_file_creation=False,
        max_created_files=0,
        max_changed_files=1,
        max_total_changed_bytes=1024,
        max_single_file_changed_bytes=1024,
        path_case_sensitive=False,
    )
    repairs._evaluator_only_start(run.run_id, policy, baseline.baseline_id, baseline.root_digest)
    profiles = ManagedProfileRegistry(resolver)
    profiles.register(
        ManagedProfileDefinition(
            profile_id="visible",
            name="Visible",
            description="Visible tests",
            executable=sys.executable,
            argv=("-m", "pytest", "tests/visible", "-q"),
            cwd=".",
            allowed_env={},
            timeout_seconds=10,
            max_output_bytes=4096,
            profile_version=1,
        )
    )
    coordinator = BaselineExecutionCoordinator(
        BaselineExecutionRepository(database),
        BaselineExecutionWorkflow(database),
        profiles,
        ManagedTestExecutionCore(supervisor_factory=PassingSupervisor),
    )
    coordinator.ensure_created(
        run_id=run.run_id,
        task_id="baseline-gate",
        workspace_baseline=baseline,
        profile_id="visible",
        expected_failure=ExpectedBaselineFailure(
            failed_node_ids=("tests/visible/test_flow.py::test_once",)
        ),
    )
    harness = EvaluationHarness(
        runtime,
        repairs,
        events,
        EvaluationWorkspaceHandle.create(
            workspace,
            run_id=run.run_id,
            policy_digest=policy.policy_digest,
        ),
        EvaluationRunRepository(database),
        baseline_coordinator=coordinator,
    )

    result = await harness.execute(
        run.run_id,
        EvaluationRunMetadata(
            protocol_digest=SHA,
            campaign_id=uuid4(),
            slot_id=uuid4(),
            attempt_id=uuid4(),
            attempt_number=1,
            task_id="baseline-gate",
            repetition_index=0,
            model_id="mock",
            model_parameters_digest=SHA,
            system_prompt_digest=SHA,
            task_prompt_digest=SHA,
            tool_schema_digest=SHA,
            context_policy_version=1,
            initial_workspace_digest=baseline.root_digest,
            task_policy_digest=policy.policy_digest,
            budget_profile=BudgetProfile.BASIC,
            completion_correction_mode=CompletionCorrectionMode.DEFAULT,
        ),
    )

    assert model.requests == []
    assert runs.get(run.run_id).status is RunStatus.FAILED
    assert result.final_status is RepairCompletionStatus.RUNTIME_FAILURE
    assert result.model_calls == 0
    assert result.test_runs == 0
    assert result.baseline_execution_id == coordinator.get(run.run_id).baseline_execution_id
