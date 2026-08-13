from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select

from agentforge.application.contracts import ProfilePurpose, ReceiptStatus
from agentforge.application.kernel_errors import (
    InvalidReceiptTransitionError,
    ProfileTrustMismatchError,
)
from agentforge.domain.enums import ConfigSourceKind
from agentforge.domain.errors import TestProfileBindingMismatchError
from agentforge.domain.models import utc_now
from agentforge.domain.test_execution import redact_argv_for_review
from agentforge.persistence.application_uow import ApplicationUnitOfWork
from agentforge.persistence.database import Database
from agentforge.persistence.event_log import EventLog, WorkspaceCommandAuthority
from agentforge.persistence.product_tables import TrustedProfileRow
from agentforge.persistence.receipts import ReceiptStore
from agentforge.tools.testing.profiles import TestProfileRegistry

_DIGEST_PATTERN = r"^[0-9a-f]{64}$"


class TrustedProfileIdentity(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, revalidate_instances="always"
    )

    profile_id: str = Field(pattern=r"^[a-z_][a-z0-9_-]*$", max_length=100)
    profile_version: int = Field(gt=0)
    profile_digest: str = Field(pattern=_DIGEST_PATTERN)
    executable_digest: str = Field(pattern=_DIGEST_PATTERN)
    argv_digest: str = Field(pattern=_DIGEST_PATTERN)
    cwd_identity: str = Field(min_length=1, max_length=4096)
    config_source_digest: str = Field(pattern=_DIGEST_PATTERN)

    @classmethod
    def model_construct(
        cls, _fields_set: set[str] | None = None, **values: object
    ) -> TrustedProfileIdentity:
        raise TypeError("trusted profile identities must be validated")

    def model_copy(
        self, *, update: Mapping[str, object] | None = None, deep: bool = False
    ) -> TrustedProfileIdentity:
        if update is not None:
            raise TypeError("trusted profile identities do not permit update copies")
        return type(self).model_validate(BaseModel.model_dump(self, mode="python"))

    @model_validator(mode="before")
    @classmethod
    def strict_existing_instance(cls, value: object) -> object:
        if isinstance(value, cls):
            return BaseModel.model_dump(value, mode="python")
        return value


class ProfileTrustChallenge(TrustedProfileIdentity):
    workspace_identity: str = Field(pattern=_DIGEST_PATTERN)
    purpose: ProfilePurpose = ProfilePurpose.DEVELOPMENT
    executable_path: str = Field(min_length=1, max_length=4096)
    argv_review: tuple[str, ...] = Field(min_length=1, max_length=100)
    config_source_kind: ConfigSourceKind

    @field_validator("argv_review", mode="before")
    @classmethod
    def redact_argv(cls, value: object) -> object:
        if not isinstance(value, tuple) or any(type(item) is not str for item in value):
            raise ValueError("profile review argv must be a tuple of strings")
        return redact_argv_for_review(value)


class TrustedProfile(TrustedProfileIdentity):
    workspace_identity: str = Field(pattern=_DIGEST_PATTERN)
    purpose: ProfilePurpose


class TrustProfileCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    command_id: UUID
    workspace_identity: str = Field(pattern=_DIGEST_PATTERN)
    purpose: ProfilePurpose
    identity: TrustedProfileIdentity

    @property
    def command_type(self) -> str:
        return "TRUST_PROFILE"


