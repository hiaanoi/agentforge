"""Frozen, public-only protocol for the Verified-10 pass-one rerun.

This module deliberately contains protocol validation and dataset projection only.  It
does not run an agent, call a model, generate predictions, or score a patch.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from enum import StrEnum
from pathlib import Path
from typing import Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from agentforge.evaluation.protocol import canonical_digest

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
PUBLIC_ROW_FIELDS = frozenset(
    {"instance_id", "repo", "base_commit", "problem_statement"}
)

_FROZEN_TASK_BINDINGS = (
    (
        "0021a6cc0b3f00fa98c226473304f8a8c77e3563f412f8a00caa393f291cd909",
        "scikit-learn__scikit-learn-13142",
        "scikit-learn/scikit-learn",
        "1c8668b0a021832386470ddf740d834e02c66f69",
        "9f06307581ce7500feef1f13a0787b8a6cae79148b74cc4f8be359fc19121cf6",
    ),
    (
        "0034bdf3cf80162dc8ac1db996043c25ae04242fd47d7fd55f12c8f9812fd66c",
        "django__django-12419",
        "django/django",
        "7fa1a93c6c8109010a6ff3f604fda83b604e0e97",
        "25f3f330f1d759f32e075a9044e65b2b4f6bf511dab97c524a87fdac5f742a24",
    ),
    (
        "012581344961f487b90d5363574659a3c90b03cac716d705f755861e6b887a0b",
        "django__django-13212",
        "django/django",
        "f4e93919e4608cfc50849a1f764fd856e0917401",
        "25efae42f34a4b48cde1f0c6baabc8e7bf2727c9f66dede2bcc02279cdd447f8",
    ),
    (
        "016bdfacb0eaae6e9eb824ee8ebe4d412fc056c8a37de97204d4e77042f4c3b1",
        "scikit-learn__scikit-learn-13496",
        "scikit-learn/scikit-learn",
        "3aefc834dce72e850bff48689bea3c7dff5f3fad",
        "eaa512bc8bef6ad4d5bb745ba320447bf0878eecfa8015fbed2287b3eb2b243b",
    ),
    (
        "03b8a5895ff385c7f718202f58f8388d51a7c128da6f1cf2355bef27ae54178d",
        "django__django-13343",
        "django/django",
        "ece18207cbb64dd89014e279ac636a6c9829828e",
        "53284d1ec9f445c5929f03c62c97153caf40d44c2157eb433d453272b1027bae",
    ),
    (
        "048c279800db517bd66ee630d35a280253f03dcd534e23e69b7cd4a3576cb962",
        "matplotlib__matplotlib-24026",
        "matplotlib/matplotlib",
        "14c96b510ebeba40f573e512299b1976f35b620e",
        "5f16b267c474e37aab014af334324d450743eaff39744a37cbb3155d88005fdd",
    ),
    (
        "04fefb9bb4515de22f0b688db0b851178a895792cfc63e3aa263726b28292c64",
        "django__django-12050",
        "django/django",
        "b93a0e34d9b9b99d41103782b7e7aeabf47517e3",
        "bcd5d62508d55f0a1b62da171623297b5205c3878b4d8bca2b9cb11bf0d1052a",
    ),
    (
        "053c82003c4072f9c4a9d24a20f1943402c9b16ae7dc42f44868bd770e6fb16d",
        "pytest-dev__pytest-7571",
        "pytest-dev/pytest",
        "422685d0bdc110547535036c1ff398b5e1c44145",
        "b495b461821edfedf6c31666490d7f3c89a4cef0f7e4075d8f3d5575dd097434",
    ),
    (
        "055bb685b4111ed49e1b46fbd4c16b547238db38dcfe461c9af75adc2d0ec8e1",
        "sympy__sympy-16886",
        "sympy/sympy",
        "c50643a49811e9fe2f4851adff4313ad46f7325e",
        "5bcf752353d75ecbdbee43dbe44c6e70d64a7e32cf13327a9a47d5e0933b1ec7",
    ),
    (
        "057a3a83545da946ace7c672d5c5428d9d651a81d3c8deeb534b87f4a77bb4dd",
        "pylint-dev__pylint-8898",
        "pylint-dev/pylint",
        "1f8c4d9eb185c16a2c1d881c054f015e1c2eb334",
        "055f83fc61e4e97be328dcdc7c34eb09bbe2981fe1a69e51a97702e50be10b3d",
    ),
)
EXPECTED_INSTANCE_IDS = tuple(binding[1] for binding in _FROZEN_TASK_BINDINGS)

_FROZEN_DATASET_FINGERPRINT = "1fdfd21ba2621130"
_FROZEN_SOURCE_SELECTION_SHA256 = (
    "9c385f13580c05e3cb5590e2abb43b278fa8ed99597315d7010f6953785ca9c0"
)
_MINI_SWE_AGENT_CONFIG_URL = (
    "https://github.com/SWE-agent/mini-swe-agent/blob/main/"
    "src/minisweagent/config/benchmarks/swebench.yaml"
)
_SWE_AGENT_MODELS_URL = (
    "https://github.com/princeton-nlp/SWE-agent/blob/main/sweagent/agent/models.py"
)
_OPENHANDS_CONFIG_URL = "https://github.com/OpenHands/OpenHands/blob/main/config.template.toml"
_FROZEN_PRIOR_ARTIFACT_HASHES = {
    "selection_sha256": _FROZEN_SOURCE_SELECTION_SHA256,
    "experiment_protocol_sha256": (
        "b511f1c6c7974f7b36ed1eb4ba9ee7435a6dcf21211b7ed5e879fe9fcc06c62a"
    ),
    "agentforge_predictions_sha256": (
        "2e98e29e37f5c42598fe055c1db5972a44bec9d5d68ad3721cf9e00c40efaf48"
    ),
    "mini_swe_agent_predictions_sha256": (
        "42fcc0377abdb3538a12ea814a862b9ddd46dfb69fef544058a817134373e1fb"
    ),
    "agentforge_official_report_sha256": (
        "a9c19d540f1170d9026161c4bdf997c1b487bd000208f3fe8b2044b5ddea0243"
    ),
    "mini_swe_agent_official_report_sha256": (
        "9d6274d5ad446dde3cf000276466da8a00bd0203e01fffdc8ebe7a7a0ca48c9f"
    ),
}


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


ATTEMPT_STATUS_VALUES = tuple(item.value for item in AttemptStatus)


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

    @model_validator(mode="after")
    def validate_frozen_artifacts(self) -> Self:
        if self.model_dump(mode="json") != _FROZEN_PRIOR_ARTIFACT_HASHES:
            raise ValueError("prior artifact SHA256 values do not match old evidence bytes")
        return self


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
        actual_bindings = tuple(
            (
                task.selection_rank,
                task.instance_id,
                task.repo,
                task.base_commit,
                task.public_task_sha256,
            )
            for task in self.tasks
        )
        if actual_bindings != _FROZEN_TASK_BINDINGS:
            raise ValueError("tasks do not match the frozen old selection bindings")
        if self.dataset_fingerprint != _FROZEN_DATASET_FINGERPRINT:
            raise ValueError("dataset_fingerprint does not match the frozen selection")
        if self.source_selection_sha256 != _FROZEN_SOURCE_SELECTION_SHA256:
            raise ValueError("source_selection_sha256 does not match the frozen selection")
        if len(self.budget_rationale) != 4:
            raise ValueError("budget_rationale must contain four official values")
        actual_rationale = tuple(
            (item.benchmark, item.source_url, item.value, item.unit)
            for item in self.budget_rationale
        )
        expected_rationale = (
            ("mini-swe-agent", _MINI_SWE_AGENT_CONFIG_URL, 250, "steps"),
            ("mini-swe-agent", _MINI_SWE_AGENT_CONFIG_URL, 3, "usd"),
            ("SWE-agent", _SWE_AGENT_MODELS_URL, 3, "usd"),
            ("OpenHands", _OPENHANDS_CONFIG_URL, 500, "iterations"),
        )
        if actual_rationale != expected_rationale:
            raise ValueError("budget_rationale sources or official values drifted")
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


class Verified10ProtocolError(ValueError):
    """Stable domain error for every protocol loading failure."""


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


def validate_public_task(
    row: Mapping[str, object], task: Verified10Task
) -> dict[str, str]:
    """Validate one public runtime row against one frozen task binding."""

    projected = project_public_task(row)
    if projected["instance_id"] != task.instance_id:
        raise ValueError("runtime dataset instance does not match selection")
    if projected["repo"] != task.repo or projected["base_commit"] != task.base_commit:
        raise ValueError(f"runtime dataset repo/base mismatch for {task.instance_id}")
    actual_hash = public_task_sha256(**projected)
    if actual_hash != task.public_task_sha256:
        raise ValueError(f"public task hash mismatch for {task.instance_id}")
    return projected


def validate_public_dataset_rows(
    rows: Sequence[Mapping[str, object]], protocol: Verified10Protocol
) -> tuple[dict[str, str], ...]:
    """Validate public runtime rows against the frozen selection and task hashes."""

    if len(rows) != len(protocol.tasks):
        raise ValueError("runtime dataset must contain exactly ten rows")
    projected_rows: list[dict[str, str]] = []
    for row, task in zip(rows, protocol.tasks, strict=True):
        projected_rows.append(validate_public_task(row, task))
    return tuple(projected_rows)


def load_verified10_protocol(path: str | Path) -> Verified10Protocol:
    """Load and validate the JSON protocol artifact."""

    try:
        resolved = Path(path).expanduser().resolve(strict=True)
        return Verified10Protocol.model_validate_json(resolved.read_bytes())
    except (OSError, UnicodeError, ValueError, ValidationError):
        raise Verified10ProtocolError("Unable to load Verified-10 protocol") from None
