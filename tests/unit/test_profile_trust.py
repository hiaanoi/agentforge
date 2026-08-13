from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest
from pydantic import BaseModel
from sqlalchemy import select

from agentforge.application.contracts import ReceiptStatus
from agentforge.application.kernel_errors import (
    IdempotencyConflictError,
    ProfileTrustMismatchError,
)
from agentforge.domain.enums import ConfigSourceKind
from agentforge.persistence.database import Database
from agentforge.persistence.product_tables import (
    ApplicationCommandReceiptRow,
    TrustedProfileRow,
)
from agentforge.persistence.profile_trust import (
    ProfileKernel,
    ProfilePurpose,
    ProfileTrustChallenge,
)
from agentforge.persistence.tables import EventRow
from agentforge.tools.paths import WorkspacePathResolver
from agentforge.tools.testing.profiles import (
    TestProfileDefinition as ProfileDefinition,
)
from agentforge.tools.testing.profiles import (
    TestProfileRegistry as ProfileRegistry,
)


def _kernel(
    tmp_path: Path,
    *,
    argv: tuple[str, ...] = ("--safe",),
    config_source_identity: str = "unit-test:profiles.py",
) -> tuple[ProfileKernel, Database]:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    executable = workspace / "runner.exe"
    executable.write_bytes(b"safe runner")
    registry = ProfileRegistry(WorkspacePathResolver(workspace))
    registry.register(
        ProfileDefinition(
            profile_id="visible-tests",
            name="Visible tests",
            description="Run visible tests",
            executable=str(executable),
            argv=argv,
            cwd=".",
            allowed_env={"PYTHONUTF8": "1"},
            timeout_seconds=30,
            max_output_bytes=4096,
            profile_version=1,
            config_source_identity=config_source_identity,
        )
    )
    database = Database.from_path(tmp_path / "product.sqlite3")
    database.create_schema()
    return ProfileKernel(database, registry), database


def test_trust_challenge_redacts_secret_bearing_argv_and_binds_source_provenance(
    tmp_path: Path,
) -> None:
    secret = "sk-private-do-not-serialize"
    kernel, database = _kernel(
        tmp_path,
        argv=(
            "--api-key=" + secret,
            "--token",
            secret,
            "--password",
            secret,
            "--credential=" + secret,
        ),
        config_source_identity="unit-test:trusted-profile-config",
    )

    challenge = kernel.challenge("visible-tests")
    trusted = kernel.trust(challenge, command_id=uuid4())

    serialized = challenge.model_dump_json()
    assert secret not in serialized
    assert challenge.argv_review == (
        challenge.executable_path,
        "<redacted>",
        "<redacted>",
        "<redacted>",
        "<redacted>",
        "<redacted>",
        "<redacted>",
    )
    assert challenge.config_source_kind is ConfigSourceKind.BUILTIN
    assert "unit-test:trusted-profile-config" not in serialized
    assert challenge.config_source_digest != kernel._registry.get(
        "visible-tests"
    ).environment_digest
    assert trusted.config_source_digest == challenge.config_source_digest
    database.close()


def test_trust_review_only_exposes_allowlisted_argv_structure(tmp_path: Path) -> None:
    secret = "Bearer secret-value"
    kernel, database = _kernel(
        tmp_path,
        argv=(
            "-m",
            "pytest",
            "--bearer",
            secret,
            "--authorization",
            secret,
            "private-positional",
            "--unknown=private-inline",
            "--unknown",
            "private-paired",
            "-q",
            "{SOURCE}/package",
            "unicode-秘密",
            "control\nvalue",
            "C:\\private\\path",
        ),
    )

    challenge = kernel.challenge("visible-tests")

    assert challenge.argv_review == (
        challenge.executable_path,
        "-m",
        "pytest",
        "<redacted>",
        "<redacted>",
        "<redacted>",
        "<redacted>",
        "<redacted>",
        "<redacted>",
        "<redacted>",
        "<redacted>",
        "-q",
        "{SOURCE}/package",
        "<redacted>",
        "<redacted>",
        "<redacted>",
    )
    serialized = challenge.model_dump_json()
    for private_value in (
        secret,
        "private-positional",
        "private-inline",
        "private-paired",
        "unicode-秘密",
        "control\\nvalue",
        "C:\\private\\path",
    ):
        assert private_value not in serialized
    database.close()


