import hashlib
import json
from threading import Lock
from uuid import UUID

from agentforge.context.models import ContextItem, ContextItemKind
from agentforge.domain.errors import TestProfileBindingMismatchError, ToolRuntimeError
from agentforge.evaluation.baseline_models import (
    BaselineExecutionRecord,
    BaselineExecutionStatus,
    BaselineFailureReason,
    ExpectedBaselineFailure,
)
from agentforge.evaluation.baseline_parser import PytestBaselineResultParser
from agentforge.evaluation.baseline_persistence import (
    BaselineExecutionRepository,
    BaselineExecutionWorkflow,
)
from agentforge.evaluation.workspace import WorkspaceBaseline
from agentforge.process.managed import (
    ManagedExecutionKey,
    ManagedExecutionOrigin,
    ManagedTestExecutionCore,
)
from agentforge.tools.testing.profiles import TestProfileRegistry


class BaselineExecutionCoordinator:
    def __init__(
        self,
        repository: BaselineExecutionRepository,
        workflow: BaselineExecutionWorkflow,
        profiles: TestProfileRegistry,
        managed_core: ManagedTestExecutionCore,
        *,
        parser: PytestBaselineResultParser | None = None,
    ) -> None:
        self._repository = repository
        self._workflow = workflow
        self._profiles = profiles
        self._managed = managed_core
        self._parser = parser or PytestBaselineResultParser()
        self._active_runs: set[UUID] = set()
        self._active_lock = Lock()

    @property
    def profiles(self) -> TestProfileRegistry:
        return self._profiles

    def ensure_created(
        self,
        *,
        run_id: UUID,
        task_id: str,
        workspace_baseline: WorkspaceBaseline,
        profile_id: str,
        expected_failure: ExpectedBaselineFailure,
    ) -> BaselineExecutionRecord:
        if workspace_baseline.task_id != task_id:
            raise ValueError("Workspace baseline does not belong to the evaluation task")
        test_plan = self._profiles.prepare(profile_id)
        return self._repository.create(
            BaselineExecutionRecord.create(
                run_id=run_id,
                task_id=task_id,
                workspace_baseline_id=workspace_baseline.baseline_id,
                initial_workspace_digest=workspace_baseline.root_digest,
                test_plan=test_plan,
                expected_failure=expected_failure,
            )
        )

    async def execute(self, run_id: UUID) -> BaselineExecutionRecord:
        with self._active_lock:
            if run_id in self._active_runs:
                raise RuntimeError("Baseline execution is already active")
            self._active_runs.add(run_id)
        try:
            return await self._execute_once(run_id)
        finally:
            with self._active_lock:
                self._active_runs.discard(run_id)

    async def _execute_once(self, run_id: UUID) -> BaselineExecutionRecord:
        record = self._repository.get_for_run(run_id)
        if record.status in {
            BaselineExecutionStatus.VERIFIED_EXPECTED_FAILURE,
            BaselineExecutionStatus.BLOCKED,
            BaselineExecutionStatus.INDETERMINATE,
        }:
            return record
        if record.status is BaselineExecutionStatus.STARTED:
            return self._workflow.recover(run_id)
        started = self._workflow.claim(
            run_id,
            expected_version=record.record_version,
        )
        if started is None:
            current = self._repository.get_for_run(run_id)
            if current.status is BaselineExecutionStatus.STARTED:
                raise RuntimeError("Baseline execution is already active")
            return current
        try:
            profile = self._profiles.require_plan(started.test_plan)
        except (TestProfileBindingMismatchError, ToolRuntimeError):
            return self._workflow.finish(
                run_id,
                expected_version=started.record_version,
                status=BaselineExecutionStatus.BLOCKED,
                failure_reason=BaselineFailureReason.PROFILE_BINDING_MISMATCH,
                termination_reason="test_profile_binding_mismatch",
            )
        managed = await self._managed.execute(self._key(started), profile)
        outcome = managed.outcome
        evaluation = self._parser.evaluate(outcome, started.expected_failure)
        summary_data = evaluation.safe_summary.model_dump(mode="json")
        summary_digest = hashlib.sha256(
            json.dumps(
                summary_data,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return self._workflow.finish(
            run_id,
            expected_version=started.record_version,
            status=evaluation.status,
            failure_reason=evaluation.failure_reason,
            actual_fingerprint_digest=evaluation.actual_fingerprint_digest,
            failed_node_ids=list(evaluation.failed_node_ids),
            exit_code=outcome.exit_code,
            root_pid=outcome.root_pid,
            process_group_id=outcome.process_group_id,
            job_id=outcome.job_id,
            duration_ms=outcome.duration_ms,
            termination_reason=outcome.termination_reason,
            termination_result=outcome.termination_result,
            stdout_digest=outcome.stdout.sha256_digest,
            stderr_digest=outcome.stderr.sha256_digest,
            stdout_size=outcome.stdout.size,
            stderr_size=outcome.stderr.size,
            output_truncated=(outcome.stdout.truncated or outcome.stderr.truncated),
            safe_failure_summary_data=summary_data,
            safe_failure_summary_digest=summary_digest,
        )

    def recover(self, run_id: UUID) -> BaselineExecutionRecord:
        return self._workflow.recover(run_id)

    def get(self, run_id: UUID) -> BaselineExecutionRecord:
        return self._repository.get_for_run(run_id)

    def cancel(self, run_id: UUID, reason: str) -> bool:
        record = self._repository.get_for_run(run_id)
        if record.status is not BaselineExecutionStatus.STARTED:
            return False
        return self._managed.cancel(self._key(record), reason)

    def context_item(self, run_id: UUID) -> ContextItem:
        record = self._repository.get_for_run(run_id)
        if (
            record.status is not BaselineExecutionStatus.VERIFIED_EXPECTED_FAILURE
            or record.safe_failure_summary is None
        ):
            raise RuntimeError("Only a verified baseline can enter model context")
        summary = record.safe_failure_summary
        return ContextItem(
            kind=ContextItemKind.EVALUATION_BASELINE_FAILURE,
            payload={
                "schema_version": summary.schema_version,
                "message": "Evaluator confirmed the buggy visible baseline failure.",
                "exit_code": summary.exit_code,
                "failed_node_ids": list(summary.failed_node_ids),
                "failure_count": summary.failure_count,
                "diagnostic_summary": summary.diagnostic_summary,
                "truncated": summary.truncated,
                "instruction": (
                    "Choose the read, edit, and retest sequence yourself. "
                    "Do not assume any other test or action has run."
                ),
            },
        )

    @staticmethod
    def _key(record: BaselineExecutionRecord) -> ManagedExecutionKey:
        return ManagedExecutionKey(
            origin=ManagedExecutionOrigin.EVALUATION_BASELINE,
            execution_id=str(record.baseline_execution_id),
        )
