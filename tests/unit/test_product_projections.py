import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from agentforge.application.contracts import LifecycleStatus, OutcomeStatus, ProfilePurpose
from agentforge.application.events import (
    PRODUCT_EVENT_ADAPTER,
    ApprovalRequestedPayload,
    ProductEvent,
    ProductEventName,
    ProfileTrustedPayload,
    RunFinishedPayload,
    RunStatePayload,
)
from agentforge.application.kernel_errors import EventPayloadError
from agentforge.application.projections import (
    REVIEWED_EXCLUDED_EVENT_TYPES,
    REVIEWED_PRODUCT_EVENT_TYPES,
    ProductProjectionError,
    ProductProjector,
)
from agentforge.application.views import (
    ExportRunDetailsView,
    RunProjectionFacts,
)
from agentforge.domain.enums import ApprovalStatus, EventType, RunStatus
from agentforge.domain.models import ApprovalRequest, PersistedEvent, Run
from agentforge.domain.repair import RepairCompletionStatus
from agentforge.domain.strict_json import canonical_json_size
from agentforge.evaluation.public_artifacts import (
    ForbiddenPublicArtifactError,
    PublicArtifactScanner,
)
from agentforge.persistence.event_log import EventLog


def _event(
    event_type: EventType | str,
    payload: dict[str, object],
    *,
    cursor: int = 1,
) -> PersistedEvent:
    run_id = uuid4()
    return PersistedEvent(
        event_id=uuid4(),
        global_cursor=cursor,
        scope_type="RUN",
        scope_id=str(run_id),
        run_id=run_id,
        sequence_number=cursor,
        event_type=event_type.value if isinstance(event_type, EventType) else event_type,
        payload=payload,
        created_at=datetime(2026, 8, 10, tzinfo=UTC),
    )


def test_product_event_is_closed_and_rejects_type_drift_and_nonfinite() -> None:
    run_id = uuid4()
    event = ProductEvent(
        event_id=uuid4(),
        scope_type="RUN",
        scope_id=str(run_id),
        cursor=1,
        run_id=run_id,
        sequence_number=1,
        occurred_at=datetime(2026, 8, 10, tzinfo=UTC),
        payload=RunStatePayload(
            lifecycle_status=LifecycleStatus.RUNNING,
            outcome_status=None,
        ),
    )
    PRODUCT_EVENT_ADAPTER.validate_json(event.model_dump_json())
    raw = event.model_dump(mode="python")
    with pytest.raises(ValidationError):
        PRODUCT_EVENT_ADAPTER.validate_python({**raw, "cursor": True})
    with pytest.raises(ValidationError):
        PRODUCT_EVENT_ADAPTER.validate_python({**raw, "cursor": 1.0})
    with pytest.raises(ValidationError):
        PRODUCT_EVENT_ADAPTER.validate_python(
            {**raw, "payload": {"type": "unknown", "duration_ms": float("nan")}}
        )
    with pytest.raises(ValidationError):
        PRODUCT_EVENT_ADAPTER.validate_python({**raw, "unexpected": "value"})


def test_product_event_scope_topology_is_closed() -> None:
    with pytest.raises(ValidationError):
        ProductEvent(
            event_id=uuid4(),
            scope_type="RUN",
            scope_id=str(uuid4()),
            cursor=1,
            run_id=uuid4(),
            sequence_number=1,
            occurred_at=datetime(2026, 8, 10, tzinfo=UTC),
            payload=RunStatePayload(
                lifecycle_status=LifecycleStatus.CREATED,
                outcome_status=None,
            ),
        )


def test_projector_allowlists_approval_payload_and_discards_arguments() -> None:
    approval_id = uuid4()
    persisted = _event(
        EventType.APPROVAL_REQUESTED,
        {
            "approval_id": str(approval_id),
            "tool_name": "write_file",
            "sanitized_arguments": {"path": "C:\\private\\prompt.txt"},
            "raw_output": "secret",
        },
    )
    event = ProductProjector().event(persisted)

    assert event.payload == ApprovalRequestedPayload(
        approval_id=approval_id, tool_name="write_file"
    )
    PublicArtifactScanner().validate(event.model_dump_json())
    assert "arguments" not in event.model_dump_json()
    assert "secret" not in event.model_dump_json()


