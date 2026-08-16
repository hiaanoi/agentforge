from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

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
        if not model_identity or any(character.isspace() for character in model_identity):
            raise SWEbenchPredictionError("Model identity must be non-empty and whitespace-free")

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
            model_name_or_path=f"agentforge:{model_identity}",
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
    target = path.resolve(strict=False)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.tmp")
    payload = json.dumps(
        prediction.harness_record(),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    try:
        temporary.write_text(payload + "\n", encoding="utf-8", newline="\n")
        os.replace(temporary, target)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise SWEbenchPredictionError("SWE-bench prediction could not be saved") from exc
