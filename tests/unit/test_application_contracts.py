from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel, ValidationError

from agentforge.application.commands import (
    APPLICATION_COMMAND_ADAPTER,
    DecideApproval,
    ResumeRun,
    StartRun,
    TrustProfile,
)
from agentforge.application.contracts import LifecycleStatus, ProfilePurpose
from agentforge.application.errors import (
    ApplicationError,
    ApplicationErrorCode,
    application_error_from_exception,
)
from agentforge.application.events import ProductEvent, RunStatePayload
from agentforge.application.kernel_errors import (
    IdempotencyConflictError,
    ProfileTrustMismatchError,
)
from agentforge.application.queries import (
    APPLICATION_QUERY_ADAPTER,
    DoctorReport,
    ExportRunDetails,
    PendingApprovals,
    ProfileTrustDetails,
    RunDetails,
)
from agentforge.application.run_commands import ResumeRecoveryChoice
from agentforge.domain.enums import ApprovalStatus, ConfigSourceKind, RejectionStrategy
from agentforge.persistence.profile_trust import TrustedProfileIdentity


def test_commands_are_closed_discriminated_union() -> None:
    command_id = uuid4()
    run_id = uuid4()
    commands = (
        (
            {
                "type": "start_run",
                "command_id": command_id,
                "task": "fix the failing test",
                "workspace": Path("."),
            },
            StartRun,
        ),
        (
            {
                "type": "resume_run",
                "command_id": command_id,
                "run_id": run_id,
                "recovery_choice": ResumeRecoveryChoice.AUTO,
            },
            ResumeRun,
        ),
        (
            {
                "type": "decide_approval",
                "command_id": command_id,
                "approval_id": uuid4(),
                "status": ApprovalStatus.APPROVED,
                "strategy": RejectionStrategy.CONTINUE,
            },
            DecideApproval,
        ),
        (
            {
                "type": "trust_profile",
                "command_id": command_id,
                "workspace_identity": "a" * 64,
                "purpose": ProfilePurpose.VERIFICATION,
                "identity": {
                    "profile_id": "verify",
                    "profile_version": 1,
                    "profile_digest": "b" * 64,
                    "executable_digest": "c" * 64,
                    "argv_digest": "d" * 64,
                    "cwd_identity": "e" * 64,
                    "config_source_digest": "f" * 64,
                },
            },
            TrustProfile,
        ),
    )

    for raw, expected in commands:
        parsed = APPLICATION_COMMAND_ADAPTER.validate_python(raw)
        assert isinstance(parsed, expected)
        with pytest.raises(ValidationError):
            APPLICATION_COMMAND_ADAPTER.validate_python({**raw, "unexpected": True})


@pytest.mark.parametrize("type_", ["cancel_run", "start", "START_RUN", ""])
def test_commands_reject_unknown_discriminator(type_: str) -> None:
    with pytest.raises(ValidationError):
        APPLICATION_COMMAND_ADAPTER.validate_python({"type": type_})


def test_commands_reject_uuid_path_status_and_integer_type_drift() -> None:
    with pytest.raises(ValidationError):
        APPLICATION_COMMAND_ADAPTER.validate_python(
            {
                "type": "start_run",
                "command_id": str(uuid4()),
                "task": "fix",
                "workspace": ".",
            }
        )
    with pytest.raises(ValidationError):
        APPLICATION_COMMAND_ADAPTER.validate_python(
            {
                "type": "resume_run",
                "command_id": uuid4(),
                "run_id": uuid4(),
                "recovery_choice": "AUTO",
            }
        )
    with pytest.raises(ValidationError):
        APPLICATION_COMMAND_ADAPTER.validate_python(
            {
                "type": "decide_approval",
                "command_id": uuid4(),
                "approval_id": uuid4(),
                "status": "APPROVED",
            }
        )
    with pytest.raises(ValidationError):
        APPLICATION_COMMAND_ADAPTER.validate_python(
            {
                "type": "trust_profile",
                "command_id": uuid4(),
                "workspace_identity": "a" * 64,
                "purpose": ProfilePurpose.VERIFICATION,
                "identity": {
                    "profile_id": "verify",
                    "profile_version": True,
                    "profile_digest": "b" * 64,
                    "executable_digest": "c" * 64,
                    "argv_digest": "d" * 64,
                    "cwd_identity": "e" * 64,
                    "config_source_digest": "f" * 64,
                },
            }
        )


