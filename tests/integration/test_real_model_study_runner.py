import sys
from datetime import date
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest

from agentforge.domain.enums import (
    EvaluationCampaignStatus,
    EvaluationStudyStatus,
)
from agentforge.domain.models import utc_now
from agentforge.evaluation.campaign_persistence import (
    EvaluationCampaignRepository,
)
from agentforge.evaluation.costs import PricingSnapshot
from agentforge.evaluation.pilot_factory import PilotRuntimeFactory
from agentforge.evaluation.pilot_runner import PilotCampaignResult
from agentforge.evaluation.provider_factory import MockEvaluationProviderFactory
from agentforge.evaluation.source_provenance import SourceProvenance
from agentforge.evaluation.study_builder import (
    EvaluationStudyBuild,
    EvaluationStudyBuilder,
)
from agentforge.evaluation.study_models import RealModelAuthorization
from agentforge.evaluation.study_persistence import EvaluationStudyRepository
from agentforge.evaluation.study_reports import EvaluationStudyReportBuilder
from agentforge.evaluation.study_runner import (
    EvaluationStudyRegistrar,
    EvaluationStudyRunner,
    StudyExecutionConflictError,
)
from agentforge.persistence.database import Database
from agentforge.persistence.tables import (
    EvaluationCampaignRow,
    EvaluationSlotRow,
)

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "evaluation" / "fixtures"


@pytest.fixture
def database(tmp_path: Path) -> Database:
    value = Database.from_path(tmp_path / "study.sqlite3")
    value.create_schema()
    return value


@pytest.fixture
def study_build(database: Database) -> EvaluationStudyBuild:
    return make_study_build(database)


def make_study_build(database: Database) -> EvaluationStudyBuild:
    factory = PilotRuntimeFactory(
        database,
        MockEvaluationProviderFactory([]),
        executable=Path(sys.executable),
        allowed_env={
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "PYTHONHASHSEED": "0",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
        },
    )
    return EvaluationStudyBuilder(factory, FIXTURES).build(
        model_id="gpt-test-exact",
        source_provenance=SourceProvenance(
            git_commit_sha="a" * 40,
            git_worktree_clean=True,
            runtime_source_digest="b" * 64,
            pyproject_sha256="c" * 64,
            uv_lock_sha256="d" * 64,
        ),
        pricing_snapshot=PricingSnapshot(
            model_id="gpt-test-exact",
            effective_date=date(2026, 7, 29),
            source_url="https://example.test/pricing",
            input_per_million=Decimal("1"),
            cached_input_per_million=Decimal("0.1"),
            output_per_million=Decimal("8"),
        ),
    )


class FakeCampaignRunner:
    def __init__(
        self,
        database: Database,
        statuses: list[EvaluationCampaignStatus],
        *,
        fail_after_terminal_call: int | None = None,
    ) -> None:
        self._database = database
        self._statuses = list(statuses)
        self._fail_after_terminal_call = fail_after_terminal_call
        self.calls: list[tuple[str, UUID]] = []

    async def run_campaign(self, campaign_id: UUID) -> PilotCampaignResult:
        return self._execute("run", campaign_id)

    async def recover_campaign(
        self,
        campaign_id: UUID,
    ) -> PilotCampaignResult:
        return self._execute("recover", campaign_id)

    def _execute(
        self,
        operation: str,
        campaign_id: UUID,
    ) -> PilotCampaignResult:
        with self._database.session() as session:
            assert session.query(EvaluationCampaignRow).count() == 4
            assert session.query(EvaluationSlotRow).count() == 12
        self.calls.append((operation, campaign_id))
        status = self._statuses[len(self.calls) - 1]
        campaign = self._terminalize(campaign_id, status)
        if self._fail_after_terminal_call == len(self.calls):
            raise RuntimeError("simulated process interruption")
        return PilotCampaignResult(campaign=campaign)

    def _terminalize(
        self,
        campaign_id: UUID,
        status: EvaluationCampaignStatus,
    ):
        with self._database.session() as session:
            row = session.get(EvaluationCampaignRow, str(campaign_id))
            assert row is not None
            now = utc_now()
            row.status = status.value
            row.record_version += 1
            row.updated_at = now
            row.completed_at = now
        from agentforge.evaluation.campaign_persistence import (
            EvaluationCampaignRepository,
        )

        return EvaluationCampaignRepository(self._database).get_campaign(
            campaign_id
        )


def register_and_authorize(
    database: Database,
    build: EvaluationStudyBuild,
):
    study = EvaluationStudyRegistrar(database).register(build)
    authorization = RealModelAuthorization(
        study_definition_digest=build.definition.definition_digest,
        protocol_digests=build.definition.protocol_digests,
        model_id=build.definition.model_id,
        network_access_acknowledged=True,
    )
    repository = EvaluationStudyRepository(database)
    return repository.authorize(
        study.study_id,
        authorization,
        expected_version=study.record_version,
    )


