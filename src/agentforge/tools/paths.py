import os
import stat
from pathlib import Path, PureWindowsPath

from agentforge.domain.enums import PathKind, ToolErrorCode
from agentforge.domain.errors import WorkspacePathError


class WorkspacePathResolver:
    def __init__(self, workspace: Path) -> None:
        try:
            resolved = workspace.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise WorkspacePathError(
                ToolErrorCode.PATH_NOT_FOUND, "Workspace does not exist"
            ) from exc
        if not resolved.is_dir():
            raise WorkspacePathError(
                ToolErrorCode.PATH_TYPE_MISMATCH, "Workspace must be a directory"
            )
        self._workspace = resolved

    @property
    def workspace(self) -> Path:
        return self._workspace

    def resolve(self, requested: str, kind: PathKind = PathKind.ANY) -> Path:
        normalized = self._normalize_request(requested)
        unresolved = (self._workspace / normalized).resolve(strict=False)
        self._require_contained(unresolved)
        try:
            candidate = (self._workspace / normalized).resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise WorkspacePathError(
                ToolErrorCode.PATH_NOT_FOUND, "Requested workspace path does not exist"
            ) from exc
        self._require_contained(candidate)
        if kind is PathKind.FILE and not candidate.is_file():
            raise WorkspacePathError(
                ToolErrorCode.PATH_TYPE_MISMATCH, "Requested path must be a file"
            )
        if kind is PathKind.DIRECTORY and not candidate.is_dir():
            raise WorkspacePathError(
                ToolErrorCode.PATH_TYPE_MISMATCH, "Requested path must be a directory"
            )
        return candidate

    def relative(self, resolved: Path) -> str:
        self._require_contained(resolved)
        relative = resolved.relative_to(self._workspace)
        return "." if relative == Path(".") else relative.as_posix()

    def resolve_mutation_target(self, requested: str) -> Path:
        normalized = self._normalize_request(requested)
        candidate = Path(os.path.abspath(self._workspace / normalized))
        self._require_contained(candidate)
        relative = candidate.relative_to(self._workspace)
        current = self._workspace
        parts = relative.parts
        for index, part in enumerate(parts):
            current = current / part
            final = index == len(parts) - 1
            if current.is_symlink() or self._is_reparse_point(current):
                raise WorkspacePathError(
                    ToolErrorCode.PATH_OUTSIDE_WORKSPACE,
                    "Mutation paths cannot contain symlinks or reparse points",
                )
            if not current.exists():
                if final:
                    break
                raise WorkspacePathError(
                    ToolErrorCode.PATH_NOT_FOUND,
                    "Mutation target parent does not exist",
                )
            if not final and not current.is_dir():
                raise WorkspacePathError(
                    ToolErrorCode.PATH_TYPE_MISMATCH,
                    "Mutation target parent must be a directory",
                )
        if candidate.exists() and not candidate.is_file():
            raise WorkspacePathError(
                ToolErrorCode.PATH_TYPE_MISMATCH,
                "Mutation target must be a regular file",
            )
        if not candidate.parent.is_dir():
            raise WorkspacePathError(
                ToolErrorCode.PATH_NOT_FOUND,
                "Mutation target parent does not exist",
            )
        return candidate

    @staticmethod
    def _normalize_request(requested: str) -> Path:
        if not requested or not requested.strip():
            raise WorkspacePathError(ToolErrorCode.INVALID_PATH, "Path must not be empty")
        raw = requested.strip()
        if "\x00" in raw:
            raise WorkspacePathError(
                ToolErrorCode.INVALID_PATH, "Path contains invalid characters"
            )
        windows_path = PureWindowsPath(raw)
        if Path(raw).is_absolute() or windows_path.is_absolute() or windows_path.drive:
            raise WorkspacePathError(
                ToolErrorCode.PATH_OUTSIDE_WORKSPACE, "Absolute paths are not allowed"
            )
        if raw.startswith(("\\\\", "//")):
            raise WorkspacePathError(
                ToolErrorCode.PATH_OUTSIDE_WORKSPACE, "UNC paths are not allowed"
            )
        portable = raw.replace("\\", os.sep).replace("/", os.sep)
        return Path(portable)

    def _require_contained(self, candidate: Path) -> None:
        workspace_key = os.path.normcase(str(self._workspace))
        candidate_key = os.path.normcase(str(candidate))
        try:
            common = os.path.commonpath((workspace_key, candidate_key))
        except ValueError as exc:
            raise WorkspacePathError(
                ToolErrorCode.PATH_OUTSIDE_WORKSPACE,
                "Requested path resolves outside the workspace",
            ) from exc
        if common != workspace_key:
            raise WorkspacePathError(
                ToolErrorCode.PATH_OUTSIDE_WORKSPACE,
                "Requested path resolves outside the workspace",
            )

    @staticmethod
    def _is_reparse_point(candidate: Path) -> bool:
        try:
            attributes = getattr(os.lstat(candidate), "st_file_attributes", 0)
        except OSError:
            return False
        marker = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        return bool(attributes & marker)
