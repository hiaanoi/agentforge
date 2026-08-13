from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Self
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import Select, func, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from agentforge.application.kernel_errors import KernelPersistenceError, StaleFenceError
from agentforge.persistence.application_uow import ApplicationUnitOfWork
from agentforge.persistence.database import Database
from agentforge.persistence.event_log import RunLeaseAuthority
from agentforge.persistence.product_tables import RunLeaseRow
from agentforge.persistence.tables import RunRow


def claim_bound_write(
    session: Session,
    run_id: UUID,
    authority: RunLeaseAuthority,
) -> RunLeaseAuthority:
    """Fence a transaction's first mutation with explicitly supplied authority."""
    if authority.run_id != run_id:
        raise StaleFenceError()
    RunLeaseStore(None).claim_write(session, authority)
    session.info["agentforge_run_authority"] = authority
    return authority


class RunLease(BaseModel):
    """A strict snapshot of one durable cross-process Run ownership claim."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    run_id: UUID
    owner_id: str = Field(min_length=1, max_length=200)
    lease_token: UUID
    fencing_token: int = Field(gt=0)
    version: int = Field(gt=0)
    acquired_at: datetime
    heartbeat_at: datetime
    expires_at: datetime

    @field_validator("owner_id")
    @classmethod
    def owner_is_canonical(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("owner_id must be canonical")
        return value

    @field_validator("acquired_at", "heartbeat_at", "expires_at")
    @classmethod
    def time_is_aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("lease time must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def time_topology_is_valid(self) -> Self:
        if not self.acquired_at <= self.heartbeat_at < self.expires_at:
            raise ValueError("invalid lease time topology")
        return self

    @property
    def authority(self) -> RunLeaseAuthority:
        return RunLeaseAuthority(
            self.run_id,
            self.owner_id,
            self.lease_token,
            self.fencing_token,
            self.version,
        )


class LeaseAcquireDisposition(StrEnum):
    """The caller either owns this epoch or is a read-only observer."""

    OWNER = "OWNER"
    OBSERVER = "OBSERVER"


@dataclass(frozen=True, slots=True)
class LeaseAcquisition:
    """Result of one atomic acquire-or-observe decision."""

    disposition: LeaseAcquireDisposition
    lease: RunLease

    @property
    def authority(self) -> RunLeaseAuthority | None:
        return self.lease.authority if self.disposition is LeaseAcquireDisposition.OWNER else None


class RunLeaseStore:
    """CAS lease operations with public UoW and session-bound variants."""

    def __init__(
        self,
        database: Database | None,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._database = database
        self._clock = clock

    @contextmanager
    def session(self) -> Iterator[Session]:
        """Read-only session used to validate an adopted authority."""
        if self._database is None:
            raise KernelPersistenceError()
        with self._database.session() as session:
            yield session

    def acquire(self, run_id: UUID, *, owner_id: str, ttl: timedelta) -> RunLease:
        if self._database is None:
            raise KernelPersistenceError()
        with ApplicationUnitOfWork(self._database) as uow:
            lease = self.acquire_in_session(
                uow.session, run_id, owner_id=owner_id, ttl=ttl
            )
            uow.commit()
            return lease

    def acquire_or_observe(
        self, run_id: UUID, *, owner_id: str, ttl: timedelta
    ) -> LeaseAcquisition:
        """Atomically acquire an inactive Run lease or observe its active epoch.

        Unlike :meth:`acquire`, this method never renews or adopts a live epoch,
        even if the textual owner id happens to match.  It is the product-facing
        primitive for competing application processes.
        """
        if self._database is None:
            raise KernelPersistenceError()
        with ApplicationUnitOfWork(self._database) as uow:
            result = self.acquire_or_observe_in_session(
                uow.session, run_id, owner_id=owner_id, ttl=ttl
            )
            uow.commit()
            return result

    def acquire_or_observe_in_session(
        self,
        session: Session,
        run_id: UUID,
        *,
        owner_id: str,
        ttl: timedelta,
    ) -> LeaseAcquisition:
        """Session variant used where the surrounding transaction is the mutex."""
        self._validate_identity(run_id, owner_id)
        ttl = self._validate_ttl(ttl)
        now = self._now(session)
        expires = now + ttl
        try:
            if session.get(RunRow, str(run_id)) is None:
                raise StaleFenceError()
            row = session.get(RunLeaseRow, str(run_id))
            if row is None:
                row = RunLeaseRow(
                    run_id=str(run_id),
                    owner_id=owner_id,
                    lease_token=str(uuid4()),
                    fencing_token=1,
                    version=1,
                    acquired_at=now,
                    heartbeat_at=now,
                    expires_at=expires,
                    released_at=None,
                )
                session.add(row)
                session.flush()
                return LeaseAcquisition(LeaseAcquireDisposition.OWNER, self._to_domain(row))

            if row.released_at is None and row.expires_at > now:
                return LeaseAcquisition(LeaseAcquireDisposition.OBSERVER, self._to_domain(row))

            expected_version = row.version
            changed = self._rowcount(
                session.execute(
                    update(RunLeaseRow)
                    .where(
                        RunLeaseRow.run_id == str(run_id),
                        RunLeaseRow.version == expected_version,
                    )
                    .values(
                        owner_id=owner_id,
                        lease_token=str(uuid4()),
                        fencing_token=row.fencing_token + 1,
                        version=expected_version + 1,
                        acquired_at=now,
                        heartbeat_at=now,
                        expires_at=expires,
                        released_at=None,
                    )
                    .execution_options(synchronize_session=False)
                )
            )
            if changed != 1:
                raise StaleFenceError()
            session.expire(row)
            return LeaseAcquisition(LeaseAcquireDisposition.OWNER, self._to_domain(row))
        except StaleFenceError:
            raise
        except SQLAlchemyError:
            raise KernelPersistenceError() from None

    def acquire_in_session(
        self,
        session: Session,
        run_id: UUID,
        *,
        owner_id: str,
        ttl: timedelta,
    ) -> RunLease:
        self._validate_identity(run_id, owner_id)
        ttl = self._validate_ttl(ttl)
        now = self._now(session)
        expires = now + ttl
        try:
            if session.get(RunRow, str(run_id)) is None:
                raise StaleFenceError()
            row = session.get(RunLeaseRow, str(run_id))
            if row is None:
                row = RunLeaseRow(
                    run_id=str(run_id),
                    owner_id=owner_id,
                    lease_token=str(uuid4()),
                    fencing_token=1,
                    version=1,
                    acquired_at=now,
                    heartbeat_at=now,
                    expires_at=expires,
                    released_at=None,
                )
                session.add(row)
                session.flush()
                return self._to_domain(row)

            active = row.released_at is None and row.expires_at > now
            if active and row.owner_id != owner_id:
                raise StaleFenceError()
            expected_version = row.version
            values: dict[str, object] = {
                "owner_id": owner_id,
                "heartbeat_at": now,
                "expires_at": expires,
                "released_at": None,
                "version": expected_version + 1,
            }
            if not active:
                values.update(
                    lease_token=str(uuid4()),
                    fencing_token=row.fencing_token + 1,
                    acquired_at=now,
                )
            changed = self._rowcount(
                session.execute(
                    update(RunLeaseRow)
                    .where(
                        RunLeaseRow.run_id == str(run_id),
                        RunLeaseRow.version == expected_version,
                    )
                    .values(**values)
                    .execution_options(synchronize_session=False)
                )
            )
            if changed != 1:
                raise StaleFenceError()
            session.expire(row)
            return self._to_domain(row)
        except StaleFenceError:
            raise
        except SQLAlchemyError:
            raise KernelPersistenceError() from None

    def renew(self, authority: RunLeaseAuthority, *, ttl: timedelta) -> RunLease:
        if self._database is None:
            raise KernelPersistenceError()
        with ApplicationUnitOfWork(self._database) as uow:
            lease = self.renew_in_session(uow.session, authority, ttl=ttl)
            uow.commit()
            return lease

    def renew_in_session(
        self,
        session: Session,
        authority: RunLeaseAuthority,
        *,
        ttl: timedelta,
    ) -> RunLease:
        self._require_authority(authority)
        ttl = self._validate_ttl(ttl)
        now = self._now(session)
        try:
            changed = self._rowcount(
                session.execute(
                    update(RunLeaseRow)
                    .where(*self.active_conditions(authority, now=now))
                    .values(
                        heartbeat_at=now,
                        expires_at=now + ttl,
                        version=RunLeaseRow.version + 1,
                    )
                    .execution_options(synchronize_session=False)
                )
            )
            if changed != 1:
                raise StaleFenceError()
            row = session.get(RunLeaseRow, str(authority.run_id))
            if row is None:
                raise StaleFenceError()
            session.refresh(row)
            return self._to_domain(row)
        except StaleFenceError:
            raise
        except SQLAlchemyError:
            raise KernelPersistenceError() from None

    def release(self, authority: RunLeaseAuthority) -> None:
        if self._database is None:
            raise KernelPersistenceError()
        with ApplicationUnitOfWork(self._database) as uow:
            self.release_in_session(uow.session, authority)
            uow.commit()

    def release_in_session(self, session: Session, authority: RunLeaseAuthority) -> None:
        self._require_authority(authority)
        now = self._now(session)
        try:
            changed = self._rowcount(
                session.execute(
                    update(RunLeaseRow)
                    .where(*self.active_conditions(authority, now=now))
                    .values(
                        released_at=now,
                        version=RunLeaseRow.version + 1,
                    )
                    .execution_options(synchronize_session=False)
                )
            )
            if changed != 1:
                raise StaleFenceError()
        except StaleFenceError:
            raise
        except SQLAlchemyError:
            raise KernelPersistenceError() from None

    def current(self, run_id: UUID) -> RunLease | None:
        if self._database is None:
            raise KernelPersistenceError()
        if type(run_id) is not UUID:
            raise ValueError("run_id must be an exact UUID")
        with self._database.session() as session:
            return self.current_in_session(session, run_id)

    def current_in_session(self, session: Session, run_id: UUID) -> RunLease | None:
        if type(run_id) is not UUID:
            raise ValueError("run_id must be an exact UUID")
        now = self._now(session)
        try:
            row = session.scalar(
                select(RunLeaseRow).where(
                    RunLeaseRow.run_id == str(run_id),
                    RunLeaseRow.released_at.is_(None),
                    RunLeaseRow.expires_at > now,
                )
            )
            return None if row is None else self._to_domain(row)
        except SQLAlchemyError:
            raise KernelPersistenceError() from None

    def require_active(self, session: Session, authority: RunLeaseAuthority) -> None:
        self._require_authority(authority)
        now = self._now(session)
        try:
            statement: Select[tuple[int]] = select(func.count()).select_from(
                RunLeaseRow
            ).where(*self.active_conditions(authority, now=now))
            if session.scalar(statement) != 1:
                raise StaleFenceError()
        except StaleFenceError:
            raise
        except SQLAlchemyError:
            raise KernelPersistenceError() from None

    def claim_write(self, session: Session, authority: RunLeaseAuthority) -> None:
        """Fence a write by ownership identity; lease version is renew-CAS metadata.

        A heartbeat may advance ``version`` between an async caller's snapshot and its
        synchronous database write.  Token + fence identify the ownership epoch while
        the database clock and expiry still prove that epoch is live.
        """
        self._require_authority(authority)
        now = self._now(session)
        try:
            changed = self._rowcount(
                session.execute(
                    update(RunLeaseRow)
                    .where(*self.write_conditions(authority, now=now))
                    .values(owner_id=RunLeaseRow.owner_id)
                    .execution_options(synchronize_session=False)
                )
            )
            if changed != 1:
                raise StaleFenceError()
        except StaleFenceError:
            raise
        except SQLAlchemyError:
            raise KernelPersistenceError() from None

    @staticmethod
    def active_conditions(
        authority: RunLeaseAuthority, *, now: datetime
    ) -> tuple[Any, ...]:
        return (
            RunLeaseRow.run_id == str(authority.run_id),
            RunLeaseRow.owner_id == authority.owner_id,
            RunLeaseRow.lease_token == str(authority.lease_token),
            RunLeaseRow.fencing_token == authority.fencing_token,
            RunLeaseRow.version == authority.version,
            RunLeaseRow.released_at.is_(None),
            RunLeaseRow.expires_at > now,
        )

    @staticmethod
    def write_conditions(
        authority: RunLeaseAuthority, *, now: datetime
    ) -> tuple[Any, ...]:
        return (
            RunLeaseRow.run_id == str(authority.run_id),
            RunLeaseRow.owner_id == authority.owner_id,
            RunLeaseRow.lease_token == str(authority.lease_token),
            RunLeaseRow.fencing_token == authority.fencing_token,
            RunLeaseRow.released_at.is_(None),
            RunLeaseRow.expires_at > now,
        )

    def _now(self, session: Session) -> datetime:
        value = (
            self._clock()
            if self._clock is not None
            else session.scalar(func.current_timestamp())
        )
        if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
            if self._clock is not None or type(value) is not datetime:
                raise ValueError("lease clock must return an aware datetime")
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def now(self, session: Session) -> datetime:
        """Return the store's authoritative aware UTC instant for guarded SQL."""
        return self._now(session)

    @staticmethod
    def _validate_identity(run_id: UUID, owner_id: str) -> None:
        if type(run_id) is not UUID:
            raise ValueError("run_id must be an exact UUID")
        if (
            type(owner_id) is not str
            or not owner_id
            or owner_id != owner_id.strip()
            or len(owner_id) > 200
        ):
            raise ValueError("owner_id is invalid")

    @staticmethod
    def _validate_ttl(ttl: timedelta) -> timedelta:
        if type(ttl) is not timedelta or ttl <= timedelta(0):
            raise ValueError("lease ttl must be a positive timedelta")
        return ttl

    @staticmethod
    def _require_authority(authority: RunLeaseAuthority) -> None:
        if type(authority) is not RunLeaseAuthority:
            raise StaleFenceError()

    @staticmethod
    def _rowcount(result: object) -> int:
        value = getattr(result, "rowcount", None)
        return value if type(value) is int else 0

    @staticmethod
    def _to_domain(row: RunLeaseRow) -> RunLease:
        try:
            return RunLease(
                run_id=UUID(row.run_id),
                owner_id=row.owner_id,
                lease_token=UUID(row.lease_token),
                fencing_token=row.fencing_token,
                version=row.version,
                acquired_at=row.acquired_at,
                heartbeat_at=row.heartbeat_at,
                expires_at=row.expires_at,
            )
        except (TypeError, ValueError):
            raise KernelPersistenceError() from None
