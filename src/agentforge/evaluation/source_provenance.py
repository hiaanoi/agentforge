import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agentforge.evaluation.protocol import canonical_digest

_MAX_GIT_OUTPUT_BYTES = 64 * 1024
_GIT_TIMEOUT_SECONDS = 10.0


class SourceProvenanceError(RuntimeError):
    pass


class SourceProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    git_commit_sha: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    git_worktree_clean: bool
    runtime_source_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    pyproject_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    uv_lock_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    provenance_digest: str = Field(
        default="",
        pattern=r"^$|^[0-9a-f]{64}$",
    )

    @model_validator(mode="after")
    def validate_digest(self) -> Self:
        expected = canonical_digest(
            self.model_dump(mode="json", exclude={"provenance_digest"})
        )
        if self.provenance_digest and self.provenance_digest != expected:
            raise ValueError("Source provenance digest does not match facts")
        object.__setattr__(self, "provenance_digest", expected)
        return self


class SourceProvenanceCollector:
    def __init__(self, git_executable: Path | None = None) -> None:
        if git_executable is None:
            discovered = shutil.which("git")
            if discovered is None:
                raise SourceProvenanceError("Git executable is unavailable")
            git_executable = Path(discovered)
        try:
            resolved = git_executable.resolve(strict=True)
        except OSError as exc:
            raise SourceProvenanceError(
                "Git executable could not be resolved"
            ) from exc
        if not resolved.is_file():
            raise SourceProvenanceError("Git executable must be a file")
        self._git_executable = resolved

    def collect(self, repository_root: Path) -> SourceProvenance:
        try:
            root = repository_root.resolve(strict=True)
        except OSError as exc:
            raise SourceProvenanceError(
                "Source repository could not be resolved"
            ) from exc
        if not root.is_dir():
            raise SourceProvenanceError("Source repository must be a directory")

        commit = self._run_git(
            root,
            "rev-parse",
            "--verify",
            "HEAD",
        ).decode("ascii").strip()
        status = self._run_git(
            root,
            "status",
            "--porcelain=v1",
            "--untracked-files=normal",
            "--ignored=no",
        )
        tracked_output = self._run_git(
            root,
            "ls-files",
            "-z",
            "--",
            "src/agentforge",
        )
        tracked_paths = tuple(
            item.decode("utf-8")
            for item in tracked_output.split(b"\0")
            if item
        )
        if not tracked_paths:
            raise SourceProvenanceError(
                "Tracked AgentForge runtime source is missing"
            )
        return SourceProvenance(
            git_commit_sha=commit,
            git_worktree_clean=not status,
            runtime_source_digest=self._tree_digest(root, tracked_paths),
            pyproject_sha256=self._file_digest(root, "pyproject.toml"),
            uv_lock_sha256=self._file_digest(root, "uv.lock"),
        )

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
            raise SourceProvenanceError(
                "Bound Git command could not complete"
            ) from exc
        if (
            len(completed.stdout) > _MAX_GIT_OUTPUT_BYTES
            or len(completed.stderr) > _MAX_GIT_OUTPUT_BYTES
        ):
            raise SourceProvenanceError("Bound Git output exceeded its limit")
        if completed.returncode != 0:
            raise SourceProvenanceError("Bound Git command failed")
        return completed.stdout

    @staticmethod
    def _git_environment() -> dict[str, str]:
        environment = {
            "LANG": "C",
            "LC_ALL": "C",
            "NO_COLOR": "1",
        }
        if sys.platform == "win32":
            normalized = {
                name.upper(): value for name, value in os.environ.items()
            }
            for name in ("SYSTEMROOT", "WINDIR"):
                value = normalized.get(name)
                if value:
                    environment[name] = value
        return environment

    @classmethod
    def _tree_digest(
        cls,
        root: Path,
        tracked_paths: tuple[str, ...],
    ) -> str:
        digest = hashlib.sha256()
        for relative_text in sorted(set(tracked_paths)):
            relative = PurePosixPath(relative_text)
            if (
                relative.is_absolute()
                or ".." in relative.parts
                or not relative.parts
            ):
                raise SourceProvenanceError(
                    "Tracked runtime path is invalid"
                )
            candidate = root.joinpath(*relative.parts)
            if candidate.is_symlink():
                raise SourceProvenanceError(
                    "Tracked runtime source cannot be a symlink"
                )
            try:
                resolved = candidate.resolve(strict=True)
                resolved.relative_to(root)
            except (OSError, ValueError) as exc:
                raise SourceProvenanceError(
                    "Tracked runtime path escapes the repository"
                ) from exc
            if not resolved.is_file():
                raise SourceProvenanceError(
                    "Tracked runtime source must be a file"
                )
            data = resolved.read_bytes()
            digest.update(relative.as_posix().encode("utf-8"))
            digest.update(b"\0")
            digest.update(hashlib.sha256(data).digest())
            digest.update(b"\0")
        return digest.hexdigest()

    @staticmethod
    def _file_digest(root: Path, relative: str) -> str:
        path = root / relative
        if path.is_symlink():
            raise SourceProvenanceError(
                "Bound dependency file cannot be a symlink"
            )
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(root)
        except (OSError, ValueError) as exc:
            raise SourceProvenanceError(
                "Bound dependency file is unavailable"
            ) from exc
        if not resolved.is_file():
            raise SourceProvenanceError(
                "Bound dependency path must be a file"
            )
        return hashlib.sha256(resolved.read_bytes()).hexdigest()
