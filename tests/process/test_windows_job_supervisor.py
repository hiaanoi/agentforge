import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from agentforge.domain.enums import ConfigSourceKind
from agentforge.domain.test_execution import TestProfile as Profile
from agentforge.process.base import SupervisorStatus
from agentforge.process.windows_job import (
    WindowsJobObjectSupervisor,
    is_process_alive,
)

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows Job Object test")
SHA = "a" * 64


def profile(tmp_path: Path, script: str, *, timeout: float) -> Profile:
    executable = str(Path(sys.executable).resolve())
    return Profile(
        profile_id="process_test",
        name="Process test",
        description="Exercise a real Windows Job Object",
        executable_path=executable,
        argv=(executable, "-u", "-c", script),
        cwd=str(tmp_path.resolve()),
        allowed_env={},
        timeout_seconds=timeout,
        max_output_bytes=1024,
        enabled=True,
        profile_version=1,
        executable_digest=SHA,
        argv_digest=SHA,
        cwd_digest=SHA,
        environment_digest=SHA,
        config_source_kind=ConfigSourceKind.BUILTIN,
        config_source_identity="windows-job-supervisor-test",
        config_source_digest=SHA,
        profile_digest=SHA,
    )


def wait_for_pids(path: Path, timeout: float = 5) -> tuple[int, int]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists() and "," in (value := path.read_text(encoding="ascii")):
            root, child = value.split(",", maxsplit=1)
            return int(root), int(child)
        time.sleep(0.02)
    raise AssertionError("test process did not publish its child PID")


def child_tree_script(pid_file: Path) -> str:
    child_code = "import time; time.sleep(30)"
    return (
        "import os, pathlib, subprocess, sys, time; "
        f"child=subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
        f"pathlib.Path({str(pid_file)!r}).write_text("
        "f'{os.getpid()},{child.pid}', encoding='ascii'); "
        "time.sleep(30)"
    )


def test_windows_timeout_terminates_complete_job_tree(tmp_path: Path) -> None:
    pid_file = tmp_path / "pids.txt"
    supervisor = WindowsJobObjectSupervisor()

    outcome = supervisor.run(profile(tmp_path, child_tree_script(pid_file), timeout=1))
    root_pid, child_pid = wait_for_pids(pid_file)

    assert outcome.status is SupervisorStatus.TIMEOUT
    assert outcome.root_pid is not None
    assert outcome.job_id
    assert outcome.termination_confirmed is True
    assert not is_process_alive(outcome.root_pid)
    assert not is_process_alive(root_pid)
    assert not is_process_alive(child_pid)


def test_windows_cancel_terminates_complete_job_tree_without_residue(
    tmp_path: Path,
) -> None:
    pid_file = tmp_path / "pids.txt"
    supervisor = WindowsJobObjectSupervisor()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            supervisor.run,
            profile(tmp_path, child_tree_script(pid_file), timeout=30),
        )
        root_pid, child_pid = wait_for_pids(pid_file)
        assert supervisor.cancel("runtime_cancel") is True
        assert not is_process_alive(root_pid)
        assert not is_process_alive(child_pid)
        outcome = future.result(timeout=10)

    assert outcome.status is SupervisorStatus.CANCELLED
    assert outcome.termination_confirmed is True
    assert not is_process_alive(root_pid)
    assert not is_process_alive(child_pid)


def test_windows_job_preserves_bounded_stdout_and_fixed_environment(
    tmp_path: Path,
) -> None:
    script = (
        "import os; "
        "print(os.environ.get('PROFILE_VALUE')); "
        "print(os.environ.get('OPENAI_API_KEY')); "
        "print('x' * 5000)"
    )
    configured = profile(tmp_path, script, timeout=10).model_copy(
        update={"allowed_env": {"PROFILE_VALUE": "fixed"}}
    )

    outcome = WindowsJobObjectSupervisor().run(configured)

    assert outcome.status is SupervisorStatus.EXITED
    assert outcome.exit_code == 0
    assert "fixed" in outcome.stdout.summary
    assert "None" in outcome.stdout.summary
    assert outcome.stdout.truncated is True
    assert len(outcome.stdout.retained_bytes) == configured.max_output_bytes
