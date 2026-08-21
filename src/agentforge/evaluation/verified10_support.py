"""Command, source-verification, and durable state adapters for Verified-10."""

from __future__ import annotations

import hashlib
import json
import re
import stat
import subprocess
import sys
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agentforge.evaluation.verified10_campaign import (
    EXPECTED_INSTANCE_IDS,
    AttemptFailureClass,
    AttemptStatus,
    BenchmarkArm,
    CampaignExecutionError,
    Verified10Protocol,
    validate_public_dataset_rows,
)

SHA256_PATTERN = r"^[0-9a-f]{64}$"
GIT_COMMIT_PATTERN = r"^[0-9a-f]{40}$"
MINI_COMMIT = "25941c89cfbc91eb40b3f8756348c91d9977d57e"
MINI_VERSION = "2.4.6"
MINI_LOCK_SHA256 = "cbff5b81ed1a8b8763fa5b57f71a6c1b365a2a79eff44b096f2f1258ea479eb8"
APPROVAL_REQUIRED = 20
_TERMINAL_RETURN_CODES = frozenset({0, 1, 20, 22})
_UUID_PATTERN = re.compile(
    r"(?<![0-9a-f])([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?![0-9a-f])",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class CampaignCommand:
    argv: tuple[str, ...]
    cwd: Path | None = None
    environment: Mapping[str, str] | None = field(default=None, repr=False, compare=False)
    timeout_seconds: int | None = None
    acceptable_returncodes: frozenset[int] = frozenset({0})

    @property
    def environment_names(self) -> tuple[str, ...]:
        return tuple(sorted(self.environment)) if self.environment is not None else ()


@dataclass(frozen=True, slots=True)
class CampaignCommandResult:
    returncode: int
    stdout: str
    stderr: str


class CommandRunner(Protocol):
    def run(self, command: CampaignCommand) -> CampaignCommandResult: ...


class SubprocessCommandRunner:
    def run(self, command: CampaignCommand) -> CampaignCommandResult:
        completed = subprocess.run(
            command.argv,
            cwd=command.cwd,
            env=dict(command.environment) if command.environment is not None else None,
            capture_output=True,
            text=True,
            timeout=command.timeout_seconds,
            check=False,
            shell=False,
        )
        return CampaignCommandResult(completed.returncode, completed.stdout, completed.stderr)


@dataclass(frozen=True, slots=True)
class DockerImageBinding:
    tag: str
    digest_reference: str

    @classmethod
    def from_inspect(cls, tag: str, output: str) -> DockerImageBinding:
        repo = _image_repository(tag)
        normalized_repo = _normalized_registry_repository(repo)
        try:
            decoded = json.loads(output)
        except json.JSONDecodeError:
            decoded = output.strip()
        values: object = decoded
        if (
            isinstance(decoded, list)
            and decoded
            and all(isinstance(item, dict) for item in decoded)
        ):
            values = decoded[0].get("RepoDigests")
        if isinstance(values, str):
            values = [values]
        if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
            raise CampaignExecutionError("Docker image inspect did not return RepoDigests")
        matching: set[str] = set()
        for value in values:
            candidate, separator, digest = value.partition("@")
            if (
                separator
                and _normalized_registry_repository(candidate) == normalized_repo
                and re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
            ):
                matching.add(digest)
        if not matching:
            raise CampaignExecutionError(
                "Docker image RepoDigest does not match requested repository"
            )
        if len(matching) != 1:
            raise CampaignExecutionError("Docker image must have one unique matching RepoDigest")
        return cls(tag=tag, digest_reference=f"{repo}@{next(iter(matching))}")


def _image_repository(tag: str) -> str:
    slash = tag.rfind("/")
    colon = tag.rfind(":")
    return tag[:colon] if colon > slash else tag


def _normalized_registry_repository(value: str) -> str:
    normalized = value.casefold()
    for prefix in ("docker.io/", "index.docker.io/"):
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix) :]
            break
    return normalized if "/" in normalized else f"library/{normalized}"


def agentforge_config() -> str:
    return (
        'database_path = ".agentforge/agentforge.db"\n'
        'model = "deepseek-v4-flash"\n'
        "max_steps = 80\n"
        'profile_ids = ["compile", "default", "verify"]\n'
    )


