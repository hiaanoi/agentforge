from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

_GIT_TIMEOUT_SECONDS = 30.0
_MAX_GIT_OUTPUT_BYTES = 1024 * 1024


class SWEbenchPredictionError(RuntimeError):
    pass


class SWEbenchInstanceBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    instance_id: str = Field(pattern=r"^[A-Za-z0-9_.-]+$")
    repo: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    base_commit: str = Field(pattern=r"^[0-9a-f]{40}$")


class SWEbenchPrediction(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    instance_id: str
    model_name_or_path: str
    model_patch: str
    base_commit: str
    patch_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_patch_digest(self) -> SWEbenchPrediction:
        actual = hashlib.sha256(self.model_patch.encode("utf-8")).hexdigest()
        if self.patch_sha256 != actual:
            raise ValueError("patch_sha256 does not match model_patch bytes")
        return self

    @classmethod
    def empty(
        cls,
        binding: SWEbenchInstanceBinding,
        model_identity: str,
        *,
        namespace: Literal["agentforge", "mini-swe-agent"] = "agentforge",
    ) -> SWEbenchPrediction:
        """Construct the standard harness record for a failed/empty attempt."""

        return cls(
            instance_id=binding.instance_id,
            model_name_or_path=_normalized_model_identity(model_identity, namespace=namespace),
            model_patch="",
            base_commit=binding.base_commit,
            patch_sha256=hashlib.sha256(b"").hexdigest(),
        )

    def harness_record(self) -> dict[str, str]:
        return {
            "instance_id": self.instance_id,
            "model_name_or_path": self.model_name_or_path,
            "model_patch": self.model_patch,
        }


class SWEbenchPredictionExporter:
    def __init__(self, git_executable: Path | None = None) -> None:
        if git_executable is None:
            discovered = shutil.which("git")
            if discovered is None:
                raise SWEbenchPredictionError("Git executable is unavailable")
            git_executable = Path(discovered)
        try:
            resolved = git_executable.resolve(strict=True)
        except OSError as exc:
            raise SWEbenchPredictionError("Git executable could not be resolved") from exc
        if not resolved.is_file():
            raise SWEbenchPredictionError("Git executable must be a file")
        self._git_executable = resolved

    def capture(
        self,
        workspace: Path,
        *,
        binding: SWEbenchInstanceBinding,
        model_identity: str,
    ) -> SWEbenchPrediction:
        root = self._workspace_root(workspace)
        normalized_identity = _normalized_model_identity(model_identity, namespace="agentforge")

        head = self._run_git(root, "rev-parse", "--verify", "HEAD").decode(
            "ascii"
        ).strip()
        if head != binding.base_commit:
            raise SWEbenchPredictionError("Workspace HEAD does not match the bound base commit")

        status = self._run_git(
            root,
            "status",
            "--porcelain=v1",
            "--untracked-files=normal",
            "--ignored=no",
        )
        if any(line.startswith(b"?? ") for line in status.splitlines()):
            raise SWEbenchPredictionError(
                "Workspace contains untracked files that the patch would omit"
            )

        patch_bytes = self._run_git(
            root,
            "diff",
            "--binary",
            "--no-ext-diff",
            "--src-prefix=a/",
            "--dst-prefix=b/",
            "HEAD",
            "--",
        )
        if not patch_bytes:
            raise SWEbenchPredictionError("SWE-bench model patch is empty")
        try:
            model_patch = patch_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SWEbenchPredictionError("SWE-bench model patch is not valid UTF-8") from exc

        return SWEbenchPrediction(
            instance_id=binding.instance_id,
            model_name_or_path=normalized_identity,
            model_patch=model_patch,
            base_commit=binding.base_commit,
            patch_sha256=hashlib.sha256(patch_bytes).hexdigest(),
        )

    @staticmethod
    def _workspace_root(workspace: Path) -> Path:
        requested = Path(os.path.abspath(workspace))
        if requested.is_symlink():
            raise SWEbenchPredictionError("SWE-bench workspace cannot be a symlink")
        try:
            resolved = requested.resolve(strict=True)
        except OSError as exc:
            raise SWEbenchPredictionError("SWE-bench workspace could not be resolved") from exc
        if resolved != requested:
            raise SWEbenchPredictionError("SWE-bench workspace cannot contain symlink components")
        if not resolved.is_dir():
            raise SWEbenchPredictionError("SWE-bench workspace must be a directory")
        return resolved

    def _run_git(self, root: Path, *arguments: str) -> bytes:
        try:
            completed = subprocess.run(
                [str(self._git_executable), *arguments],
                cwd=root,
                env=self._git_environment(),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=_GIT_TIMEOUT_SECONDS,
                check=False,
                shell=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SWEbenchPredictionError("Bound Git command could not complete") from exc
        if len(completed.stdout) + len(completed.stderr) > _MAX_GIT_OUTPUT_BYTES:
            raise SWEbenchPredictionError("Bound Git output exceeded its limit")
        if completed.returncode != 0:
            raise SWEbenchPredictionError("Bound Git command failed")
        return completed.stdout

    @staticmethod
    def _git_environment() -> dict[str, str]:
        environment = {"LANG": "C", "LC_ALL": "C", "NO_COLOR": "1"}
        if sys.platform == "win32":
            normalized = {name.upper(): value for name, value in os.environ.items()}
            for name in ("SYSTEMROOT", "WINDIR"):
                value = normalized.get(name)
                if value:
                    environment[name] = value
        return environment


def save_swebench_prediction(path: Path, prediction: SWEbenchPrediction) -> None:
    prediction = _revalidate_prediction(prediction)
    target = path.resolve(strict=False)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.tmp")
    payload = json.dumps(
        prediction.harness_record(),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    _atomic_write(target, (payload + "\n").encode("utf-8"), temporary)


def save_swebench_predictions(
    path: Path,
    predictions: list[SWEbenchPrediction] | tuple[SWEbenchPrediction, ...],
    *,
    expected_instance_ids: tuple[str, ...] | list[str] | None = None,
    protocol: object | None = None,
) -> None:
    """Atomically write the official SWE-bench standard JSON array.

    The output intentionally contains only harness fields.  ``expected_instance_ids``
    (or a protocol exposing ``tasks``) supplies the denominator and ordering.
    """

    target = path.resolve(strict=False)
    payload = serialize_swebench_predictions(
        predictions,
        expected_instance_ids=expected_instance_ids,
        protocol=protocol,
    )
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(target, payload, target.with_name(f".{target.name}.tmp"))
    except OSError as exc:
        raise SWEbenchPredictionError("SWE-bench predictions could not be saved") from exc


def serialize_swebench_predictions(
    predictions: list[SWEbenchPrediction] | tuple[SWEbenchPrediction, ...],
    *,
    expected_instance_ids: tuple[str, ...] | list[str] | None = None,
    protocol: object | None = None,
) -> bytes:
    """Serialize the official prediction array without touching the filesystem."""

    try:
        predictions = tuple(_revalidate_prediction(prediction) for prediction in predictions)
    except (TypeError, ValueError):
        raise SWEbenchPredictionError("SWE-bench prediction failed validation") from None
    if expected_instance_ids is not None and protocol is not None:
        raise SWEbenchPredictionError("Specify expected_instance_ids or protocol, not both")
    if protocol is not None:
        try:
            expected_instance_ids = tuple(task.instance_id for task in protocol.tasks)  # type: ignore[attr-defined]
        except (AttributeError, TypeError):
            raise SWEbenchPredictionError("Protocol does not expose ordered tasks") from None
    if expected_instance_ids is None:
        raise SWEbenchPredictionError("Expected instance IDs or protocol are required")
    expected = tuple(expected_instance_ids)
    if len(set(expected)) != len(expected):
        raise SWEbenchPredictionError("Expected instance IDs contain duplicates")
    actual = [prediction.instance_id for prediction in predictions]
    if len(set(actual)) != len(actual):
        raise SWEbenchPredictionError("Predictions contain duplicate instance IDs")
    if set(actual) != set(expected):
        raise SWEbenchPredictionError("Predictions do not match expected instance IDs")
    by_instance = {prediction.instance_id: prediction for prediction in predictions}
    records = [by_instance[instance_id].harness_record() for instance_id in expected]
    payload = json.dumps(records, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return (payload + "\n").encode("utf-8")


def load_swebench_predictions(path: str | Path) -> tuple[dict[str, str], ...]:
    """Read the official standard prediction array without exposing private metadata."""

    try:
        decoded = json.loads(Path(path).resolve(strict=True).read_bytes())
        if not isinstance(decoded, list):
            raise ValueError
        records: list[dict[str, str]] = []
        for item in decoded:
            if not isinstance(item, dict) or set(item) != {
                "instance_id",
                "model_name_or_path",
                "model_patch",
            }:
                raise ValueError
            if not all(isinstance(value, str) for value in item.values()):
                raise ValueError
            records.append(dict(item))
        return tuple(records)
    except (OSError, UnicodeError, ValueError, TypeError):
        raise SWEbenchPredictionError("SWE-bench predictions could not be loaded") from None


read_swebench_predictions = load_swebench_predictions


def _revalidate_prediction(prediction: SWEbenchPrediction) -> SWEbenchPrediction:
    try:
        return SWEbenchPrediction.model_validate(prediction.model_dump(mode="python"))
    except (AttributeError, TypeError, ValueError, ValidationError):
        raise SWEbenchPredictionError("SWE-bench prediction failed validation") from None


def _normalized_model_identity(
    model_identity: str, *, namespace: Literal["agentforge", "mini-swe-agent"]
) -> str:
    if not model_identity or any(character.isspace() for character in model_identity):
        raise SWEbenchPredictionError("Model identity must be non-empty and whitespace-free")
    if model_identity.startswith(("agentforge:", "mini-swe-agent:")):
        raise SWEbenchPredictionError("Model identity must not include a namespace prefix")
    return f"{namespace}:{model_identity}"


def _atomic_write(target: Path, payload: bytes, temporary: Path) -> None:
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
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise SWEbenchPredictionError("SWE-bench prediction could not be saved") from exc
