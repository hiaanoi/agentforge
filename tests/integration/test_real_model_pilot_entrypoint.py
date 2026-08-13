import json
from pathlib import Path

import pytest
from sqlalchemy import func, select
from test_real_model_study_runner import FakeCampaignRunner

from agentforge.domain.enums import (
    EvaluationCampaignStatus,
    EvaluationStudyStatus,
)
from agentforge.evaluation.campaign_persistence import (
    EvaluationCampaignRepository,
)
from agentforge.evaluation.pilot_runner import PilotCampaignResult
from agentforge.evaluation.real_model_gate import (
    RealModelAuthorizationError,
)
from agentforge.evaluation.real_model_pilot import RealModelPilotApplication
from agentforge.evaluation.source_provenance import SourceProvenance
from agentforge.evaluation.study_manifest import (
    load_private_study_manifest,
)
from agentforge.evaluation.study_persistence import EvaluationStudyRepository
from agentforge.persistence.database import Database
from agentforge.persistence.tables import (
    EvaluationCampaignRow,
    EvaluationSlotRow,
)

ROOT = Path(__file__).resolve().parents[2]


class FixedSourceCollector:
    def __init__(self) -> None:
        self.value = SourceProvenance(
            git_commit_sha="a" * 40,
            git_worktree_clean=True,
            runtime_source_digest="b" * 64,
            pyproject_sha256="c" * 64,
            uv_lock_sha256="d" * 64,
        )
        self.calls = 0

    def collect(self, repository_root: Path) -> SourceProvenance:
        assert repository_root == ROOT
        self.calls += 1
        return self.value


class CanaryFakeCampaignRunner(FakeCampaignRunner):
    async def run_canary_slot(self, campaign_id):  # type: ignore[no-untyped-def]
        self.calls.append(("canary", campaign_id))
        return PilotCampaignResult(
            campaign=EvaluationCampaignRepository(self._database).get_campaign(
                campaign_id
            )
        )


def prepare_arguments(state_dir: Path) -> list[str]:
    return [
        "prepare",
        "--state-dir",
        str(state_dir),
        "--model",
        "gpt-test-exact",
        "--response-model",
        "gpt-test-exact-2026-03-17",
        "--pricing-date",
        "2026-07-29",
        "--pricing-source",
        "https://example.test/pricing",
        "--input-per-million",
        "1.25",
        "--cached-input-per-million",
        "0.25",
        "--output-per-million",
        "10",
    ]