def mini_config(*, step_limit: int = 50, wall_time_seconds: int = 1800) -> str:
    return (
        f"agent:\n  step_limit: {step_limit}\n  cost_limit: 0.0\n"
        f"  wall_time_limit_seconds: {wall_time_seconds}\n  max_consecutive_format_errors: 3\n"
        "model:\n  model_name: openai/deepseek-v4-flash\n  model_kwargs:\n"
        "    drop_params: true\n    parallel_tool_calls: false\n"
        "    api_base: https://api.deepseek.com/v1\n    temperature: 0\n"
        "    extra_body:\n      thinking:\n        type: disabled\n"
        "  cost_tracking: ignore_errors\n"
        "environment:\n  cwd: /testbed\n  timeout: 120\n"
        '  run_args: ["--rm", "--network=none"]\n'
        "  container_timeout: 2h\n  pull_timeout: 1800\n"
    )


def agentforge_runtime(
    task_id: str,
    verifier_root: Path,
    *,
    budget_profile: str = "SWE_BENCH_PASS1",
    max_model_requests: int = 52,
    max_total_tokens: int = 600000,
    repair_engine: str = "native",
    provider_kind: str = "deepseek",
) -> str:
    python = str(Path(sys.executable).resolve(strict=True)).replace("\\", "/")
    verifier = str(verifier_root.resolve(strict=True)).replace("\\", "/")
    engine_binding = (
        "" if repair_engine == "native" else f'repair_engine = "{repair_engine}"\n\n'
    )
    return f'''{engine_binding}[provider]
kind = "{provider_kind}"
timeout_seconds = 600.0
temperature = 0.0

[model_budget]
max_model_requests = {max_model_requests}
max_retries = 2
max_output_tokens_per_request = 4096
max_total_tokens = {max_total_tokens}

[policy]
task_id = "{task_id}"
policy_version = 1
difficulty = "ENGINEERING"
budget_profile = "{budget_profile}"
allowed_write_paths = ["**"]
forbidden_write_paths = [".agentforge/**", ".git/**"]
protected_paths = [".agentforge/**", ".git/**"]
allowed_development_test_profiles = ["compile", "default"]
final_verification_profile_id = "verify"
allow_file_creation = true
allowed_create_paths = ["**"]
max_created_files = 10
max_changed_files = 20
max_total_changed_bytes = 5242880
max_single_file_changed_bytes = 1048576
path_case_sensitive = false

[[profiles]]
profile_id = "default"
name = "Default development capability"
description = "Default development verification profile"
executable = "{python}"
argv = ["-m", "compileall", "-q", "."]
cwd = "."
timeout_seconds = 120
max_output_bytes = 4096
profile_version = 1
purpose = "development"

[[profiles]]
profile_id = "compile"
name = "Compile capability"
description = "Generic syntax capability; official scoring is external"
executable = "{python}"
argv = ["-m", "compileall", "-q", "."]
cwd = "."
timeout_seconds = 120
max_output_bytes = 4096
profile_version = 1
purpose = "development"

[[profiles]]
profile_id = "verify"
name = "Verification capability"
description = "Final syntax verification; official scoring is external"
executable = "{python}"
argv = ["-m", "compileall", "-q", "{{SOURCE}}"]
cwd = "."
timeout_seconds = 120
max_output_bytes = 4096
profile_version = 1
purpose = "verification"
verifier_root = "{verifier}"
'''


def select_and_validate_public_rows(
    rows: Sequence[Mapping[str, object]], protocol: Verified10Protocol
) -> tuple[dict[str, str], ...]:
    """Filter a complete public split and project the frozen ten in protocol order."""

    expected = {task.instance_id for task in protocol.tasks}
    selected: dict[str, Mapping[str, object]] = {}
    for row in rows:
        instance_id = row.get("instance_id")
        if instance_id not in expected:
            continue
        assert isinstance(instance_id, str)
        if instance_id in selected:
            raise ValueError(f"duplicate selected dataset row: {instance_id}")
        selected[instance_id] = row
    missing = expected - set(selected)
    if missing:
        raise ValueError("missing selected dataset rows")
    ordered = tuple(selected[task.instance_id] for task in protocol.tasks)
    return validate_public_dataset_rows(ordered, protocol)


class SourceVerifier(Protocol):
    def verify(self, root: Path) -> None: ...


