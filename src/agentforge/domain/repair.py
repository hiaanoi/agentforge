import hashlib
import json
from enum import StrEnum
from fnmatch import fnmatchcase
from pathlib import PureWindowsPath
from typing import Any, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agentforge.domain.models import UtcDatetime, utc_now


class BudgetProfile(StrEnum):
    BASIC = "BASIC"
    ENGINEERING = "ENGINEERING"
    CHALLENGE = "CHALLENGE"
    SWE_BENCH_PASS1 = "SWE_BENCH_PASS1"


class RepairDifficulty(StrEnum):
    BASIC = "BASIC"
    ENGINEERING = "ENGINEERING"
    CHALLENGE = "CHALLENGE"


class BudgetKind(StrEnum):
    MODEL = "MODEL"
    READ = "READ"
    EDIT = "EDIT"
    TEST = "TEST"
    COMPLETION_CORRECTION = "COMPLETION_CORRECTION"
    POLICY_VIOLATION = "POLICY_VIOLATION"


class RepairCompletionStatus(StrEnum):
    RUNNING = "RUNNING"
    VERIFIED_SUCCESS = "VERIFIED_SUCCESS"
    TESTS_FAILED = "TESTS_FAILED"
    FINAL_VERIFICATION_FAILED = "FINAL_VERIFICATION_FAILED"
    UNVERIFIED_FINAL = "UNVERIFIED_FINAL"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    POLICY_BLOCKED = "POLICY_BLOCKED"
    DIFF_POLICY_VIOLATION = "DIFF_POLICY_VIOLATION"
    LOOP_DETECTED = "LOOP_DETECTED"
    MODEL_PROTOCOL_ERROR = "MODEL_PROTOCOL_ERROR"
    MODEL_TOOL_FAILED = "MODEL_TOOL_FAILED"
    RUNTIME_FAILURE = "RUNTIME_FAILURE"
    INDETERMINATE = "INDETERMINATE"
    CANCELLED = "CANCELLED"


class RepairTerminationReason(StrEnum):
    MODEL_CALL_LIMIT = "MODEL_CALL_LIMIT"
    READ_LIMIT = "READ_LIMIT"
    EDIT_LIMIT = "EDIT_LIMIT"
    TEST_LIMIT = "TEST_LIMIT"
    WALL_TIME_LIMIT = "WALL_TIME_LIMIT"
    COMPLETION_CORRECTION_LIMIT = "COMPLETION_CORRECTION_LIMIT"
    POLICY_VIOLATION_LIMIT = "POLICY_VIOLATION_LIMIT"
    UNSUPPORTED_CAPABILITY = "UNSUPPORTED_CAPABILITY"
    DEVELOPMENT_TEST_FAILED = "DEVELOPMENT_TEST_FAILED"
    FINAL_HIDDEN_TEST_FAILED = "FINAL_HIDDEN_TEST_FAILED"
    LATEST_MUTATION_NOT_VERIFIED = "LATEST_MUTATION_NOT_VERIFIED"
    DIFF_POLICY_VIOLATION = "DIFF_POLICY_VIOLATION"
    LOOP_DETECTED = "LOOP_DETECTED"
    MODEL_AUTH_ERROR = "MODEL_AUTH_ERROR"
    MODEL_RATE_LIMITED = "MODEL_RATE_LIMITED"
    MODEL_TIMEOUT = "MODEL_TIMEOUT"
    MODEL_TRANSPORT_ERROR = "MODEL_TRANSPORT_ERROR"
    MODEL_BAD_REQUEST = "MODEL_BAD_REQUEST"
    MODEL_PROVIDER_ERROR = "MODEL_PROVIDER_ERROR"
    MODEL_PROTOCOL_ERROR = "MODEL_PROTOCOL_ERROR"
    MODEL_TOOL_FAILED = "MODEL_TOOL_FAILED"
    RUNTIME_FAILURE = "RUNTIME_FAILURE"
    INDETERMINATE_SIDE_EFFECT = "INDETERMINATE_SIDE_EFFECT"
    CANCELLED = "CANCELLED"


class CompletionCorrectionMode(StrEnum):
    DEFAULT = "DEFAULT"
    STRICT = "STRICT"


class CompletionAction(StrEnum):
    CORRECT = "CORRECT"
    REQUEST_FINAL_VERIFICATION = "REQUEST_FINAL_VERIFICATION"
    TERMINAL = "TERMINAL"