@pytest.mark.parametrize(
    ("status", "repair_status", "lifecycle", "outcome"),
    [
        (RunStatus.CREATED, RepairCompletionStatus.RUNNING, LifecycleStatus.CREATED, None),
        (RunStatus.RUNNING, RepairCompletionStatus.RUNNING, LifecycleStatus.RUNNING, None),
        (
            RunStatus.WAITING_APPROVAL,
            RepairCompletionStatus.RUNNING,
            LifecycleStatus.PAUSED,
            OutcomeStatus.UNVERIFIED,
        ),
        (
            RunStatus.COMPLETED,
            RepairCompletionStatus.VERIFIED_SUCCESS,
            LifecycleStatus.TERMINAL,
            OutcomeStatus.VERIFIED,
        ),
        (
            RunStatus.FAILED,
            RepairCompletionStatus.INDETERMINATE,
            LifecycleStatus.TERMINAL,
            OutcomeStatus.UNKNOWN,
        ),
        (
            RunStatus.FAILED,
            RepairCompletionStatus.FINAL_VERIFICATION_FAILED,
            LifecycleStatus.TERMINAL,
            OutcomeStatus.FAILED,
        ),
    ],
)
def test_run_projection_keeps_lifecycle_and_nullable_outcome_orthogonal(
    status: RunStatus,
    repair_status: RepairCompletionStatus,
    lifecycle: LifecycleStatus,
    outcome: OutcomeStatus | None,
) -> None:
    run = Run(task="private prompt", status=status)
    facts = RunProjectionFacts(
        run=run,
        repair_status=repair_status,
        event_count=3,
        last_cursor=8,
    )
    view = ProductProjector().export_run(facts)
    assert view.lifecycle_status is lifecycle
    assert view.outcome_status is outcome


@pytest.mark.parametrize(
    "run_status,repair_status",
    (
        (RunStatus.COMPLETED, RepairCompletionStatus.TESTS_FAILED),
        (RunStatus.FAILED, RepairCompletionStatus.CANCELLED),
        (RunStatus.CANCELLED, RepairCompletionStatus.RUNTIME_FAILURE),
    ),
)
def test_run_projection_rejects_malformed_terminal_status_pairs(
    run_status: RunStatus, repair_status: RepairCompletionStatus
) -> None:
    with pytest.raises(ProductProjectionError):
        ProductProjector().run_details(
            RunProjectionFacts(
                run=Run(task="private", status=run_status),
                repair_status=repair_status,
                event_count=1,
                last_cursor=1,
            )
        )


def test_public_event_and_export_view_are_safe_and_stable() -> None:
    run = Run(
        task="PRIVATE PROMPT /home/alice/secret",
        status=RunStatus.COMPLETED,
        model_provider="openai/secret-model",
        final_output="C:\\private\\raw_output.txt",
        error_message="sk-private",
        created_at=datetime(2026, 8, 10, tzinfo=UTC),
        updated_at=datetime(2026, 8, 10, 1, tzinfo=UTC),
    )
    facts = RunProjectionFacts(
        run=run,
        repair_status=RepairCompletionStatus.VERIFIED_SUCCESS,
        event_count=3,
        last_cursor=8,
        source_digest="a" * 64,
        config_digest="b" * 64,
        profile_digest="c" * 64,
    )
    exported = ProductProjector().export_run(facts)
    encoded = exported.model_dump_json()

    assert ExportRunDetailsView.model_validate_json(encoded).model_dump_json() == encoded
    PublicArtifactScanner(forbidden_values=("sk-private",)).validate(encoded)
    assert "PRIVATE PROMPT" not in encoded
    assert "secret-model" not in encoded
    assert "raw_output" not in encoded


def test_projector_rejects_unknown_or_tampered_persisted_facts() -> None:
    projector = ProductProjector()
    with pytest.raises(ProductProjectionError):
        projector.event(_event("UNRECOGNIZED_EVENT", {}))
    with pytest.raises(ProductProjectionError):
        projector.event(
            _event(
                EventType.APPROVAL_REQUESTED,
                {"approval_id": "not-a-uuid", "tool_name": "write_file"},
            )
        )
    with pytest.raises(ProductProjectionError):
        projector.event(
            _event(EventType.MODEL_REQUESTED, {"estimated_cost": float("inf")})
        )
    with pytest.raises(ProductProjectionError):
        projector.event(
            _event(
                EventType.APPROVAL_REQUESTED,
                {"approval_id": str(uuid4()), "tool_name": "/home/alice/tool"},
            )
        )


