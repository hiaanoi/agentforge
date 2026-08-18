"""Frozen, public-only protocol for the Verified-10 pass-one rerun.

This module deliberately contains protocol validation and dataset projection only.  It
does not run an agent, call a model, generate predictions, or score a patch.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from enum import StrEnum
from pathlib import Path
from typing import Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SHA256_PATTERN = r"^[0-9a-f]{64}$"
GIT_COMMIT_PATTERN = r"^[0-9a-f]{40}$"
FORBIDDEN_GENERATION_FIELDS = (
    "FAIL_TO_PASS",
    "PASS_TO_PASS",
    "hints",
    "hints_text",
    "patch",
    "test_patch",
)
EXPECTED_INSTANCE_IDS = (
    "scikit-learn__scikit-learn-13142",
    "django__django-12419",
    "django__django-13212",
    "scikit-learn__scikit-learn-13496",
    "django__django-13343",
    "matplotlib__matplotlib-24026",
    "django__django-12050",
    "pytest-dev__pytest-7571",
    "sympy__sympy-16886",
    "pylint-dev__pylint-8898",
)
ATTEMPT_STATUS_VALUES = ("PLANNED", "RUNNING", "COMPLETED", "FAILED")
PUBLIC_ROW_FIELDS = frozenset(
    {"instance_id", "repo", "base_commit", "problem_statement"}
)

_TASK_METADATA: dict[str, tuple[str, str]] = {
    "scikit-learn__scikit-learn-13142": (
        "scikit-learn/scikit-learn",
        "1c8668b0a021832386470ddf740d834e02c66f69",
    ),
    "django__django-12419": (
        "django/django",
        "7fa1a93c6c8109010a6ff3f604fda83b604e0e97",
    ),
    "django__django-13212": (
        "django/django",
        "f4e93919e4608cfc50849a1f764fd856e0917401",
    ),
    "scikit-learn__scikit-learn-13496": (
        "scikit-learn/scikit-learn",
        "3aefc834dce72e850bff48689bea3c7dff5f3fad",
    ),
    "django__django-13343": (
        "django/django",
        "ece18207cbb64dd89014e279ac636a6c9829828e",
    ),
    "matplotlib__matplotlib-24026": (
        "matplotlib/matplotlib",
        "14c96b510ebeba40f573e512299b1976f35b620e",
    ),
    "django__django-12050": (
        "django/django",
        "b93a0e34d9b9b99d41103782b7e7aeabf47517e3",
    ),
    "pytest-dev__pytest-7571": (
        "pytest-dev/pytest",
        "422685d0bdc110547535036c1ff398b5e1c44145",
    ),
    "sympy__sympy-16886": (
        "sympy/sympy",
        "c50643a49811e9fe2f4851adff4313ad46f7325e",
    ),
    "pylint-dev__pylint-8898": (
        "pylint-dev/pylint",
        "1f8c4d9eb185c16a2c1d881c054f015e1c2eb334",
    ),
}


def canonical_digest(value: object) -> str:
    """Return the protocol's stable SHA256 encoding for JSON-compatible values."""

    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def public_task_sha256(
    *, instance_id: str, repo: str, base_commit: str, problem_statement: str
) -> str:
    """Hash only the public task projection, including no gold/private fields."""

    return canonical_digest(
        {
            "base_commit": base_commit,
            "instance_id": instance_id,
            "problem_statement": problem_statement,
            "repo": repo,
        }
    )


class BenchmarkArm(StrEnum):
    AGENTFORGE = "AGENTFORGE"
    MINI_SWE_AGENT = "MINI_SWE_AGENT"


class AttemptStatus(StrEnum):
    PLANNED = "PLANNED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class Verified10Task(_FrozenModel):
    selection_rank: str | None = Field(default=None, pattern=SHA256_PATTERN)
    instance_id: str = Field(min_length=1, max_length=200)
    repo: str = Field(min_length=1, max_length=200)
    base_commit: str = Field(pattern=GIT_COMMIT_PATTERN)
    public_task_sha256: str = Field(pattern=SHA256_PATTERN)


