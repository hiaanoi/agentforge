from __future__ import annotations

import sys
from typing import TextIO

from agentforge.application.errors import ApplicationError
from agentforge.application.events import (
    ApprovalRequestedPayload,
    ProductEvent,
    RunFinishedPayload,
    RunStatePayload,
)
from agentforge.application.views import (
    DoctorReportView,
    ExportRunDetailsView,
    LocalRunDetailsView,
    PendingApprovalsView,
    ProfileTrustDetailsView,
)


def render_event(event: ProductEvent, *, stream: TextIO | None = None) -> None:
    stream = sys.stdout if stream is None else stream
    assert event.event is not None
    fields = [f"cursor={event.cursor}", f"event={event.event.value}"]
    if event.run_id is not None:
        fields.append(f"run_id={event.run_id}")
    payload = event.payload
    if isinstance(payload, ApprovalRequestedPayload):
        fields.append(f"approval_id={payload.approval_id}")
        fields.append(f"tool={payload.tool_name}")
    elif isinstance(payload, (RunStatePayload, RunFinishedPayload)):
        fields.append(f"lifecycle={payload.lifecycle_status.value}")
        if payload.outcome_status is not None:
            fields.append(f"outcome={payload.outcome_status.value}")
    stream.write(" ".join(fields) + "\n")


def render_view(value: object, *, stream: TextIO | None = None) -> None:
    stream = sys.stdout if stream is None else stream
    if isinstance(value, (LocalRunDetailsView, ExportRunDetailsView)):
        fields = [
            f"run_id={value.run_id}",
            f"lifecycle={value.lifecycle_status.value}",
            f"current_step={value.current_step}",
            f"tool_calls={value.tool_call_count}",
        ]
        if value.outcome_status is not None:
            fields.append(f"outcome={value.outcome_status.value}")
        stream.write(" ".join(fields) + "\n")
        return
    if isinstance(value, PendingApprovalsView):
        for approval in value.approvals:
            stream.write(
                f"approval_id={approval.approval_id} run_id={approval.run_id} "
                f"tool={approval.tool_name}\n"
            )
        return
    if isinstance(value, DoctorReportView):
        stream.write(f"ready={str(value.ready).lower()}\n")
        for check in value.checks:
            stream.write(f"check={check.check} status={check.status.value}\n")
        return
    if isinstance(value, ProfileTrustDetailsView):
        stream.write(
            f"profile_id={value.profile_id} purpose={value.purpose.value} "
            f"trusted={str(value.trusted).lower()} profile_digest={value.profile_digest} "
            f"config_source_digest={value.config_source_digest}\n"
        )
        return
    raise TypeError("closed product view cannot be rendered")


def render_trust_review(
    value: ProfileTrustDetailsView, *, stream: TextIO | None = None
) -> None:
    """Render only the operator-reviewable portion of a trust identity."""

    stream = sys.stdout if stream is None else stream
    argv = ("<executable>", *value.argv_review[1:])
    fields = [
        f"profile_id={value.profile_id}",
        f"purpose={value.purpose.value}",
        f"trusted={str(value.trusted).lower()}",
        f"profile_digest={value.profile_digest}",
        f"executable_digest={value.executable_digest}",
        f"argv_digest={value.argv_digest}",
        f"cwd_identity={value.cwd_identity}",
        f"config_source_kind={value.config_source_kind.value}",
        f"config_source_digest={value.config_source_digest}",
        "argv=" + ",".join(argv),
    ]
    stream.write(" ".join(fields) + "\n")


def render_error(error: ApplicationError, *, stream: TextIO | None = None) -> None:
    stream = sys.stderr if stream is None else stream
    fields = [f"code={error.code.value}", f"error_id={error.error_id}"]
    if error.run_id is not None:
        fields.append(f"run_id={error.run_id}")
    fields.append(f"message={error.safe_message}")
    stream.write(" ".join(fields) + "\n")


def render_configuration_error(*, stream: TextIO | None = None) -> None:
    (sys.stderr if stream is None else stream).write("configuration_error\n")