class DiffViolationKind(StrEnum):
    FORBIDDEN_PATH_MODIFIED = "FORBIDDEN_PATH_MODIFIED"
    PROTECTED_FILE_MODIFIED = "PROTECTED_FILE_MODIFIED"
    UNAUTHORIZED_FILE_CREATED = "UNAUTHORIZED_FILE_CREATED"
    FILE_DELETED = "FILE_DELETED"
    FILE_RENAMED = "FILE_RENAMED"
    SYMLINK_OR_REPARSE_CREATED = "SYMLINK_OR_REPARSE_CREATED"
    SYMLINK_OR_REPARSE_CHANGED = "SYMLINK_OR_REPARSE_CHANGED"
    FILE_TYPE_CHANGED = "FILE_TYPE_CHANGED"
    CHANGESET_TOO_LARGE = "CHANGESET_TOO_LARGE"
    TOO_MANY_FILES_CHANGED = "TOO_MANY_FILES_CHANGED"
    SINGLE_FILE_CHANGE_TOO_LARGE = "SINGLE_FILE_CHANGE_TOO_LARGE"
    SENSITIVE_FILE_TOUCHED = "SENSITIVE_FILE_TOUCHED"
    TEST_INFRASTRUCTURE_MODIFIED = "TEST_INFRASTRUCTURE_MODIFIED"
    SUSPICIOUS_TEST_BYPASS = "SUSPICIOUS_TEST_BYPASS"
    BASELINE_MISMATCH = "BASELINE_MISMATCH"
    EXTERNAL_WORKSPACE_CHANGE = "EXTERNAL_WORKSPACE_CHANGE"


class SuspiciousFindingKind(StrEnum):
    TEST_SKIP = "TEST_SKIP"
    TEST_XFAIL = "TEST_XFAIL"
    TEST_ENVIRONMENT_BRANCH = "TEST_ENVIRONMENT_BRANCH"
    SYS_PATH_MANIPULATION = "SYS_PATH_MANIPULATION"
    SITE_CUSTOMIZATION = "SITE_CUSTOMIZATION"
    DYNAMIC_TEST_IMPORT = "DYNAMIC_TEST_IMPORT"
    FIXTURE_SPECIFIC_BRANCH = "FIXTURE_SPECIFIC_BRANCH"


class RepairBudgetLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_model_calls: int = Field(gt=0)
    max_read_calls: int = Field(gt=0)
    max_edit_attempts: int = Field(gt=0)
    max_test_runs: int = Field(gt=0)
    max_completion_corrections: int = Field(ge=0)
    max_policy_violations: int = Field(gt=0)
    max_wall_time_seconds: int = Field(gt=0)


_FIXED_BUDGETS: dict[BudgetProfile, RepairBudgetLimits] = {
    BudgetProfile.BASIC: RepairBudgetLimits(
        max_model_calls=6,
        max_read_calls=20,
        max_edit_attempts=2,
        max_test_runs=3,
        max_completion_corrections=1,
        max_policy_violations=2,
        max_wall_time_seconds=300,
    ),
    BudgetProfile.ENGINEERING: RepairBudgetLimits(
        max_model_calls=10,
        max_read_calls=35,
        max_edit_attempts=4,
        max_test_runs=5,
        max_completion_corrections=1,
        max_policy_violations=2,
        max_wall_time_seconds=600,
    ),
    BudgetProfile.CHALLENGE: RepairBudgetLimits(
        max_model_calls=14,
        max_read_calls=50,
        max_edit_attempts=6,
        max_test_runs=7,
        max_completion_corrections=1,
        max_policy_violations=2,
        max_wall_time_seconds=900,
    ),
    BudgetProfile.SWE_BENCH_PASS1: RepairBudgetLimits(
        max_model_calls=50,
        max_read_calls=80,
        max_edit_attempts=8,
        max_test_runs=8,
        max_completion_corrections=2,
        max_policy_violations=3,
        max_wall_time_seconds=1800,
    ),
}


def fixed_budget(profile: BudgetProfile) -> RepairBudgetLimits:
    return _FIXED_BUDGETS[profile].model_copy(deep=True)


_BUDGET_FIELDS = tuple(RepairBudgetLimits.model_fields)
_PATH_FIELDS = (
    "allowed_write_paths",
    "forbidden_write_paths",
    "protected_paths",
    "allowed_create_paths",
)


class RepairTaskPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str = Field(min_length=1, max_length=200)
    policy_version: int = Field(gt=0)
    policy_digest: str = ""
    difficulty: RepairDifficulty
    budget_profile: BudgetProfile
    allowed_write_paths: tuple[str, ...]
    forbidden_write_paths: tuple[str, ...] = ()
    protected_paths: tuple[str, ...]
    allowed_development_test_profiles: tuple[str, ...]
    final_verification_profile_id: str = Field(min_length=1, max_length=100)
    allow_file_creation: bool = False
    allowed_create_paths: tuple[str, ...] = ()
    max_created_files: int = Field(ge=0)
    max_changed_files: int = Field(gt=0)
    max_total_changed_bytes: int = Field(gt=0)
    max_single_file_changed_bytes: int = Field(gt=0)
    max_model_calls: int = Field(gt=0)
    max_read_calls: int = Field(gt=0)
    max_edit_attempts: int = Field(gt=0)
    max_test_runs: int = Field(gt=0)
    max_completion_corrections: int = Field(ge=0)
    max_policy_violations: int = Field(gt=0)
    max_wall_time_seconds: int = Field(gt=0)
    path_case_sensitive: bool

    @model_validator(mode="before")
    @classmethod
    def bind_fixed_budget(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        raw_profile = data.get("budget_profile")
        try:
            profile = (
                raw_profile
                if isinstance(raw_profile, BudgetProfile)
                else BudgetProfile(str(raw_profile))
            )
        except (TypeError, ValueError):
            return data
        limits = fixed_budget(profile)
        for field_name in _BUDGET_FIELDS:
            expected = getattr(limits, field_name)
            if field_name in data and data[field_name] != expected:
                raise ValueError(f"{field_name} does not match the {profile.value} fixed budget")
            data[field_name] = expected
        return data

    @field_validator(*_PATH_FIELDS, mode="before")
    @classmethod
    def normalize_patterns(cls, value: Any) -> tuple[str, ...]:
        if not isinstance(value, (list, tuple)):
            raise ValueError("Path rules must be a list or tuple")
        return tuple(sorted({_normalize_pattern(item) for item in value}))

    @field_validator("allowed_development_test_profiles", mode="before")
    @classmethod
    def normalize_profile_ids(cls, value: Any) -> tuple[str, ...]:
        if not isinstance(value, (list, tuple)):
            raise ValueError("Development test profiles must be a list or tuple")
        normalized = tuple(sorted({str(item).strip() for item in value if str(item).strip()}))
        if not normalized:
            raise ValueError("At least one development test profile is required")
        return normalized

    @model_validator(mode="after")
    def validate_and_digest(self) -> Self:
        if self.final_verification_profile_id in self.allowed_development_test_profiles:
            raise ValueError("Final verification profile cannot be model-selectable")
        if not self.allow_file_creation and self.allowed_create_paths:
            raise ValueError("Creation paths require allow_file_creation")
        if self.max_single_file_changed_bytes > self.max_total_changed_bytes:
            raise ValueError("Single-file limit cannot exceed total changed-byte limit")
        payload = self.model_dump(mode="json", exclude={"policy_digest"})
        expected = _digest(payload)
        if self.policy_digest and self.policy_digest != expected:
            raise ValueError("policy_digest does not match normalized policy")
        object.__setattr__(self, "policy_digest", expected)
        return self

    def allows_write(self, relative_path: str, *, creating: bool) -> bool:
        candidate = _normalize_candidate(relative_path)
        if self._matches(candidate, self.forbidden_write_paths):
            return False
        if self._matches(candidate, self.protected_paths):
            return False
        if not self._matches(candidate, self.allowed_write_paths):
            return False
        if creating:
            return self.allow_file_creation and self._matches(candidate, self.allowed_create_paths)
        return True

    def allows_development_profile(self, profile_id: str) -> bool:
        return profile_id in self.allowed_development_test_profiles

    def write_rule(self, relative_path: str, *, creating: bool) -> str:
        candidate = _normalize_candidate(relative_path)
        if self._matches(candidate, self.forbidden_write_paths):
            return "FORBIDDEN"
        if self._matches(candidate, self.protected_paths):
            return "PROTECTED"
        if not self._matches(candidate, self.allowed_write_paths):
            return "NOT_ALLOWED"
        if creating and (
            not self.allow_file_creation or not self._matches(candidate, self.allowed_create_paths)
        ):
            return "CREATE_NOT_ALLOWED"
        return "ALLOWED"

    def _matches(self, candidate: str, patterns: tuple[str, ...]) -> bool:
        compared = candidate if self.path_case_sensitive else candidate.casefold()
        for pattern in patterns:
            rule = pattern if self.path_case_sensitive else pattern.casefold()
            if fnmatchcase(compared, rule):
                return True
            if rule.endswith("/**") and compared == rule[:-3]:
                return True
        return False


class RepairState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: UUID
    task_id: str
    policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: RepairCompletionStatus = RepairCompletionStatus.RUNNING
    model_calls_used: int = Field(default=0, ge=0)
    read_calls_used: int = Field(default=0, ge=0)
    edit_attempts_used: int = Field(default=0, ge=0)
    test_runs_used: int = Field(default=0, ge=0)
    completion_corrections_used: int = Field(default=0, ge=0)
    policy_violations: int = Field(default=0, ge=0)
    started_at: UtcDatetime = Field(default_factory=utc_now)
    deadline_at: UtcDatetime
    baseline_id: UUID
    baseline_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    last_mutation_execution_id: UUID | None = None
    last_mutation_committed_at: UtcDatetime | None = None
    last_development_test_execution_id: UUID | None = None
    last_development_test_success: bool | None = None
    last_development_test_completed_at: UtcDatetime | None = None
    final_verification_execution_id: UUID | None = None
    final_verification_success: bool | None = None
    final_verification_completed_at: UtcDatetime | None = None
    last_diff_validation_id: str | None = None
    final_workspace_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    final_diff_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    latest_source_verified: bool = False
    pending_final_verification: bool = False
    final_answer_received: bool = False
    failure_reason: RepairTerminationReason | None = None
    state_version: int = Field(default=1, gt=0)
    updated_at: UtcDatetime = Field(default_factory=utc_now)

    @property
    def terminal(self) -> bool:
        return self.status is not RepairCompletionStatus.RUNNING


class BudgetConsumptionDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    state: RepairState
    consumed: bool
    exhausted: bool = False


_TERMINAL_PRIORITY: dict[RepairCompletionStatus, int] = {
    RepairCompletionStatus.INDETERMINATE: 1,
    RepairCompletionStatus.CANCELLED: 2,
    RepairCompletionStatus.RUNTIME_FAILURE: 3,
    RepairCompletionStatus.MODEL_PROTOCOL_ERROR: 3,
    RepairCompletionStatus.MODEL_TOOL_FAILED: 3,
    RepairCompletionStatus.POLICY_BLOCKED: 4,
    RepairCompletionStatus.DIFF_POLICY_VIOLATION: 4,
    RepairCompletionStatus.BUDGET_EXHAUSTED: 5,
    RepairCompletionStatus.FINAL_VERIFICATION_FAILED: 6,
    RepairCompletionStatus.UNVERIFIED_FINAL: 7,
    RepairCompletionStatus.TESTS_FAILED: 8,
    RepairCompletionStatus.VERIFIED_SUCCESS: 9,
    RepairCompletionStatus.LOOP_DETECTED: 3,
    RepairCompletionStatus.RUNNING: 100,
}


def terminal_priority(status: RepairCompletionStatus) -> int:
    return _TERMINAL_PRIORITY[status]


def _normalize_pattern(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("Path rules must contain strings")
    return _normalize_path(value, allow_glob=True)


def _normalize_candidate(value: str) -> str:
    return _normalize_path(value, allow_glob=False)


def _normalize_path(value: str, *, allow_glob: bool) -> str:
    raw = value.strip().replace("\\", "/")
    windows = PureWindowsPath(raw)
    if (
        not raw
        or "\x00" in raw
        or raw.startswith("/")
        or windows.is_absolute()
        or bool(windows.drive)
    ):
        raise ValueError("Path must be a safe workspace-relative path")
    parts = raw.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("Path cannot contain empty or traversal segments")
    if not allow_glob and any(marker in raw for marker in ("*", "?", "[", "]")):
        raise ValueError("Concrete path cannot contain glob syntax")
    return "/".join(parts)


def _digest(value: object) -> str:
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
