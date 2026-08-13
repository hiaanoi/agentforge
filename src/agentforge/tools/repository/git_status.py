"""Confined Git read tools.

A2 intentionally rejects linked worktrees, submodules, bare repositories, and
hardlinked local clones. Those layouts need a broader repository trust model.
"""

from __future__ import annotations

import ctypes
import hashlib
import importlib
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Protocol, cast

from pydantic import BaseModel, ConfigDict

from agentforge.domain.enums import ToolErrorCode, ToolRisk
from agentforge.domain.errors import ToolExecutionError
from agentforge.domain.models import ToolResult, ToolSpec
from agentforge.tools.paths import WorkspacePathResolver


class _ByHandleFileInformation(ctypes.Structure):
    _fields_ = [
        ("file_attributes", ctypes.c_uint32),
        ("creation_time_low", ctypes.c_uint32),
        ("creation_time_high", ctypes.c_uint32),
        ("last_access_time_low", ctypes.c_uint32),
        ("last_access_time_high", ctypes.c_uint32),
        ("last_write_time_low", ctypes.c_uint32),
        ("last_write_time_high", ctypes.c_uint32),
        ("volume_serial_number", ctypes.c_uint32),
        ("file_size_high", ctypes.c_uint32),
        ("file_size_low", ctypes.c_uint32),
        ("number_of_links", ctypes.c_uint32),
        ("file_index_high", ctypes.c_uint32),
        ("file_index_low", ctypes.c_uint32),
    ]

_DEFAULT_MAX_OUTPUT_BYTES = 100_000
_MAX_ERROR_BYTES = 8_192
_READ_CHUNK_BYTES = 8_192
_MAX_CONTROL_PATHS = 50_000
_MAX_WORKSPACE_PATHS = 50_000
_MAX_GIT_EXECUTABLE_BYTES = 512 * 1024 * 1024
_MAX_LOCAL_CONFIG_BYTES = 1024 * 1024
_PROCESS_CLEANUP_SECONDS = 1.0
_REQUIRED_CONTROL_DIRECTORIES = ("objects", "refs")
_OPTIONAL_CONTROL_DIRECTORIES = ("objects/info", "objects/pack")
_REQUIRED_CONTROL_FILES = ("config", "HEAD")
_OPTIONAL_CONTROL_FILES = (
    "packed-refs",
    "index",
    "shallow",
    "info/grafts",
)
_RECURSIVE_CONTROL_ROOTS = ("refs", "objects/info", "objects/pack")
_FORBIDDEN_REDIRECTIONS = (
    "commondir",
    "gitdir",
    "config.worktree",
    "objects/info/alternates",
)
_HEX_CHARACTERS = frozenset("0123456789abcdefABCDEF")
_FANOUT_NAME_LENGTHS = frozenset({2})
_LOOSE_OBJECT_NAME_LENGTHS = frozenset({38, 62})
_DANGEROUS_LOCAL_CONFIG_KEYS = frozenset(
    {
        "core.attributesfile",
        "core.excludesfile",
        "core.fsmonitor",
        "core.hookspath",
        "core.worktree",
        "diff.external",
        "interactive.difffilter",
    }
)


class GitStatusArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


@dataclass(frozen=True, slots=True)
class _GitProcessResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    stdout_truncated: bool
    object_id_length: int


@dataclass(frozen=True, slots=True)
class _PathIdentity:
    device: int
    inode: int
    file_type: int
    file_attributes: int
    size: int
    modified_ns: int
    changed_ns: int
    link_count: int


@dataclass(frozen=True, slots=True)
class _RepositoryBinding:
    workspace: Path
    git_directory: Path
    workspace_identity: _PathIdentity
    git_directory_identity: _PathIdentity
    control_manifest: tuple[tuple[str, _PathIdentity], ...]
    submodule_indicators: tuple[tuple[str, bool], ...]
    object_id_length: int


