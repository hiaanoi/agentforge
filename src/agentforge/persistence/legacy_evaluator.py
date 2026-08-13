from __future__ import annotations

from datetime import timedelta
from uuid import UUID, uuid4

from pydantic import JsonValue

from agentforge.domain.enums import EventType
from agentforge.domain.models import Event
from agentforge.persistence.database import Database
from agentforge.persistence.event_log import EventLog
from agentforge.persistence.repositories import EventRepository
from agentforge.persistence.run_leases import RunLeaseStore


class LegacyEvaluatorEventRepository(EventRepository):
    """Explicit compatibility adapter for evaluator-owned non-product events."""

    def __init__(self, database: Database) -> None:
        super().__init__(database)
        self._legacy_database = database

    def append(
        self,
        run_id: UUID,
        event_type: EventType,
        payload: dict[str, JsonValue] | None = None,
    ) -> Event:
        if event_type is EventType.RUN_CREATED:
            return self.append_created(run_id, payload)
        lease = RunLeaseStore(self._legacy_database).acquire(
            run_id,
            owner_id=f"legacy-evaluator:event:{uuid4()}",
            ttl=timedelta(seconds=30),
        )
        authority = lease.authority
        try:
            with self._legacy_database.session() as session:
                persisted = EventLog().append(
                    session, authority, event_type, payload or {}
                )
                return self._to_legacy_domain(persisted)
        finally:
            RunLeaseStore(self._legacy_database).release(authority)