class ProfileKernel:
    """Durable exact-profile trust scoped to one non-secret workspace identity."""

    def __init__(self, database: Database, registry: TestProfileRegistry) -> None:
        self._database = database
        self._registry = registry

    def challenge(
        self,
        profile_id: str,
        *,
        purpose: ProfilePurpose | None = None,
    ) -> ProfileTrustChallenge:
        profile = self._registry.get(profile_id)
        if purpose is not None and purpose is not profile.purpose:
            raise ProfileTrustMismatchError()
        try:
            self._registry.require_executable_identity(profile)
        except TestProfileBindingMismatchError as exc:
            raise ProfileTrustMismatchError() from exc
        return ProfileTrustChallenge(
            workspace_identity=self._registry.workspace_identity,
            purpose=profile.purpose,
            profile_id=profile.profile_id,
            profile_version=profile.profile_version,
            profile_digest=profile.profile_digest,
            executable_digest=profile.executable_digest,
            argv_digest=profile.argv_digest,
            cwd_identity=profile.cwd_digest,
            config_source_digest=profile.config_source_digest,
            executable_path=profile.executable_path,
            argv_review=self._registry.argv_review(profile),
            config_source_kind=profile.config_source_kind,
        )

    def trust(
        self, challenge: ProfileTrustChallenge, *, command_id: UUID
    ) -> TrustedProfile:
        identity = TrustedProfileIdentity.model_validate(
            challenge.model_dump(include=set(TrustedProfileIdentity.model_fields))
        )
        command = TrustProfileCommand(
            command_id=command_id,
            workspace_identity=challenge.workspace_identity,
            purpose=challenge.purpose,
            identity=identity,
        )
        receipts = ReceiptStore()
        with ApplicationUnitOfWork(self._database) as uow:
            receipt = receipts.accept_for_scope(
                uow.session,
                command,
                scope_type="WORKSPACE",
                scope_id=challenge.workspace_identity,
            )
            if receipt.status is ReceiptStatus.COMPLETED:
                trusted = self._resolve_in_session(uow.session, challenge.profile_id)
                uow.commit()
                return trusted
            if receipt.status is not ReceiptStatus.ACCEPTED:
                raise InvalidReceiptTransitionError()
            if challenge != self.challenge(
                challenge.profile_id, purpose=challenge.purpose
            ):
                raise ProfileTrustMismatchError()
            existing = uow.session.scalar(
                select(TrustedProfileRow).where(
                    TrustedProfileRow.workspace_identity
                    == challenge.workspace_identity,
                    TrustedProfileRow.profile_id == challenge.profile_id,
                )
            )
            if existing is None:
                uow.session.add(
                    TrustedProfileRow(
                        trust_id=str(uuid4()),
                        workspace_identity=challenge.workspace_identity,
                        profile_id=challenge.profile_id,
                        profile_version=challenge.profile_version,
                        profile_digest=challenge.profile_digest,
                        executable_digest=challenge.executable_digest,
                        argv_digest=challenge.argv_digest,
                        cwd_identity=challenge.cwd_identity,
                        config_source_digest=challenge.config_source_digest,
                        purpose=challenge.purpose.value,
                        enabled_at=utc_now(),
                        disabled_at=None,
                    )
                )
                uow.session.flush()
            elif self._to_domain(existing) != TrustedProfile(
                workspace_identity=challenge.workspace_identity,
                purpose=challenge.purpose,
                **identity.model_dump(),
            ):
                raise ProfileTrustMismatchError()
            EventLog().append(
                uow.session,
                WorkspaceCommandAuthority(
                    workspace_identity=challenge.workspace_identity,
                    command_receipt=command_id,
                ),
                "PROFILE_TRUSTED",
                {
                    "profile_id": challenge.profile_id,
                    "profile_version": challenge.profile_version,
                    "profile_digest": challenge.profile_digest,
                    "executable_digest": challenge.executable_digest,
                    "argv_digest": challenge.argv_digest,
                    "cwd_identity": challenge.cwd_identity,
                    "config_source_digest": challenge.config_source_digest,
                    "purpose": challenge.purpose.value,
                },
            )
            receipts.complete(uow.session, command_id, at=utc_now())
            trusted = self._resolve_in_session(uow.session, challenge.profile_id)
            uow.commit()
            return trusted

    def resolve_trusted(
        self,
        profile_id: str,
        *,
        purpose: ProfilePurpose | None = None,
        argv: tuple[str, ...] | None = None,
    ) -> TrustedProfile:
        with self._database.session() as session:
            trusted = self._resolve_in_session(session, profile_id)
        if purpose is not None and trusted.purpose is not purpose:
            raise ProfileTrustMismatchError()
        challenge = self.challenge(profile_id, purpose=trusted.purpose)
        if (
            challenge.profile_digest != trusted.profile_digest
            or challenge.executable_digest != trusted.executable_digest
            or challenge.argv_digest != trusted.argv_digest
            or challenge.cwd_identity != trusted.cwd_identity
            or challenge.config_source_digest != trusted.config_source_digest
        ):
            raise ProfileTrustMismatchError()
        if argv is not None and self._digest(list(argv)) != trusted.argv_digest:
            raise ProfileTrustMismatchError()
        return trusted

    def _resolve_in_session(self, session: object, profile_id: str) -> TrustedProfile:
        row = session.scalar(  # type: ignore[attr-defined]
            select(TrustedProfileRow).where(
                TrustedProfileRow.workspace_identity == self._registry.workspace_identity,
                TrustedProfileRow.profile_id == profile_id,
                TrustedProfileRow.disabled_at.is_(None),
            )
        )
        if row is None:
            raise ProfileTrustMismatchError()
        return self._to_domain(row)

    @staticmethod
    def _to_domain(row: TrustedProfileRow) -> TrustedProfile:
        try:
            return TrustedProfile(
                workspace_identity=row.workspace_identity,
                purpose=ProfilePurpose(row.purpose),
                profile_id=row.profile_id,
                profile_version=row.profile_version,
                profile_digest=row.profile_digest,
                executable_digest=row.executable_digest,
                argv_digest=row.argv_digest,
                cwd_identity=row.cwd_identity,
                config_source_digest=row.config_source_digest,
            )
        except (TypeError, ValueError):
            raise ProfileTrustMismatchError() from None

    @staticmethod
    def _digest(value: object) -> str:
        canonical = json.dumps(value, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