@dataclass(frozen=True, slots=True)
class _Deadline:
    expires_at: float

    @classmethod
    def start(cls, timeout_seconds: float) -> _Deadline:
        return cls(time.monotonic() + timeout_seconds)

    def remaining(self) -> float:
        remaining = self.expires_at - time.monotonic()
        if remaining <= 0:
            raise ToolExecutionError(
                ToolErrorCode.TOOL_TIMEOUT,
                "Git repository operation timed out",
            )
        return remaining

    def check(self) -> None:
        self.remaining()


class _WindowsJobApiProtocol(Protocol):
    def create(self, name: str) -> int: ...

    def assign_pid(self, job_handle: int, pid: int) -> None: ...

    def terminate_and_wait(self, job_handle: int, timeout: float) -> bool: ...

    def close(self, job_handle: int) -> None: ...


class _ManagedGitProcess:
    def __init__(
        self,
        process: subprocess.Popen[bytes],
        *,
        windows_api: _WindowsJobApiProtocol | None = None,
        windows_job_handle: int | None = None,
        pid_path: Path | None = None,
    ) -> None:
        self.process = process
        self._windows_api = windows_api
        self._windows_job_handle = windows_job_handle
        self._pid_path = pid_path

    def terminate(self) -> bool:
        if self._windows_api is not None and self._windows_job_handle is not None:
            try:
                confirmed = self._windows_api.terminate_and_wait(
                    self._windows_job_handle,
                    _PROCESS_CLEANUP_SECONDS,
                )
            except OSError:
                confirmed = False
            if not confirmed:
                self._windows_api.close(self._windows_job_handle)
                self._windows_job_handle = None
            return confirmed
        return self._terminate_posix_group()

    def _terminate_posix_group(self) -> bool:
        if self.process.pid <= 0:
            return False
        kill_group = cast(Callable[[int, int], None], os.__dict__["killpg"])
        cleanup_deadline = time.monotonic() + _PROCESS_CLEANUP_SECONDS
        try:
            kill_group(self.process.pid, signal.__dict__["SIGTERM"])
        except ProcessLookupError:
            return True
        except OSError:
            return False
        try:
            self.process.wait(timeout=min(0.2, _PROCESS_CLEANUP_SECONDS))
        except (OSError, subprocess.TimeoutExpired):
            pass
        try:
            kill_group(self.process.pid, signal.__dict__["SIGKILL"])
        except ProcessLookupError:
            return True
        except OSError:
            return False
        while time.monotonic() < cleanup_deadline:
            try:
                kill_group(self.process.pid, 0)
            except ProcessLookupError:
                return True
            except OSError:
                return False
            time.sleep(0.01)
        return False

    def close(self) -> None:
        if self._windows_api is not None and self._windows_job_handle is not None:
            self._windows_api.close(self._windows_job_handle)
            self._windows_job_handle = None
        if self._pid_path is not None:
            self._pid_path.unlink(missing_ok=True)


