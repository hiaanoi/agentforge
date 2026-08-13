from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from agentforge.domain.enums import (
    ConfigSourceKind,
    ProcessExecutionStatus,
    ProcessFailureKind,
)
from agentforge.domain.test_execution import (
    ProcessExecutionRecord,
)
from agentforge.domain.test_execution import (
    TestApprovalBinding as ApprovalBinding,
)
from agentforge.domain.test_execution import (
    TestProfile as Profile,
)
from agentforge.domain.test_execution import (
    TestResult as Result,
)

SHA = "a" * 64


def make_profile(tmp_path: Path) -> Profile:
    executable = Path(__file__).resolve()
    workspace = tmp_path.resolve()
    return Profile(
        profile_id="unit_tests",
        name="Unit tests",
        description="Run the registered unit test suite",
        executable_path=str(executable),
        argv=(str(executable), "-m", "pytest", "tests/unit"),
        cwd=str(workspace),
        allowed_env={"PYTHONUTF8": "1"},
        timeout_seconds=30,
        max_output_bytes=4096,
        enabled=True,
        profile_version=1,
        executable_digest=SHA,
        argv_digest=SHA,
        cwd_digest=SHA,
        environment_digest=SHA,
        config_source_kind=ConfigSourceKind.BUILTIN,
        config_source_identity="unit-test:fixture",
        config_source_digest=SHA,
        profile_digest=SHA,
    )


def test_test_profile_is_frozen_and_requires_absolute_execution_identity(
    tmp_path: Path,
) -> None:
    profile = make_profile(tmp_path)

    assert profile.argv[0] == profile.executable_path
    with pytest.raises(ValidationError):
        profile.name = "changed"
    with pytest.raises(ValidationError):
        Profile(**{**profile.model_dump(), "profile_version": 0})
    with pytest.raises(ValidationError):
        Profile(
            **{
                **profile.model_dump(),
                "executable_path": "pytest",
                "argv": ("pytest",),
            }
        )


def test_test_approval_binding_contains_only_execution_identity(tmp_path: Path) -> None:
    profile = make_profile(tmp_path)
    binding = ApprovalBinding(
        approval_id=uuid4(),
        run_id=uuid4(),
        checkpoint_id=uuid4(),
        tool_call_digest="b" * 64,
        profile_id=profile.profile_id,
        profile_version=profile.profile_version,
        profile_digest=profile.profile_digest,
        executable_path=profile.executable_path,
        argv_digest=profile.argv_digest,
        cwd=profile.cwd,
        environment_digest=profile.environment_digest,
    )

    fields = set(type(binding).model_fields)
    assert "argv" not in fields
    assert "allowed_env" not in fields
    assert binding.tool_call_digest == "b" * 64


def test_process_execution_record_tracks_attempt_and_schema_versions() -> None:
    record = ProcessExecutionRecord(
        run_id=uuid4(),
        approval_id=uuid4(),
        tool_call_digest=SHA,
        attempt_number=2,
        record_version=3,
        result_schema_version=1,
        profile_id="unit_tests",
        profile_version=1,
        profile_digest=SHA,
        executable_path="C:\\Python\\python.exe",
        argv_digest=SHA,
        cwd="C:\\workspace",
        environment_digest=SHA,
        status=ProcessExecutionStatus.FAILED,
        failure_kind=ProcessFailureKind.TEST_FAILURE,
        exit_code=1,
        stdout_digest=SHA,
        stderr_digest=SHA,
        stdout_size=12,
        stderr_size=0,
        stdout_summary="one failed",
        stderr_summary="",
        stdout_truncated=False,
        stderr_truncated=False,
        duration_ms=15,
    )

    assert record.attempt_number == 2
    assert record.record_version == 3
    assert record.result_schema_version == 1
    with pytest.raises(ValidationError):
        ProcessExecutionRecord(
            **{
                **record.model_dump(),
                "status": ProcessExecutionStatus.COMPLETED,
                "failure_kind": ProcessFailureKind.TEST_FAILURE,
            }
        )


def test_test_result_has_bounded_summary_fields_not_raw_streams() -> None:
    result = Result(
        success=False,
        exit_code=1,
        duration_ms=20,
        stdout_summary="failed",
        stderr_summary="assertion",
        stdout_digest=SHA,
        stderr_digest=SHA,
        stdout_size=6,
        stderr_size=9,
        truncated=False,
        digest="b" * 64,
    )

    fields = set(type(result).model_fields)
    assert "stdout" not in fields
    assert "stderr" not in fields
    assert result.success is False
