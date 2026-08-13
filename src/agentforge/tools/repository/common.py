import os
from collections.abc import Iterator
from pathlib import Path

from agentforge.domain.enums import PathKind, ToolErrorCode
from agentforge.domain.errors import ToolExecutionError, WorkspacePathError
from agentforge.policy.sensitive import SensitiveFilePolicy
from agentforge.tools.paths import WorkspacePathResolver

IGNORED_DIRECTORY_NAMES = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "node_modules",
        "venv",
    }
)

MAX_SEARCH_FILE_BYTES = 1_048_576
MAX_SNIPPET_CHARS = 240


def iter_safe_files(
    resolver: WorkspacePathResolver,
    sensitive_files: SensitiveFilePolicy,
    root: Path,
    *,
    recursive: bool,
) -> Iterator[tuple[Path, str]]:
    root_relative = Path(resolver.relative(root))
    if any(part.casefold() in IGNORED_DIRECTORY_NAMES for part in root_relative.parts):
        return
    if not recursive:
        try:
            entries = sorted(root.iterdir(), key=lambda entry: entry.name.casefold())
        except OSError as exc:
            raise ToolExecutionError(
                ToolErrorCode.TOOL_EXECUTION_ERROR,
                "Unable to enumerate the requested directory",
            ) from exc
        for candidate in entries:
            item = _validated_file(resolver, sensitive_files, candidate)
            if item is not None:
                yield item
        return

    for current, directories, files in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        directories[:] = sorted(
            (
                name
                for name in directories
                if name.casefold() not in IGNORED_DIRECTORY_NAMES
                and not (current_path / name).is_symlink()
            ),
            key=str.casefold,
        )
        for name in sorted(files, key=str.casefold):
            item = _validated_file(resolver, sensitive_files, current_path / name)
            if item is not None:
                yield item


def decode_utf8(data: bytes, *, allow_truncated_tail: bool = False) -> str:
    if b"\x00" in data:
        raise ToolExecutionError(ToolErrorCode.BINARY_FILE, "Binary files are not supported")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        if allow_truncated_tail and exc.end == len(data):
            for trim in range(1, min(4, len(data)) + 1):
                try:
                    return data[:-trim].decode("utf-8")
                except UnicodeDecodeError:
                    continue
        raise ToolExecutionError(
            ToolErrorCode.ENCODING_ERROR,
            "File is not valid UTF-8 text",
        ) from exc


def _validated_file(
    resolver: WorkspacePathResolver,
    sensitive_files: SensitiveFilePolicy,
    candidate: Path,
) -> tuple[Path, str] | None:
    if candidate.is_symlink():
        return None
    try:
        logical_relative = candidate.relative_to(resolver.workspace).as_posix()
        resolved = resolver.resolve(logical_relative, PathKind.FILE)
        canonical_relative = resolver.relative(resolved)
    except (OSError, WorkspacePathError):
        return None
    if sensitive_files.match(canonical_relative) is not None:
        return None
    return resolved, logical_relative