def _start_managed_process(
    command: tuple[str, ...],
    *,
    cwd: Path,
    environment: dict[str, str],
) -> _ManagedGitProcess:
    windows_api: _WindowsJobApiProtocol | None = None
    windows_job_handle: int | None = None
    pid_path: Path | None = None
    if os.name == "nt":
        job_module = importlib.import_module("agentforge.process.windows_job")
        api_type = cast(
            type[_WindowsJobApiProtocol],
            job_module.__dict__["_WindowsJobApi"],
        )
        windows_api = api_type()
        windows_job_handle = windows_api.create(
            f"AgentForgeGit-{os.getpid()}-{time.monotonic_ns()}"
        )
    try:
        if windows_api is not None and windows_job_handle is not None:
            python_executable = str(
                Path(sys.__dict__.get("_base_executable", sys.executable)).resolve(
                    strict=True
                )
            )
            launcher_module = importlib.import_module(
                "agentforge.process.windows_launcher"
            )
            if launcher_module.__file__ is None:
                raise OSError("Git process launcher is unavailable")
            launcher_script = str(Path(launcher_module.__file__).resolve(strict=True))
            pid_path = (
                Path(tempfile.gettempdir())
                / f"agentforge-git-{os.getpid()}-{time.monotonic_ns()}.pid"
            )
            process = subprocess.Popen(
                (python_executable, launcher_script, str(pid_path)),
                executable=python_executable,
                cwd=cwd,
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
            )
            windows_api.assign_pid(windows_job_handle, process.pid)
            assert process.stdin is not None
            payload = json.dumps(
                {
                    "executable_path": command[0],
                    "argv": list(command),
                    "cwd": str(cwd),
                    "environment": environment,
                },
                ensure_ascii=True,
                separators=(",", ":"),
            )
            process.stdin.write(payload.encode("ascii"))
            process.stdin.close()
        else:
            process = subprocess.Popen(
                command,
                cwd=cwd,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                start_new_session=True,
            )
        return _ManagedGitProcess(
            process,
            windows_api=windows_api,
            windows_job_handle=windows_job_handle,
            pid_path=pid_path,
        )
    except Exception:
        if windows_api is not None and windows_job_handle is not None:
            if "process" in locals():
                try:
                    process.kill()
                    process.wait(timeout=_PROCESS_CLEANUP_SECONDS)
                except (OSError, subprocess.SubprocessError):
                    pass
            windows_api.close(windows_job_handle)
        if pid_path is not None:
            pid_path.unlink(missing_ok=True)
        raise


@dataclass(frozen=True, slots=True)
class _TrustedGitExecutable:
    path: Path
    identity: _PathIdentity
    digest: str

    @classmethod
    def discover(cls, requested: Path | None) -> _TrustedGitExecutable:
        discovered = str(requested) if requested is not None else shutil.which("git")
        if discovered is None:
            raise ToolExecutionError(
                ToolErrorCode.GIT_COMMAND_FAILED,
                "Git executable is unavailable",
            )
        try:
            path = Path(discovered).resolve(strict=True)
            metadata = os.lstat(path)
            if not stat.S_ISREG(metadata.st_mode) or _is_link_or_reparse(metadata):
                raise OSError("unsafe Git executable")
            return cls(
                path=path,
                identity=_path_identity(metadata, content_sensitive=True),
                digest=_hash_bounded_file(path, _MAX_GIT_EXECUTABLE_BYTES),
            )
        except (OSError, RuntimeError) as exc:
            raise ToolExecutionError(
                ToolErrorCode.GIT_COMMAND_FAILED,
                "Git executable identity is unsafe",
            ) from exc

    def require_unchanged(self, deadline: _Deadline | None = None) -> None:
        try:
            metadata = os.lstat(self.path)
            if (
                _path_identity(metadata, content_sensitive=True) != self.identity
                or _hash_bounded_file(
                    self.path,
                    _MAX_GIT_EXECUTABLE_BYTES,
                    deadline=deadline,
                )
                != self.digest
            ):
                raise OSError("Git executable changed")
        except (OSError, RuntimeError) as exc:
            raise ToolExecutionError(
                ToolErrorCode.GIT_COMMAND_FAILED,
                "Git executable identity changed",
            ) from exc


class GitStatusTool:
    def __init__(
        self,
        resolver: WorkspacePathResolver,
        *,
        timeout_seconds: float = 10.0,
        max_output_bytes: int = _DEFAULT_MAX_OUTPUT_BYTES,
        git_executable: Path | None = None,
    ) -> None:
        if timeout_seconds <= 0 or max_output_bytes <= 0:
            raise ValueError("Git tool bounds must be positive")
        self._resolver = resolver
        self._timeout_seconds = timeout_seconds
        self._max_output_bytes = max_output_bytes
        self._git_executable = _TrustedGitExecutable.discover(git_executable)

    @classmethod
    def arguments_model(cls) -> BaseModel:
        return GitStatusArguments()

    @property
    def input_model(self) -> type[BaseModel]:
        return GitStatusArguments

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="git_status",
            description="Return bounded short status for the workspace Git repository.",
            input_schema=self.input_model.model_json_schema(),
            risk_level=ToolRisk.READ,
            timeout_seconds=self._timeout_seconds,
        )

    def execute(self, arguments: BaseModel) -> ToolResult:
        GitStatusArguments.model_validate(arguments)
        result = _run_fixed_git(
            self._resolver,
            ("status", "--short", "--untracked-files=normal"),
            timeout_seconds=self._timeout_seconds,
            max_output_bytes=self._max_output_bytes,
            executable=self._git_executable,
        )
        _require_success(result, operation="status")
        text = _safe_text(result.stdout, self._max_output_bytes)
        size = len(text.encode("utf-8"))
        return ToolResult(
            success=True,
            output={
                "status": text,
                "bytes": size,
                "truncated": result.stdout_truncated,
            },
            truncated=result.stdout_truncated,
            metadata={"captured_bytes": len(result.stdout)},
        )


