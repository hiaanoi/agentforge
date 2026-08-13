from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

from agentforge.domain.repair import (
    BudgetProfile,
    RepairDifficulty,
    RepairTaskPolicy,
    fixed_budget,
)


class EvaluationTaskDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, gt=0)
    task_id: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=10_000)
    fixture_path: str = Field(min_length=1, max_length=4096)
    difficulty: RepairDifficulty
    difficulty_rationale: str = Field(min_length=1, max_length=1000)
    budget_profile: BudgetProfile
    allowed_write_paths: tuple[str, ...]
    forbidden_write_paths: tuple[str, ...] = ()
    protected_paths: tuple[str, ...]
    allow_file_creation: bool
    allowed_create_paths: tuple[str, ...] = ()
    max_created_files: int = Field(ge=0)
    max_changed_files: int = Field(gt=0)
    max_total_changed_bytes: int = Field(gt=0)
    max_single_file_changed_bytes: int = Field(gt=0)
    development_test_profile_id: str = Field(min_length=1, max_length=100)
    final_verification_profile_id: str = Field(min_length=1, max_length=100)
    success_conditions: tuple[str, ...] = Field(min_length=1)
    provenance: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("fixture_path")
    @classmethod
    def fixture_path_must_be_absolute(cls, value: str) -> str:
        path = Path(value)
        if not path.is_absolute():
            raise ValueError("Evaluation fixture_path must be absolute")
        return str(path.resolve())

    @field_validator("success_conditions")
    @classmethod
    def success_conditions_must_be_bounded(
        cls, value: tuple[str, ...]
    ) -> tuple[str, ...]:
        normalized = tuple(item.strip() for item in value if item.strip())
        if not normalized or any(len(item) > 1000 for item in normalized):
            raise ValueError("Success conditions must contain bounded text")
        return normalized

    @model_validator(mode="after")
    def validate_profiles(self) -> Self:
        if self.development_test_profile_id == self.final_verification_profile_id:
            raise ValueError("Final verification profile must remain hidden")
        return self

    def to_policy(self, *, path_case_sensitive: bool) -> RepairTaskPolicy:
        limits = fixed_budget(self.budget_profile)
        return RepairTaskPolicy(
            task_id=self.task_id,
            policy_version=self.schema_version,
            difficulty=self.difficulty,
            budget_profile=self.budget_profile,
            allowed_write_paths=self.allowed_write_paths,
            forbidden_write_paths=self.forbidden_write_paths,
            protected_paths=self.protected_paths,
            allowed_development_test_profiles=(self.development_test_profile_id,),
            final_verification_profile_id=self.final_verification_profile_id,
            allow_file_creation=self.allow_file_creation,
            allowed_create_paths=self.allowed_create_paths,
            max_created_files=self.max_created_files,
            max_changed_files=self.max_changed_files,
            max_total_changed_bytes=self.max_total_changed_bytes,
            max_single_file_changed_bytes=self.max_single_file_changed_bytes,
            max_model_calls=limits.max_model_calls,
            max_read_calls=limits.max_read_calls,
            max_edit_attempts=limits.max_edit_attempts,
            max_test_runs=limits.max_test_runs,
            max_completion_corrections=limits.max_completion_corrections,
            max_policy_violations=limits.max_policy_violations,
            max_wall_time_seconds=limits.max_wall_time_seconds,
            path_case_sensitive=path_case_sensitive,
        )