@pytest.mark.parametrize(
    "payload",
    (
        {"large": "x" * 64_001},
        {"wide": list(range(10_001))},
    ),
)
def test_projector_rejects_oversized_or_wide_forged_payloads(payload: dict[str, object]) -> None:
    with pytest.raises(ProductProjectionError):
        ProductProjector().event(_event(EventType.MODEL_REQUESTED, payload))


def _canonical_boundary_payload(*, over_limit: bool = False) -> dict[str, object]:
    """Exactly 1,000,000 canonical bytes; +1 byte when requested.

    The control characters exercise escaped-byte accounting while ``é`` verifies
    ``ensure_ascii=False`` UTF-8 sizing rather than Python character counts.
    """
    final = "\x00" * 8_394 + ("aa" if over_limit else "a")
    return {"values": ["\x00" * 15 + "é"] * 9_996 + [final]}


@pytest.mark.parametrize(
    "payload",
    [
        {"values": ["x" * 100 for _ in range(9_997)]},
        _canonical_boundary_payload(over_limit=True),
        {"huge": 1 << 3_000_000},
    ],
    ids=["9997-strings-over-1mb", "canonical-boundary-plus-one", "huge-integer"],
)
def test_event_log_and_projector_reject_the_same_invalid_canonical_payload(
    payload: dict[str, object],
) -> None:
    with pytest.raises(EventPayloadError):
        EventLog._copy_json_payload(payload)
    with pytest.raises(ProductProjectionError):
        ProductProjector().event(_event(EventType.MODEL_REQUESTED, payload))


def test_event_log_and_projector_accept_the_exact_canonical_payload_limit() -> None:
    payload = _canonical_boundary_payload()

    assert canonical_json_size(payload) == 1_000_000
    copied = EventLog._copy_json_payload(payload)
    projected = ProductProjector().event(_event(EventType.MODEL_REQUESTED, payload))

    assert copied == payload
    assert projected.event is ProductEventName.PROGRESS


def test_canonical_json_size_counts_utf8_and_json_escaping_exactly() -> None:
    payload = {
        "\x00\x1f\t\n\\\"é😀": ["\b\f\r\x00\x1f\\\"é😀", None, -0.0]
    }

    assert canonical_json_size(payload) == len(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )


def test_event_log_and_projector_accept_a_normal_strict_payload() -> None:
    payload: dict[str, object] = {"attempt": 2, "result": [True, None, "ok"]}

    assert EventLog._copy_json_payload(payload) == payload
    assert ProductProjector().event(_event(EventType.MODEL_REQUESTED, payload)).event is (
        ProductEventName.PROGRESS
    )


def test_projector_supports_safe_workspace_profile_trust_event() -> None:
    persisted = PersistedEvent(
        event_id=uuid4(),
        global_cursor=9,
        scope_type="WORKSPACE",
        scope_id="a" * 64,
        event_type="PROFILE_TRUSTED",
        payload={
            "profile_id": "verify",
            "profile_version": 1,
            "profile_digest": "b" * 64,
            "executable_digest": "c" * 64,
            "cwd_identity": "C:\\private\\workspace",
            "purpose": "verification",
        },
        created_at=datetime(2026, 8, 10, tzinfo=UTC),
    )
    projected = ProductProjector().event(persisted)
    assert projected.payload == ProfileTrustedPayload(
        profile_id="verify",
        profile_version=1,
        profile_digest="b" * 64,
        purpose=ProfilePurpose.VERIFICATION,
    )
    PublicArtifactScanner().validate(projected.model_dump_json())


def test_product_events_sort_by_global_cursor_not_run_sequence() -> None:
    events = [
        _event(EventType.RUN_STARTED, {}, cursor=7),
        _event(EventType.RUN_CREATED, {}, cursor=2),
    ]
    projected = ProductProjector().events(events)
    assert [event.cursor for event in projected] == [2, 7]


