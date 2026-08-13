from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import tempfile
import time
from ctypes import wintypes
from pathlib import Path
from threading import Event, Lock
from typing import Any, BinaryIO, cast
from uuid import uuid4

from agentforge.domain.test_execution import TestProfile
from agentforge.process.base import (
    StreamCaptureWorker,
    SupervisorOutcome,
    SupervisorStatus,
    empty_captured_stream,
)

_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION = 1
_JOB_OBJECT_BASIC_PROCESS_ID_LIST = 3
_PROCESS_TERMINATE = 0x0001
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_SYNCHRONIZE = 0x00100000
_WAIT_TIMEOUT = 0x00000102
_ERROR_MORE_DATA = 234


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _BasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _ExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _BasicAccountingInformation(ctypes.Structure):
    _fields_ = [
        ("TotalUserTime", ctypes.c_longlong),
        ("TotalKernelTime", ctypes.c_longlong),
        ("ThisPeriodTotalUserTime", ctypes.c_longlong),
        ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
        ("TotalPageFaultCount", wintypes.DWORD),
        ("TotalProcesses", wintypes.DWORD),
        ("ActiveProcesses", wintypes.DWORD),
        ("TotalTerminatedProcesses", wintypes.DWORD),
    ]


class _WindowsJobApi:
    def __init__(self) -> None:
        if os.name != "nt":
            raise OSError("Windows Job Objects are available only on Windows")
        self._kernel32: Any = ctypes.WinDLL(
            "kernel32",
            use_last_error=True,
        )
        self._configure_signatures()

    def create(self, name: str) -> int:
        handle = self._kernel32.CreateJobObjectW(None, name)
        if not handle:
            self._raise_last_error("CreateJobObjectW")
        information = _ExtendedLimitInformation()
        information.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self._kernel32.SetInformationJobObject(
            handle,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(information),
            ctypes.sizeof(information),
        ):
            self.close(int(handle))
            self._raise_last_error("SetInformationJobObject")
        return int(handle)

    def assign_pid(self, job_handle: int, pid: int) -> None:
        process_handle = self._kernel32.OpenProcess(
            _PROCESS_TERMINATE | _PROCESS_SET_QUOTA | _PROCESS_QUERY_LIMITED_INFORMATION,
            False,
            pid,
        )
        if not process_handle:
            self._raise_last_error("OpenProcess")
        try:
            if not self._kernel32.AssignProcessToJobObject(job_handle, process_handle):
                self._raise_last_error("AssignProcessToJobObject")
        finally:
            self._kernel32.CloseHandle(process_handle)

    def terminate(self, job_handle: int, exit_code: int = 1) -> bool:
        return bool(self._kernel32.TerminateJobObject(job_handle, exit_code))

    def active_processes(self, job_handle: int) -> int:
        information = _BasicAccountingInformation()
        if not self._kernel32.QueryInformationJobObject(
            job_handle,
            _JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION,
            ctypes.byref(information),
            ctypes.sizeof(information),
            None,
        ):
            self._raise_last_error("QueryInformationJobObject")
        return int(information.ActiveProcesses)

    def wait_empty(self, job_handle: int, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.active_processes(job_handle) == 0:
                return True
            time.sleep(0.02)
        return self.active_processes(job_handle) == 0

    def terminate_and_wait(self, job_handle: int, timeout: float) -> bool:
        handles = self._open_job_process_handles(job_handle)
        try:
            if not self.terminate(job_handle):
                return False
            deadline = time.monotonic() + timeout
            if not self.wait_empty(job_handle, timeout):
                return False
            for handle in handles:
                remaining_ms = max(0, int((deadline - time.monotonic()) * 1000))
                if self._kernel32.WaitForSingleObject(handle, remaining_ms) == _WAIT_TIMEOUT:
                    return False
            return True
        finally:
            for handle in handles:
                self._kernel32.CloseHandle(handle)

    def close(self, job_handle: int) -> None:
        if job_handle:
            self._kernel32.CloseHandle(job_handle)

    def process_alive(self, pid: int) -> bool:
        handle = self._kernel32.OpenProcess(
            _SYNCHRONIZE | _PROCESS_QUERY_LIMITED_INFORMATION,
            False,
            pid,
        )
        if not handle:
            return False
        try:
            return bool(
                self._kernel32.WaitForSingleObject(handle, 0) == _WAIT_TIMEOUT
            )
        finally:
            self._kernel32.CloseHandle(handle)

    def _open_job_process_handles(self, job_handle: int) -> list[int]:
        process_ids = self._job_process_ids(job_handle)
        handles: list[int] = []
        for pid in process_ids:
            handle = self._kernel32.OpenProcess(
                _SYNCHRONIZE | _PROCESS_QUERY_LIMITED_INFORMATION,
                False,
                pid,
            )
            if handle:
                handles.append(int(handle))
        return handles

    def _job_process_ids(self, job_handle: int) -> list[int]:
        capacity = 64
        pointer_size = ctypes.sizeof(ctypes.c_size_t)
        while capacity <= 65_536:
            buffer = ctypes.create_string_buffer(8 + capacity * pointer_size)
            if self._kernel32.QueryInformationJobObject(
                job_handle,
                _JOB_OBJECT_BASIC_PROCESS_ID_LIST,
                buffer,
                len(buffer),
                None,
            ):
                count = ctypes.c_uint32.from_buffer(buffer, 4).value
                values = (ctypes.c_size_t * count).from_buffer(buffer, 8)
                return [int(value) for value in values]
            if ctypes.get_last_error() != _ERROR_MORE_DATA:
                self._raise_last_error("QueryInformationJobObject")
            capacity *= 2
        raise OSError("Job contains too many processes to terminate safely")

    def _configure_signatures(self) -> None:
        self._kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self._kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        self._kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        self._kernel32.SetInformationJobObject.restype = wintypes.BOOL
        self._kernel32.AssignProcessToJobObject.argtypes = [
            wintypes.HANDLE,
            wintypes.HANDLE,
        ]
        self._kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        self._kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self._kernel32.TerminateJobObject.restype = wintypes.BOOL
        self._kernel32.QueryInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.c_void_p,
        ]
        self._kernel32.QueryInformationJobObject.restype = wintypes.BOOL
        self._kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self._kernel32.OpenProcess.restype = wintypes.HANDLE
        self._kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel32.CloseHandle.restype = wintypes.BOOL
        self._kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self._kernel32.WaitForSingleObject.restype = wintypes.DWORD

    @staticmethod
    def _raise_last_error(operation: str) -> None:
        code = ctypes.get_last_error()
        raise OSError(code, f"{operation} failed")


