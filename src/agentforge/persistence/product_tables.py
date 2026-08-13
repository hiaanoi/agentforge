from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

from agentforge.application.contracts import (
    ReceiptStatus,
    RunControlRequestStatus,
    RunControlRequestType,
)
from agentforge.persistence.tables import Base

PRODUCT_SCHEMA_VERSION = 9


class UtcDateTime(TypeDecorator[datetime]):
    """Persist aware instants and restore them as UTC-aware datetimes."""

    impl = DateTime
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> Any:
        return dialect.type_descriptor(DateTime(timezone=True))

    def process_bind_param(
        self, value: datetime | None, dialect: Dialect
    ) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("durable datetime must be timezone-aware")
        utc_value = value.astimezone(UTC)
        if dialect.name == "sqlite":
            return utc_value.replace(tzinfo=None)
        return utc_value

    def process_result_value(
        self, value: datetime | None, dialect: Dialect
    ) -> datetime | None:
        del dialect
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


def _closed_values(
    values: (
        type[ReceiptStatus]
        | type[RunControlRequestStatus]
        | type[RunControlRequestType]
    ),
) -> str:
    return ", ".join(f"'{item.value}'" for item in values)


class ProductSchemaVersionRow(Base):
    __tablename__ = "product_schema_version"
    __table_args__ = (
        CheckConstraint("singleton_id = 1", name="ck_product_schema_singleton"),
        CheckConstraint("version >= 1", name="ck_product_schema_version_positive"),
    )

    singleton_id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    version: Mapped[int] = mapped_column(Integer, nullable=False)


class ApplicationCommandReceiptRow(Base):
    __tablename__ = "application_command_receipts"
    __table_args__ = (
        CheckConstraint("length(command_id) = 36", name="ck_receipt_command_uuid_length"),
        CheckConstraint(
            "length(request_digest) = 64", name="ck_receipt_request_digest_length"
        ),
        CheckConstraint(
            f"status IN ({_closed_values(ReceiptStatus)})",
            name="ck_receipt_status_closed",
        ),
        CheckConstraint(
            "(result_scope_type IS NULL AND result_scope_id IS NULL) OR "
            "(result_scope_type IS NOT NULL AND result_scope_id IS NOT NULL)",
            name="ck_receipt_result_scope_pair",
        ),
    )

    command_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    command_type: Mapped[str] = mapped_column(String(64), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    result_scope_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    result_scope_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)


class ConversationRow(Base):
    __tablename__ = "conversations"
    __table_args__ = (
        CheckConstraint(
            "next_ordinal >= 1",
            name="ck_conversation_next_ordinal_positive",
        ),
        CheckConstraint("version >= 1", name="ck_conversation_version_positive"),
    )

    conversation_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    next_ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)


class RunLeaseRow(Base):
    __tablename__ = "run_leases"
    __table_args__ = (
        CheckConstraint("length(run_id) = 36", name="ck_run_lease_run_uuid_length"),
        CheckConstraint("length(lease_token) = 36", name="ck_run_lease_uuid_length"),
        CheckConstraint("fencing_token >= 1", name="ck_run_lease_fence_positive"),
        CheckConstraint("version >= 1", name="ck_run_lease_version_positive"),
        CheckConstraint(
            "acquired_at <= heartbeat_at AND heartbeat_at < expires_at",
            name="ck_run_lease_time_topology",
        ),
        CheckConstraint(
            "released_at IS NULL OR released_at >= heartbeat_at",
            name="ck_run_lease_release_topology",
        ),
        UniqueConstraint("lease_token"),
    )

    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("runs.run_id", ondelete="CASCADE"), primary_key=True
    )
    owner_id: Mapped[str] = mapped_column(String(200), nullable=False)
    lease_token: Mapped[str] = mapped_column(String(36), nullable=False)
    fencing_token: Mapped[int] = mapped_column(Integer, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    acquired_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    heartbeat_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    released_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)