def test_trust_profile_source_identity_changes_the_trust_identity(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    executable = workspace / "runner.exe"
    executable.write_bytes(b"safe runner")

    def register(
        source: str,
        kind: ConfigSourceKind = ConfigSourceKind.BUILTIN,
    ) -> object:
        return ProfileRegistry(WorkspacePathResolver(workspace)).register(
            ProfileDefinition(
                profile_id="visible-tests",
                name="Visible tests",
                description="Run visible tests",
                executable=str(executable),
                argv=("--safe",),
                cwd=".",
                allowed_env={"PYTHONUTF8": "1"},
                timeout_seconds=30,
                max_output_bytes=4096,
                profile_version=1,
                config_source_identity=source,
                config_source_kind=kind,
            )
        )

    first = register("fixture:one")
    second = register("fixture:two")
    third = register("fixture:one", ConfigSourceKind.PROJECT)

    assert first.profile_digest != second.profile_digest  # type: ignore[attr-defined]
    assert first.config_source_digest != second.config_source_digest  # type: ignore[attr-defined]
    assert first.profile_digest != third.profile_digest  # type: ignore[attr-defined]
    assert first.config_source_digest != third.config_source_digest  # type: ignore[attr-defined]


def test_project_profile_requires_exact_digest_trust(tmp_path: Path) -> None:
    kernel, database = _kernel(tmp_path)
    challenge = kernel.challenge(
        "visible-tests", purpose=ProfilePurpose.DEVELOPMENT
    )

    trusted = kernel.trust(challenge, command_id=uuid4())

    assert kernel.resolve_trusted("visible-tests") == trusted
    assert trusted.profile_digest == challenge.profile_digest
    with pytest.raises(ProfileTrustMismatchError):
        kernel.resolve_trusted(
            "visible-tests", argv=(challenge.executable_path, "different")
        )
    database.close()


def test_trust_profile_receipt_fact_and_workspace_event_are_atomic_and_replayable(
    tmp_path: Path,
) -> None:
    kernel, database = _kernel(tmp_path)
    challenge = kernel.challenge("visible-tests")
    command_id = uuid4()

    first = kernel.trust(challenge, command_id=command_id)
    replay = kernel.trust(challenge, command_id=command_id)

    assert replay == first
    with database.session() as session:
        receipt = session.get(ApplicationCommandReceiptRow, str(command_id))
        trusts = session.scalars(select(TrustedProfileRow)).all()
        events = session.scalars(select(EventRow)).all()
    assert receipt is not None
    assert receipt.status == ReceiptStatus.COMPLETED.value
    assert receipt.result_scope_type == "WORKSPACE"
    assert receipt.result_scope_id == challenge.workspace_identity
    assert len(trusts) == len(events) == 1
    assert events[0].scope_type == "WORKSPACE"
    assert events[0].event_type == "PROFILE_TRUSTED"
    serialized = " ".join(
        [str(trusts[0].__dict__), str(events[0].payload), str(receipt.__dict__)]
    )
    assert "PYTHONUTF8" not in serialized
    assert "1" not in events[0].payload.values()
    database.close()


def test_same_command_id_with_different_profile_digest_conflicts(tmp_path: Path) -> None:
    kernel, database = _kernel(tmp_path)
    challenge = kernel.challenge("visible-tests")
    command_id = uuid4()
    kernel.trust(challenge, command_id=command_id)

    changed = ProfileTrustChallenge(
        **{**BaseModel.model_dump(challenge), "profile_digest": "b" * 64}
    )
    with pytest.raises(IdempotencyConflictError):
        kernel.trust(changed, command_id=command_id)

    assert kernel.resolve_trusted("visible-tests").profile_digest == challenge.profile_digest
    database.close()


def test_trusted_profile_cannot_be_resolved_for_a_different_purpose(
    tmp_path: Path,
) -> None:
    kernel, database = _kernel(tmp_path)
    challenge = kernel.challenge(
        "visible-tests", purpose=ProfilePurpose.DEVELOPMENT
    )
    kernel.trust(challenge, command_id=uuid4())

    with pytest.raises(ProfileTrustMismatchError):
        kernel.resolve_trusted(
            "visible-tests", purpose=ProfilePurpose.VERIFICATION
        )

    database.close()


def test_challenge_purpose_must_match_registered_profile(tmp_path: Path) -> None:
    kernel, database = _kernel(tmp_path)

    with pytest.raises(ProfileTrustMismatchError):
        kernel.challenge("visible-tests", purpose=ProfilePurpose.VERIFICATION)

    database.close()


def test_reopened_trust_rejects_replaced_executable_bytes(tmp_path: Path) -> None:
    kernel, database = _kernel(tmp_path)
    challenge = kernel.challenge("visible-tests")
    kernel.trust(challenge, command_id=uuid4())
    database.close()

    (tmp_path / "workspace" / "runner.exe").write_bytes(b"malicious replacement")
    reopened = Database.from_path(tmp_path / "product.sqlite3")
    reopened.validate_product_schema()
    registry = ProfileRegistry(WorkspacePathResolver(tmp_path / "workspace"))
    registry.register(
        ProfileDefinition(
            profile_id="visible-tests",
            name="Visible tests",
            description="Run visible tests",
            executable=str(tmp_path / "workspace" / "runner.exe"),
            argv=("--safe",),
            cwd=".",
            allowed_env={"PYTHONUTF8": "1"},
            timeout_seconds=30,
            max_output_bytes=4096,
            profile_version=1,
        )
    )

    with pytest.raises(ProfileTrustMismatchError):
        ProfileKernel(reopened, registry).resolve_trusted("visible-tests")

    reopened.close()


def test_concurrent_same_trust_command_cannot_commit_different_digest_and_replays(
    tmp_path: Path,
) -> None:
    kernel, database = _kernel(tmp_path)
    challenge = kernel.challenge("visible-tests")
    changed = ProfileTrustChallenge(
        **{**BaseModel.model_dump(challenge), "profile_digest": "b" * 64}
    )
    command_id = uuid4()
    barrier = Barrier(2)

    def submit(candidate: ProfileTrustChallenge) -> object:
        barrier.wait()
        try:
            return kernel.trust(candidate, command_id=command_id)
        except (IdempotencyConflictError, ProfileTrustMismatchError) as exc:
            return type(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, (challenge, changed)))

    trusted = kernel.resolve_trusted("visible-tests")
    assert trusted.profile_digest == challenge.profile_digest
    assert sum(result == trusted for result in results) == 1
    assert any(
        result in {IdempotencyConflictError, ProfileTrustMismatchError}
        for result in results
    )
    database.close()

    reopened = Database.from_path(tmp_path / "product.sqlite3")
    reopened.validate_product_schema()
    registry = ProfileRegistry(WorkspacePathResolver(tmp_path / "workspace"))
    registry.register(
        ProfileDefinition(
            profile_id="visible-tests",
            name="Visible tests",
            description="Run visible tests",
            executable=str(tmp_path / "workspace" / "runner.exe"),
            argv=("--safe",),
            cwd=".",
            allowed_env={"PYTHONUTF8": "1"},
            timeout_seconds=30,
            max_output_bytes=4096,
            profile_version=1,
        )
    )
    replay = ProfileKernel(reopened, registry).trust(challenge, command_id=command_id)
    assert replay == trusted
    reopened.close()
