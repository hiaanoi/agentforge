from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from agentforge.domain.enums import EventType, MutationExecutionStatus, WriteMode
from agentforge.domain.mutations import (
    MutationApprovalBinding,
    MutationExecutionRecord,
    MutationPlan,
)

SHA_A = "a" * 64
SHA_B = "b" * 64


def test_mutation_enums_and_events_are_explicit() -> None:
    assert [mode.value for mode in WriteMode] == [
        "CREATE_ONLY",
        "EXPECTED_HASH_REPLACE",
    ]
    assert [status.value for status in MutationExecutionStatus] == [
        "PREPARED",
        "WRITING",
        "COMMITTED",
        "FAILED",
        "INDETERMINATE",
    ]
    assert {
        EventType.MUTATION_REQUESTED,
        EventType.MUTATION_STARTED,
        EventType.MUTATION_COMMITTED,
        EventType.MUTATION_FAILED,
        EventType.MUTATION_INDETERMINATE,
    }


def test_mutation_plan_distinguishes_absent_and_existing_targets() -> None:
    created = MutationPlan(
        tool_name="write_file",
        target_path="src/new.py",
        target_existed=False,
        before_sha256=None,
        expected_after_sha256=SHA_B,
        bytes_written=12,
    )
    replaced = created.model_copy(
        update={"target_existed": True, "before_sha256": SHA_A}
    )

    assert created.before_sha256 is None
    assert replaced.before_sha256 == SHA_A
    with pytest.raises(ValidationError):
        MutationPlan(
            tool_name="write_file",
            target_path="src/app.py",
            target_existed=True,
            before_sha256=None,
            expected_after_sha256=SHA_B,
            bytes_written=12,
        )


def test_mutation_binding_is_immutable_and_contains_no_source_text() -> None:
    binding = MutationApprovalBinding(
        approval_id=uuid4(),
        run_id=uuid4(),
        checkpoint_id=uuid4(),
        tool_call_digest=SHA_A,
        tool_name="edit_file",
        target_path="src/app.py",
        target_existed=True,
        before_sha256=SHA_A,
        expected_after_sha256=SHA_B,
        bytes_written=32,
    )

    assert not ({"content", "old_text", "new_text"} & binding.model_dump().keys())
    with pytest.raises(ValidationError):
        binding.target_path = "other.py"


def test_mutation_execution_record_validates_state_and_bounds_summary() -> None:
    now = datetime.now(UTC)
    record = MutationExecutionRecord(
        execution_id=uuid4(),
        run_id=uuid4(),
        approval_id=uuid4(),
        tool_call_digest=SHA_A,
        tool_name="write_file",
        target_path="src/app.py",
        before_sha256=SHA_A,
        expected_after_sha256=SHA_B,
        before_workspace_digest=SHA_A,
        expected_after_workspace_digest=SHA_B,
        actual_after_sha256=None,
        bytes_written=0,
        status=MutationExecutionStatus.PREPARED,
        result_summary=None,
        created_at=now,
        updated_at=now,
    )

    assert record.status is MutationExecutionStatus.PREPARED
    assert not ({"content", "old_text", "new_text"} & record.model_dump().keys())
    with pytest.raises(ValidationError):
        MutationExecutionRecord.model_validate(
            {**record.model_dump(), "result_summary": "x" * 501}
        )
