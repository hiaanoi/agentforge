import hashlib
import json
import os
import shutil
import stat
import tempfile
from enum import StrEnum
from pathlib import Path
from typing import Literal, Self
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agentforge.domain.models import UtcDatetime, utc_now
from agentforge.evaluation.task_definition import EvaluationTaskDefinition
from agentforge.tools.paths import WorkspacePathResolver


class FileContentKind(StrEnum):
    TEXT = "TEXT"
    BINARY = "BINARY"


class WorkspaceFileBaseline(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    relative_path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    file_kind: Literal["REGULAR_FILE", "SYMLINK"] = "REGULAR_FILE"
    executable_bit: bool
    is_symlink: bool = False
    is_reparse_point: bool = False
    content_kind: FileContentKind

    @model_validator(mode="after")
    def require_consistent_link_metadata(self) -> Self:
        if (self.file_kind, self.is_symlink, self.is_reparse_point) not in {
            ("REGULAR_FILE", False, False),
            ("SYMLINK", True, False),
        }:
            raise ValueError("Workspace file kind and link metadata are inconsistent")
        return self


class WorkspaceBaseline(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    baseline_id: UUID = Field(default_factory=uuid4)
    task_id: str = Field(min_length=1)
    workspace_root: str = Field(min_length=1)
    root_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    manifest_version: int = 1
    created_at: UtcDatetime = Field(default_factory=utc_now)
    files: tuple[WorkspaceFileBaseline, ...]


class WorkspaceScan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workspace_root: str
    root_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    files: tuple[WorkspaceFileBaseline, ...]


class WorkspaceBaselineBuilder:
    def __init__(self, resolver: WorkspacePathResolver) -> None:
        self._resolver = resolver

    def build(self, *, task_id: str) -> WorkspaceBaseline:
        scan = self.scan()
        return WorkspaceBaseline(
            task_id=task_id,
            workspace_root=scan.workspace_root,
            root_digest=scan.root_digest,
            files=scan.files,
        )

    def scan(self) -> WorkspaceScan:
        entries: list[WorkspaceFileBaseline] = []
        self._scan_directory(self._resolver.workspace, entries)
        entries.sort(key=lambda item: item.relative_path)
        digest = _manifest_digest(entries)
        return WorkspaceScan(
            workspace_root=str(self._resolver.workspace),
            root_digest=digest,
            files=tuple(entries),
        )

    def _scan_directory(
        self,
        directory: Path,
        entries: list[WorkspaceFileBaseline],
    ) -> None:
        try:
            children = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError as exc:
            raise RuntimeError("Workspace baseline scan failed") from exc
        for child in children:
            path = Path(child.path)
            if child.is_symlink() or _is_reparse_point(path):
                raise RuntimeError("Workspace contains a symlink or reparse point")
            try:
                if child.is_dir(follow_symlinks=False):
                    self._scan_directory(path, entries)
                    continue
                if not child.is_file(follow_symlinks=False):
                    raise RuntimeError("Workspace contains an unsupported file type")
                relative = self._resolver.relative(path)
                data = path.read_bytes()
                mode = path.stat(follow_symlinks=False).st_mode
            except OSError as exc:
                raise RuntimeError("Workspace baseline file scan failed") from exc
            entries.append(
                WorkspaceFileBaseline(
                    relative_path=relative,
                    sha256=hashlib.sha256(data).hexdigest(),
                    size_bytes=len(data),
                    executable_bit=bool(mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)),
                    content_kind=_content_kind(data),
                )
            )


class EvaluationFixtureWorkspace:
    def __init__(
        self,
        temporary_directory: tempfile.TemporaryDirectory[str],
        root: Path,
    ) -> None:
        self._temporary_directory = temporary_directory
        self.root = root

    @classmethod
    def create(
        cls,
        task: EvaluationTaskDefinition,
    ) -> "EvaluationFixtureWorkspace":
        source = Path(task.fixture_path).resolve(strict=True)
        if not source.is_dir():
            raise ValueError("Evaluation fixture must be a directory")
        try:
            WorkspaceBaselineBuilder(WorkspacePathResolver(source)).scan()
        except RuntimeError as exc:
            raise ValueError(
                "Evaluation fixture contains a symlink, reparse point, or unsupported file"
            ) from exc
        temporary_directory = tempfile.TemporaryDirectory(
            prefix="agentforge-evaluation-"
        )
        root = Path(temporary_directory.name) / "workspace"
        try:
            shutil.copytree(source, root, symlinks=False)
        except OSError as exc:
            temporary_directory.cleanup()
            raise ValueError("Evaluation fixture copy failed") from exc
        return cls(temporary_directory, root)

    def __enter__(self) -> "EvaluationFixtureWorkspace":
        return self

    def __exit__(self, *_: object) -> None:
        self._temporary_directory.cleanup()


def _content_kind(data: bytes) -> FileContentKind:
    if b"\x00" in data:
        return FileContentKind.BINARY
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return FileContentKind.BINARY
    return FileContentKind.TEXT


def _manifest_digest(entries: list[WorkspaceFileBaseline]) -> str:
    payload = [entry.model_dump(mode="json") for entry in entries]
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = getattr(os.lstat(path), "st_file_attributes", 0)
    except OSError as exc:
        raise RuntimeError("Workspace entry metadata could not be read") from exc
    marker = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & marker)
