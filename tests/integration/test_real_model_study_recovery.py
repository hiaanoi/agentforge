from pathlib import Path

import pytest
from test_real_model_study_runner import (
    FakeCampaignRunner,
    make_study_build,
    register_and_authorize,
)

from agentforge.domain.enums import (
    EvaluationCampaignStatus,
    EvaluationStudyStatus,
)
from agentforge.evaluation.study_builder import EvaluationStudyBuild
from agentforge.evaluation.study_persistence import EvaluationStudyRepository
from agentforge.evaluation.study_runner import EvaluationStudyRunner
from agentforge.persistence.database import Database


@pytest.fixture
def database(tmp_path: Path) -> Database:
    value = Database.from_path(tmp_path / "study-recovery.sqlite3")
    value.create_schema()
    return value


@pytest.fixture
def study_build(database: Database) -> EvaluationStudyBuild:
    return make_study_build(database)


@pytest.mark.asyncio
async def test_recovery_adopts_only_unrecorded_campaigns(
    database: Database,
    study_build: EvaluationStudyBuild,
) -> None:
    study = register_and_authorize(database, study_build)
    interrupted = FakeCampaignRunner(
        database,
        [EvaluationCampaignStatus.COMPLETED],
        fail_after_terminal_call=1,
    )

    with pytest.raises(RuntimeError, match="interruption"):
        await EvaluationStudyRunner(database, interrupted).run(study.study_id)

    persisted = EvaluationStudyRepository(database).get_study(study.study_id)
    assert persisted.status is EvaluationStudyStatus.RUNNING
    replacement = FakeCampaignRunner(
        database,
        [EvaluationCampaignStatus.COMPLETED] * 3,
    )
    result = await EvaluationStudyRunner(database, replacement).recover(
        study.study_id
    )

    assert result.study.status is EvaluationStudyStatus.COMPLETED
    assert [operation for operation, _ in replacement.calls] == [
        "recover",
        "recover",
        "recover",
    ]
    assert all(binding.campaign_status is not None for binding in result.bindings)


@pytest.mark.asyncio
async def test_terminal_run_and_recover_are_side_effect_free(
    database: Database,
    study_build: EvaluationStudyBuild,
) -> None:
    study = register_and_authorize(database, study_build)
    first_driver = FakeCampaignRunner(
        database,
        [EvaluationCampaignStatus.COMPLETED] * 4,
    )
    first = await EvaluationStudyRunner(database, first_driver).run(
        study.study_id
    )
    replay_driver = FakeCampaignRunner(database, [])
    runner = EvaluationStudyRunner(database, replay_driver)

    replayed_run = await runner.run(study.study_id)
    replayed_recover = await runner.recover(study.study_id)

    assert replayed_run == first
    assert replayed_recover == first
    assert replay_driver.calls == []


@pytest.mark.asyncio
async def test_recovery_can_start_authorized_but_unstarted_study(
    database: Database,
    study_build: EvaluationStudyBuild,
) -> None:
    study = register_and_authorize(database, study_build)
    driver = FakeCampaignRunner(
        database,
        [EvaluationCampaignStatus.COMPLETED_WITH_INVALID] * 4,
    )

    result = await EvaluationStudyRunner(database, driver).recover(
        study.study_id
    )

    assert result.study.status is (
        EvaluationStudyStatus.COMPLETED_WITH_INFRASTRUCTURE_GAPS
    )
    assert [operation for operation, _ in driver.calls] == ["recover"] * 4