def test_queries_are_closed_and_exact() -> None:
    run_id = uuid4()
    cases = (
        ({"type": "run_details", "run_id": run_id}, RunDetails),
        ({"type": "pending_approvals", "run_id": run_id}, PendingApprovals),
        ({"type": "doctor_report", "workspace": Path(".")}, DoctorReport),
        (
            {
                "type": "profile_trust_details",
                "workspace": Path("."),
                "profile_id": "verify",
            },
            ProfileTrustDetails,
        ),
        ({"type": "export_run_details", "run_id": run_id}, ExportRunDetails),
    )
    for raw, expected in cases:
        assert isinstance(APPLICATION_QUERY_ADAPTER.validate_python(raw), expected)
        with pytest.raises(ValidationError):
            APPLICATION_QUERY_ADAPTER.validate_python({**raw, "extra": 1})
    with pytest.raises(ValidationError):
        APPLICATION_QUERY_ADAPTER.validate_python({"type": "conversation_history"})
    with pytest.raises(ValidationError):
        APPLICATION_QUERY_ADAPTER.validate_python(
            {"type": "run_details", "run_id": str(run_id)}
        )


def test_contracts_are_frozen() -> None:
    command = StartRun(command_id=uuid4(), task="fix", workspace=Path("."))
    query = RunDetails(run_id=uuid4())
    with pytest.raises(ValidationError):
        command.task = "replace"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        query.run_id = uuid4()  # type: ignore[misc]


def test_profile_trust_details_supports_review_to_trust_roundtrip() -> None:
    from agentforge.application.views import ProfileTrustDetailsView

    details = ProfileTrustDetailsView(
        workspace_identity="a" * 64,
        profile_id="verify",
        profile_version=2,
        purpose=ProfilePurpose.VERIFICATION,
        trusted=False,
        profile_digest="b" * 64,
        executable_path="C:\\Python\\python.exe",
        executable_digest="c" * 64,
        argv_review=("C:\\Python\\python.exe", "-m", "pytest"),
        argv_digest="d" * 64,
        cwd="C:\\workspace",
        cwd_identity="e" * 64,
        config_source_kind=ConfigSourceKind.FORMAL_MANIFEST,
        config_source_digest="f" * 64,
    )
    command = TrustProfile(
        command_id=uuid4(),
        workspace_identity=details.workspace_identity,
        purpose=details.purpose,
        identity=details.trusted_identity(),
    )
    assert command.identity == TrustedProfileIdentity(
        profile_id=details.profile_id,
        profile_version=details.profile_version,
        profile_digest=details.profile_digest,
        executable_digest=details.executable_digest,
        argv_digest=details.argv_digest,
        cwd_identity=details.cwd_identity,
        config_source_digest=details.config_source_digest,
    )
    dumped = details.model_dump()
    assert "allowed_env" not in dumped
    assert "environment" not in dumped
    assert "config_source_identity" not in dumped


def test_trust_profile_command_adapter_revalidates_a_forged_nested_identity() -> None:
    forged = BaseModel.model_construct.__func__(
        TrustedProfileIdentity,
        profile_id="verify",
        profile_version=True,
        profile_digest="b" * 64,
        executable_digest="c" * 64,
        argv_digest="d" * 64,
        cwd_identity="C:\\unsafe",
        config_source_digest="f" * 64,
    )
    with pytest.raises(ValidationError):
        APPLICATION_COMMAND_ADAPTER.validate_python(
            {
                "type": "trust_profile",
                "command_id": uuid4(),
                "workspace_identity": "a" * 64,
                "purpose": ProfilePurpose.VERIFICATION,
                "identity": forged,
            }
        )


def test_profile_trust_details_redacts_secret_bearing_argv_before_serialization() -> None:
    from agentforge.application.views import ProfileTrustDetailsView

    secret = "sk-private-do-not-serialize"
    details = ProfileTrustDetailsView(
        workspace_identity="a" * 64,
        profile_id="verify",
        profile_version=2,
        purpose=ProfilePurpose.VERIFICATION,
        trusted=False,
        profile_digest="b" * 64,
        executable_path="C:\\Python\\python.exe",
        executable_digest="c" * 64,
        argv_review=(
            "C:\\Python\\python.exe",
            "--api-key=" + secret,
            "--token",
            secret,
            "--secret",
            secret,
        ),
        argv_digest="d" * 64,
        cwd="C:\\workspace",
        cwd_identity="e" * 64,
        config_source_kind=ConfigSourceKind.FORMAL_MANIFEST,
        config_source_digest="f" * 64,
    )

    assert secret not in details.model_dump_json()
    assert details.argv_review == (
        "C:\\Python\\python.exe",
        "<redacted>",
        "<redacted>",
        "<redacted>",
        "<redacted>",
        "<redacted>",
    )


