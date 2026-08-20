from typing import Literal, cast
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from agentforge.domain.enums import (
    EvaluationFailureClass,
    EvaluationOutcomeClass,
)
from agentforge.domain.repair import (
    BudgetProfile,
    CompletionCorrectionMode,
    RepairCompletionStatus,
)
from agentforge.evaluation.models import RepairEvaluationRun
from agentforge.evaluation.validators import DiffValidationResult
from agentforge.evaluation.workspace import (
    FileContentKind,
    WorkspaceBaseline,
    WorkspaceFileBaseline,
)
from agentforge.persistence.database import Database
from agentforge.persistence.tables import (
    DiffValidationResultRow,
    RepairEvaluationRunRow,
    WorkspaceBaselineFileRow,
    WorkspaceBaselineRow,
)


class EvaluationWorkspaceRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    def save_baseline(self, baseline: WorkspaceBaseline) -> WorkspaceBaseline:
        try:
            with self._database.session() as session:
                existing = session.get(WorkspaceBaselineRow, str(baseline.baseline_id))
                if existing is not None:
                    if existing.root_digest != baseline.root_digest:
                        raise RuntimeError("Workspace baseline identity conflict")
                    return self.get_baseline(baseline.baseline_id)
                session.add(
                    WorkspaceBaselineRow(
                        baseline_id=str(baseline.baseline_id),
                        task_id=baseline.task_id,
                        workspace_root=baseline.workspace_root,
                        root_digest=baseline.root_digest,
                        manifest_version=baseline.manifest_version,
                        created_at=baseline.created_at,
                    )
                )
                session.flush()
                for entry in baseline.files:
                    session.add(
                        WorkspaceBaselineFileRow(
                            file_id=str(uuid4()),
                            baseline_id=str(baseline.baseline_id),
                            relative_path=entry.relative_path,
                            sha256=entry.sha256,
                            size_bytes=entry.size_bytes,
                            file_kind=entry.file_kind,
                            executable_bit=entry.executable_bit,
                            is_symlink=entry.is_symlink,
                            is_reparse_point=entry.is_reparse_point,
                            content_kind=entry.content_kind.value,
                        )
                    )
        except IntegrityError as exc:
            raise RuntimeError("Workspace baseline could not be persisted") from exc
        return baseline

    def get_baseline(self, baseline_id: UUID) -> WorkspaceBaseline:
        with self._database.session() as session:
            row = session.get(WorkspaceBaselineRow, str(baseline_id))
            if row is None:
                raise RuntimeError(f"Workspace baseline {baseline_id} is missing")
            files = session.scalars(
                select(WorkspaceBaselineFileRow)
                .where(WorkspaceBaselineFileRow.baseline_id == str(baseline_id))
                .order_by(WorkspaceBaselineFileRow.relative_path)
            ).all()
            return WorkspaceBaseline(
                baseline_id=UUID(row.baseline_id),
                task_id=row.task_id,
                workspace_root=row.workspace_root,
                root_digest=row.root_digest,
                manifest_version=row.manifest_version,
                created_at=row.created_at,
                files=tuple(
                    WorkspaceFileBaseline(
                        relative_path=item.relative_path,
                        sha256=item.sha256,
                        size_bytes=item.size_bytes,
                        file_kind=cast(Literal["REGULAR_FILE", "SYMLINK"], item.file_kind),
                        executable_bit=item.executable_bit,
                        is_symlink=item.is_symlink,
                        is_reparse_point=item.is_reparse_point,
                        content_kind=FileContentKind(item.content_kind),
                    )
                    for item in files
                ),
            )

    def save_diff(
        self,
        run_id: UUID,
        policy_digest: str,
        result: DiffValidationResult,
    ) -> DiffValidationResult:
        with self._database.session() as session:
            existing = session.scalar(
                select(DiffValidationResultRow).where(
                    DiffValidationResultRow.run_id == str(run_id),
                    DiffValidationResultRow.baseline_digest == result.baseline_digest,
                    DiffValidationResultRow.final_workspace_digest
                    == result.final_workspace_digest,
                )
            )
            if existing is not None:
                if (
                    existing.policy_digest != policy_digest
                    or existing.diff_digest != result.diff_digest
                ):
                    raise RuntimeError("Diff validation binding conflict")
                return DiffValidationResult.model_validate(existing.result_data)
            session.add(
                DiffValidationResultRow(
                    validation_id=result.validation_id,
                    run_id=str(run_id),
                    policy_digest=policy_digest,
                    baseline_digest=result.baseline_digest,
                    final_workspace_digest=result.final_workspace_digest,
                    diff_digest=result.diff_digest,
                    compliant=result.compliant,
                    result_data=result.model_dump(mode="json"),
                    validation_version=result.validation_version,
                    created_at=result.created_at,
                )
            )
        return result

    def latest_diff(self, run_id: UUID) -> DiffValidationResult | None:
        with self._database.session() as session:
            row = session.scalar(
                select(DiffValidationResultRow)
                .where(DiffValidationResultRow.run_id == str(run_id))
                .order_by(DiffValidationResultRow.created_at.desc())
                .limit(1)
            )
            return (
                DiffValidationResult.model_validate(row.result_data)
                if row is not None
                else None
            )


class EvaluationRunRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    def save(self, record: RepairEvaluationRun) -> RepairEvaluationRun:
        with self._database.session() as session:
            existing = session.get(
                RepairEvaluationRunRow, str(record.evaluation_run_id)
            )
            if existing is not None:
                persisted = self._to_domain(existing)
                if persisted != record:
                    raise RuntimeError("Evaluation run identity conflict")
                return persisted
            session.add(self._to_row(record))
        return record

    def get(self, evaluation_run_id: UUID) -> RepairEvaluationRun:
        with self._database.session() as session:
            row = session.get(RepairEvaluationRunRow, str(evaluation_run_id))
            if row is None:
                raise RuntimeError(f"Evaluation run {evaluation_run_id} is missing")
            return self._to_domain(row)

    def list_for_task(self, task_id: str) -> list[RepairEvaluationRun]:
        with self._database.session() as session:
            rows = session.scalars(
                select(RepairEvaluationRunRow)
                .where(RepairEvaluationRunRow.task_id == task_id)
                .order_by(
                    RepairEvaluationRunRow.repetition_index,
                    RepairEvaluationRunRow.created_at,
                )
            ).all()
            return [self._to_domain(row) for row in rows]

    def list_for_campaign(self, campaign_id: UUID) -> list[RepairEvaluationRun]:
        with self._database.session() as session:
            rows = session.scalars(
                select(RepairEvaluationRunRow)
                .where(RepairEvaluationRunRow.campaign_id == str(campaign_id))
                .order_by(
                    RepairEvaluationRunRow.repetition_index,
                    RepairEvaluationRunRow.attempt_number,
                )
            ).all()
            return [self._to_domain(row) for row in rows]

    def find_for_attempt(self, attempt_id: UUID) -> RepairEvaluationRun | None:
        with self._database.session() as session:
            row = session.scalar(
                select(RepairEvaluationRunRow).where(
                    RepairEvaluationRunRow.attempt_id == str(attempt_id)
                )
            )
            return self._to_domain(row) if row is not None else None

    @staticmethod
    def _to_row(record: RepairEvaluationRun) -> RepairEvaluationRunRow:
        return RepairEvaluationRunRow(
            evaluation_run_id=str(record.evaluation_run_id),
            protocol_digest=record.protocol_digest,
            campaign_id=str(record.campaign_id),
            slot_id=str(record.slot_id),
            attempt_id=str(record.attempt_id),
            attempt_number=record.attempt_number,
            task_id=record.task_id,
            repetition_index=record.repetition_index,
            model_id=record.model_id,
            model_parameters_digest=record.model_parameters_digest,
            system_prompt_digest=record.system_prompt_digest,
            task_prompt_digest=record.task_prompt_digest,
            tool_schema_digest=record.tool_schema_digest,
            context_policy_version=record.context_policy_version,
            initial_workspace_digest=record.initial_workspace_digest,
            task_policy_digest=record.task_policy_digest,
            budget_profile=record.budget_profile.value,
            completion_correction_mode=record.completion_correction_mode.value,
            run_id=str(record.run_id),
            baseline_execution_id=(
                str(record.baseline_execution_id)
                if record.baseline_execution_id is not None
                else None
            ),
            final_status=record.final_status.value,
            verified_success=record.verified_success,
            model_calls=record.model_calls,
            read_calls=record.read_calls,
            edit_attempts=record.edit_attempts,
            test_runs=record.test_runs,
            completion_corrections=record.completion_corrections,
            policy_violations=record.policy_violations,
            wall_time_ms=record.wall_time_ms,
            token_usage=record.token_usage,
            final_workspace_digest=record.final_workspace_digest,
            final_diff_digest=record.final_diff_digest,
            development_test_execution_id=(
                str(record.development_test_execution_id)
                if record.development_test_execution_id is not None
                else None
            ),
            final_verification_execution_id=(
                str(record.final_verification_execution_id)
                if record.final_verification_execution_id is not None
                else None
            ),
            failure_category=record.failure_category,
            outcome_class=record.outcome_class.value,
            failure_class=record.failure_class.value,
            infrastructure_failure=record.infrastructure_failure,
            replacement_for_evaluation_run_id=(
                str(record.replacement_for_evaluation_run_id)
                if record.replacement_for_evaluation_run_id is not None
                else None
            ),
            created_at=record.created_at,
            completed_at=record.completed_at,
            result_schema_version=record.result_schema_version,
        )

    @staticmethod
    def _to_domain(row: RepairEvaluationRunRow) -> RepairEvaluationRun:
        return RepairEvaluationRun(
            evaluation_run_id=UUID(row.evaluation_run_id),
            protocol_digest=row.protocol_digest,
            campaign_id=UUID(row.campaign_id),
            slot_id=UUID(row.slot_id),
            attempt_id=UUID(row.attempt_id),
            attempt_number=row.attempt_number,
            task_id=row.task_id,
            repetition_index=row.repetition_index,
            model_id=row.model_id,
            model_parameters_digest=row.model_parameters_digest,
            system_prompt_digest=row.system_prompt_digest,
            task_prompt_digest=row.task_prompt_digest,
            tool_schema_digest=row.tool_schema_digest,
            context_policy_version=row.context_policy_version,
            initial_workspace_digest=row.initial_workspace_digest,
            task_policy_digest=row.task_policy_digest,
            budget_profile=BudgetProfile(row.budget_profile),
            completion_correction_mode=CompletionCorrectionMode(
                row.completion_correction_mode
            ),
            run_id=UUID(row.run_id),
            baseline_execution_id=(
                UUID(row.baseline_execution_id)
                if row.baseline_execution_id is not None
                else None
            ),
            final_status=RepairCompletionStatus(row.final_status),
            verified_success=row.verified_success,
            model_calls=row.model_calls,
            read_calls=row.read_calls,
            edit_attempts=row.edit_attempts,
            test_runs=row.test_runs,
            completion_corrections=row.completion_corrections,
            policy_violations=row.policy_violations,
            wall_time_ms=row.wall_time_ms,
            token_usage=row.token_usage,
            final_workspace_digest=row.final_workspace_digest,
            final_diff_digest=row.final_diff_digest,
            development_test_execution_id=(
                UUID(row.development_test_execution_id)
                if row.development_test_execution_id is not None
                else None
            ),
            final_verification_execution_id=(
                UUID(row.final_verification_execution_id)
                if row.final_verification_execution_id is not None
                else None
            ),
            failure_category=row.failure_category,
            outcome_class=EvaluationOutcomeClass(row.outcome_class),
            failure_class=EvaluationFailureClass(row.failure_class),
            infrastructure_failure=row.infrastructure_failure,
            replacement_for_evaluation_run_id=(
                UUID(row.replacement_for_evaluation_run_id)
                if row.replacement_for_evaluation_run_id is not None
                else None
            ),
            created_at=row.created_at,
            completed_at=row.completed_at,
            result_schema_version=row.result_schema_version,
        )