class WindowsJobObjectSupervisor:
    def __init__(self, *, termination_grace_seconds: float = 3) -> None:
        self._api = _WindowsJobApi()
        self._termination_grace_seconds = termination_grace_seconds
        self._cancelled = Event()
        self._lock = Lock()
        self._job_handle: int | None = None
        self._termination_reason: str | None = None

    def run(self, profile: TestProfile) -> SupervisorOutcome:
        started = time.perf_counter()
        job_id = str(uuid4())
        job_handle = self._api.create(f"AgentForgeTest-{job_id}")
        pid_path = self._new_pid_path(job_id)
        launcher: subprocess.Popen[bytes] | None = None
        stdout_worker: StreamCaptureWorker | None = None
        stderr_worker: StreamCaptureWorker | None = None
        root_pid: int | None = None
        status = SupervisorStatus.LAUNCH_FAILED
        exit_code: int | None = None
        termination_confirmed = False
        termination_result: str | None = None
        try:
            launcher_executable = str(
                Path(sys.__dict__.get("_base_executable", sys.executable)).resolve()
            )
            launcher_script = str(Path(__file__).with_name("windows_launcher.py").resolve())
            launcher = subprocess.Popen(
                [launcher_executable, launcher_script, str(pid_path)],
                executable=launcher_executable,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
            )
            if launcher.stdin is None or launcher.stdout is None or launcher.stderr is None:
                raise RuntimeError("Windows launcher pipes were not created")
            self._api.assign_pid(job_handle, launcher.pid)
            with self._lock:
                self._job_handle = job_handle
            stdout_worker = StreamCaptureWorker(
                cast(BinaryIO, launcher.stdout),
                profile.max_output_bytes,
            )
            stderr_worker = StreamCaptureWorker(
                cast(BinaryIO, launcher.stderr),
                profile.max_output_bytes,
            )
            stdout_worker.start()
            stderr_worker.start()
            payload = json.dumps(
                {
                    "executable_path": profile.executable_path,
                    "argv": list(profile.argv),
                    "cwd": profile.cwd,
                    "environment": dict(profile.allowed_env),
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            launcher.stdin.write(payload.encode("utf-8"))
            launcher.stdin.close()
            root_pid = self._wait_for_root_pid(pid_path, launcher, timeout=5)
            if root_pid is None:
                status = SupervisorStatus.LAUNCH_FAILED
            else:
                status, termination_confirmed, termination_result = self._wait_for_process(
                    launcher,
                    job_handle,
                    profile.timeout_seconds,
                )
            exit_code = launcher.wait(timeout=self._termination_grace_seconds)
        except (OSError, RuntimeError, subprocess.SubprocessError):
            if launcher is not None and launcher.poll() is None:
                termination_confirmed = self._api.terminate_and_wait(
                    job_handle,
                    self._termination_grace_seconds,
                )
            status = (
                SupervisorStatus.LAUNCH_FAILED
                if termination_confirmed or launcher is None
                else SupervisorStatus.INDETERMINATE
            )
            termination_result = "launcher_failed"
        finally:
            if launcher is not None and launcher.poll() is None:
                termination_confirmed = self._api.terminate_and_wait(
                    job_handle,
                    self._termination_grace_seconds,
                )
                try:
                    launcher.wait(timeout=self._termination_grace_seconds)
                except subprocess.TimeoutExpired:
                    pass
            with self._lock:
                self._job_handle = None
            self._api.close(job_handle)
            pid_path.unlink(missing_ok=True)
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
            root_pid=root_pid,
            process_group_id=None,
            job_id=job_id,
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
            if self._job_handle is None:
                return False
            return self._api.terminate_and_wait(
                self._job_handle,
                self._termination_grace_seconds,
            )

    def _wait_for_process(
        self,
        launcher: subprocess.Popen[bytes],
        job_handle: int,
        timeout: float,
    ) -> tuple[SupervisorStatus, bool, str]:
        deadline = time.monotonic() + timeout
        while True:
            if self._cancelled.is_set():
                confirmed = self._api.terminate_and_wait(
                    job_handle,
                    self._termination_grace_seconds,
                )
                return (
                    SupervisorStatus.CANCELLED
                    if confirmed
                    else SupervisorStatus.INDETERMINATE,
                    confirmed,
                    "job_terminated" if confirmed else "job_termination_unconfirmed",
                )
            if launcher.poll() is not None:
                confirmed = self._api.wait_empty(
                    job_handle,
                    self._termination_grace_seconds,
                )
                return (
                    SupervisorStatus.EXITED
                    if confirmed
                    else SupervisorStatus.INDETERMINATE,
                    confirmed,
                    "natural_exit" if confirmed else "job_not_empty_after_exit",
                )
            if time.monotonic() >= deadline:
                with self._lock:
                    if self._termination_reason is None:
                        self._termination_reason = "timeout"
                confirmed = self._api.terminate_and_wait(
                    job_handle,
                    self._termination_grace_seconds,
                )
                return (
                    SupervisorStatus.TIMEOUT
                    if confirmed
                    else SupervisorStatus.INDETERMINATE,
                    confirmed,
                    "job_terminated" if confirmed else "job_termination_unconfirmed",
                )
            time.sleep(0.02)

    @staticmethod
    def _wait_for_root_pid(
        pid_path: Path,
        launcher: subprocess.Popen[bytes],
        *,
        timeout: float,
    ) -> int | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if pid_path.exists():
                value = pid_path.read_text(encoding="ascii").strip()
                if value.isdigit():
                    return int(value)
                if value == "LAUNCH_ERROR":
                    return None
            if launcher.poll() is not None:
                return None
            time.sleep(0.01)
        return None

    @staticmethod
    def _new_pid_path(job_id: str) -> Path:
        return Path(tempfile.gettempdir()) / f"agentforge-{job_id}.pid"


def is_process_alive(pid: int) -> bool:
    if os.name != "nt":
        raise OSError("Windows process checks are available only on Windows")
    return _WindowsJobApi().process_alive(pid)