class MiniSourceVerifier:
    """Verify the exact local mini-swe-agent source without network access."""

    def verify(self, root: Path) -> None:
        requested = root.absolute()
        try:
            if requested.is_symlink() or requested.resolve(strict=True) != requested:
                raise CampaignExecutionError("Pinned mini root is not a safe local checkout")
            if not requested.is_dir() or not (requested / ".git").exists():
                raise CampaignExecutionError("Pinned mini root is not a local Git checkout")
            head = self._git(requested, "rev-parse", "--verify", "HEAD")
            if head != MINI_COMMIT:
                raise CampaignExecutionError("Pinned mini root commit does not match protocol")
            if self._git(requested, "status", "--porcelain=v1", "--untracked-files=no"):
                raise CampaignExecutionError("Pinned mini root tracked tree is not clean")
            lock = requested / "uv.lock"
            if hashlib.sha256(lock.read_bytes()).hexdigest() != MINI_LOCK_SHA256:
                raise CampaignExecutionError(
                    "Pinned mini root uv.lock hash does not match protocol"
                )
            if self._version(requested) != MINI_VERSION:
                raise CampaignExecutionError("Pinned mini root version does not match protocol")
            builtin = requested / "src" / "minisweagent" / "config" / "benchmarks" / "swebench.yaml"
            metadata = builtin.lstat()
            if not stat.S_ISREG(metadata.st_mode) or builtin.is_symlink():
                raise CampaignExecutionError("Pinned mini builtin SWE-bench config is unavailable")
        except CampaignExecutionError:
            raise
        except (OSError, UnicodeError, ValueError, tomllib.TOMLDecodeError):
            raise CampaignExecutionError("Pinned mini root is invalid") from None

    @staticmethod
    def _git(root: Path, *arguments: str) -> str:
        try:
            completed = subprocess.run(
                ("git", "-C", str(root), *arguments),
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
                shell=False,
            )
        except (OSError, subprocess.SubprocessError):
            raise CampaignExecutionError("Pinned mini root Git verification failed") from None
        if completed.returncode != 0:
            raise CampaignExecutionError("Pinned mini root Git verification failed")
        return completed.stdout.strip()

    @staticmethod
    def _version(root: Path) -> str:
        pyproject = root / "pyproject.toml"
        if pyproject.is_file():
            parsed = tomllib.loads(pyproject.read_text(encoding="utf-8"))
            project = parsed.get("project")
            if isinstance(project, dict):
                version = project.get("version")
                if isinstance(version, str):
                    return version
        source = (root / "src" / "minisweagent" / "__init__.py").read_text(encoding="utf-8")
        match = re.search(r"^__version__\s*=\s*['\"]([^'\"]+)['\"]", source, re.MULTILINE)
        return match.group(1) if match else ""


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


def _safe_relative(value: str) -> str:
    normalized = value.replace("\\", "/")
    if not normalized or normalized.startswith("/") or ":" in normalized:
        raise ValueError("artifact path must be relative")
    if any(part in {"", ".", ".."} for part in normalized.split("/")):
        raise ValueError("artifact path contains an unsafe component")
    return normalized