def _run_fixed_git(
    resolver: WorkspacePathResolver,
    operation: tuple[str, ...],
    *,
    timeout_seconds: float,
    max_output_bytes: int,
    executable: _TrustedGitExecutable,
) -> _GitProcessResult:
    deadline = _Deadline.start(timeout_seconds)
    executable.require_unchanged(deadline)
    binding = _require_confined_repository(resolver, deadline)
    environment = _minimal_git_environment()
    disabled_hooks = "NUL" if os.name == "nt" else "/dev/null"
    command = (
        str(executable.path),
        f"--git-dir={binding.git_directory}",
        f"--work-tree={binding.workspace}",
        "--no-pager",
        "--no-replace-objects",
        "--no-optional-locks",
        "-c",
        "core.pager=cat",
        "-c",
        "pager.status=false",
        "-c",
        "pager.log=false",
        "-c",
        "diff.external=",
        "-c",
        "core.fsmonitor=false",
        "-c",
        f"core.hooksPath={disabled_hooks}",
        *operation,
    )
    try:
        managed = _start_managed_process(
            command,
            cwd=resolver.workspace,
            environment=environment,
        )
    except (FileNotFoundError, OSError) as exc:
        raise ToolExecutionError(
            ToolErrorCode.GIT_COMMAND_FAILED,
            "Git executable is unavailable",
        ) from exc
    process = managed.process
    readers: tuple[threading.Thread, ...] = ()
    try:
        _require_unchanged_repository(binding, deadline)
    except ToolExecutionError:
        _stop_managed_process(managed, readers)
        raise
    assert process.stdout is not None
    assert process.stderr is not None
    stdout = bytearray()
    stderr = bytearray()
    stdout_overflow = [False]
    stdout_reader = threading.Thread(
        target=_bounded_read,
        args=(process.stdout, stdout, max_output_bytes, stdout_overflow),
        daemon=True,
    )
    stderr_reader = threading.Thread(
        target=_bounded_read,
        args=(process.stderr, stderr, _MAX_ERROR_BYTES, [False]),
        daemon=True,
    )
    stdout_reader.start()
    stderr_reader.start()
    readers = (stdout_reader, stderr_reader)
    try:
        returncode = process.wait(timeout=deadline.remaining())
        for reader in readers:
            reader.join(timeout=deadline.remaining())
            if reader.is_alive():
                raise subprocess.TimeoutExpired(command, 0)
        _require_unchanged_repository(binding, deadline)
        executable.require_unchanged(deadline)
        return _GitProcessResult(
            returncode=returncode,
            stdout=bytes(stdout),
            stderr=bytes(stderr),
            stdout_truncated=stdout_overflow[0],
            object_id_length=binding.object_id_length,
        )
    except subprocess.TimeoutExpired as exc:
        raise ToolExecutionError(
            ToolErrorCode.TOOL_TIMEOUT,
            "Git repository operation timed out",
        ) from exc
    finally:
        _stop_managed_process(managed, readers)


def _stop_managed_process(
    managed: _ManagedGitProcess,
    readers: tuple[threading.Thread, ...],
) -> bool:
    confirmed = managed.terminate()
    managed.close()
    cleanup_deadline = time.monotonic() + _PROCESS_CLEANUP_SECONDS
    for reader in readers:
        remaining = max(0.0, cleanup_deadline - time.monotonic())
        reader.join(timeout=remaining)
    return confirmed


