from uuid import uuid4

from agentforge.domain.digests import compute_tool_call_digest
from agentforge.domain.enums import (
    ApprovalConsumptionState,
    ApprovalStatus,
    RejectionStrategy,
    ResumePhase,
)
from agentforge.domain.models import ApprovalRequest, PendingToolCall, RuntimeSnapshot


def test_runtime_snapshot_contains_versioned_resume_context() -> None:
    run_id = uuid4()
    approval_id = uuid4()
    checkpoint_id = uuid4()
    call = PendingToolCall(
        tool="approval_probe",
        arguments={"value": "raw-secret"},
        reason="test approval",
    )
    digest = compute_tool_call_digest(
        tool_name=call.tool,
        validated_arguments=call.arguments,
        checkpoint_id=checkpoint_id,
        step_number=2,
    )

    snapshot = RuntimeSnapshot(
        run_id=run_id,
        step_number=2,
        history=[],
        pending_tool_call=call,
        pending_approval_id=approval_id,
        tool_call_digest=digest,
        resume_phase=ResumePhase.AWAITING_APPROVAL,
    )

    assert snapshot.schema_version == 1
    assert snapshot.pending_tool_call == call
    assert snapshot.tool_call_digest == digest


def test_approval_request_has_safe_defaults() -> None:
    approval = ApprovalRequest(
        run_id=uuid4(),
        checkpoint_id=uuid4(),
        tool_name="approval_probe",
        sanitized_arguments={"value": "<redacted>"},
        request_digest="a" * 64,
    )

    assert approval.status is ApprovalStatus.PENDING
    assert approval.rejection_strategy is RejectionStrategy.CONTINUE
    assert approval.consumption_state is ApprovalConsumptionState.NOT_STARTED
    assert approval.result_status is None
    assert approval.result_summary is None


def test_tool_call_digest_is_stable_and_binds_checkpoint_and_step() -> None:
    checkpoint_id = uuid4()
    values = {
        "tool_name": "approval_probe",
        "validated_arguments": {"second": [2, 1], "first": "raw-secret"},
        "checkpoint_id": checkpoint_id,
        "step_number": 3,
    }

    first = compute_tool_call_digest(**values)
    reordered = compute_tool_call_digest(
        tool_name="approval_probe",
        validated_arguments={"first": "raw-secret", "second": [2, 1]},
        checkpoint_id=checkpoint_id,
        step_number=3,
    )
    other_checkpoint = compute_tool_call_digest(
        **{**values, "checkpoint_id": uuid4()}
    )
    other_step = compute_tool_call_digest(**{**values, "step_number": 4})

    assert len(first) == 64
    assert first == reordered
    assert first != other_checkpoint
    assert first != other_step
