from uuid import uuid4

import pytest

from agentforge.domain.enums import ProcessExecutionStatus, ResumePhase
from agentforge.domain.test_execution import PendingTestExecution
from agentforge.domain.test_execution import TestResult as Result
from agentforge.runtime.snapshots import (
    RuntimeSnapshotV2,
    RuntimeSnapshotV3,
    SnapshotVersionError,
    load_runtime_snapshot,
)

SHA = "a" * 64


def test_v2_migrates_to_current_snapshot_with_empty_test_state() -> None:
    legacy = RuntimeSnapshotV2(
        run_id=uuid4(),
        step_number=2,
        resume_phase=ResumePhase.READY_FOR_MODEL,
    )

    loaded = load_runtime_snapshot(
        legacy.model_dump(mode="json"),
        run_id=legacy.run_id,
        step_number=legacy.step_number,
    )

    assert loaded.schema_version == 4
    assert loaded.pending_test_execution is None
    assert loaded.last_test_result is None
    assert loaded.test_execution_state is None


def test_v3_round_trip_persists_only_safe_test_recovery_state() -> None:
    approval_id = uuid4()
    snapshot = RuntimeSnapshotV3(
        run_id=uuid4(),
        step_number=3,
        pending_approval_id=approval_id,
        pending_test_execution=PendingTestExecution(
            approval_id=approval_id,
            execution_id=uuid4(),
            profile_id="unit_tests",
            profile_version=1,
            profile_digest=SHA,
            argv_digest=SHA,
            environment_digest=SHA,
            attempt_number=1,
        ),
        last_test_result=Result(
            success=False,
            exit_code=1,
            duration_ms=20,
            stdout_summary="one failed",
            stderr_summary="",
            stdout_digest=SHA,
            stderr_digest=SHA,
            stdout_size=10,
            stderr_size=0,
            truncated=False,
            digest="b" * 64,
        ),
        test_execution_state=ProcessExecutionStatus.FAILED,
        resume_phase=ResumePhase.READY_FOR_MODEL,
    )

    loaded = load_runtime_snapshot(
        snapshot.model_dump(mode="json"),
        run_id=snapshot.run_id,
        step_number=snapshot.step_number,
    )
    serialized = loaded.model_dump_json()

    assert loaded.schema_version == 4
    assert loaded.run_id == snapshot.run_id
    assert loaded.pending_test_execution == snapshot.pending_test_execution
    assert loaded.last_test_result == snapshot.last_test_result
    assert loaded.test_execution_state == snapshot.test_execution_state
    assert '"argv"' not in serialized
    assert "allowed_env" not in serialized
    assert '"stdout"' not in serialized
    assert '"stderr"' not in serialized


def test_v3_rejects_identity_mismatch_and_future_version() -> None:
    snapshot = RuntimeSnapshotV3(
        run_id=uuid4(),
        step_number=1,
        resume_phase=ResumePhase.READY_FOR_MODEL,
    )

    with pytest.raises(SnapshotVersionError):
        load_runtime_snapshot(
            snapshot.model_dump(mode="json"),
            run_id=uuid4(),
            step_number=1,
        )
    with pytest.raises(SnapshotVersionError):
        load_runtime_snapshot(
            {"schema_version": 5},
            run_id=uuid4(),
            step_number=1,
        )