def _bounded_read(
    stream: BinaryIO,
    destination: bytearray,
    maximum: int,
    overflow: list[bool],
) -> None:
    try:
        while chunk := stream.read(_READ_CHUNK_BYTES):
            remaining = maximum - len(destination)
            if remaining > 0:
                destination.extend(chunk[:remaining])
            if len(chunk) > remaining:
                overflow[0] = True
    finally:
        try:
            stream.close()
        except OSError:
            pass


def _require_confined_repository(
    resolver: WorkspacePathResolver,
    deadline: _Deadline,
) -> _RepositoryBinding:
    control_directory = resolver.workspace / ".git"
    try:
        workspace_metadata = os.lstat(resolver.workspace)
        metadata = os.lstat(control_directory)
    except (OSError, RuntimeError) as exc:
        raise ToolExecutionError(
            ToolErrorCode.NOT_GIT_REPOSITORY,
            "Workspace is not a Git repository",
        ) from exc
    try:
        if (
            not stat.S_ISDIR(workspace_metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or _is_link_or_reparse(workspace_metadata)
            or _is_link_or_reparse(metadata)
            or control_directory.resolve(strict=True).parent != resolver.workspace
        ):
            raise OSError("unsafe repository control directory")
        for relative in _FORBIDDEN_REDIRECTIONS:
            redirection = control_directory / Path(relative)
            try:
                os.lstat(redirection)
            except FileNotFoundError:
                continue
            raise OSError("repository redirection is not supported")
        submodule_indicators = _capture_submodule_indicators(
            resolver.workspace,
            control_directory,
            deadline,
        )
        if any(present for _, present in submodule_indicators):
            raise OSError("submodule layouts are unsupported")
        _scan_workspace_for_nested_git(resolver.workspace, deadline)
        control_manifest = _capture_control_manifest(control_directory, deadline)
        object_id_length = _inspect_local_config(
            control_directory / "config", deadline
        )
    except (OSError, RuntimeError) as exc:
        raise ToolExecutionError(
            ToolErrorCode.UNSAFE_REPOSITORY_LAYOUT,
            "Repository layout is unsupported by A2 Git tools",
        ) from exc
    return _RepositoryBinding(
        workspace=resolver.workspace,
        git_directory=control_directory,
        workspace_identity=_path_identity(workspace_metadata),
        git_directory_identity=_path_identity(metadata),
        control_manifest=control_manifest,
        submodule_indicators=submodule_indicators,
        object_id_length=object_id_length,
    )


def _require_unchanged_repository(
    binding: _RepositoryBinding,
    deadline: _Deadline,
) -> None:
    try:
        _scan_workspace_for_nested_git(binding.workspace, deadline)
        if (
            _path_identity(os.lstat(binding.workspace))
            != binding.workspace_identity
            or _path_identity(os.lstat(binding.git_directory))
            != binding.git_directory_identity
            or _capture_control_manifest(binding.git_directory, deadline)
            != binding.control_manifest
            or _capture_submodule_indicators(
                binding.workspace,
                binding.git_directory,
                deadline,
            )
            != binding.submodule_indicators
        ):
            raise OSError("repository identity changed")
        for relative in _FORBIDDEN_REDIRECTIONS:
            redirection = binding.git_directory / Path(relative)
            try:
                os.lstat(redirection)
            except FileNotFoundError:
                continue
            raise OSError("repository redirection appeared")
    except (OSError, RuntimeError) as exc:
        raise ToolExecutionError(
            ToolErrorCode.UNSAFE_REPOSITORY_LAYOUT,
            "Repository layout changed during Git execution",
        ) from exc


def _scan_workspace_for_nested_git(
    workspace: Path,
    deadline: _Deadline,
) -> None:
    pending = [workspace]
    scanned_entries = 0
    while pending:
        deadline.check()
        current = pending.pop()
        canonical_names: set[str] = set()
        with os.scandir(current) as entries:
            for entry in entries:
                deadline.check()
                scanned_entries += 1
                if scanned_entries > _MAX_WORKSPACE_PATHS:
                    raise OSError("workspace traversal bound exceeded")
                normalized_name = unicodedata.normalize("NFC", entry.name)
                if any(
                    0xD800 <= ord(character) <= 0xDFFF
                    for character in normalized_name
                ):
                    raise OSError("workspace name is not canonical Unicode")
                canonical_name = normalized_name.casefold()
                if canonical_name in canonical_names:
                    raise OSError("workspace contains a canonical name collision")
                canonical_names.add(canonical_name)
                if canonical_name == ".git":
                    if current == workspace and entry.name == ".git":
                        continue
                    raise OSError("nested Git control path is unsupported")
                metadata = entry.stat(follow_symlinks=False)
                if _is_link_or_reparse(metadata):
                    raise OSError("workspace traversal link is unsupported")
                if stat.S_ISDIR(metadata.st_mode):
                    pending.append(Path(entry.path))
                elif not stat.S_ISREG(metadata.st_mode):
                    raise OSError("workspace special path is unsupported")


def _capture_submodule_indicators(
    workspace: Path,
    git_directory: Path,
    deadline: _Deadline,
) -> tuple[tuple[str, bool], ...]:
    indicators = (
        ("root-config", workspace / ".gitmodules"),
        ("module-storage", git_directory / "modules"),
    )
    captured: list[tuple[str, bool]] = []
    for label, candidate in indicators:
        deadline.check()
        try:
            os.lstat(candidate)
        except FileNotFoundError:
            captured.append((label, False))
        else:
            captured.append((label, True))
    return tuple(captured)


def _minimal_git_environment() -> dict[str, str]:
    allowed = (
        "COMSPEC",
        "PATHEXT",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "WINDIR",
    )
    environment = {name: os.environ[name] for name in allowed if name in os.environ}
    null_path = "NUL" if os.name == "nt" else "/dev/null"
    environment.update(
        {
            "GIT_CONFIG_GLOBAL": null_path,
            "GIT_CONFIG_NOSYSTEM": "1",
            "HOME": null_path,
            "XDG_CONFIG_HOME": null_path,
            "LC_ALL": "C",
            "LANG": "C",
            "PAGER": "",
        }
    )
    return environment


def _inspect_local_config(config: Path, deadline: _Deadline) -> int:
    deadline.check()
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(config, flags)
    try:
        content = os.read(descriptor, _MAX_LOCAL_CONFIG_BYTES + 1)
        if len(content) > _MAX_LOCAL_CONFIG_BYTES or os.read(descriptor, 1):
            raise OSError("Git local config exceeds bound")
    finally:
        os.close(descriptor)
    deadline.check()
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise OSError("Git local config is not UTF-8") from exc
    section = ""
    object_format = "sha1"
    for raw_line in text.splitlines():
        deadline.check()
        line = raw_line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].split(maxsplit=1)[0].casefold()
            if section in {"include", "includeif"}:
                raise OSError("Git config includes are unsupported")
            continue
        if not section or line.endswith("\\"):
            raise OSError("Git local config syntax is unsupported")
        key, separator, value = line.partition("=")
        if not separator:
            parts = line.split(maxsplit=1)
            key = parts[0]
            value = parts[1] if len(parts) == 2 else "true"
        qualified = f"{section}.{key.strip().casefold()}"
        if (
            qualified in _DANGEROUS_LOCAL_CONFIG_KEYS
            or section in {"filter", "credential", "protocol"}
            or section == "pager"
        ):
            raise OSError("Git local config contains an unsafe capability")
        if qualified == "extensions.objectformat":
            object_format = value.strip().casefold()
    if object_format == "sha1":
        return 40
    if object_format == "sha256":
        return 64
    raise OSError("Git object format is unsupported")


