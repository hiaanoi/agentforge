from uuid import uuid4

import pytest

from agentforge.context.models import ContextItemKind, ResumeContextState
from agentforge.domain.enums import ResumePhase
from agentforge.runtime.snapshots import (
    RuntimeSnapshotV2,
    SnapshotVersionError,
    load_runtime_snapshot,
)


def test_unversioned_history_checkpoint_migrates_to_current_snapshot() -> None:
    run_id = uuid4()

    snapshot = load_runtime_snapshot(
        {"history": [{"message": "legacy"}]},
        run_id=run_id,
        step_number=2,
    )

    assert snapshot.schema_version == 4
    assert snapshot.run_id == run_id
    assert snapshot.context_items[0].kind is ContextItemKind.LEGACY


def test_v1_approval_snapshot_preserves_pending_state_and_call_id() -> None:
    run_id = uuid4()
    approval_id = uuid4()
    state = {
        "schema_version": 1,
        "run_id": str(run_id),
        "step_number": 3,
        "history": [],
        "pending_tool_call": {
            "tool": "read_file",
            "call_id": "call_1",
            "arguments": {"path": "README.md"},
            "reason": None,
        },
        "pending_approval_id": str(approval_id),
        "tool_call_digest": "a" * 64,
        "resume_phase": "AWAITING_APPROVAL",
    }

    snapshot = load_runtime_snapshot(state, run_id=run_id, step_number=3)

    assert snapshot.pending_tool_call is not None
    assert snapshot.pending_tool_call.call_id == "call_1"
    assert snapshot.pending_approval_id == approval_id
    assert snapshot.resume_phase is ResumePhase.AWAITING_APPROVAL


def test_v2_round_trip_migrates_without_losing_existing_state() -> None:
    snapshot = RuntimeSnapshotV2(
        run_id=uuid4(),
        step_number=1,
        resume_phase=ResumePhase.READY_FOR_MODEL,
        context_state=ResumeContextState(),
    )

    loaded = load_runtime_snapshot(
        snapshot.model_dump(mode="json"),
        run_id=snapshot.run_id,
        step_number=1,
    )

    assert loaded.schema_version == 4
    assert loaded.run_id == snapshot.run_id
    assert loaded.step_number == snapshot.step_number
    assert loaded.resume_phase is snapshot.resume_phase
    assert loaded.context_state == snapshot.context_state


def test_unknown_snapshot_version_is_rejected() -> None:
    with pytest.raises(SnapshotVersionError):
        load_runtime_snapshot(
            {"schema_version": 99},
            run_id=uuid4(),
            step_number=1,
        )
