from __future__ import annotations

from types import TracebackType
from typing import Self

from sqlalchemy.orm import Session

from agentforge.application.kernel_errors import (
    PersistenceBoundaryError,
    UnitOfWorkStateError,
)
from agentforge.persistence.database import Database


class ApplicationUnitOfWork:
    """One explicit Session and transaction for an application command."""

    def __init__(self, database: Database) -> None:
        self._database = database
        self._session: Session | None = None
        self._state = "NEW"

    def __enter__(self) -> Self:
        if self._state != "NEW":
            raise UnitOfWorkStateError()
        try:
            session = self._database.new_session()
        except Exception:
            self._state = "CLOSED"
            raise PersistenceBoundaryError() from None
        self._session = session
        self._state = "ACTIVE"
        try:
            connection = session.connection()
            if connection.dialect.name == "sqlite":
                driver = getattr(connection.connection, "driver_connection", None)
                if not bool(getattr(driver, "in_transaction", False)):
                    connection.exec_driver_sql("BEGIN IMMEDIATE")
        except Exception:
            self._cleanup(session, rollback=True)
            self._session = None
            self._state = "CLOSED"
            raise PersistenceBoundaryError() from None
        return self

    @property
    def session(self) -> Session:
        if self._state != "ACTIVE" or self._session is None:
            raise UnitOfWorkStateError()
        return self._session

    def commit(self) -> None:
        session = self.session
        try:
            session.commit()
        except Exception:
            self._cleanup(session, rollback=True)
            self._session = None
            self._state = "CLOSED"
            raise PersistenceBoundaryError() from None
        close_error = self._cleanup(session, rollback=False)
        self._session = None
        self._state = "COMMITTED"
        if close_error is not None:
            raise PersistenceBoundaryError() from None

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc, traceback
        if self._state == "ACTIVE" and self._session is not None:
            cleanup_error = self._cleanup(self._session, rollback=True)
            self._session = None
            self._state = "CLOSED"
            if exc_type is None and cleanup_error is not None:
                raise PersistenceBoundaryError() from None
        if self._state in {"ACTIVE", "COMMITTED"}:
            self._state = "CLOSED"

    @staticmethod
    def _cleanup(session: Session, *, rollback: bool) -> Exception | None:
        first_error: Exception | None = None
        if rollback:
            try:
                session.rollback()
            except Exception as exc:
                first_error = exc
        try:
            session.close()
        except Exception as exc:
            if first_error is None:
                first_error = exc
        return first_error
