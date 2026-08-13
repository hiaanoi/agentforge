from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from agentforge.domain.models import Run
from agentforge.domain.repair import (
    BudgetProfile,
    CompletionCorrectionMode,
    RepairCompletionStatus,
    RepairDifficulty,
)
from agentforge.evaluation.models import RepairEvaluationRun
from agentforge.evaluation.persistence import EvaluationRunRepository
from agentforge.evaluation.prompts import build_prompt_bundle
from agentforge.evaluation.task_definition import EvaluationTaskDefinition
from agentforge.persistence.database import Database
from agentforge.persistence.repositories import RunRepository

SHA = "a" * 64


def task_definition(fixture_path: Path) -> EvaluationTaskDefinition:
    return EvaluationTaskDefinition(
        task_id="synthetic-fix",
        title="Synthetic repair",
        description="Repair the synthetic behavior without changing tests.",
        fixture_path=str(fixture_path),
        difficulty=RepairDifficulty.BASIC,
        difficulty_rationale="One bounded source edit.",
        budget_profile=BudgetProfile.BASIC,
        allowed_write_paths=("src/**",),
        forbidden_write_paths=("src/generated/**",),
        protected_paths=("tests/**", "pyproject.toml"),
        allow_file_creation=False,
        allowed_create_paths=(),
        max_created_files=0,
        max_changed_files=2,
        max_total_changed_bytes=2048,
        max_single_file_changed_bytes=2048,
        development_test_profile_id="unit",
        final_verification_profile_id="hidden",
        success_conditions=("Allowed tests pass.", "Final verification passes."),
        provenance={"kind": "synthetic", "license": "internal-test"},
    )


def evaluation_run(run_id: object, *, repetition_index: int = 0) -> RepairEvaluationRun:
    from uuid import UUID

    assert isinstance(run_id, UUID)
    return RepairEvaluationRun(
        protocol_digest=SHA,
        campaign_id=uuid4(),
        slot_id=uuid4(),
        attempt_id=uuid4(),
        attempt_number=1,
        task_id="synthetic-fix",
        repetition_index=repetition_index,
        model_id="mock",
        model_parameters_digest=SHA,
        system_prompt_digest=SHA,
        task_prompt_digest=SHA,
        tool_schema_digest=SHA,
        context_policy_version=1,
        initial_workspace_digest=SHA,
        task_policy_digest=SHA,
        budget_profile=BudgetProfile.BASIC,
        completion_correction_mode=CompletionCorrectionMode.DEFAULT,
        run_id=run_id,
        final_status=RepairCompletionStatus.VERIFIED_SUCCESS,
        verified_success=True,
        model_calls=3,
        read_calls=2,
        edit_attempts=1,
        test_runs=2,
        completion_corrections=0,
        policy_violations=0,
        wall_time_ms=120,
        token_usage=50,
        final_workspace_digest=SHA,
        final_diff_digest=SHA,
        development_test_execution_id=uuid4(),
        final_verification_execution_id=uuid4(),
    )


def test_task_definition_builds_deterministic_policy_and_safe_prompt(tmp_path: Path) -> None:
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    task = task_definition(fixture)

    first = task.to_policy(path_case_sensitive=False)
    second = task.to_policy(path_case_sensitive=False)
    prompts = build_prompt_bundle(task, first, tool_schema={"tools": ["read_file"]})

    assert first.policy_digest == second.policy_digest
    assert first.allowed_development_test_profiles == ("unit",)
    assert "BASIC" not in prompts.task_prompt
    assert "difficulty" not in prompts.task_prompt.lower()
    assert "budget_profile" not in prompts.task_prompt
    assert len(prompts.system_prompt_digest) == 64
    assert len(prompts.task_prompt_digest) == 64
    assert len(prompts.tool_schema_digest) == 64


def test_evaluation_run_is_immutable_and_replacement_cannot_reference_itself(
    tmp_path: Path,
) -> None:
    database = Database.from_path(tmp_path / "evaluation.db")
    database.create_schema()
    run = RunRepository(database).create(Run(task="repair"))
    record = evaluation_run(run.run_id)

    with pytest.raises(ValidationError):
        record.model_calls = 9
    with pytest.raises(ValidationError):
        RepairEvaluationRun.model_validate(
            {
                **record.model_dump(mode="json"),
                "replacement_for_evaluation_run_id": str(record.evaluation_run_id),
            }
        )


def test_evaluation_run_repository_round_trips_repetitions_and_replacements(
    tmp_path: Path,
) -> None:
    database = Database.from_path(tmp_path / "evaluation.db")
    database.create_schema()
    runs = RunRepository(database)
    first_run = runs.create(Run(task="repair one"))
    replacement_run = runs.create(Run(task="repair replacement"))
    repository = EvaluationRunRepository(database)
    first = evaluation_run(first_run.run_id)
    replacement = evaluation_run(replacement_run.run_id, repetition_index=0).model_copy(
        update={
            "attempt_number": 2,
            "replacement_for_evaluation_run_id": first.evaluation_run_id,
        }
    )

    repository.save(first)
    repository.save(replacement)

    assert repository.get(first.evaluation_run_id) == first
    assert repository.list_for_task("synthetic-fix") == [first, replacement]
