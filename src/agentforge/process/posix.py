from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Callable
from threading import Event, Lock
from typing import BinaryIO, cast

from agentforge.domain.test_execution import TestProfile
from agentforge.process.base import (
    StreamCaptureWorker,
    SupervisorOutcome,
    SupervisorStatus,
    empty_captured_stream,
)

_SIGTERM = 15
_SIGKILL = 9


class PosixProcessGroupSupervisor:
    def __init__(self, *, termination_grace_seconds: float = 3) -> None:
        self._termination_grace_seconds = termination_grace_seconds
        self._cancelled = Event()
        self._lock = Lock()
        self._process_group_id: int | None = None
        self._process: subprocess.Popen[bytes] | None = None
        self._termination_reason: str | None = None

    def run(self, profile: TestProfile) -> SupervisorOutcome:
        started = time.perf_counter()
        process: subprocess.Popen[bytes] | None = None
        stdout_worker: StreamCaptureWorker | None = None
        stderr_worker: StreamCaptureWorker | None = None
        status = SupervisorStatus.LAUNCH_FAILED
        termination_confirmed = False
        termination_result: str | None = None
        exit_code: int | None = None
        try:
            process = subprocess.Popen(
                list(profile.argv),
                executable=profile.executable_path,
                cwd=profile.cwd,
                env=dict(profile.allowed_env),
                shell=False,
                start_new_session=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            if process.stdout is None or process.stderr is None:
                raise RuntimeError("POSIX process pipes were not created")
            group_id = process.pid
            with self._lock:
                self._process_group_id = group_id
                self._process = process
            stdout_worker = StreamCaptureWorker(
                cast(BinaryIO, process.stdout),
                profile.max_output_bytes,
            )
            stderr_worker = StreamCaptureWorker(
                cast(BinaryIO, process.stderr),
                profile.max_output_bytes,
            )
            stdout_worker.start()
            stderr_worker.start()
            status, termination_confirmed, termination_result = self._wait_for_process(
                process,
                group_id,
                profile.timeout_seconds,
            )
            exit_code = process.wait(timeout=self._termination_grace_seconds)
        except (OSError, RuntimeError, subprocess.SubprocessError):
            exit_code = process.poll() if process is not None else None
            if process is not None and process.poll() is None:
                termination_confirmed = self._terminate_group(process.pid, process)
            status = (
                SupervisorStatus.LAUNCH_FAILED
                if process is None or termination_confirmed
                else SupervisorStatus.INDETERMINATE
            )
            termination_result = "process_launch_failed"
        finally:
            if process is not None and process.poll() is None:
                termination_confirmed = self._terminate_group(process.pid, process)
            with self._lock:
                self._process_group_id = None
                self._process = None
        stdout = (
            stdout_worker.finish()
            if stdout_worker is not None
            else empty_captured_stream()
        )
        stderr = (
            stderr_worker.finish()
            if stderr_worker is not None
            else empty_captured_stream()
        )
        return SupervisorOutcome(
            status=status,
            root_pid=process.pid if process is not None else None,
            process_group_id=process.pid if process is not None else None,
            job_id=None,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_ms=max(0, int((time.perf_counter() - started) * 1000)),
            termination_reason=self._termination_reason,
            termination_result=termination_result,
            termination_confirmed=(
                termination_confirmed if status is not SupervisorStatus.EXITED else True
            ),
        )

    def cancel(self, reason: str = "cancelled") -> bool:
        self._cancelled.set()
        with self._lock:
            if self._termination_reason is None:
                self._termination_reason = reason[:100]
            group_id = self._process_group_id
            process = self._process
        if group_id is None:
            return False
        return self._terminate_group(group_id, process)

    def _wait_for_process(
        self,
        process: subprocess.Popen[bytes],
        group_id: int,
        timeout: float,
    ) -> tuple[SupervisorStatus, bool, str]:
        deadline = time.monotonic() + timeout
        while True:
            if self._cancelled.is_set():
                confirmed = self._terminate_group(group_id, process)
                return (
                    SupervisorStatus.CANCELLED
                    if confirmed
                    else SupervisorStatus.INDETERMINATE,
                    confirmed,
                    "group_terminated" if confirmed else "group_termination_unconfirmed",
                )
            if process.poll() is not None:
                return SupervisorStatus.EXITED, True, "natural_exit"
            if time.monotonic() >= deadline:
                with self._lock:
                    if self._termination_reason is None:
                        self._termination_reason = "timeout"
                confirmed = self._terminate_group(group_id, process)
                return (
                    SupervisorStatus.TIMEOUT
                    if confirmed
                    else SupervisorStatus.INDETERMINATE,
                    confirmed,
                    "group_terminated" if confirmed else "group_termination_unconfirmed",
                )
            time.sleep(0.02)

    def _terminate_group(
        self,
        group_id: int,
        process: subprocess.Popen[bytes] | None = None,
    ) -> bool:
        try:
            self._send_group_signal(group_id, _SIGTERM)
        except ProcessLookupError:
            return True
        self._wait_root(process)
        deadline = time.monotonic() + self._termination_grace_seconds
        while time.monotonic() < deadline:
            if not self._group_exists(group_id):
                return True
            time.sleep(0.02)
        try:
            self._send_group_signal(group_id, _SIGKILL)
        except ProcessLookupError:
            return True
        self._wait_root(process)
        deadline = time.monotonic() + self._termination_grace_seconds
        while time.monotonic() < deadline:
            if not self._group_exists(group_id):
                return True
            time.sleep(0.02)
        return not self._group_exists(group_id)

    def _wait_root(self, process: subprocess.Popen[bytes] | None) -> None:
        if process is None or process.poll() is not None:
            return
        try:
            process.wait(timeout=self._termination_grace_seconds)
        except subprocess.TimeoutExpired:
            return

    @staticmethod
    def _group_exists(group_id: int) -> bool:
        try:
            PosixProcessGroupSupervisor._send_group_signal(group_id, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    @staticmethod
    def _send_group_signal(group_id: int, signal_number: int) -> None:
        kill_group = cast(Callable[[int, int], None], os.__dict__["killpg"])
        kill_group(group_id, signal_number)