def test_prepare_is_offline_safe_and_registers_full_study(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "private-state"
    output: list[str] = []
    app = RealModelPilotApplication(
        ROOT,
        environment={},
        source_collector=FixedSourceCollector(),
        output=output.append,
    )

    assert app.run(prepare_arguments(state_dir)) == 0
    assert app.run(prepare_arguments(state_dir)) == 0

    manifest_path = state_dir / "study_manifest.json"
    manifest = load_private_study_manifest(manifest_path)
    run_manifest_path = state_dir / "run_manifest.json"
    assert run_manifest_path.is_file()
    serialized = manifest_path.read_text(encoding="utf-8").casefold()
    run_serialized = run_manifest_path.read_text(encoding="utf-8").casefold()
    assert manifest.model_id == "gpt-test-exact"
    assert manifest.response_model_id == "gpt-test-exact-2026-03-17"
    assert "api_key" not in serialized
    assert "system_prompt" not in serialized
    assert "task_prompt" not in serialized
    assert "tests/hidden" not in serialized
    assert "api_key" not in run_serialized
    database = Database.from_path(state_dir / "study.sqlite3")
    with database.session() as session:
        assert session.scalar(select(func.count(EvaluationCampaignRow.campaign_id))) == 4
        assert session.scalar(select(func.count(EvaluationSlotRow.slot_id))) == 12
    database.close()
    assert any("study_definition_digest=" in line for line in output)


def test_report_needs_no_api_key_and_makes_no_provider_call(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "private-state"
    report_dir = tmp_path / "public"
    provider_calls = 0

    def forbidden_provider(_):
        nonlocal provider_calls
        provider_calls += 1
        raise AssertionError("report must not construct a Provider")

    app = RealModelPilotApplication(
        ROOT,
        environment={},
        source_collector=FixedSourceCollector(),
        provider_builder=forbidden_provider,
        output=lambda _: None,
    )
    app.run(prepare_arguments(state_dir))
    manifest = load_private_study_manifest(
        state_dir / "study_manifest.json"
    )

    assert (
        app.run(
            [
                "report",
                "--state-dir",
                str(state_dir),
                "--output-dir",
                str(report_dir),
            ]
        )
        == 0
    )

    assert provider_calls == 0
    payload = json.loads(
        (report_dir / "study_report.json").read_text(encoding="utf-8")
    )
    assert payload["report_schema_version"] == 2
    assert payload["summary"]["planned_slots"] == 12
    assert payload["summary"]["unexecuted_slots"] == 12
    assert payload["summary"]["macro_pass_at_1"] is None
    assert payload["summary"]["pass_at_1_eligible_task_count"] == 0
    assert payload["summary"]["macro_pass_at_3"] is None
    assert payload["summary"]["pass_at_3_eligible_task_count"] == 0
    assert payload["summary"]["first_attempt_success_count"] == 0
    assert "task_pass_at_1_count" not in payload["summary"]
    assert [len(task["slots"]) for task in payload["tasks"]] == [3, 3, 3, 3]
    assert [
        slot["repetition_index"]
        for task in payload["tasks"]
        for slot in task["slots"]
    ] == [0, 1, 2] * 4
    markdown = (report_dir / "study_report.md").read_text(encoding="utf-8")
    assert "Provider configuration digest" in markdown
    assert "Pricing digest" in markdown
    assert "Runtime source digest" in markdown
    assert "Planned Slots" in markdown
    assert all(digest in markdown for digest in manifest.protocol_digests)


def test_run_fails_at_gate_before_campaign_executor(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "private-state"
    builder_calls = 0

    def forbidden_executor(*_):
        nonlocal builder_calls
        builder_calls += 1
        raise AssertionError("gate failure must precede executor construction")

    app = RealModelPilotApplication(
        ROOT,
        environment={},
        source_collector=FixedSourceCollector(),
        campaign_executor_builder=forbidden_executor,
        output=lambda _: None,
    )
    app.run(prepare_arguments(state_dir))
    manifest = load_private_study_manifest(
        state_dir / "study_manifest.json"
    )

    with pytest.raises(RealModelAuthorizationError, match="opt-in"):
        app.run(
            [
                "run",
                "--state-dir",
                str(state_dir),
                "--confirm",
                manifest.definition_digest,
            ]
        )

    assert builder_calls == 0
    database = Database.from_path(state_dir / "study.sqlite3")
    study = EvaluationStudyRepository(database).get_study(manifest.study_id)
    database.close()
    assert study.status is EvaluationStudyStatus.DRAFT
    assert study.authorization_digest is None


def test_run_uses_authorized_executor_and_terminal_replay_is_zero_call(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "private-state"
    source = FixedSourceCollector()
    executors: list[FakeCampaignRunner] = []

    def build_executor(database, gate, api_key, private_root):
        del gate, private_root
        assert api_key.get_secret_value() == "runtime-only-secret"
        executor = FakeCampaignRunner(
            database,
            [EvaluationCampaignStatus.COMPLETED] * 4,
        )
        executors.append(executor)
        return executor

    app = RealModelPilotApplication(
        ROOT,
        environment={
            "RUN_REAL_MODEL_PILOT": "1",
            "OPENAI_API_KEY": "runtime-only-secret",
        },
        source_collector=source,
        campaign_executor_builder=build_executor,
        output=lambda _: None,
    )
    app.run(prepare_arguments(state_dir))
    manifest = load_private_study_manifest(
        state_dir / "study_manifest.json"
    )
    command = [
        "run",
        "--state-dir",
        str(state_dir),
        "--confirm",
        manifest.definition_digest,
    ]

    assert app.run(command) == 0
    assert len(executors) == 1
    assert len(executors[0].calls) == 4
    assert app.run(command) == 0
    assert len(executors) == 1


def test_canary_executes_only_one_campaign_probe(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "private-state"
    executors: list[CanaryFakeCampaignRunner] = []

    def build_executor(database, gate, api_key, private_root):
        del gate, api_key, private_root
        executor = CanaryFakeCampaignRunner(
            database,
            [EvaluationCampaignStatus.COMPLETED] * 4,
        )
        executors.append(executor)
        return executor

    app = RealModelPilotApplication(
        ROOT,
        environment={
            "RUN_REAL_MODEL_PILOT": "1",
            "OPENAI_API_KEY": "runtime-only-secret",
        },
        source_collector=FixedSourceCollector(),
        campaign_executor_builder=build_executor,
        output=lambda _: None,
    )
    app.run(prepare_arguments(state_dir))
    manifest = load_private_study_manifest(
        state_dir / "study_manifest.json"
    )
    database = Database.from_path(state_dir / "study.sqlite3")
    target_binding = next(
        binding
        for binding in EvaluationStudyRepository(database).list_campaign_bindings(
            manifest.study_id
        )
        if binding.task_id == "self-durable-double-consumption"
    )
    database.close()

    assert (
        app.run(
            [
                "canary",
                "--state-dir",
                str(state_dir),
                "--confirm",
                manifest.definition_digest,
                "--task-id",
                "self-durable-double-consumption",
            ]
        )
        == 0
    )
    assert len(executors) == 1
    assert [operation for operation, _ in executors[0].calls] == ["canary"]
    assert executors[0].calls[0][1] == target_binding.campaign_id
    database = Database.from_path(state_dir / "study.sqlite3")
    study = EvaluationStudyRepository(database).get_study(manifest.study_id)
    database.close()
    assert study.status is EvaluationStudyStatus.RUNNING


def test_state_load_closes_database_when_durable_lookup_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir = tmp_path / "private-state"
    app = RealModelPilotApplication(
        ROOT,
        environment={},
        source_collector=FixedSourceCollector(),
        output=lambda _: None,
    )
    app.run(prepare_arguments(state_dir))
    close_calls = 0
    original_close = Database.close

    def tracked_close(database: Database) -> None:
        nonlocal close_calls
        close_calls += 1
        original_close(database)

    def fail_lookup(
        repository: EvaluationStudyRepository,
        study_id: object,
    ) -> None:
        del repository, study_id
        raise RuntimeError("durable lookup failed")

    monkeypatch.setattr(Database, "close", tracked_close)
    monkeypatch.setattr(
        EvaluationStudyRepository,
        "get_definition",
        fail_lookup,
    )

    with pytest.raises(RuntimeError, match="durable lookup failed"):
        app.run(
            [
                "report",
                "--state-dir",
                str(state_dir),
                "--output-dir",
                str(tmp_path / "public"),
            ]
        )

    assert close_calls == 1