class AgentForgeBudget(_FrozenModel):
    repair_profile: Literal["SWE_BENCH_PASS1"]
    logical_model_calls: Literal[50]
    run_steps: Literal[80]
    wall_time_seconds: Literal[1800]
    provider_max_model_requests: Literal[52]
    provider_max_retries: Literal[2]
    provider_max_output_tokens: Literal[4096]
    provider_timeout_ms: Literal[600000]


class MiniSWEAgentBudget(_FrozenModel):
    step_limit: Literal[50]
    wall_time_seconds: Literal[1800]
    attempts: Literal[1]
    temperature: float
    network: Literal["none"]
    cost_limit_usd: float

    @field_validator("temperature", "cost_limit_usd")
    @classmethod
    def validate_zero(cls, value: float) -> float:
        if value != 0.0:
            raise ValueError("mini budget temperature and cost_limit_usd must be zero")
        return value

    @property
    def attempt(self) -> int:
        """Singular compatibility view for the one configured attempt."""

        return self.attempts


class BudgetRationale(_FrozenModel):
    benchmark: Literal["mini-swe-agent", "SWE-agent", "OpenHands"]
    source_url: str = Field(pattern=r"^https://")
    value: int | float = Field(ge=0)
    unit: Literal["steps", "usd", "iterations"]
    note: str = Field(min_length=1, max_length=500)


class PriorArtifactHashes(_FrozenModel):
    selection_sha256: str = Field(pattern=SHA256_PATTERN)
    experiment_protocol_sha256: str = Field(pattern=SHA256_PATTERN)
    agentforge_predictions_sha256: str = Field(pattern=SHA256_PATTERN)
    mini_swe_agent_predictions_sha256: str = Field(pattern=SHA256_PATTERN)
    agentforge_official_report_sha256: str = Field(pattern=SHA256_PATTERN)
    mini_swe_agent_official_report_sha256: str = Field(pattern=SHA256_PATTERN)


class Prior14CallBaseline(_FrozenModel):
    logical_model_calls: Literal[14]
    mini_resolved: Literal[1]
    agentforge_resolved: Literal[0]
    agentforge_admission: Literal[4]
    compatibility_failures: Literal[6]
    artifacts: PriorArtifactHashes
    output_adoption: Literal["metadata_only_do_not_adopt_old_outputs"]