def test_product_events_reject_duplicate_cursor_and_non_monotonic_run_sequence() -> None:
    run_id = uuid4()
    def fact(event_type: EventType, *, cursor: int, sequence: int) -> PersistedEvent:
        return _event(event_type, {}, cursor=cursor).model_copy(
            update={"run_id": run_id, "scope_id": str(run_id), "sequence_number": sequence}
        )
    duplicate_cursor = [
        fact(EventType.RUN_CREATED, cursor=1, sequence=1),
        fact(EventType.RUN_STARTED, cursor=1, sequence=2),
    ]
    decreasing_sequence = [
        fact(EventType.RUN_CREATED, cursor=1, sequence=2),
        fact(EventType.RUN_STARTED, cursor=2, sequence=1),
    ]
    for facts in (duplicate_cursor, decreasing_sequence):
        with pytest.raises(ProductProjectionError):
            ProductProjector().events(facts)


def test_product_event_rejects_a_run_payload_in_workspace_scope() -> None:
    with pytest.raises(ValidationError):
        ProductEvent(
            event_id=uuid4(),
            scope_type="WORKSPACE",
            scope_id="a" * 64,
            cursor=1,
            occurred_at=datetime(2026, 8, 10, tzinfo=UTC),
            payload=RunStatePayload(
                lifecycle_status=LifecycleStatus.RUNNING, outcome_status=None
            ),
        )


def test_pending_approval_view_contains_no_arguments_or_notes() -> None:
    approval = ApprovalRequest(
        run_id=uuid4(),
        checkpoint_id=uuid4(),
        tool_name="write_file",
        sanitized_arguments={"path": "C:\\private\\file.py"},
        request_digest="a" * 64,
        status=ApprovalStatus.PENDING,
        decision_note="private",
    )
    view = ProductProjector().pending_approvals((approval,))
    encoded = view.model_dump_json()
    PublicArtifactScanner().validate(encoded)
    assert "sanitized_arguments" not in encoded
    assert "decision_note" not in encoded


def test_scanner_detects_accidental_unsafe_payload_regression() -> None:
    with pytest.raises(ForbiddenPublicArtifactError):
        PublicArtifactScanner().validate('{"raw_output":"secret"}')


@pytest.mark.parametrize(
    "artifact",
    (
        '{"safe": 1, "safe": 2}',
        '{"\\u0072aw_output": "secret"}',
        '{"nested": {"\\u0068idden": "secret"}}',
        '{"path": "\\u002fhome\\u002falice"}',
    ),
)
def test_public_artifact_scanner_rejects_duplicate_and_unicode_escaped_unsafe_json(
    artifact: str,
) -> None:
    with pytest.raises(ForbiddenPublicArtifactError):
        PublicArtifactScanner().validate(artifact)


@pytest.mark.parametrize("artifact", ('"\\u002fhome\\u002falice"', '"raw_output"'))
def test_public_artifact_scanner_rejects_unsafe_json_scalar_roots(artifact: str) -> None:
    with pytest.raises(ForbiddenPublicArtifactError):
        PublicArtifactScanner().validate(artifact)


@pytest.mark.parametrize(
    ("event_type", "payload", "outcome"),
    [
        (
            EventType.RUN_COMPLETED,
            {"repair_status": RepairCompletionStatus.VERIFIED_SUCCESS.value},
            OutcomeStatus.VERIFIED,
        ),
        (EventType.RUN_COMPLETED, {"outcome": "COMPLETED"}, OutcomeStatus.UNVERIFIED),
        (
            EventType.RUN_COMPLETED,
            {"final_output": "private answer"},
            OutcomeStatus.UNVERIFIED,
        ),
        (EventType.RUN_FAILED, {"code": "COMMAND_FAILED"}, OutcomeStatus.FAILED),
        (
            EventType.RUN_FAILED,
            {"code": "COMMAND_INDETERMINATE"},
            OutcomeStatus.UNKNOWN,
        ),
        (
            EventType.RUN_FAILED,
            {"code": "UNVERIFIED_FINAL"},
            OutcomeStatus.UNVERIFIED,
        ),
        (
            EventType.RUN_FAILED,
            {"code": "TEST_EXECUTION_INDETERMINATE"},
            OutcomeStatus.UNKNOWN,
        ),
        (
            EventType.RUN_FAILED,
            {"code": "TEST_PROFILE_MISMATCH"},
            OutcomeStatus.FAILED,
        ),
    ],
)
def test_terminal_events_use_explicit_persisted_payload_schemas(
    event_type: EventType,
    payload: dict[str, object],
    outcome: OutcomeStatus,
) -> None:
    projected = ProductProjector().event(_event(event_type, payload))
    assert isinstance(projected.payload, (RunStatePayload, RunFinishedPayload))
    assert projected.event is ProductEventName.RUN_FINISHED
    assert projected.payload.outcome_status is outcome
    assert "private answer" not in projected.model_dump_json()