def _hash_bounded_file(
    path: Path,
    maximum: int,
    *,
    deadline: _Deadline | None = None,
) -> str:
    digest = hashlib.sha256()
    total = 0
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        while chunk := os.read(descriptor, 1024 * 1024):
            if deadline is not None:
                deadline.check()
            total += len(chunk)
            if total > maximum:
                raise OSError("File exceeds identity bound")
            digest.update(chunk)
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def _capture_control_manifest(
    git_directory: Path,
    deadline: _Deadline,
) -> tuple[tuple[str, _PathIdentity], ...]:
    deadline.check()
    captured: dict[str, _PathIdentity] = {}
    for relative in _REQUIRED_CONTROL_DIRECTORIES:
        _capture_control_path(
            git_directory,
            relative,
            directory=True,
            required=True,
            captured=captured,
        )
    for relative in _OPTIONAL_CONTROL_DIRECTORIES:
        _capture_control_path(
            git_directory,
            relative,
            directory=True,
            required=False,
            captured=captured,
        )
    for relative in _REQUIRED_CONTROL_FILES:
        _capture_control_path(
            git_directory,
            relative,
            directory=False,
            required=True,
            captured=captured,
        )
    for relative in _OPTIONAL_CONTROL_FILES:
        _capture_control_path(
            git_directory,
            relative,
            directory=False,
            required=False,
            captured=captured,
        )
    _capture_loose_object_database(git_directory, captured, deadline)
    for relative in _RECURSIVE_CONTROL_ROOTS:
        root = git_directory / Path(relative)
        try:
            os.lstat(root)
        except FileNotFoundError:
            continue
        _capture_control_descendants(git_directory, root, captured, deadline)
    deadline.check()
    return tuple(sorted(captured.items()))