class WorkspaceSourceBindingRow(Base):
    __tablename__ = "workspace_source_bindings"
    __table_args__ = (
        CheckConstraint("length(run_id) = 36", name="ck_source_binding_run_uuid_length"),
        CheckConstraint(
            "length(initial_source_digest) = 64",
            name="ck_source_binding_initial_digest_length",
        ),
        CheckConstraint(
            "length(expected_source_digest) = 64",
            name="ck_source_binding_expected_digest_length",
        ),
        CheckConstraint(
            "source_revision_number >= 0", name="ck_source_binding_revision_nonnegative"
        ),
        CheckConstraint(
            "digest_algorithm_version >= 1",
            name="ck_source_binding_algorithm_version_positive",
        ),
        CheckConstraint(
            "length(config_digest) = 64", name="ck_source_binding_config_digest_length"
        ),
        CheckConstraint(
            "length(profile_digest) = 64", name="ck_source_binding_profile_digest_length"
        ),
    )

    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("runs.run_id", ondelete="CASCADE"), primary_key=True
    )
    workspace_root_identity: Mapped[str] = mapped_column(Text, nullable=False)
    git_head: Mapped[str | None] = mapped_column(String(64), nullable=True)
    initial_source_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    expected_source_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    source_revision_number: Mapped[int] = mapped_column(Integer, nullable=False)
    digest_algorithm_version: Mapped[int] = mapped_column(Integer, nullable=False)
    config_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    profile_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)


class TrustedProfileRow(Base):
    __tablename__ = "trusted_profiles"
    __table_args__ = (
        UniqueConstraint("workspace_identity", "profile_id"),
        CheckConstraint("length(trust_id) = 36", name="ck_trusted_profile_uuid_length"),
        CheckConstraint(
            "profile_version >= 1", name="ck_trusted_profile_version_positive"
        ),
        CheckConstraint(
            "length(profile_digest) = 64", name="ck_trusted_profile_digest_length"
        ),
        CheckConstraint(
            "length(executable_digest) = 64",
            name="ck_trusted_profile_executable_digest_length",
        ),
        CheckConstraint(
            "length(argv_digest) = 64", name="ck_trusted_profile_argv_digest_length"
        ),
        CheckConstraint(
            "length(config_source_digest) = 64",
            name="ck_trusted_profile_config_digest_length",
        ),
        CheckConstraint(
            "length(workspace_identity) = 64",
            name="ck_trusted_profile_workspace_digest_length",
        ),
        CheckConstraint(
            "purpose IN ('development', 'verification', 'utility')",
            name="ck_trusted_profile_purpose_closed",
        ),
        CheckConstraint(
            "disabled_at IS NULL OR disabled_at >= enabled_at",
            name="ck_trusted_profile_time_topology",
        ),
    )

    trust_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_identity: Mapped[str] = mapped_column(Text, nullable=False)
    profile_id: Mapped[str] = mapped_column(String(100), nullable=False)
    profile_version: Mapped[int] = mapped_column(Integer, nullable=False)
    profile_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    executable_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    argv_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    cwd_identity: Mapped[str] = mapped_column(Text, nullable=False)
    config_source_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    purpose: Mapped[str] = mapped_column(String(32), nullable=False)
    enabled_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    disabled_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)


class RunControlRequestRow(Base):
    __tablename__ = "run_control_requests"
    __table_args__ = (
        UniqueConstraint("run_id"),
        UniqueConstraint("command_id"),
        CheckConstraint(
            "length(control_request_id) = 36", name="ck_control_request_uuid_length"
        ),
        CheckConstraint("length(run_id) = 36", name="ck_control_request_run_uuid_length"),
        CheckConstraint(
            "length(command_id) = 36", name="ck_control_request_command_uuid_length"
        ),
        CheckConstraint(
            f"request_type IN ({_closed_values(RunControlRequestType)})",
            name="ck_control_request_type_closed",
        ),
        CheckConstraint(
            f"status IN ({_closed_values(RunControlRequestStatus)})",
            name="ck_control_request_status_closed",
        ),
    )

    control_request_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("runs.run_id", ondelete="CASCADE"), nullable=False
    )
    command_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("application_command_receipts.command_id", ondelete="CASCADE"),
        nullable=False,
    )
    request_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    requested_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