class CampaignAttempt(_FrozenModel):
    instance_id: str
    attempt_index: Literal[1]
    status: AttemptStatus
    failure_class: AttemptFailureClass = AttemptFailureClass.NONE
    model_patch: str = ""
    run_id: str | None = None
    terminal_reason: str | None = None
    model_calls: int | None = Field(default=None, ge=0)
    steps: int | None = Field(default=None, ge=0)
    provider_prompt_tokens: int | None = Field(default=None, ge=0)
    provider_completion_tokens: int | None = Field(default=None, ge=0)
    provider_total_tokens: int | None = Field(default=None, ge=0)
    approval_count: int | None = Field(default=None, ge=0)
    edit_count: int | None = Field(default=None, ge=0)
    test_count: int | None = Field(default=None, ge=0)
    event_count: int | None = Field(default=None, ge=0)
    wall_time_seconds: float | None = Field(default=None, ge=0)
    trajectory_path: str | None = None
    trajectory_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)
    telemetry_unavailable: tuple[str, ...] = ()
    provider_capabilities: Mapping[str, str] = Field(
        default_factory=lambda: {"temperature_control": "BOUND"}
    )

    @field_validator("telemetry_unavailable", mode="before")
    @classmethod
    def normalize_unavailable(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @field_validator("trajectory_path")
    @classmethod
    def relative_trajectory(cls, value: str | None) -> str | None:
        return _safe_relative(value) if value is not None else None

    @field_validator("run_id")
    @classmethod
    def durable_run_id_is_uuid(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            return str(UUID(value))
        except ValueError:
            raise ValueError("run_id must be a UUID") from None

    @model_validator(mode="after")
    def consistent_terminal_state(self) -> Self:
        terminal = self.status in {AttemptStatus.COMPLETED, AttemptStatus.FAILED}
        if self.status is AttemptStatus.FAILED and self.failure_class is AttemptFailureClass.NONE:
            raise ValueError("failed attempt requires failure class")
        if not terminal and self.failure_class is not AttemptFailureClass.NONE:
            raise ValueError("nonterminal attempt cannot carry failure class")
        return self


class WorkspaceRecord(_FrozenModel):
    workspace_role: Literal["PREPARED_PROVENANCE"] = "PREPARED_PROVENANCE"
    path: str
    image_tag: str
    image_digest: str = Field(pattern=r"^.+@sha256:[0-9a-f]{64}$")
    head: str = Field(pattern=GIT_COMMIT_PATTERN)
    workspace_digest: str = Field(pattern=SHA256_PATTERN)
    symlink_count: int = Field(ge=0)
    disk_bytes: int = Field(ge=0)

    @field_validator("path")
    @classmethod
    def relative_path(cls, value: str) -> str:
        return _safe_relative(value)


class CampaignState(_FrozenModel):
    schema_version: Literal[2] = 2
    protocol_digest: str = Field(pattern=SHA256_PATTERN)
    prepared: bool = False
    admission_count: int = Field(default=0, ge=0, le=10)
    public_tasks: dict[str, str] = Field(default_factory=dict)
    workspaces: dict[str, WorkspaceRecord] = Field(default_factory=dict)
    attempts: dict[str, tuple[CampaignAttempt, ...]] = Field(default_factory=dict)
    finalized_arms: tuple[str, ...] = ()

    @field_validator("attempts", mode="before")
    @classmethod
    def normalize_attempt_arrays(cls, value: object) -> object:
        if isinstance(value, dict):
            return {
                key: tuple(records) if isinstance(records, list) else records
                for key, records in value.items()
            }
        return value

    @field_validator("finalized_arms", mode="before")
    @classmethod
    def normalize_finalized_array(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def validate_topology(self) -> Self:
        expected = set(EXPECTED_INSTANCE_IDS)
        workspace_keys = {
            f"{arm.value}:{instance_id}"
            for arm in BenchmarkArm
            for instance_id in EXPECTED_INSTANCE_IDS
        }
        if self.prepared:
            if self.admission_count != 10 or set(self.public_tasks) != expected:
                raise ValueError("prepared campaign requires exactly ten public tasks")
            if set(self.workspaces) != workspace_keys:
                raise ValueError("prepared campaign requires twenty bound workspaces")
        elif self.admission_count or self.public_tasks or self.workspaces:
            raise ValueError("unprepared campaign cannot contain admitted workspaces")
        if set(self.attempts) - {arm.value for arm in BenchmarkArm}:
            raise ValueError("campaign attempts contain an unknown arm")
        for records in self.attempts.values():
            ids = [record.instance_id for record in records]
            if len(ids) != len(set(ids)) or not set(ids).issubset(expected):
                raise ValueError("campaign attempts contain invalid or duplicate instances")
        if len(set(self.finalized_arms)) != len(self.finalized_arms) or set(self.finalized_arms) - {
            arm.value for arm in BenchmarkArm
        }:
            raise ValueError("campaign finalized arms are invalid")
        return self


def swebench_image_name(instance_id: str) -> str:
    if instance_id not in EXPECTED_INSTANCE_IDS:
        raise CampaignExecutionError("Invalid SWE-bench instance identifier")
    mapped = instance_id.replace("__", "_1776_").lower()
    return f"docker.io/swebench/sweb.eval.x86_64.{mapped}:latest"


def _relative(root: Path, path: Path) -> str:
    try:
        value = path.absolute().relative_to(root.absolute()).as_posix()
    except ValueError:
        raise CampaignExecutionError("Campaign artifact path escaped output directory") from None
    try:
        return _safe_relative(value)
    except ValueError:
        raise CampaignExecutionError("Campaign artifact path is invalid") from None


def _resolve_under_root(root: Path, relative: str, *, require_exists: bool = True) -> Path:
    try:
        normalized = _safe_relative(relative)
        target = root.joinpath(*normalized.split("/")).absolute()
        target.relative_to(root.absolute())
        current = root.absolute()
        for part in normalized.split("/"):
            current /= part
            if current.exists() or current.is_symlink():
                metadata = current.lstat()
                if (
                    stat.S_ISLNK(metadata.st_mode)
                    or getattr(metadata, "st_file_attributes", 0) & 0x400
                ):
                    raise CampaignExecutionError("Campaign artifact path contains a link")
        if require_exists and not target.exists():
            raise CampaignExecutionError("Campaign artifact path is missing")
        return target
    except CampaignExecutionError:
        raise
    except (OSError, ValueError):
        raise CampaignExecutionError("Campaign artifact path is invalid") from None
