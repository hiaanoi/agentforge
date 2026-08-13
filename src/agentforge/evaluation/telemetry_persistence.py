from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from agentforge.domain.models import utc_now
from agentforge.evaluation.telemetry_models import EvaluationRunTelemetry
from agentforge.persistence.database import Database
from agentforge.persistence.tables import (
    EvaluationRunTelemetryRow,
    RepairEvaluationRunRow,
)


class TelemetryConflictError(RuntimeError):
    pass


class EvaluationTelemetryRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    def save(
        self,
        telemetry: EvaluationRunTelemetry,
    ) -> EvaluationRunTelemetry:
        try:
            with self._database.session() as session:
                result = session.get(
                    RepairEvaluationRunRow,
                    str(telemetry.evaluation_run_id),
                )
                if result is None:
                    raise TelemetryConflictError(
                        "Evaluation result is missing for telemetry"
                    )
                if (
                    result.run_id != str(telemetry.run_id)
                    or result.campaign_id != str(telemetry.campaign_id)
                    or result.attempt_id != str(telemetry.attempt_id)
                    or result.protocol_digest != telemetry.protocol_digest
                    or result.model_id != telemetry.model_id
                ):
                    raise TelemetryConflictError(
                        "Evaluation telemetry binding does not match result"
                    )
                existing = session.get(
                    EvaluationRunTelemetryRow,
                    str(telemetry.evaluation_run_id),
                )
                if existing is not None:
                    persisted = self._to_domain(existing)
                    if persisted != telemetry:
                        raise TelemetryConflictError(
                            "Evaluation telemetry identity conflict"
                        )
                    return persisted
                session.add(
                    EvaluationRunTelemetryRow(
                        evaluation_run_id=str(telemetry.evaluation_run_id),
                        run_id=str(telemetry.run_id),
                        campaign_id=str(telemetry.campaign_id),
                        attempt_id=str(telemetry.attempt_id),
                        protocol_digest=telemetry.protocol_digest,
                        telemetry_data=telemetry.model_dump(mode="json"),
                        telemetry_digest=telemetry.telemetry_digest,
                        schema_version=telemetry.schema_version,
                        created_at=utc_now(),
                    )
                )
        except IntegrityError as exc:
            raise TelemetryConflictError(
                "Evaluation telemetry identity conflict"
            ) from exc
        return telemetry

    def get(self, evaluation_run_id: UUID) -> EvaluationRunTelemetry:
        found = self.find(evaluation_run_id)
        if found is None:
            raise RuntimeError(
                f"Evaluation telemetry {evaluation_run_id} is missing"
            )
        return found

    def find(self, evaluation_run_id: UUID) -> EvaluationRunTelemetry | None:
        with self._database.session() as session:
            row = session.get(
                EvaluationRunTelemetryRow,
                str(evaluation_run_id),
            )
            return self._to_domain(row) if row is not None else None

    def list_for_campaign(
        self,
        campaign_id: UUID,
    ) -> list[EvaluationRunTelemetry]:
        with self._database.session() as session:
            rows = session.scalars(
                select(EvaluationRunTelemetryRow)
                .where(
                    EvaluationRunTelemetryRow.campaign_id == str(campaign_id)
                )
                .order_by(
                    EvaluationRunTelemetryRow.created_at,
                    EvaluationRunTelemetryRow.evaluation_run_id,
                )
            ).all()
            return [self._to_domain(row) for row in rows]

    @staticmethod
    def _to_domain(
        row: EvaluationRunTelemetryRow,
    ) -> EvaluationRunTelemetry:
        if row.telemetry_data.get("telemetry_digest") != row.telemetry_digest:
            raise TelemetryConflictError("Persisted telemetry digest column drift")
        telemetry = EvaluationRunTelemetry.model_validate(row.telemetry_data)
        if (
            telemetry.schema_version != row.schema_version
            or telemetry.evaluation_run_id != UUID(row.evaluation_run_id)
            or telemetry.run_id != UUID(row.run_id)
            or telemetry.campaign_id != UUID(row.campaign_id)
            or telemetry.attempt_id != UUID(row.attempt_id)
            or telemetry.protocol_digest != row.protocol_digest
        ):
            raise TelemetryConflictError("Persisted telemetry binding drift")
        return telemetry