def _capture_loose_object_database(
    git_directory: Path,
    captured: dict[str, _PathIdentity],
    deadline: _Deadline,
) -> None:
    objects = git_directory / "objects"
    with os.scandir(objects) as entries:
        for entry in entries:
            deadline.check()
            if entry.name in {"info", "pack"}:
                continue
            if not _is_hex_name(entry.name, lengths=_FANOUT_NAME_LENGTHS):
                raise OSError("invalid Git object database entry")
            fanout = Path(entry.path)
            metadata = entry.stat(follow_symlinks=False)
            link_count = _require_safe_control_path(
                git_directory,
                fanout,
                metadata,
                directory=True,
            )
            relative = fanout.relative_to(git_directory).as_posix()
            captured[relative] = _path_identity(
                metadata,
                link_count=link_count,
            )
            _require_manifest_bound(captured)
            _capture_loose_object_entries(git_directory, fanout, captured, deadline)


def _capture_loose_object_entries(
    git_directory: Path,
    fanout: Path,
    captured: dict[str, _PathIdentity],
    deadline: _Deadline,
) -> None:
    with os.scandir(fanout) as entries:
        for entry in entries:
            deadline.check()
            if not _is_hex_name(entry.name, lengths=_LOOSE_OBJECT_NAME_LENGTHS):
                raise OSError("invalid loose Git object name")
            candidate = Path(entry.path)
            metadata = entry.stat(follow_symlinks=False)
            link_count = _require_safe_control_path(
                git_directory,
                candidate,
                metadata,
                directory=False,
            )
            relative = candidate.relative_to(git_directory).as_posix()
            captured[relative] = _path_identity(
                metadata,
                content_sensitive=True,
                link_count=link_count,
            )
            _require_manifest_bound(captured)


def _is_hex_name(name: str, *, lengths: frozenset[int]) -> bool:
    return len(name) in lengths and all(
        character in _HEX_CHARACTERS for character in name
    )


def _require_manifest_bound(captured: dict[str, _PathIdentity]) -> None:
    if len(captured) > _MAX_CONTROL_PATHS:
        raise OSError("Git control path bound exceeded")


def _capture_control_path(
    git_directory: Path,
    relative: str,
    *,
    directory: bool,
    required: bool,
    captured: dict[str, _PathIdentity],
) -> None:
    candidate = git_directory / Path(relative)
    try:
        metadata = os.lstat(candidate)
    except FileNotFoundError:
        if required:
            raise OSError("required Git control path is missing") from None
        return
    link_count = _require_safe_control_path(
        git_directory,
        candidate,
        metadata,
        directory=directory,
    )
    captured[relative] = _path_identity(
        metadata,
        content_sensitive=not directory,
        link_count=link_count,
    )


