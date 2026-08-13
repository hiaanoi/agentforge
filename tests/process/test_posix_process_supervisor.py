import os
import sys
from pathlib import Path

import pytest

from agentforge.domain.enums import ConfigSourceKind
from agentforge.domain.test_execution import TestProfile as Profile
from agentforge.process.base import SupervisorStatus
from agentforge.process.posix import PosixProcessGroupSupervisor

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX process-group test")
SHA = "a" * 64


def test_posix_timeout_terminates_complete_process_group(tmp_path: Path) -> None:
    executable = str(Path(sys.executable).resolve())
    child_code = "import time; time.sleep(30)"
    script = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
        "time.sleep(30)"
    )
    profile = Profile(
        profile_id="posix_tree",
        name="POSIX tree",
        description="Exercise a real process group",
        executable_path=executable,
        argv=(executable, "-u", "-c", script),
        cwd=str(tmp_path.resolve()),
        allowed_env={},
        timeout_seconds=0.5,
        max_output_bytes=1024,
        enabled=True,
        profile_version=1,
        executable_digest=SHA,
        argv_digest=SHA,
        cwd_digest=SHA,
        environment_digest=SHA,
        config_source_kind=ConfigSourceKind.BUILTIN,
        config_source_identity="posix-process-supervisor-test",
        config_source_digest=SHA,
        profile_digest=SHA,
    )

    outcome = PosixProcessGroupSupervisor().run(profile)

    assert outcome.status is SupervisorStatus.TIMEOUT
    assert outcome.process_group_id == outcome.root_pid
    assert outcome.termination_confirmed is True
