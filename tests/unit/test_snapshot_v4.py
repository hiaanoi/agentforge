from uuid import uuid4

import pytest
from pydantic import ValidationError

from agentforge.domain.enums import ResumePhase
from agentforge.domain.repair import RepairCompletionStatus
from agentforge.runtime.snapshots import (
    RepairSnapshotState,
    RuntimeSnapshotV3,
    RuntimeSnapshotV4,
    RuntimeSnapshotV5,
    SnapshotVersionError,
    load_runtime_snapshot,
)


def test_v3_migrates_to_v5_without_enabling_repair_mode() -> None:
    legacy = RuntimeSnapshotV3(
        run_id=uuid4(),
        step_number=2,
        resume_phase=ResumePhase.READY_FOR_MODEL,
    )

    loaded = load_runtime_snapshot(
        legacy.model_dump(mode="json"),
        run_id=legacy.run_id,
        step_number=legacy.step_number,
    )

    assert loaded.schema_version == 5
    assert loaded.repair is None


def test_v4_upgrades_to_v5_with_safe_repair_recovery_state() -> None:
    snapshot = RuntimeSnapshotV4(
        run_id=uuid4(),
        step_number=3,
        resume_phase=ResumePhase.READY_FOR_MODEL,
        repair=RepairSnapshotState(
            task_id="repair-1",
            policy_digest="a" * 64,
            status=RepairCompletionStatus.RUNNING,
            state_version=7,
            model_calls_used=2,
            read_calls_used=3,
            edit_attempts_used=1,
            test_runs_used=1,
            completion_corrections_used=0,
            policy_violations=0,
            baseline_id=uuid4(),
            baseline_digest="b" * 64,
            last_mutation_execution_id=uuid4(),
            last_development_test_execution_id=uuid4(),
            latest_source_verified=True,
            pending_final_verification=False,
            final_verification_execution_id=None,
            final_workspace_digest="c" * 64,
            final_diff_digest="d" * 64,
        ),
    )

    loaded = load_runtime_snapshot(
        snapshot.model_dump(mode="json"),
        run_id=snapshot.run_id,
        step_number=snapshot.step_number,
    )
    serialized = loaded.model_dump_json().casefold()

    assert loaded.schema_version == 5
    assert loaded.repair == snapshot.repair
    for forbidden in (
        "api_key",
        "allowed_env",
        "full_diff",
        "hidden_test",
        "prompt_text",
        "source_code",
        "stdout_summary",
        "stderr_summary",
    ):
        assert forbidden not in serialized


def test_v4_and_v5_reject_unknown_or_sensitive_fields() -> None:
    with pytest.raises(ValidationError):
        RuntimeSnapshotV4.model_validate(
            {
                "schema_version": 4,
                "run_id": str(uuid4()),
                "step_number": 1,
                "resume_phase": "READY_FOR_MODEL",
                "hidden_test": "secret",
            }
        )
    with pytest.raises(SnapshotVersionError):
        load_runtime_snapshot(
            {"schema_version": 6},
            run_id=uuid4(),
            step_number=1,
        )
    with pytest.raises(ValidationError):
        RuntimeSnapshotV5.model_validate(
            {
                "schema_version": 5,
                "run_id": str(uuid4()),
                "step_number": 1,
                "resume_phase": "READY_FOR_MODEL",
                "hidden_test": "secret",
            }
        )
    with pytest.raises(ValidationError):
        RuntimeSnapshotV4.model_validate(
            RuntimeSnapshotV5(
                run_id=uuid4(),
                step_number=1,
                resume_phase=ResumePhase.READY_FOR_MODEL,
                provider_usage_available=True,
            ).model_dump(mode="json")
        )
