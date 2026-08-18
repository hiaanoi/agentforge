# ruff: noqa: E501, E701, E702
"""Frozen, public-only protocol for the Verified-10 pass-one rerun.

This module deliberately contains protocol validation and dataset projection only.  It
does not run an agent, call a model, generate predictions, or score a patch.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Literal, Protocol, Self
from uuid import uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from agentforge.application.product_workspace import ProductWorkspaceCapture
from agentforge.evaluation.protocol import canonical_digest
from agentforge.evaluation.swebench_prediction import (
    SWEbenchInstanceBinding,
    SWEbenchPrediction,
    SWEbenchPredictionError,
    serialize_swebench_predictions,
)

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


class AttemptFailureClass(StrEnum):
    NONE = "NONE"
    EMPTY = "EMPTY"
    EMPTY_PATCH = "EMPTY"
    COMPATIBILITY_FAILED = "COMPATIBILITY_FAILED"
    COMPATIBILITY = "COMPATIBILITY_FAILED"
    MODEL_FAILED = "MODEL_FAILED"
    MODEL = "MODEL_FAILED"
    TIMEOUT = "TIMEOUT"


ATTEMPT_FAILURE_CLASS_VALUES = tuple(item.value for item in AttemptFailureClass)


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
    failure_class: AttemptFailureClass = AttemptFailureClass.NONE
    model_calls: int = Field(default=0, ge=0)
    steps: int = Field(default=0, ge=0)
    provider_prompt_tokens: int = Field(default=0, ge=0)
    provider_completion_tokens: int = Field(default=0, ge=0)
    provider_total_tokens: int = Field(default=0, ge=0)
    approval_count: int = Field(default=0, ge=0)
    edit_count: int = Field(default=0, ge=0)
    test_count: int = Field(default=0, ge=0)
    wall_time_seconds: float = Field(default=0.0, ge=0.0)
    trajectory_path: str | None = Field(default=None, max_length=500)
    trajectory_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)
    prediction_patch_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)

    @model_validator(mode="after")
    def validate_status_failure(self) -> Self:
        if self.status is AttemptStatus.FAILED and self.failure_class is AttemptFailureClass.NONE:
            raise ValueError("FAILED attempts require a failure class")
        if (
            self.status in {AttemptStatus.PLANNED, AttemptStatus.RUNNING}
            and self.failure_class is not AttemptFailureClass.NONE
        ):
            raise ValueError("non-terminal attempts must not have a failure class")
        if (
            self.status is AttemptStatus.COMPLETED
            and self.failure_class not in {AttemptFailureClass.NONE, AttemptFailureClass.EMPTY}
        ):
            raise ValueError("COMPLETED attempts only allow NONE or EMPTY failure class")
        return self

    @field_validator("trajectory_path")
    @classmethod
    def validate_trajectory_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.replace("\\", "/")
        if not normalized or normalized.startswith("/") or ":" in normalized:
            raise ValueError("trajectory_path must be artifact-relative")
        if any(part in {"", ".", ".."} for part in normalized.split("/")):
            raise ValueError("trajectory_path must not contain absolute or parent paths")
        return normalized


class Verified10ProtocolError(ValueError):
    """Stable domain error for every protocol loading failure."""


class CampaignArtifactError(Verified10ProtocolError):
    """Stable domain error for campaign artifact loading and finalization."""


class CampaignArtifactResult(_FrozenModel):
    predictions_sha256: str = Field(pattern=SHA256_PATTERN)
    ledger_sha256: str = Field(pattern=SHA256_PATTERN)


def _revalidate_attempt(record: BenchmarkAttemptRecord) -> BenchmarkAttemptRecord:
    try:
        return BenchmarkAttemptRecord.model_validate(record.model_dump(mode="python"))
    except (AttributeError, TypeError, ValueError, ValidationError):
        raise CampaignArtifactError("Campaign attempt failed validation") from None


def _revalidate_protocol(protocol: Verified10Protocol) -> Verified10Protocol:
    try:
        return Verified10Protocol.model_validate(protocol.model_dump(mode="python"))
    except (AttributeError, TypeError, ValueError, ValidationError):
        raise CampaignArtifactError("Campaign protocol failed validation") from None


def save_attempt_ledger(path: str | Path, attempts: Sequence[BenchmarkAttemptRecord]) -> str:
    """Atomically save the private attempt ledger and return its written-byte digest."""

    payload = _serialize_attempt_ledger(attempts)
    target = _prepare_artifact_target(path)
    _atomic_artifact_write(target, payload)
    try:
        return hashlib.sha256(target.read_bytes()).hexdigest()
    except OSError:
        raise CampaignArtifactError("Unable to save campaign artifact") from None


def load_attempt_ledger(path: str | Path) -> tuple[BenchmarkAttemptRecord, ...]:
    """Read and strictly validate a private attempt ledger."""

    try:
        content = Path(path).expanduser().resolve(strict=True).read_bytes()
        return TypeAdapter(tuple[BenchmarkAttemptRecord, ...]).validate_json(content)
    except (OSError, UnicodeError, ValueError, TypeError, ValidationError):
        raise CampaignArtifactError("Unable to load campaign attempt ledger") from None


def finalize_verified10_campaign(
    protocol: Verified10Protocol,
    attempts: Sequence[BenchmarkAttemptRecord],
    predictions: Sequence[SWEbenchPrediction],
    prediction_path: str | Path,
    ledger_path: str | Path,
    arm: BenchmarkArm | None = None,
) -> CampaignArtifactResult:
    """Validate a complete pass and atomically emit public predictions plus private ledger."""

    protocol = _revalidate_protocol(protocol)
    try:
        attempts = tuple(_revalidate_attempt(record) for record in attempts)
        predictions = tuple(
            SWEbenchPrediction.model_validate(prediction.model_dump(mode="python"))
            for prediction in predictions
        )
    except (AttributeError, TypeError, ValueError, ValidationError):
        raise CampaignArtifactError("Campaign artifact failed validation") from None
    if len(protocol.tasks) != 10:
        raise CampaignArtifactError("Campaign protocol must contain exactly ten tasks")
    expected_ids = tuple(task.instance_id for task in protocol.tasks)
    if len(attempts) != len(expected_ids) or len(predictions) != len(expected_ids):
        raise CampaignArtifactError(
            "Campaign finalizer requires exactly ten attempts and predictions"
        )
    if any(record.protocol_sha256 != protocol.protocol_sha256 for record in attempts):
        raise CampaignArtifactError("Campaign attempt protocol digest mismatch")
    arms = {record.arm for record in attempts}
    if len(arms) != 1 or (arm is not None and arms != {arm}):
        raise CampaignArtifactError("Campaign attempts contain cross-arm records")
    if any(
        record.status not in {AttemptStatus.COMPLETED, AttemptStatus.FAILED}
        for record in attempts
    ):
        raise CampaignArtifactError("Campaign attempts must have terminal status")
    attempt_ids = [record.instance_id for record in attempts]
    prediction_ids = [prediction.instance_id for prediction in predictions]
    if len(set(attempt_ids)) != len(attempt_ids) or len(set(prediction_ids)) != len(prediction_ids):
        raise CampaignArtifactError("Campaign attempts or predictions contain duplicate IDs")
    if set(attempt_ids) != set(expected_ids) or set(prediction_ids) != set(expected_ids):
        raise CampaignArtifactError("Campaign attempts or predictions do not cover the protocol")
    by_attempt = {record.instance_id: record for record in attempts}
    by_prediction = {prediction.instance_id: prediction for prediction in predictions}
    selected_arm = next(iter(arms))
    expected_model_name = (
        "agentforge:" if selected_arm is BenchmarkArm.AGENTFORGE else "mini-swe-agent:"
    ) + protocol.model
    for instance_id in expected_ids:
        record = by_attempt[instance_id]
        prediction = by_prediction[instance_id]
        task = next(task for task in protocol.tasks if task.instance_id == instance_id)
        if prediction.base_commit != task.base_commit:
            raise CampaignArtifactError("Prediction base commit does not match protocol task")
        if prediction.model_name_or_path != expected_model_name:
            raise CampaignArtifactError("Prediction model identity does not match campaign arm")
        if record.attempt_index != 1:
            raise CampaignArtifactError("Campaign attempt index is not one")
        actual_patch_sha256 = hashlib.sha256(prediction.model_patch.encode("utf-8")).hexdigest()
        if prediction.patch_sha256 != actual_patch_sha256:
            raise CampaignArtifactError("Prediction patch digest does not match model patch bytes")
        if record.prediction_patch_sha256 != prediction.patch_sha256:
            raise CampaignArtifactError("Prediction patch digest does not match attempt ledger")
        if prediction.model_patch:
            if (
                record.status is not AttemptStatus.COMPLETED
                or record.failure_class is not AttemptFailureClass.NONE
            ):
                raise CampaignArtifactError("Non-empty prediction must be a completed attempt")
        elif (
            record.status is AttemptStatus.COMPLETED
            and record.failure_class is not AttemptFailureClass.EMPTY
        ):
            raise CampaignArtifactError("Completed empty prediction has an invalid failure class")
    try:
        # Serialization and parent readiness happen before either artifact is published.
        prediction_payload = serialize_swebench_predictions(
            tuple(by_prediction.values()), expected_instance_ids=expected_ids
        )
        ordered_attempts = tuple(by_attempt[instance_id] for instance_id in expected_ids)
        ledger_payload = _serialize_attempt_ledger(ordered_attempts)
        prediction_target, ledger_target = _ensure_distinct_artifacts(
            prediction_path, ledger_path
        )
        lock_handles = _acquire_finalize_locks(prediction_target, ledger_target)
        try:
            prediction_target = _prepare_artifact_target(prediction_target, reject_existing=True)
            ledger_target = _prepare_artifact_target(ledger_target, reject_existing=True)
            # Files are not a transaction: the public prediction is the final commit marker.
            _atomic_artifact_write(ledger_target, ledger_payload)
            _atomic_artifact_write(prediction_target, prediction_payload)
            prediction_digest = hashlib.sha256(prediction_target.read_bytes()).hexdigest()
            ledger_digest = hashlib.sha256(ledger_target.read_bytes()).hexdigest()
        finally:
            for lock_path, lock_fd in reversed(lock_handles):
                _release_finalize_lock(lock_path, lock_fd)
    except CampaignArtifactError:
        raise
    except (OSError, SWEbenchPredictionError, TypeError, ValueError):
        raise CampaignArtifactError("Unable to finalize campaign artifacts") from None
    return CampaignArtifactResult(
        predictions_sha256=prediction_digest,
        ledger_sha256=ledger_digest,
    )


def _atomic_artifact_write(target: Path, payload: bytes) -> None:
    temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        if os.name != "nt":
            directory_fd = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except OSError:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise CampaignArtifactError("Unable to save campaign artifact") from None


def _serialize_attempt_ledger(attempts: Sequence[BenchmarkAttemptRecord]) -> bytes:
    try:
        validated = tuple(_revalidate_attempt(record) for record in attempts)
        return (
            json.dumps(
                [record.model_dump(mode="json") for record in validated],
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )
    except (TypeError, ValueError):
        raise CampaignArtifactError("Unable to serialize campaign ledger") from None


def _prepare_artifact_target(path: str | Path, *, reject_existing: bool = False) -> Path:
    try:
        target = Path(path).expanduser().resolve(strict=False)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.parent.is_dir() or (target.exists() and (reject_existing or target.is_dir())):
            if reject_existing and target.exists():
                raise CampaignArtifactError("Campaign artifact target already exists")
            raise OSError
        return target
    except CampaignArtifactError:
        raise
    except (OSError, RuntimeError, ValueError):
        raise CampaignArtifactError("Unable to prepare campaign artifact path") from None


def _ensure_distinct_artifacts(
    prediction_path: str | Path, ledger_path: str | Path
) -> tuple[Path, Path]:
    try:
        prediction = Path(prediction_path).expanduser().resolve(strict=False)
        ledger = Path(ledger_path).expanduser().resolve(strict=False)
        if prediction == ledger:
            raise CampaignArtifactError("Public and private artifact paths must not be the same")
        if prediction.exists() and ledger.exists() and os.path.samefile(prediction, ledger):
            raise CampaignArtifactError("Public and private artifacts must not be the same file")
        return prediction, ledger
    except CampaignArtifactError:
        raise
    except (OSError, RuntimeError, ValueError):
        raise CampaignArtifactError("Unable to prepare campaign artifact paths") from None


def _acquire_finalize_lock(public_target: Path) -> tuple[Path, int]:
    """Acquire a deterministic publication lock; stale locks are operator-managed."""

    try:
        public_target.parent.mkdir(parents=True, exist_ok=True)
        lock_path = public_target.with_name(f".{public_target.name}.finalize.lock")
        lock_fd = os.open(
            lock_path,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            0o600,
        )
        try:
            os.write(lock_fd, b"finalize lock\n")
        except OSError:
            os.close(lock_fd)
            lock_path.unlink(missing_ok=True)
            raise
        return lock_path, lock_fd
    except FileExistsError:
        raise CampaignArtifactError(
            "Campaign finalization is already in progress or stale"
        ) from None
    except OSError:
        raise CampaignArtifactError("Unable to acquire campaign finalization lock") from None


def _acquire_finalize_locks(
    prediction_target: Path, ledger_target: Path
) -> tuple[tuple[Path, int], ...]:
    targets = sorted(
        (prediction_target, ledger_target),
        key=lambda target: os.path.normcase(
            str(target.with_name(f".{target.name}.finalize.lock"))
        ),
    )
    acquired: list[tuple[Path, int]] = []
    try:
        for target in targets:
            acquired.append(_acquire_finalize_lock(target))
        return tuple(acquired)
    except CampaignArtifactError:
        for lock_path, lock_fd in reversed(acquired):
            _release_finalize_lock(lock_path, lock_fd)
        raise


def _release_finalize_lock(lock_path: Path, lock_fd: int) -> None:
    try:
        os.close(lock_fd)
    finally:
        try:
            lock_path.unlink(missing_ok=True)
        except OSError:
            pass


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


# The campaign runner intentionally depends on this narrow seam rather than
# subprocess directly.  That makes preparation and both arms replayable in
# tests without a registry, Docker daemon, or model credential.
@dataclass(frozen=True, slots=True)
class CampaignCommand:
    argv: tuple[str, ...]
    cwd: Path | None = None
    environment: Mapping[str, str] | None = None
    timeout_seconds: int | None = None


@dataclass(frozen=True, slots=True)
class CampaignCommandResult:
    returncode: int
    stdout: str
    stderr: str


class CommandRunner(Protocol):
    def run(self, command: CampaignCommand) -> CampaignCommandResult: ...


class SubprocessCommandRunner:
    """The real runner; callers may replace it with a local fake in tests."""

    def run(self, command: CampaignCommand) -> CampaignCommandResult:
        completed = subprocess.run(
            command.argv,
            cwd=command.cwd,
            env=dict(command.environment) if command.environment is not None else None,
            capture_output=True,
            text=True,
            timeout=command.timeout_seconds,
            check=False,
        )
        return CampaignCommandResult(completed.returncode, completed.stdout, completed.stderr)


class CampaignExecutionError(Verified10ProtocolError):
    """Stable, non-secret error exposed by the campaign CLI."""


class _CampaignAttempt(_FrozenModel):
    instance_id: str
    attempt_index: Literal[1]
    status: AttemptStatus
    failure_class: AttemptFailureClass
    model_patch: str = ""
    run_id: str | None = None
    model_calls: int = Field(default=0, ge=0)
    steps: int = Field(default=0, ge=0)
    wall_time_seconds: float = Field(default=0, ge=0)
    trajectory_path: str | None = None
    trajectory_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)


class _WorkspaceRecord(_FrozenModel):
    path: str
    image: str
    image_digest: str
    workspace_digest: str = Field(pattern=SHA256_PATTERN)
    symlink_count: int = Field(ge=0)
    disk_bytes: int = Field(ge=0)


class _CampaignState(_FrozenModel):
    schema_version: Literal[1] = 1
    protocol_digest: str = Field(pattern=SHA256_PATTERN)
    prepared: bool = False
    admission_count: int = Field(default=0, ge=0, le=10)
    workspaces: dict[str, _WorkspaceRecord] = Field(default_factory=dict)
    attempts: dict[str, tuple[_CampaignAttempt, ...]] = Field(default_factory=dict)
    finalized_arms: tuple[str, ...] = ()


def swebench_image_name(instance_id: str) -> str:
    """Official SWE-bench evaluation image mapping, with no registry lookup."""

    if not isinstance(instance_id, str) or not instance_id:
        raise CampaignExecutionError("Invalid SWE-bench instance identifier")
    return "docker.io/swebench/sweb.eval.x86_64." + instance_id.replace("__", "_1776_").lower() + ":latest"


def _relative(root: Path, path: Path) -> str:
    try:
        value = path.resolve(strict=False).relative_to(root.resolve(strict=False)).as_posix()
    except ValueError:
        raise CampaignExecutionError("Campaign artifact path escaped output directory") from None
    if not value or value.startswith("../"):
        raise CampaignExecutionError("Campaign artifact path is invalid")
    return value


class Verified10Campaign:
    """Durable matched-run orchestration around public-only command specs."""

    def __init__(
        self,
        protocol_path: str | Path,
        output_dir: str | Path,
        *,
        runner: CommandRunner | None = None,
    ) -> None:
        self.protocol_path = Path(protocol_path).resolve(strict=True)
        self.protocol = load_verified10_protocol(self.protocol_path)
        self.root = self._safe_root(Path(output_dir))
        self.runner = runner or SubprocessCommandRunner()

    @staticmethod
    def _safe_root(requested: Path) -> Path:
        if not requested.is_absolute():
            requested = requested.absolute()
        if any(part in {"", ".", ".."} for part in requested.parts):
            raise CampaignExecutionError("Output directory path is unsafe")
        current = Path(requested.anchor)
        for part in requested.parts[1:]:
            current /= part
            if current.exists() and current.is_symlink():
                raise CampaignExecutionError("Output directory path is unsafe")
        return requested

    @property
    def _state_path(self) -> Path:
        return self.root / "campaign-state.json"

    def _bootstrap_or_load(self) -> _CampaignState:
        if not self.root.exists():
            self.root.mkdir(parents=True, exist_ok=False)
            protocol_copy = self.root / "protocol.json"
            self._atomic(protocol_copy, self.protocol_path.read_bytes())
            state = _CampaignState(protocol_digest=self.protocol.protocol_digest)
            self._save(state)
            return state
        return self._load()

    def _load(self) -> _CampaignState:
        try:
            copied = load_verified10_protocol(self.root / "protocol.json")
            state = _CampaignState.model_validate_json(self._state_path.read_bytes())
        except (OSError, ValueError, ValidationError, Verified10ProtocolError):
            raise CampaignExecutionError("Campaign state is missing or invalid") from None
        if copied.protocol_digest != self.protocol.protocol_digest or state.protocol_digest != self.protocol.protocol_digest:
            raise CampaignExecutionError("Campaign protocol digest mismatch")
        return state

    def _save(self, state: _CampaignState) -> None:
        self._atomic(self._state_path, state.model_dump_json(indent=2).encode("utf-8") + b"\n")

    @staticmethod
    def _atomic(path: Path, payload: bytes) -> None:
        temporary = path.with_name("." + path.name + ".tmp")
        try:
            temporary.write_bytes(payload)
            os.replace(temporary, path)
        except OSError:
            temporary.unlink(missing_ok=True)
            raise CampaignExecutionError("Unable to write campaign artifact") from None

    def _run(self, command: CampaignCommand, *, label: str) -> CampaignCommandResult:
        try:
            result = self.runner.run(command)
        except (OSError, subprocess.SubprocessError, TimeoutError):
            raise CampaignExecutionError(label + " command failed") from None
        if result.returncode != 0:
            raise CampaignExecutionError(label + " command failed")
        return result

    @staticmethod
    def _dataset_command() -> CampaignCommand:
        # The output deliberately contains only the public projection; the
        # runner never receives a request for tests, hints, or gold patches.
        script = (
            "import json; from datasets import load_dataset; "
            "d=load_dataset('princeton-nlp/SWE-bench_Verified', split='test'); "
            "print(json.dumps([{k:r[k] for k in ('instance_id','repo','base_commit','problem_statement')} for r in d]))"
        )
        return CampaignCommand(("python", "-c", script), environment={"HF_ENDPOINT": os.environ.get("HF_ENDPOINT", "https://huggingface.co")}, timeout_seconds=600)

    def prepare(self) -> None:
        state = self._bootstrap_or_load()
        if state.prepared:
            return
        try:
            rows = json.loads(self._run(self._dataset_command(), label="dataset").stdout)
            validate_public_dataset_rows(rows, self.protocol)
        except (json.JSONDecodeError, TypeError, ValueError, Verified10ProtocolError):
            raise CampaignExecutionError("Public Verified dataset validation failed") from None
        self._run(CampaignCommand(("docker", "version", "--format", "{{json .}}")), label="docker")
        records: dict[str, _WorkspaceRecord] = {}
        for task in self.protocol.tasks:
            image = swebench_image_name(task.instance_id)
            inspected = self._run(CampaignCommand(("docker", "image", "inspect", image, "--format", "{{index .RepoDigests 0}}")), label="docker image")
            digest = inspected.stdout.strip()
            if "@sha256:" not in digest:
                self._run(CampaignCommand(("docker", "pull", image), timeout_seconds=1800), label="docker pull")
                digest = self._run(CampaignCommand(("docker", "image", "inspect", image, "--format", "{{index .RepoDigests 0}}")), label="docker image").stdout.strip()
            if "@sha256:" not in digest:
                raise CampaignExecutionError("Docker image digest is unavailable")
            for arm in BenchmarkArm:
                workspace = self.root / "workspaces" / arm.value.lower() / task.instance_id
                workspace.parent.mkdir(parents=True, exist_ok=True)
                created = self._run(CampaignCommand(("docker", "create", image)), label="docker create").stdout.strip()
                if not created:
                    raise CampaignExecutionError("Docker create returned no container")
                try:
                    self._run(CampaignCommand(("docker", "cp", created + ":/testbed/.", str(workspace))), label="docker copy")
                finally:
                    # Removal is best effort only after a successful copy; a
                    # failed cleanup must not change the materialized record.
                    try:
                        self.runner.run(CampaignCommand(("docker", "rm", created)))
                    except (OSError, subprocess.SubprocessError):
                        pass
                head = self._run(CampaignCommand(("git", "-C", str(workspace), "rev-parse", "HEAD")), label="git").stdout.strip()
                if head != task.base_commit:
                    raise CampaignExecutionError("Materialized workspace base commit mismatch")
                capture = ProductWorkspaceCapture().capture(workspace, task_id=task.instance_id, command_id=uuid4())
                files = capture.baseline.files
                records[arm.value + ":" + task.instance_id] = _WorkspaceRecord(
                    path=_relative(self.root, workspace), image=image, image_digest=digest,
                    workspace_digest=capture.baseline.root_digest,
                    symlink_count=sum(item.is_symlink for item in files),
                    disk_bytes=sum(item.size_bytes for item in files),
                )
        state = state.model_copy(update={"prepared": True, "admission_count": len(self.protocol.tasks), "workspaces": records})
        self._save(state)

    def _require_prepared(self, state: _CampaignState) -> None:
        if not state.prepared or state.admission_count != 10 or len(state.workspaces) != 20:
            raise CampaignExecutionError("Campaign is not admitted; run prepare successfully first")

    def _records(self, state: _CampaignState, arm: BenchmarkArm) -> dict[str, _CampaignAttempt]:
        records = state.attempts.get(arm.value, ())
        return {record.instance_id: record for record in records}

    def run_agentforge(self, *, recover_running: bool = False, retry_failed: bool = False) -> None:
        self._run_arm(BenchmarkArm.AGENTFORGE, recover_running=recover_running, retry_failed=retry_failed, mini_root=None)

    def run_mini(self, *, mini_root: str | Path, recover_running: bool = False, retry_failed: bool = False) -> None:
        self._run_arm(BenchmarkArm.MINI_SWE_AGENT, recover_running=recover_running, retry_failed=retry_failed, mini_root=Path(mini_root))

    def _run_arm(self, arm: BenchmarkArm, *, recover_running: bool, retry_failed: bool, mini_root: Path | None) -> None:
        state = self._load(); self._require_prepared(state)
        if arm is BenchmarkArm.AGENTFORGE and state.admission_count != 10:
            raise CampaignExecutionError("AgentForge admission gate failed")
        records = self._records(state, arm)
        changed = False
        for task in self.protocol.tasks:
            existing = records.get(task.instance_id)
            if existing is not None and existing.status in {AttemptStatus.COMPLETED, AttemptStatus.FAILED}:
                if retry_failed and existing.status is AttemptStatus.FAILED:
                    raise CampaignExecutionError("Protocol permits only one model attempt")
                continue
            if existing is not None and existing.status is AttemptStatus.RUNNING:
                if not recover_running:
                    raise CampaignExecutionError("RUNNING attempt requires --recover-running")
                records[task.instance_id] = existing.model_copy(update={"status": AttemptStatus.FAILED, "failure_class": AttemptFailureClass.COMPATIBILITY_FAILED})
                changed = True
                continue
            records[task.instance_id] = _CampaignAttempt(instance_id=task.instance_id, attempt_index=1, status=AttemptStatus.RUNNING, failure_class=AttemptFailureClass.NONE)
            self._replace_records(state, arm, records)
            state = self._load()
            try:
                attempt = self._execute_attempt(arm, task, state, mini_root)
            except CampaignExecutionError:
                attempt = _CampaignAttempt(instance_id=task.instance_id, attempt_index=1, status=AttemptStatus.FAILED, failure_class=AttemptFailureClass.COMPATIBILITY_FAILED)
            records[task.instance_id] = attempt; changed = True
            self._replace_records(state, arm, records); state = self._load()
        if changed:
            self._replace_records(state, arm, records)

    def _replace_records(self, state: _CampaignState, arm: BenchmarkArm, records: Mapping[str, _CampaignAttempt]) -> None:
        attempts = dict(state.attempts)
        attempts[arm.value] = tuple(records[task.instance_id] for task in self.protocol.tasks if task.instance_id in records)
        self._save(state.model_copy(update={"attempts": attempts}))

    def _execute_attempt(self, arm: BenchmarkArm, task: Verified10Task, state: _CampaignState, mini_root: Path | None) -> _CampaignAttempt:
        record = state.workspaces[arm.value + ":" + task.instance_id]
        workspace = self.root / record.path
        if arm is BenchmarkArm.MINI_SWE_AGENT:
            if mini_root is None:
                raise CampaignExecutionError("mini root is required")
            self._validate_mini_root(mini_root)
            secret = os.environ.get("DEEPSEEK_API_KEY")
            if not secret:
                raise CampaignExecutionError("Mini runtime credential is unavailable")
            output = self.root / "mini-output" / task.instance_id; output.mkdir(parents=True, exist_ok=True)
            config = self.root / "configs" / "mini" / (task.instance_id + ".yaml"); config.parent.mkdir(parents=True, exist_ok=True)
            self._atomic(config, self._mini_config().encode("utf-8"))
            command = CampaignCommand(
                ("uv", "run", "--project", str(mini_root), "--frozen", "python", "-m", "minisweagent.run.benchmarks.swebench", "--subset", "verified", "--split", "test", "--filter", "^" + task.instance_id.replace("-", "\\-") + "$", "--output", str(output), "--workers", "1", "--model", "openai/deepseek-v4-flash", "--config", str(mini_root / "src/minisweagent/config/benchmarks/swebench.yaml"), "--config", str(config)),
                # This mapping is process-local only.  It is deliberately not
                # serialized into config, state, command arguments, or logs.
                environment={**os.environ, "OPENAI_API_KEY": secret}, timeout_seconds=1800,
            )
            result = self._run(command, label="mini")
            patch = self._mini_patch(output, task.instance_id)
            trajectory = output / task.instance_id / (task.instance_id + ".traj.json")
            return self._terminal_attempt(task.instance_id, patch, trajectory, model_calls=self._mini_calls(trajectory), steps=0)
        runtime = workspace / ".agentforge" / "runtime.toml"; runtime.parent.mkdir(exist_ok=True)
        self._atomic(runtime, self._agentforge_runtime(task.instance_id).encode("utf-8"))
        command = CampaignCommand(("uv", "run", "--frozen", "agentforge", "exec", "--workspace", str(workspace), "--database-path", ".agentforge/agentforge.db", "--model", "deepseek-v4-flash", "--max-steps", "80", task.instance_id), timeout_seconds=1800)
        result = self._run(command, label="agentforge")
        run_id = self._field(result.stdout, "run_id")
        patch = self._git_patch(workspace)
        trajectory = self.root / "trajectories" / "agentforge" / (task.instance_id + ".log")
        trajectory.parent.mkdir(parents=True, exist_ok=True); self._atomic(trajectory, result.stdout.encode("utf-8"))
        return self._terminal_attempt(task.instance_id, patch, trajectory, run_id=run_id, model_calls=0, steps=0)

    def _terminal_attempt(self, instance_id: str, patch: str, trajectory: Path, *, run_id: str | None = None, model_calls: int, steps: int) -> _CampaignAttempt:
        raw = trajectory.read_bytes() if trajectory.is_file() else b""
        return _CampaignAttempt(instance_id=instance_id, attempt_index=1, status=AttemptStatus.COMPLETED, failure_class=AttemptFailureClass.EMPTY if not patch else AttemptFailureClass.NONE, model_patch=patch, run_id=run_id, model_calls=model_calls, steps=steps, trajectory_path=_relative(self.root, trajectory) if trajectory.exists() else None, trajectory_sha256=hashlib.sha256(raw).hexdigest() if trajectory.exists() else None)

    @staticmethod
    def _field(text: str, name: str) -> str | None:
        for token in text.split():
            if token.startswith(name + "="):
                return token.split("=", 1)[1]
        return None

    def _git_patch(self, workspace: Path) -> str:
        try:
            return self._run(CampaignCommand(("git", "-C", str(workspace), "diff", "--binary")), label="git").stdout
        except CampaignExecutionError:
            return ""

    @staticmethod
    def _mini_config() -> str:
        return """agent:\n  step_limit: 50\n  cost_limit: 0.0\n  wall_time_limit_seconds: 1800\n  max_consecutive_format_errors: 3\nmodel:\n  model_name: openai/deepseek-v4-flash\n  model_kwargs:\n    drop_params: true\n    parallel_tool_calls: false\n    api_base: https://api.deepseek.com/v1\n    temperature: 0\n    extra_body:\n      thinking:\n        type: disabled\nenvironment:\n  cwd: /testbed\n  timeout: 120\n  run_args: [\"--rm\", \"--network=none\"]\n  container_timeout: 2h\n  pull_timeout: 1800\n"""

    @staticmethod
    def _agentforge_runtime(task_id: str) -> str:
        python = sys.executable.replace("\\", "/")
        return f'''[provider]\nkind = "deepseek"\n\n[model_budget]\nmax_model_requests = 52\nmax_retries = 2\nmax_output_tokens_per_request = 4096\nmax_total_tokens = 600000\n\n[policy]\ntask_id = "{task_id}"\npolicy_version = 1\ndifficulty = "ENGINEERING"\nbudget_profile = "SWE_BENCH_PASS1"\nallowed_write_paths = ["**"]\nforbidden_write_paths = [".agentforge/**", ".git/**"]\nprotected_paths = [".agentforge/**"]\nallowed_development_test_profiles = ["compile"]\nfinal_verification_profile_id = "verify"\nallow_file_creation = true\nallowed_create_paths = ["**"]\npath_case_sensitive = false\n\n[[profiles]]\nprofile_id = "compile"\nname = "Compile capability"\ndescription = "Generic syntax capability; official harness is Task7"\nexecutable = "{python}"\nargv = ["-m", "compileall", "-q", "."]\ncwd = "."\ntimeout_seconds = 120\nmax_output_bytes = 4096\nprofile_version = 1\npurpose = "development"\n\n[[profiles]]\nprofile_id = "verify"\nname = "Verification capability"\ndescription = "Official scoring is external"\nexecutable = "{python}"\nargv = ["-m", "compileall", "-q", "."]\ncwd = "."\ntimeout_seconds = 120\nmax_output_bytes = 4096\nprofile_version = 1\npurpose = "verification"\n'''

    @staticmethod
    def _validate_mini_root(root: Path) -> None:
        try:
            if root.is_symlink() or not root.is_dir(): raise ValueError
            commit = subprocess.run(("git", "-C", str(root), "rev-parse", "HEAD"), capture_output=True, text=True, check=False).stdout.strip()
            version = (root / "src" / "minisweagent" / "__init__.py").read_text(encoding="utf-8")
            lock = (root / "uv.lock").read_bytes()
        except OSError:
            raise CampaignExecutionError("Pinned mini root is invalid") from None
        if commit != "25941c89cfbc91eb40b3f8756348c91d9977d57e" or "2.4.6" not in version or not lock:
            raise CampaignExecutionError("Pinned mini root does not match protocol")

    @staticmethod
    def _mini_patch(output: Path, instance_id: str) -> str:
        try:
            data = json.loads((output / "preds.json").read_text(encoding="utf-8"))
            value = data.get(instance_id, "") if isinstance(data, dict) else ""
            return value if isinstance(value, str) else ""
        except (OSError, json.JSONDecodeError):
            return ""

    @staticmethod
    def _mini_calls(trajectory: Path) -> int:
        try:
            value = json.loads(trajectory.read_text(encoding="utf-8"))["info"]["model_stats"]["api_calls"]
            return value if type(value) is int and value >= 0 else 0
        except (OSError, KeyError, TypeError, json.JSONDecodeError):
            return 0

    def status(self) -> dict[str, int]:
        state = self._load()
        counts = {"planned": 0, "running": 0, "completed": 0, "failed": 0, "admission": state.admission_count}
        for arm_records in state.attempts.values():
            for record in arm_records: counts[record.status.value.lower()] += 1
        return counts

    def finalize_predictions(self, arm: BenchmarkArm) -> CampaignArtifactResult:
        state = self._load(); self._require_prepared(state)
        if arm.value in state.finalized_arms:
            raise CampaignExecutionError("Predictions for this arm are already finalized")
        records = self._records(state, arm)
        if set(records) != {task.instance_id for task in self.protocol.tasks} or any(record.status not in {AttemptStatus.COMPLETED, AttemptStatus.FAILED} for record in records.values()):
            raise CampaignExecutionError("Finalization requires ten terminal attempts")
        predictions: list[SWEbenchPrediction] = []; attempts: list[BenchmarkAttemptRecord] = []
        namespace: Literal["agentforge", "mini-swe-agent"] = "agentforge" if arm is BenchmarkArm.AGENTFORGE else "mini-swe-agent"
        for task in self.protocol.tasks:
            record = records[task.instance_id]
            binding = SWEbenchInstanceBinding(instance_id=task.instance_id, repo=task.repo, base_commit=task.base_commit)
            prediction = SWEbenchPrediction.empty(binding, self.protocol.model, namespace=namespace) if not record.model_patch else SWEbenchPrediction(instance_id=task.instance_id, model_name_or_path=namespace + ":" + self.protocol.model, model_patch=record.model_patch, base_commit=task.base_commit, patch_sha256=hashlib.sha256(record.model_patch.encode()).hexdigest())
            predictions.append(prediction)
            attempts.append(BenchmarkAttemptRecord(protocol_sha256=self.protocol.protocol_sha256, arm=arm, instance_id=task.instance_id, attempt_index=1, status=record.status, failure_class=record.failure_class, model_calls=record.model_calls, steps=record.steps, trajectory_path=record.trajectory_path, trajectory_sha256=record.trajectory_sha256, prediction_patch_sha256=prediction.patch_sha256))
        result = finalize_verified10_campaign(self.protocol, attempts, predictions, self.root / (arm.value.lower() + "-predictions.json"), self.root / (arm.value.lower() + "-ledger.json"), arm=arm)
        self._save(state.model_copy(update={"finalized_arms": (*state.finalized_arms, arm.value)}))
        return result
