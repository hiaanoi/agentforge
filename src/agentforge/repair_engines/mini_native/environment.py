from __future__ import annotations

import asyncio
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter

BashCommandRunner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True, slots=True)
class BashObservation:
    output: str
    returncode: int | None
    exception_info: str | None
    timed_out: bool
    duration_ms: int


class DockerBashEnvironment:
    """Execute one non-interactive bash command in an existing task container."""

    def __init__(
        self,
        container: str,
        *,
        workspace: str | None = None,
        runner: BashCommandRunner | None = None,
    ) -> None:
        if not container.strip():
            raise ValueError("Docker container must be non-empty")
        if workspace is not None and not workspace.strip():
            raise ValueError("Docker workspace must be non-empty")
        self._container = container.strip()
        self._workspace = workspace
        self._runner = runner or subprocess.run

    @property
    def container(self) -> str:
        return self._container

    @property
    def workspace(self) -> str | None:
        return self._workspace

    async def execute(
        self,
        command: str,
        *,
        cwd: str,
        timeout_seconds: float | None,
    ) -> BashObservation:
        started = perf_counter()
        arguments = (
            "docker",
            "exec",
            "-w",
            self._workspace or cwd,
            self._container,
            "/bin/bash",
            "-lc",
            command,
        )
        try:
            completed = await asyncio.to_thread(
                self._runner,
                arguments,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            return BashObservation(
                output=_stream_text(error.stdout) + _stream_text(error.stderr),
                returncode=None,
                exception_info=_exception_info(error),
                timed_out=True,
                duration_ms=_duration_ms(started),
            )
        except (OSError, subprocess.SubprocessError) as error:
            return BashObservation(
                output="",
                returncode=None,
                exception_info=_exception_info(error),
                timed_out=False,
                duration_ms=_duration_ms(started),
            )
        return BashObservation(
            output=completed.stdout + completed.stderr,
            returncode=completed.returncode,
            exception_info=None,
            timed_out=False,
            duration_ms=_duration_ms(started),
        )


class WorkspaceBashEnvironment:
    """Execute bash directly in the assembled product workspace."""

    def __init__(
        self,
        workspace: Path,
        *,
        runner: BashCommandRunner | None = None,
    ) -> None:
        self._workspace = workspace.resolve(strict=True)
        self._runner = runner or subprocess.run

    @property
    def workspace(self) -> Path:
        return self._workspace

    async def execute(
        self,
        command: str,
        *,
        cwd: str,
        timeout_seconds: float | None,
    ) -> BashObservation:
        started = perf_counter()
        requested_cwd = await asyncio.to_thread(Path(cwd).resolve, strict=True)
        try:
            requested_cwd.relative_to(self._workspace)
        except ValueError:
            return BashObservation(
                output="",
                returncode=None,
                exception_info="ValueError: bash cwd is outside the assembled workspace",
                timed_out=False,
                duration_ms=_duration_ms(started),
            )
        arguments = ("bash", "-lc", command)
        try:
            completed = await asyncio.to_thread(
                self._runner,
                arguments,
                cwd=str(requested_cwd),
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            return BashObservation(
                output=_stream_text(error.stdout) + _stream_text(error.stderr),
                returncode=None,
                exception_info=_exception_info(error),
                timed_out=True,
                duration_ms=_duration_ms(started),
            )
        except (OSError, subprocess.SubprocessError) as error:
            return BashObservation(
                output="",
                returncode=None,
                exception_info=_exception_info(error),
                timed_out=False,
                duration_ms=_duration_ms(started),
            )
        return BashObservation(
            output=completed.stdout + completed.stderr,
            returncode=completed.returncode,
            exception_info=None,
            timed_out=False,
            duration_ms=_duration_ms(started),
        )


def _stream_text(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value or ""


def _exception_info(error: BaseException) -> str:
    return f"{type(error).__name__}: {error}"


def _duration_ms(started: float) -> int:
    return max(0, int((perf_counter() - started) * 1000))


__all__ = ["BashObservation", "DockerBashEnvironment", "WorkspaceBashEnvironment"]