@pytest.mark.parametrize(
    ("executable_path", "argv_review", "cwd"),
    [
        ("python", ("python", "-m", "pytest"), "C:\\workspace"),
        (
            "C:\\Python\\python.exe",
            ("C:\\Other\\python.exe", "-m", "pytest"),
            "C:\\workspace",
        ),
        (
            "C:\\Python\\python.exe",
            ("C:\\Python\\python.exe", "-m", "pytest"),
            "relative/workspace",
        ),
        (
            "C:\\Python\\python.exe\u202e",
            ("C:\\Python\\python.exe\u202e", "-m", "pytest"),
            "C:\\workspace",
        ),
    ],
)
def test_profile_trust_details_rejects_unresolved_or_mismatched_launch_identity(
    executable_path: str, argv_review: tuple[str, ...], cwd: str
) -> None:
    from agentforge.application.views import ProfileTrustDetailsView

    with pytest.raises(ValidationError):
        ProfileTrustDetailsView(
            workspace_identity="a" * 64,
            profile_id="verify",
            profile_version=2,
            purpose=ProfilePurpose.VERIFICATION,
            trusted=False,
            profile_digest="b" * 64,
            executable_path=executable_path,
            executable_digest="c" * 64,
            argv_review=argv_review,
            argv_digest="d" * 64,
            cwd=cwd,
            cwd_identity="e" * 64,
            config_source_kind=ConfigSourceKind.FORMAL_MANIFEST,
            config_source_digest="f" * 64,
        )


def test_application_error_catalog_is_closed_and_does_not_leak_exception_text() -> None:
    secret = "sk-private-do-not-copy"
    unknown = application_error_from_exception(RuntimeError(secret), error_id=uuid4())
    conflict = application_error_from_exception(
        IdempotencyConflictError(), error_id=uuid4()
    )
    trust = application_error_from_exception(
        ProfileTrustMismatchError(), error_id=uuid4()
    )

    assert unknown.code is ApplicationErrorCode.INTERNAL_ERROR
    assert unknown.exit_code == 1
    assert unknown.safe_message == "AgentForge could not complete the request."
    assert secret not in unknown.model_dump_json()
    assert conflict.code is ApplicationErrorCode.COMMAND_CONFLICT
    assert conflict.exit_code == 5
    assert trust.code is ApplicationErrorCode.PROFILE_TRUST_MISMATCH
    assert trust.exit_code == 4
    assert UUID(str(unknown.error_id)) == unknown.error_id


@pytest.mark.parametrize(
    "dto",
    (
        StartRun(command_id=uuid4(), task="fix", workspace=Path(".")),
        RunDetails(run_id=uuid4()),
        ProductEvent(
            event_id=uuid4(),
            scope_type="RUN",
            scope_id="00000000-0000-0000-0000-000000000001",
            cursor=1,
            run_id=UUID("00000000-0000-0000-0000-000000000001"),
            sequence_number=1,
            occurred_at=datetime(2026, 8, 10, tzinfo=UTC),
            payload=RunStatePayload(
                lifecycle_status=LifecycleStatus.RUNNING, outcome_status=None
            ),
        ),
        ApplicationError(
            code=ApplicationErrorCode.INTERNAL_ERROR,
            safe_message="AgentForge could not complete the request.",
            retryable=False,
            exit_code=1,
            error_id=uuid4(),
        ),
    ),
)
def test_public_dtos_cannot_bypass_validation_through_pydantic_construction(
    dto: object,
) -> None:
    """Public boundaries must not retain forged instances for later serialization."""
    assert isinstance(dto, BaseModel)
    with pytest.raises((TypeError, ValidationError)):
        dto.model_copy(update={"unexpected": True})
    with pytest.raises((TypeError, ValidationError)):
        dto.copy(update={"unexpected": True})
    with pytest.raises((TypeError, ValidationError)):
        type(dto).model_construct(**dto.model_dump())