def test_run_failed_uses_a_closed_safe_code_without_persisting_diagnostics() -> None:
    secret = "sk-private-diagnostic"
    projected = ProductProjector().event(
        _event(EventType.RUN_FAILED, {"code": "RUNTIME_FAILURE"})
    )

    assert isinstance(projected.payload, RunFinishedPayload)
    assert projected.payload.outcome_status is OutcomeStatus.FAILED
    assert secret not in projected.model_dump_json()
    with pytest.raises(ProductProjectionError):
        ProductProjector().event(
            _event(EventType.RUN_FAILED, {"code": secret})
        )


@pytest.mark.parametrize("schema_version", [2, True, "1"])
def test_product_projection_rejects_model_copy_bypasses_of_event_schema_version(
    schema_version: object,
) -> None:
    forged = _event(EventType.RUN_STARTED, {}).model_copy(
        update={"schema_version": schema_version}
    )

    with pytest.raises(ProductProjectionError):
        ProductProjector().event(forged)


@pytest.mark.parametrize(
    ("event_type", "payload"),
    [
        (EventType.RUN_COMPLETED, {}),
        (EventType.RUN_COMPLETED, {"repair_status": "TESTS_FAILED"}),
        (EventType.RUN_COMPLETED, {"repair_status": "VERIFIED_SUCESS"}),
        (EventType.RUN_COMPLETED, {"outcome": "COMPLETE"}),
        (EventType.RUN_COMPLETED, {"outcome": "COMPLETED", "extra": True}),
        (EventType.RUN_FAILED, {}),
        (EventType.RUN_FAILED, {"reason": "arbitrary internal exception"}),
        (EventType.RUN_FAILED, {"code": "TEST_EXECUTION_INDETERMINAT"}),
        (EventType.RUN_FAILED, {"outcome": "UNKNOWN"}),
        (EventType.RUN_FAILED, {"code": "COMMAND_FAILED", "reason": "diagnostic"}),
    ],
)
def test_terminal_events_reject_missing_unknown_or_ambiguous_payloads(
    event_type: EventType, payload: dict[str, object]
) -> None:
    with pytest.raises(ProductProjectionError):
        ProductProjector().event(_event(event_type, payload))


def test_every_domain_event_is_explicitly_reviewed_for_product_projection() -> None:
    assert REVIEWED_PRODUCT_EVENT_TYPES.isdisjoint(REVIEWED_EXCLUDED_EVENT_TYPES)
    assert REVIEWED_PRODUCT_EVENT_TYPES | REVIEWED_EXCLUDED_EVENT_TYPES == frozenset(
        EventType
    )
    assert EventType.EVALUATION_RUN_STARTED in REVIEWED_EXCLUDED_EVENT_TYPES
    assert EventType.MODEL_REQUESTED in REVIEWED_PRODUCT_EVENT_TYPES


@pytest.mark.parametrize(
    "event_type",
    sorted(REVIEWED_EXCLUDED_EVENT_TYPES, key=lambda value: value.value),
)
def test_evaluator_only_events_fail_closed_in_product_projector(
    event_type: EventType,
) -> None:
    with pytest.raises(ProductProjectionError):
        ProductProjector().event(_event(event_type, {}))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("current_step", True),
        ("current_step", -1),
        ("current_step", 11),
        ("max_steps", 0),
        ("tool_call_count", 1.5),
        ("tool_call_count", 11),
        ("max_tool_calls", -1),
        ("total_token_usage", True),
        ("total_token_usage", -1),
        ("estimated_cost", float("nan")),
        ("estimated_cost", float("inf")),
        ("estimated_cost", -0.1),
        ("status", "COMPLETED"),
        ("model_provider", ""),
    ],
)
def test_run_projection_revalidates_malformed_durable_run_facts(
    field: str, value: object
) -> None:
    run = Run(task="private", status=RunStatus.COMPLETED).model_copy(
        update={field: value}
    )
    facts = RunProjectionFacts(
        run=run,
        repair_status=RepairCompletionStatus.VERIFIED_SUCCESS,
        event_count=1,
        last_cursor=1,
    )
    with pytest.raises(ProductProjectionError):
        ProductProjector().export_run(facts)