def _capture_control_descendants(
    git_directory: Path,
    root: Path,
    captured: dict[str, _PathIdentity],
    deadline: _Deadline,
) -> None:
    pending = [root]
    while pending:
        deadline.check()
        current = pending.pop()
        with os.scandir(current) as entries:
            for entry in entries:
                deadline.check()
                candidate = Path(entry.path)
                metadata = entry.stat(follow_symlinks=False)
                directory = stat.S_ISDIR(metadata.st_mode)
                link_count = _require_safe_control_path(
                    git_directory,
                    candidate,
                    metadata,
                    directory=directory,
                )
                relative = candidate.relative_to(git_directory).as_posix()
                captured[relative] = _path_identity(
                    metadata,
                    content_sensitive=not directory,
                    link_count=link_count,
                )
                _require_manifest_bound(captured)
                if directory:
                    pending.append(candidate)


def _require_safe_control_path(
    git_directory: Path,
    candidate: Path,
    metadata: os.stat_result,
    *,
    directory: bool,
) -> int:
    link_count = metadata.st_nlink if directory else _file_link_count(candidate, metadata)
    if (
        _is_link_or_reparse(metadata)
        or (directory and not stat.S_ISDIR(metadata.st_mode))
        or (not directory and not stat.S_ISREG(metadata.st_mode))
        or (not directory and link_count != 1)
    ):
        raise OSError("unsafe Git control path")
    resolved = candidate.resolve(strict=True)
    try:
        resolved.relative_to(git_directory)
    except ValueError:
        raise OSError("Git control path escapes repository") from None
    return link_count


def _file_link_count(candidate: Path, metadata: os.stat_result) -> int:
    if os.name != "nt":
        return metadata.st_nlink
    descriptor = os.open(
        candidate,
        os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        msvcrt = importlib.import_module("msvcrt")
        handle = msvcrt.get_osfhandle(descriptor)
        information = _ByHandleFileInformation()
        loader = ctypes.WinDLL
        kernel32 = loader("kernel32", use_last_error=True)
        get_information = kernel32.GetFileInformationByHandle
        get_information.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(_ByHandleFileInformation),
        ]
        get_information.restype = ctypes.c_int
        succeeded = get_information(
            ctypes.c_void_p(handle),
            ctypes.byref(information),
        )
        if not succeeded or information.number_of_links < 1:
            raise OSError("Git control file link count is unavailable")
        return int(information.number_of_links)
    finally:
        os.close(descriptor)


def _path_identity(
    metadata: os.stat_result,
    *,
    content_sensitive: bool = False,
    link_count: int | None = None,
) -> _PathIdentity:
    return _PathIdentity(
        device=metadata.st_dev,
        inode=metadata.st_ino,
        file_type=stat.S_IFMT(metadata.st_mode),
        file_attributes=getattr(metadata, "st_file_attributes", 0),
        size=metadata.st_size if content_sensitive else 0,
        modified_ns=metadata.st_mtime_ns if content_sensitive else 0,
        changed_ns=metadata.st_ctime_ns if content_sensitive else 0,
        link_count=metadata.st_nlink if link_count is None else link_count,
    )


def _is_link_or_reparse(metadata: os.stat_result) -> bool:
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse_marker = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(metadata.st_mode) or bool(attributes & reparse_marker)


def _require_success(result: _GitProcessResult, *, operation: str) -> None:
    if result.returncode != 0:
        raise ToolExecutionError(
            ToolErrorCode.GIT_COMMAND_FAILED,
            f"Git could not produce bounded {operation} output",
        )


def _safe_text(value: bytes, maximum: int) -> str:
    text = value.decode("utf-8", errors="replace").replace("\x00", "\ufffd")
    while len(text.encode("utf-8")) > maximum:
        text = text[:-1]
    return text
