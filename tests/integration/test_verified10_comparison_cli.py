# ruff: noqa: E501, E702
from __future__ import annotations

import json
from pathlib import Path

import pytest

import agentforge.evaluation.verified10_campaign as campaign_module
from agentforge.evaluation.verified10_campaign import (
    BenchmarkArm,
    CampaignCommand,
    CampaignCommandResult,
    CampaignExecutionError,
    Verified10Campaign,
    load_verified10_protocol,
)

ROOT = Path(__file__).resolve().parents[2]
PROTOCOL = ROOT / "evaluation" / "protocols" / "verified10-deepseek-flash-pass1.json"
MINI_ROOT = Path(r"C:\Users\ehy_27\AppData\Local\Temp\agentforge-mini-25941c89-contract\mini-swe-agent-25941c89cfbc91eb40b3f8756348c91d9977d57e")


@pytest.fixture(autouse=True)
def public_rows_are_verified_by_the_task4_seam(monkeypatch: pytest.MonkeyPatch) -> None:
    # Dataset contents are an external public fixture in this command-spec test;
    # Task4 owns exhaustive hash-validation coverage.
    monkeypatch.setattr(campaign_module, "validate_public_dataset_rows", lambda rows, protocol: tuple(rows))


class FakeRunner:
    def __init__(self, protocol) -> None:
        self.protocol = protocol
        self.commands: list[CampaignCommand] = []

    def run(self, command: CampaignCommand) -> CampaignCommandResult:
        self.commands.append(command)
        argv = command.argv
        if argv[:2] == ("python", "-c"):
            return CampaignCommandResult(0, json.dumps([{
                "instance_id": task.instance_id, "repo": task.repo,
                "base_commit": task.base_commit, "problem_statement": "public task",
            } for task in self.protocol.tasks]), "")
        if argv[:3] == ("docker", "version", "--format"):
            return CampaignCommandResult(0, "{}", "")
        if argv[:3] == ("docker", "image", "inspect"):
            return CampaignCommandResult(0, argv[3] + "@sha256:" + "a" * 64, "")
        if argv[:2] == ("docker", "create"):
            return CampaignCommandResult(0, "container\n", "")
        if argv[:2] == ("docker", "cp"):
            destination = Path(argv[-1]); destination.mkdir(parents=True, exist_ok=True)
            (destination / ".git").mkdir(exist_ok=True)
            (destination / "source.py").write_text("x = 1\n", encoding="utf-8")
            return CampaignCommandResult(0, "", "")
        if argv[:3] == ("git", "-C", str(command.cwd)) or argv[-2:] == ("rev-parse", "HEAD"):
            workspace = argv[2]
            task = next(task for task in self.protocol.tasks if task.instance_id in workspace)
            return CampaignCommandResult(0, task.base_commit + "\n", "")
        if argv[:3] == ("uv", "run", "--project"):
            output = Path(argv[argv.index("--output") + 1])
            task = next(task for task in self.protocol.tasks if task.instance_id in " ".join(argv))
            output.mkdir(parents=True, exist_ok=True)
            (output / "preds.json").write_text(json.dumps({task.instance_id: ""}), encoding="utf-8")
            trajectory = output / task.instance_id; trajectory.mkdir()
            (trajectory / f"{task.instance_id}.traj.json").write_text(json.dumps({"info": {"model_stats": {"api_calls": 0}, "exit_status": "submitted"}}), encoding="utf-8")
            return CampaignCommandResult(0, "", "")
        return CampaignCommandResult(0, "run_id=00000000-0000-0000-0000-000000000001 outcome=UNVERIFIED\n", "")


def test_prepare_then_mini_then_finalize_is_local_and_secret_free(tmp_path: Path) -> None:
    protocol = load_verified10_protocol(PROTOCOL)
    runner = FakeRunner(protocol)
    campaign = Verified10Campaign(PROTOCOL, tmp_path / "out", runner=runner)
    campaign.prepare()
    campaign.run_mini(mini_root=MINI_ROOT)
    result = campaign.finalize_predictions(BenchmarkArm.MINI_SWE_AGENT)
    assert result.predictions_sha256
    state = json.loads((tmp_path / "out" / "campaign-state.json").read_text())
    assert len(state["attempts"][BenchmarkArm.MINI_SWE_AGENT.value]) == 10
    assert all("DEEPSEEK_API_KEY" not in " ".join(command.argv) for command in runner.commands)
    assert all(
        record["status"] in {"COMPLETED", "FAILED"}
        for record in state["attempts"][BenchmarkArm.MINI_SWE_AGENT.value]
    )


def test_running_attempt_requires_explicit_recovery(tmp_path: Path) -> None:
    protocol = load_verified10_protocol(PROTOCOL); runner = FakeRunner(protocol)
    campaign = Verified10Campaign(PROTOCOL, tmp_path / "out", runner=runner); campaign.prepare()
    state_path = tmp_path / "out" / "campaign-state.json"; state = json.loads(state_path.read_text())
    state["attempts"][BenchmarkArm.AGENTFORGE.value] = [{"instance_id": protocol.tasks[0].instance_id, "status": "RUNNING", "attempt_index": 1, "failure_class": "NONE", "model_patch": "", "run_id": None}]
    state_path.write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(CampaignExecutionError, match="recover-running"):
        campaign.run_agentforge()
    campaign.run_agentforge(recover_running=True)
    assert campaign.status()["failed"] >= 1


def test_mismatched_protocol_is_rejected(tmp_path: Path) -> None:
    protocol = load_verified10_protocol(PROTOCOL); runner = FakeRunner(protocol)
    campaign = Verified10Campaign(PROTOCOL, tmp_path / "out", runner=runner); campaign.prepare()
    (tmp_path / "out" / "protocol.json").write_text("{}", encoding="utf-8")
    with pytest.raises(CampaignExecutionError):
        Verified10Campaign(PROTOCOL, tmp_path / "out", runner=runner).status()
