from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from threading import Thread
from types import TracebackType
from typing import BinaryIO, Protocol

from agentforge.domain.test_execution import TestProfile
from agentforge.process.streaming import CapturedStream, capture_stream


class SupervisorStatus(StrEnum):
    EXITED = "EXITED"
    TIMEOUT = "TIMEOUT"
    CANCELLED = "CANCELLED"
    LAUNCH_FAILED = "LAUNCH_FAILED"
    INDETERMINATE = "INDETERMINATE"


@dataclass(frozen=True)
class SupervisorOutcome:
    status: SupervisorStatus
    root_pid: int | None
    process_group_id: int | None
    job_id: str | None
    exit_code: int | None
    stdout: CapturedStream
    stderr: CapturedStream
    duration_ms: int
    termination_reason: str | None
    termination_result: str | None
    termination_confirmed: bool


class ProcessTreeSupervisor(Protocol):
    def run(self, profile: TestProfile) -> SupervisorOutcome: ...

    def cancel(self, reason: str = "cancelled") -> bool: ...


class StreamCaptureWorker:
    def __init__(self, stream: BinaryIO, max_output_bytes: int) -> None:
        self._stream = stream
        self._max_output_bytes = max_output_bytes
        self._result: CapturedStream | None = None
        self._error: BaseException | None = None
        self._thread = Thread(target=self._capture, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def finish(self, timeout: float = 5) -> CapturedStream:
        self._thread.join(timeout)
        if self._thread.is_alive():
            raise RuntimeError("Process output reader did not stop")
        if self._error is not None:
            raise RuntimeError("Process output reader failed") from self._error
        if self._result is None:
            raise RuntimeError("Process output reader produced no result")
        return self._result

    def _capture(self) -> None:
        try:
            self._result = capture_stream(
                self._stream,
                max_output_bytes=self._max_output_bytes,
            )
        except BaseException as exc:
            self._error = exc

    def __enter__(self) -> StreamCaptureWorker:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._stream.close()


def empty_captured_stream() -> CapturedStream:
    return CapturedStream(
        retained_bytes=b"",
        summary="",
        sha256_digest="e3b0c44298fc1c149afbf4c8996fb924"
        "27ae41e4649b934ca495991b7852b855",
        size=0,
        truncated=False,
    )
