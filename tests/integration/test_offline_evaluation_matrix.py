import hashlib
import os
import sys
from pathlib import Path

import pytest

from agentforge.domain.enums import EvaluationCampaignStatus
from agentforge.evaluation.costs import PricingSnapshot
from agentforge.evaluation.formal_fixtures import FormalFixtureLoader
from agentforge.evaluation.pilot_factory import PilotRuntimeFactory
from agentforge.evaluation.pilot_runner import PilotRunner
from agentforge.evaluation.pilot_workspace import PilotWorkspaceManager
from agentforge.evaluation.protocol import EvaluationExecutionMode
from agentforge.evaluation.provider_factory import ProtocolBoundModelProvider
from agentforge.evaluation.source_provenance import SourceProvenance
from agentforge.evaluation.study_builder import EvaluationStudyBuilder
from agentforge.evaluation.study_persistence import EvaluationStudyRepository
from agentforge.evaluation.study_reports import EvaluationStudyReportBuilder
from agentforge.evaluation.study_runner import EvaluationStudyRegistrar
from agentforge.models.mock import MockModelProvider
from agentforge.persistence.database import Database

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "evaluation" / "fixtures"
TASK_IDS = (
    "bugsinpy-black-21",
    "quixbugs-shortest-path-length",
    "self-durable-double-consumption",
    "swebench-pytest-10051",
)


def scripted_repair_responses(task_root: Path) -> list[object]:
    manifest = FormalFixtureLoader().load(task_root)
    source_relative = Path("workspace") / Path(
        manifest.reference_files[0]
    ).relative_to(
        "reference/fixed_files"
    )
    source = task_root / source_relative
    reference = task_root / manifest.reference_files[0]
    return [
        {
            "type": "tool_call",
            "tool": "read_file",
            "arguments": {"path": source_relative.as_posix()},
        },
        {
            "type": "tool_call",
            "tool": "edit_file",
            "arguments": {
                "path": source_relative.as_posix(),
                "old_text": source.read_text(encoding="utf-8"),
                "new_text": reference.read_text(encoding="utf-8"),
                "expected_sha256": hashlib.sha256(
                    source.read_bytes()
                ).hexdigest(),
            },
        },
        {
            "type": "tool_call",
            "tool": "run_tests",
            "arguments": {"profile_id": f"visible-{manifest.task_id}"},
        },
        {
            "type": "final",
            "answer": f"Repaired {manifest.task_id}.",
        },
    ]


class OfflineMatrixProviderFactory:
    def __init__(self, fixture_root: Path) -> None:
        self._fixture_root = fixture_root
        self.request_counts: dict[str, int] = {}

    def create(self, protocol):  # type: ignore[no-untyped-def]
        task_root = self._fixture_root / "tasks" / protocol.task_id
        provider = MockModelProvider(
            scripted_repair_responses(task_root),
            model_id=protocol.provider_binding.model_id,
        )
        self.request_counts.setdefault(protocol.task_id, 0)
        original_generate = provider.generate

        async def generate(request):  # type: ignore[no-untyped-def]
            self.request_counts[protocol.task_id] += 1
            return await original_generate(request)

        provider.generate = generate  # type: ignore[method-assign]
        return ProtocolBoundModelProvider(protocol, provider)


@pytest.mark.asyncio
async def test_offline_four_task_three_repetition_matrix_is_fully_scored(
    tmp_path: Path,
) -> None:
    database = Database.from_path(tmp_path / "offline-matrix.sqlite3")
    database.create_schema()
    factory = OfflineMatrixProviderFactory(FIXTURES)
    runtime_factory = PilotRuntimeFactory(
        database,
        factory,
        executable=Path(sys.executable),
        allowed_env={
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTEST_ADDOPTS": "-p no:cacheprovider",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "PYTHONHASHSEED": "0",
            "PYTHONIOENCODING": "utf-8",
            **{
                name: os.environ[name]
                for name in ("SYSTEMROOT", "WINDIR")
                if name in os.environ
            },
        },
    )
    build = EvaluationStudyBuilder(runtime_factory, FIXTURES).build(
        model_id="mock-evaluation",
        source_provenance=SourceProvenance(
            git_commit_sha="a" * 40,
            git_worktree_clean=True,
            runtime_source_digest="b" * 64,
            pyproject_sha256="c" * 64,
            uv_lock_sha256="d" * 64,
        ),
        pricing_snapshot=PricingSnapshot(
            model_id="mock-evaluation",
            effective_date="2026-07-29",
            source_url="https://example.test/pricing",
            input_per_million="1",
            cached_input_per_million="0.1",
            output_per_million="8",
        ),
        execution_mode=EvaluationExecutionMode.OFFLINE_TEST,
    )
    study = EvaluationStudyRegistrar(database).register(build)
    pilot = PilotRunner(
        database,
        runtime_factory,
        PilotWorkspaceManager(tmp_path / "pilot-workspaces"),
        {
            task_id: FIXTURES / "tasks" / task_id
            for task_id in TASK_IDS
        },
    )

    bindings = EvaluationStudyRepository(database).list_campaign_bindings(
        study.study_id
    )
    for binding in bindings:
        result = await pilot.run_campaign(binding.campaign_id)
        assert result.campaign.status is EvaluationCampaignStatus.COMPLETED

    report = EvaluationStudyReportBuilder(database).build(
        study.study_id,
        build.pricing_snapshot,
    )
    assert report.summary.planned_slots == 12
    assert report.summary.scored_slots == 12
    assert report.summary.successful_slots == 12
    assert report.report_schema_version == 2
    assert report.summary.macro_pass_at_1 == 1
    assert report.summary.pass_at_1_eligible_task_count == 4
    assert report.summary.macro_pass_at_3 == 1
    assert report.summary.pass_at_3_eligible_task_count == 4
    assert report.summary.first_attempt_success_count == 4
    assert all(task.pass_at_1 and task.pass_at_3 for task in report.tasks)
    assert all(
        factory.request_counts[task_id] == 12 for task_id in TASK_IDS
    )
    database.close()