@pytest.mark.asyncio
async def test_registers_all_campaigns_and_runs_in_fixed_order(
    database: Database,
    study_build: EvaluationStudyBuild,
) -> None:
    first = EvaluationStudyRegistrar(database).register(study_build)
    repeated = EvaluationStudyRegistrar(database).register(study_build)
    assert repeated == first
    study = register_and_authorize(database, study_build)
    driver = FakeCampaignRunner(
        database,
        [EvaluationCampaignStatus.COMPLETED] * 4,
    )

    result = await EvaluationStudyRunner(database, driver).run(study.study_id)

    assert result.study.status is EvaluationStudyStatus.COMPLETED
    assert [item.task_id for item in result.bindings] == list(
        study_build.definition.task_ids
    )
    assert [operation for operation, _ in driver.calls] == ["run"] * 4
    assert all(
        binding.campaign_status is EvaluationCampaignStatus.COMPLETED
        for binding in result.bindings
    )


@pytest.mark.asyncio
async def test_infrastructure_gap_continues_and_is_reported(
    database: Database,
    study_build: EvaluationStudyBuild,
) -> None:
    study = register_and_authorize(database, study_build)
    driver = FakeCampaignRunner(
        database,
        [
            EvaluationCampaignStatus.COMPLETED,
            EvaluationCampaignStatus.COMPLETED_WITH_INVALID,
            EvaluationCampaignStatus.COMPLETED,
            EvaluationCampaignStatus.COMPLETED,
        ],
    )

    result = await EvaluationStudyRunner(database, driver).run(study.study_id)

    assert result.study.status is (
        EvaluationStudyStatus.COMPLETED_WITH_INFRASTRUCTURE_GAPS
    )
    assert len(driver.calls) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("campaign_status", "study_status"),
    [
        (
            EvaluationCampaignStatus.BLOCKED,
            EvaluationStudyStatus.ABORTED_CONFIGURATION,
        ),
        (
            EvaluationCampaignStatus.INDETERMINATE,
            EvaluationStudyStatus.INDETERMINATE,
        ),
    ],
)
async def test_stops_immediately_on_abort_or_indeterminate(
    database: Database,
    study_build: EvaluationStudyBuild,
    campaign_status: EvaluationCampaignStatus,
    study_status: EvaluationStudyStatus,
) -> None:
    study = register_and_authorize(database, study_build)
    driver = FakeCampaignRunner(
        database,
        [
            EvaluationCampaignStatus.COMPLETED,
            campaign_status,
        ],
    )

    result = await EvaluationStudyRunner(database, driver).run(study.study_id)

    assert result.study.status is study_status
    assert len(driver.calls) == 2
    assert result.bindings[2].campaign_status is None
    assert result.campaigns[2].status is EvaluationCampaignStatus.CREATED


@pytest.mark.asyncio
async def test_normal_run_refuses_to_adopt_running_study(
    database: Database,
    study_build: EvaluationStudyBuild,
) -> None:
    study = register_and_authorize(database, study_build)
    repository = EvaluationStudyRepository(database)
    repository.start(study.study_id, expected_version=study.record_version)
    driver = FakeCampaignRunner(
        database,
        [EvaluationCampaignStatus.COMPLETED] * 4,
    )
    runner = EvaluationStudyRunner(database, driver)

    with pytest.raises(StudyExecutionConflictError, match="recovery"):
        await runner.run(study.study_id)

    result = await runner.recover(study.study_id)
    assert result.study.status is EvaluationStudyStatus.COMPLETED
    assert [operation for operation, _ in driver.calls] == ["recover"] * 4


def test_report_builder_reads_registered_study_without_mutating_it(
    database: Database,
    study_build: EvaluationStudyBuild,
) -> None:
    study = EvaluationStudyRegistrar(database).register(study_build)

    report = EvaluationStudyReportBuilder(database).build(
        study.study_id,
        study_build.pricing_snapshot,
    )

    assert report.study_status is EvaluationStudyStatus.DRAFT
    assert report.summary.planned_slots == 12
    assert report.summary.scored_slots == 0
    assert report.summary.unexecuted_slots == 12
    assert all(
        campaign.status is EvaluationCampaignStatus.CREATED
        for campaign in (
            EvaluationCampaignRepository(database).get_campaign(
                binding.campaign_id
            )
            for binding in EvaluationStudyRepository(
                database
            ).list_campaign_bindings(study.study_id)
        )
    )
