from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, JsonValue
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from agentforge.domain.models import UtcDatetime, utc_now
from agentforge.evaluation.protocol import EvaluationProtocol
from agentforge.persistence.database import Database
from agentforge.persistence.tables import (
    EvaluationProtocolEventRow,
    EvaluationProtocolRow,
)


class ProtocolConflictError(RuntimeError):
    pass


class EvaluationProtocolAuditEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: UUID
    protocol_digest: str
    event_type: str
    payload: dict[str, JsonValue]
    created_at: UtcDatetime


class EvaluationProtocolRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    def register(self, protocol: EvaluationProtocol) -> EvaluationProtocol:
        try:
            with self._database.session() as session:
                by_digest = session.get(
                    EvaluationProtocolRow,
                    protocol.protocol_digest,
                )
                if by_digest is not None:
                    persisted = self._to_domain(by_digest)
                    if persisted != protocol:
                        raise ProtocolConflictError(
                            "Protocol digest is bound to different facts"
                        )
                    return persisted
                by_name = session.scalar(
                    select(EvaluationProtocolRow).where(
                        EvaluationProtocolRow.protocol_name
                        == protocol.protocol_name
                    )
                )
                if by_name is not None:
                    raise ProtocolConflictError(
                        "Protocol name is already bound to a different digest"
                    )
                now = utc_now()
                session.add(
                    EvaluationProtocolRow(
                        protocol_digest=protocol.protocol_digest,
                        protocol_name=protocol.protocol_name,
                        task_id=protocol.task_id,
                        provider=protocol.provider_binding.provider,
                        model_id=protocol.provider_binding.model_id,
                        protocol_data=protocol.model_dump(mode="json"),
                        created_at=now,
                    )
                )
                session.flush()
                session.add(
                    EvaluationProtocolEventRow(
                        event_id=str(uuid4()),
                        protocol_digest=protocol.protocol_digest,
                        event_type="EVALUATION_PROTOCOL_REGISTERED",
                        payload=self._audit_payload(protocol),
                        created_at=now,
                    )
                )
        except IntegrityError as exc:
            raise ProtocolConflictError(
                "Evaluation protocol registration conflicted"
            ) from exc
        return protocol

    def get(self, protocol_digest: str) -> EvaluationProtocol:
        with self._database.session() as session:
            row = session.get(EvaluationProtocolRow, protocol_digest)
            if row is None:
                raise KeyError(f"Evaluation protocol {protocol_digest} is missing")
            return self._to_domain(row)

    def get_by_name(self, protocol_name: str) -> EvaluationProtocol:
        with self._database.session() as session:
            row = session.scalar(
                select(EvaluationProtocolRow).where(
                    EvaluationProtocolRow.protocol_name == protocol_name
                )
            )
            if row is None:
                raise KeyError(f"Evaluation protocol {protocol_name!r} is missing")
            return self._to_domain(row)

    def list_audit_events(
        self,
        protocol_digest: str,
    ) -> list[EvaluationProtocolAuditEvent]:
        with self._database.session() as session:
            rows = session.scalars(
                select(EvaluationProtocolEventRow)
                .where(
                    EvaluationProtocolEventRow.protocol_digest
                    == protocol_digest
                )
                .order_by(EvaluationProtocolEventRow.created_at)
            ).all()
            return [
                EvaluationProtocolAuditEvent(
                    event_id=UUID(row.event_id),
                    protocol_digest=row.protocol_digest,
                    event_type=row.event_type,
                    payload=row.payload,
                    created_at=row.created_at,
                )
                for row in rows
            ]

    @staticmethod
    def _to_domain(row: EvaluationProtocolRow) -> EvaluationProtocol:
        return EvaluationProtocol.model_validate(row.protocol_data)

    @staticmethod
    def _audit_payload(protocol: EvaluationProtocol) -> dict[str, JsonValue]:
        return {
            "protocol_digest": protocol.protocol_digest,
            "protocol_name": protocol.protocol_name,
            "schema_version": protocol.schema_version,
            "task_id": protocol.task_id,
            "execution_mode": protocol.execution_mode.value,
            "provider": protocol.provider_binding.provider,
            "model_id": protocol.provider_binding.model_id,
            "fixture_registry_digest": protocol.fixture_registry_digest,
            "fixture_asset_digest": protocol.fixture_asset_digest,
        }
