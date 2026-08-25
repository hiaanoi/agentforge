from __future__ import annotations

import asyncio
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from time import perf_counter

DockerCommandRunner = Callable[..., subprocess.CompletedProcess[str]]


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
        runner: DockerCommandRunner | None = None,
    ) -> None:
        if not container.strip():
            raise ValueError("Docker container must be non-empty")
        self._container = container
        self._runner = runner or subprocess.run

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
            cwd,
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


def _stream_text(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value or ""


def _exception_info(error: BaseException) -> str:
    return f"{type(error).__name__}: {error}"


def _duration_ms(started: float) -> int:
    return max(0, int((perf_counter() - started) * 1000))


__all__ = ["BashObservation", "DockerBashEnvironment"]