class Verified10Protocol(_FrozenModel):
    schema_version: Literal[1]
    protocol_name: Literal["verified10-deepseek-flash-pass1"]
    dataset_name: Literal["princeton-nlp/SWE-bench_Verified"]
    dataset_split: Literal["test"]
    dataset_fingerprint: str = Field(min_length=1, max_length=200)
    source_selection_sha256: str = Field(pattern=SHA256_PATTERN)
    swebench_commit: Literal["4e6126978a16bdfebc6538db8f28cacc2c8b77dc"]
    mini_swe_agent_commit: Literal["25941c89cfbc91eb40b3f8756348c91d9977d57e"]
    mini_swe_agent_version: Literal["2.4.6"]
    attempts_per_instance: Literal[1]
    model: Literal["deepseek-v4-flash"]
    thinking_enabled: Literal[False]
    temperature: float
    tasks: tuple[Verified10Task, ...]
    generation_forbidden_fields: tuple[str, ...]
    agentforge_budget: AgentForgeBudget
    mini_budget: MiniSWEAgentBudget
    budget_rationale: tuple[BudgetRationale, ...]
    budget_rationale_note: Literal[
        "Units differ across tools; these values are not a cost-normalized comparison."
    ]
    prior_baseline: Prior14CallBaseline

    @field_validator("temperature")
    @classmethod
    def validate_temperature(cls, value: float) -> float:
        if value != 0.0:
            raise ValueError("temperature must be zero")
        return value

    @field_validator("tasks", "generation_forbidden_fields", "budget_rationale", mode="before")
    @classmethod
    def normalize_json_arrays(cls, value: object) -> object:
        # JSON has arrays; the in-memory frozen representation is tuple-based.
        if isinstance(value, list):
            return tuple(value)
        return value

    @field_validator("generation_forbidden_fields")
    @classmethod
    def validate_forbidden_fields(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != FORBIDDEN_GENERATION_FIELDS:
            raise ValueError("generation_forbidden_fields must match the frozen exact list")
        return value

    @model_validator(mode="after")
    def validate_frozen_protocol(self) -> Self:
        ids = tuple(task.instance_id for task in self.tasks)
        if ids != EXPECTED_INSTANCE_IDS or len(set(ids)) != 10:
            raise ValueError("tasks must contain the exact ten instance IDs in selection order")
        for task in self.tasks:
            expected_repo, expected_base = _TASK_METADATA[task.instance_id]
            if (task.repo, task.base_commit) != (expected_repo, expected_base):
                raise ValueError(f"repo/base_commit mismatch for {task.instance_id}")
        if len(self.budget_rationale) != 4:
            raise ValueError("budget_rationale must contain four official values")
        rationale_keys = {
            (item.benchmark, item.value, item.unit) for item in self.budget_rationale
        }
        expected_keys = {
            ("mini-swe-agent", 250, "steps"),
            ("mini-swe-agent", 3, "usd"),
            ("SWE-agent", 3, "usd"),
            ("OpenHands", 500, "iterations"),
        }
        if rationale_keys != expected_keys:
            raise ValueError("budget_rationale official values are incomplete or drifted")
        return self

    @property
    def protocol_sha256(self) -> str:
        """Digest of model_dump JSON, with this computed property excluded naturally."""

        return self.canonical_digest()

    @property
    def protocol_digest(self) -> str:
        """Compatibility name for consumers that call the digest a protocol digest."""

        return self.protocol_sha256

    def canonical_digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json"))


class BenchmarkAttemptRecord(_FrozenModel):
    protocol_sha256: str = Field(pattern=SHA256_PATTERN)
    arm: BenchmarkArm
    instance_id: str = Field(min_length=1, max_length=200)
    attempt_index: Literal[1]
    status: AttemptStatus


def project_public_task(row: Mapping[str, object]) -> dict[str, str]:
    """Project a dataset row to exactly the public fields allowed for generation."""

    forbidden = sorted(set(row).intersection(FORBIDDEN_GENERATION_FIELDS))
    if forbidden:
        raise ValueError(f"forbidden private generation fields supplied: {forbidden}")
    if set(row) != PUBLIC_ROW_FIELDS:
        raise ValueError(
            "public dataset row must contain exactly instance_id, repo, base_commit, "
            "and problem_statement"
        )
    projected: dict[str, str] = {}
    for key in PUBLIC_ROW_FIELDS:
        value = row[key]
        if not isinstance(value, str):
            raise ValueError(f"public field {key} must be a string")
        projected[key] = value
    return projected


def validate_public_dataset_rows(
    rows: Sequence[Mapping[str, object]], protocol: Verified10Protocol
) -> tuple[dict[str, str], ...]:
    """Validate public runtime rows against the frozen selection and task hashes."""

    if len(rows) != len(protocol.tasks):
        raise ValueError("runtime dataset must contain exactly ten rows")
    projected_rows: list[dict[str, str]] = []
    for row, task in zip(rows, protocol.tasks, strict=True):
        projected = project_public_task(row)
        if projected["instance_id"] != task.instance_id:
            raise ValueError("runtime dataset instance order does not match selection")
        if projected["repo"] != task.repo or projected["base_commit"] != task.base_commit:
            raise ValueError(f"runtime dataset repo/base mismatch for {task.instance_id}")
        actual_hash = public_task_sha256(**projected)
        if actual_hash != task.public_task_sha256:
            raise ValueError(f"public task hash mismatch for {task.instance_id}")
        projected_rows.append(projected)
    return tuple(projected_rows)


def load_verified10_protocol(path: str | Path) -> Verified10Protocol:
    """Load and validate the JSON protocol artifact."""

    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return Verified10Protocol.model_validate(cast(dict[str, object], payload))
