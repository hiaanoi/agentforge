from __future__ import annotations

import base64
import gzip
import json
import re
from pathlib import Path

import pytest

from agentforge.evaluation.verified10_campaign import (
    BenchmarkArm,
    CampaignCommand,
    CampaignCommandResult,
    CampaignExecutionError,
    Verified10Campaign,
    load_verified10_protocol,
)
from agentforge.evaluation.verified10_cli import main

ROOT = Path(__file__).resolve().parents[2]
PROTOCOL = ROOT / "evaluation" / "protocols" / "verified10-deepseek-flash-pass1.json"
CANARY_PROTOCOL = (
    ROOT / "evaluation" / "protocols" / "verified50-openai-gpt54mini-mini-native-canary.json"
)
PARENT_PROTOCOL = ROOT / "evaluation" / "protocols" / "verified50-openai-gpt54mini.json"
MINI_ROOT = Path("C:/verified-mini")
_PUBLIC_ROWS_GZIP_BASE64 = """
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
""".strip()


class FakeMiniVerifier:
    def __init__(self) -> None:
        self.calls: list[Path] = []

    def verify(self, root: Path) -> None:
        self.calls.append(root)


def public_rows() -> list[dict[str, str]]:
    # Public-only rows captured from the frozen selection artifact.  Keeping the
    # fixture compressed avoids duplicating a large issue corpus in source.
    encoded = _PUBLIC_ROWS_GZIP_BASE64
    return json.loads(gzip.decompress(base64.b64decode(encoded)).decode("utf-8"))


class FakeRunner:
    def __init__(self, protocol) -> None:
        self.protocol = protocol
        self.commands: list[CampaignCommand] = []

    def run(self, command: CampaignCommand) -> CampaignCommandResult:
        self.commands.append(command)
        argv = command.argv
        if len(argv) > 1 and argv[1] == "-c":
            return CampaignCommandResult(
                0,
                json.dumps(
                    [
                        public_rows()[-1],
                        *public_rows(),
                        {
                            "instance_id": "noise__repo-1",
                            "repo": "noise/repo",
                            "base_commit": "f" * 40,
                            "problem_statement": "noise",
                        },
                    ][1:]
                ),
                "",
            )
        if argv[:3] == ("docker", "version", "--format"):
            return CampaignCommandResult(0, "{}", "")
        if argv[:3] == ("docker", "image", "inspect"):
            tag = argv[-1]
            repo = tag.rsplit(":", 1)[0]
            return CampaignCommandResult(0, json.dumps([repo + "@sha256:" + "a" * 64]), "")
        if argv[:2] == ("docker", "create"):
            return CampaignCommandResult(0, "container\n", "")
        if argv[:2] == ("docker", "cp"):
            destination = Path(argv[-1])
            destination.mkdir(parents=True, exist_ok=True)
            (destination / ".git").mkdir(exist_ok=True)
            (destination / "source.py").write_text("x = 1\n", encoding="utf-8")
            return CampaignCommandResult(0, "", "")
        if argv[:2] == ("git", "-C") and "rev-parse" in argv:
            workspace = argv[2]
            task = next(task for task in self.protocol.tasks if task.instance_id in workspace)
            return CampaignCommandResult(0, task.base_commit + "\n", "")
        if argv[:3] == ("uv", "run", "--project"):
            output = Path(argv[argv.index("--output") + 1])
            task = next(task for task in self.protocol.tasks if task.instance_id in " ".join(argv))
            output.mkdir(parents=True, exist_ok=True)
            (output / "preds.json").write_text(
                json.dumps(
                    {
                        task.instance_id: {
                            "model_name_or_path": "openai/deepseek-v4-flash",
                            "instance_id": task.instance_id,
                            "model_patch": "",
                        }
                    }
                ),
                encoding="utf-8",
            )
            trajectory = output / task.instance_id
            trajectory.mkdir()
            problem = next(
                row["problem_statement"]
                for row in public_rows()
                if row["instance_id"] == task.instance_id
            )
            (trajectory / f"{task.instance_id}.traj.json").write_text(
                json.dumps(
                    {
                        "instance_id": task.instance_id,
                        "info": {"model_stats": {"api_calls": 1}, "exit_status": "submitted"},
                        "messages": [
                            {
                                "role": "user",
                                "content": (
                                    "<pr_description>\nConsider the following PR description:\n"
                                    + problem
                                    + "\n</pr_description>"
                                ),
                            }
                        ],
                        "config": {"model": "openai/deepseek-v4-flash"},
                    }
                ),
                encoding="utf-8",
            )
            return CampaignCommandResult(0, "", "")
        return CampaignCommandResult(
            0, "run_id=00000000-0000-0000-0000-000000000001 outcome=UNVERIFIED\n", ""
        )


def test_prepare_then_mini_then_finalize_is_local_and_secret_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    protocol = load_verified10_protocol(PROTOCOL)
    runner = FakeRunner(protocol)
    verifier = FakeMiniVerifier()
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-secret")
    campaign = Verified10Campaign(
        PROTOCOL, tmp_path / "out", runner=runner, mini_source_verifier=verifier
    )
    campaign.prepare()
    campaign.run_mini(mini_root=MINI_ROOT)
    result = campaign.finalize_predictions(BenchmarkArm.MINI_SWE_AGENT)
    assert result.predictions_sha256
    state = json.loads((tmp_path / "out" / "campaign-state.json").read_text(encoding="utf-8"))
    assert len(state["attempts"][BenchmarkArm.MINI_SWE_AGENT.value]) == 10
    assert verifier.calls == [MINI_ROOT]
    assert all("test-secret" not in repr(command) for command in runner.commands)
    mini_commands = [
        command
        for command in runner.commands
        if "minisweagent.run.benchmarks.swebench" in command.argv
    ]
    assert len(mini_commands) == 10
    first = mini_commands[0]
    task = protocol.tasks[0]
    output = tmp_path / "out" / "mini-output" / task.instance_id
    generated = tmp_path / "out" / "configs" / "mini" / f"{task.instance_id}.yaml"
    builtin = MINI_ROOT / "src" / "minisweagent" / "config" / "benchmarks" / "swebench.yaml"
    assert first.argv == (
        "uv",
        "run",
        "--project",
        str(MINI_ROOT),
        "--frozen",
        "python",
        "-m",
        "minisweagent.run.benchmarks.swebench",
        "--subset",
        "verified",
        "--split",
        "test",
        "--filter",
        f"^{re.escape(task.instance_id)}$",
        "--output",
        str(output),
        "--workers",
        "1",
        "--model",
        "openai/deepseek-v4-flash",
        "--config",
        str(builtin),
        "--config",
        str(generated),
    )
    assert "OPENAI_API_KEY" in first.environment_names
    assert first.environment is not None
    assert first.environment["OPENAI_API_KEY"] == "test-secret"
    creates = [command for command in runner.commands if command.argv[:2] == ("docker", "create")]
    pulls = [command for command in runner.commands if command.argv[:2] == ("docker", "pull")]
    inspections = [
        command for command in runner.commands if command.argv[:3] == ("docker", "image", "inspect")
    ]
    assert len(pulls) == 10
    assert len(inspections) == 30
    resets = [
        command
        for command in runner.commands
        if len(command.argv) >= 5 and command.argv[0] == "git" and command.argv[3] == "reset"
    ]
    cleans = [
        command
        for command in runner.commands
        if len(command.argv) >= 5 and command.argv[0] == "git" and command.argv[3] == "clean"
    ]
    assert len(resets) == 20
    assert len(cleans) == 20
    assert len(creates) == 20
    assert all(
        "@sha256:" in command.argv[-1] and not command.argv[-1].endswith(":latest")
        for command in creates
    )
    assert all(
        record["status"] in {"COMPLETED", "FAILED"}
        for record in state["attempts"][BenchmarkArm.MINI_SWE_AGENT.value]
    )
    assert all(
        record["provider_capabilities"]["temperature_control"] == "BOUND"
        for record in state["attempts"][BenchmarkArm.MINI_SWE_AGENT.value]
    )


def test_running_attempt_requires_explicit_recovery(tmp_path: Path) -> None:
    protocol = load_verified10_protocol(PROTOCOL)
    runner = FakeRunner(protocol)
    campaign = Verified10Campaign(PROTOCOL, tmp_path / "out", runner=runner)
    campaign.prepare()
    state_path = tmp_path / "out" / "campaign-state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["attempts"][BenchmarkArm.AGENTFORGE.value] = [
        {
            "instance_id": protocol.tasks[0].instance_id,
            "status": "RUNNING",
            "attempt_index": 1,
            "failure_class": "NONE",
            "model_patch": "",
            "run_id": None,
        }
    ]
    state_path.write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(CampaignExecutionError, match="recover-running"):
        campaign.run_agentforge()
    campaign.run_agentforge(recover_running=True)
    recovered = json.loads(state_path.read_text(encoding="utf-8"))["attempts"][
        BenchmarkArm.AGENTFORGE.value
    ][0]
    assert recovered["failure_class"] == "INTERRUPTED"
    assert recovered["terminal_reason"] == "INTERRUPTED_NO_DURABLE_RESUME"


def test_mismatched_protocol_is_rejected(tmp_path: Path) -> None:
    protocol = load_verified10_protocol(PROTOCOL)
    runner = FakeRunner(protocol)
    campaign = Verified10Campaign(PROTOCOL, tmp_path / "out", runner=runner)
    campaign.prepare()
    (tmp_path / "out" / "protocol.json").write_text("{}", encoding="utf-8")
    with pytest.raises(CampaignExecutionError):
        Verified10Campaign(PROTOCOL, tmp_path / "out", runner=runner).status()


def test_state_load_revalidates_public_problem_statement_hash(tmp_path: Path) -> None:
    protocol = load_verified10_protocol(PROTOCOL)
    campaign = Verified10Campaign(PROTOCOL, tmp_path / "out", runner=FakeRunner(protocol))
    campaign.prepare()
    state_path = tmp_path / "out" / "campaign-state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["public_tasks"][protocol.tasks[0].instance_id] = "tampered problem"
    state_path.write_text(json.dumps(state), encoding="utf-8")

    with pytest.raises(CampaignExecutionError, match="public task"):
        campaign.status()


def test_prepare_refuses_to_adopt_any_existing_output_directory(tmp_path: Path) -> None:
    protocol = load_verified10_protocol(PROTOCOL)
    for name in ("empty", "nonempty"):
        output = tmp_path / name
        output.mkdir()
        if name == "nonempty":
            (output / "foreign.txt").write_text("foreign", encoding="utf-8")
        with pytest.raises(CampaignExecutionError, match="must not exist"):
            Verified10Campaign(PROTOCOL, output, runner=FakeRunner(protocol)).prepare()


def test_resume_commands_require_existing_campaign_state(tmp_path: Path) -> None:
    protocol = load_verified10_protocol(PROTOCOL)
    campaign = Verified10Campaign(PROTOCOL, tmp_path / "missing", runner=FakeRunner(protocol))
    with pytest.raises(CampaignExecutionError, match="state"):
        campaign.status()
    assert not (tmp_path / "missing").exists()


def test_campaign_rejects_filesystem_root_output() -> None:
    with pytest.raises(CampaignExecutionError, match="filesystem root"):
        Verified10Campaign(PROTOCOL, Path(PROTOCOL.anchor))


def test_mini_malformed_predictions_are_protocol_failure_not_completed_empty(
    tmp_path: Path,
) -> None:
    protocol = load_verified10_protocol(PROTOCOL)
    campaign = Verified10Campaign(PROTOCOL, tmp_path / "campaign", runner=FakeRunner(protocol))
    task = protocol.tasks[0]
    output = tmp_path / "campaign" / "mini-output"
    output.mkdir(parents=True)
    expected = {
        "model_name_or_path": "openai/deepseek-v4-flash",
        "instance_id": task.instance_id,
        "model_patch": "diff --git a/a b/a\n",
    }
    (output / "preds.json").write_text(
        json.dumps({task.instance_id: expected, "stale__instance-1": expected}),
        encoding="utf-8",
    )
    trajectory = output / task.instance_id / f"{task.instance_id}.traj.json"
    trajectory.parent.mkdir()
    problem = next(
        row["problem_statement"] for row in public_rows() if row["instance_id"] == task.instance_id
    )
    trajectory.write_text(
        json.dumps(
            {
                "instance_id": task.instance_id,
                "info": {"exit_status": "submitted", "model_stats": {}},
                "messages": [
                    {
                        "role": "user",
                        "content": (
                            "Consider the following PR description:\n"
                            + problem
                            + "\n</pr_description>"
                        ),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    attempt = campaign._parse_mini_result(
        task, output, trajectory, CampaignCommandResult(0, "", ""), 0.0
    )

    assert attempt.status.value == "FAILED"
    assert attempt.failure_class.value == "PROTOCOL_FAILED"
    assert attempt.model_patch == ""
    assert "steps" in attempt.telemetry_unavailable

    (output / "preds.json").write_text(json.dumps({task.instance_id: expected}), encoding="utf-8")
    trajectory_payload = json.loads(trajectory.read_text(encoding="utf-8"))
    trajectory_payload["info"]["exit_status"] = "LimitsExceeded"
    trajectory.write_text(json.dumps(trajectory_payload), encoding="utf-8")
    failed_with_patch = campaign._parse_mini_result(
        task, output, trajectory, CampaignCommandResult(0, "", ""), 0.0
    )
    assert failed_with_patch.status.value == "FAILED"
    assert failed_with_patch.failure_class.value == "MODEL_FAILED"
    assert failed_with_patch.model_patch == expected["model_patch"]

    trajectory_payload["info"]["exit_status"] = "submitted"
    trajectory_payload["messages"][0]["content"] = (
        "Consider the following PR description:\ntampered\n</pr_description>"
    )
    trajectory.write_text(json.dumps(trajectory_payload), encoding="utf-8")
    prompt_drift = campaign._parse_mini_result(
        task, output, trajectory, CampaignCommandResult(0, "", ""), 0.0
    )
    assert prompt_drift.status.value == "FAILED"
    assert prompt_drift.failure_class.value == "PROTOCOL_FAILED"
    assert prompt_drift.model_patch == expected["model_patch"]


@pytest.mark.parametrize("exec_code", [1, 20, 22])
def test_agentforge_fresh_command_flow_uses_problem_and_accepts_terminal_codes(
    tmp_path: Path,
    exec_code: int,
) -> None:
    protocol = load_verified10_protocol(PROTOCOL)
    run_id = "00000000-0000-0000-0000-000000000123"
    approval_ids = (
        "00000000-0000-0000-0000-000000000201",
        "00000000-0000-0000-0000-000000000202",
    )

    class AgentForgeRunner(FakeRunner):
        def __init__(self, protocol) -> None:
            super().__init__(protocol)
            self.resumed = False

        def run(self, command: CampaignCommand) -> CampaignCommandResult:
            argv = command.argv
            if "agentforge" not in argv:
                return super().run(command)
            self.commands.append(command)
            action = argv[argv.index("agentforge") + 1]
            if action == "exec":
                return CampaignCommandResult(exec_code, f"run_id={run_id}\n", "")
            if action == "approvals":
                return CampaignCommandResult(
                    0,
                    "\n".join(
                        f"approval_id={approval_id} run_id={run_id} tool=edit_file"
                        for approval_id in approval_ids
                    ),
                    "",
                )
            if action == "resume":
                self.resumed = True
                return CampaignCommandResult(22, f"run_id={run_id}\n", "")
            if action == "inspect":
                lifecycle = "TERMINAL" if self.resumed else "PAUSED"
                return CampaignCommandResult(
                    0, f"run_id={run_id} lifecycle={lifecycle} outcome=UNVERIFIED\n", ""
                )
            return CampaignCommandResult(0, "", "")

    class ContractCampaign(Verified10Campaign):
        def _preflight_agentforge(self, workspace: Path, task) -> None:
            return None

        def _git_patch(self, workspace: Path, task) -> str:
            return "diff --git a/user.py b/user.py\n"

        @staticmethod
        def _agentforge_telemetry(workspace: Path, durable_run_id: str) -> dict[str, object]:
            return {
                "lifecycle": "TERMINAL",
                "model_calls": 3,
                "steps": 4,
                "approval_count": 2,
                "edit_count": 1,
                "test_count": 1,
                "event_count": 9,
                "unavailable": ("provider_prompt_tokens",),
            }

    runner = AgentForgeRunner(protocol)
    campaign = ContractCampaign(PROTOCOL, tmp_path / "out", runner=runner)
    campaign.prepare()
    state_path = tmp_path / "out" / "campaign-state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["attempts"][BenchmarkArm.AGENTFORGE.value] = [
        {
            "instance_id": task.instance_id,
            "attempt_index": 1,
            "status": "FAILED",
            "failure_class": "MODEL_FAILED",
        }
        for task in protocol.tasks[1:]
    ]
    state_path.write_text(json.dumps(state), encoding="utf-8")

    campaign.run_agentforge()

    exec_command = next(command for command in runner.commands if "exec" in command.argv)
    expected_problem = next(
        row["problem_statement"]
        for row in public_rows()
        if row["instance_id"] == protocol.tasks[0].instance_id
    )
    assert exec_command.argv[-1] == expected_problem
    assert sum("approve" in command.argv for command in runner.commands) == 2
    record = json.loads(state_path.read_text(encoding="utf-8"))["attempts"][
        BenchmarkArm.AGENTFORGE.value
    ][0]
    assert record["run_id"] == run_id
    assert record["status"] == "COMPLETED"
    assert record["model_patch"]


def test_agentforge_failure_after_run_id_preserves_durable_recovery_identity(
    tmp_path: Path,
) -> None:
    protocol = load_verified10_protocol(PROTOCOL)
    run_id = "00000000-0000-0000-0000-000000000321"

    class FailingRunner(FakeRunner):
        def run(self, command: CampaignCommand) -> CampaignCommandResult:
            argv = command.argv
            if "agentforge" not in argv:
                return super().run(command)
            self.commands.append(command)
            action = argv[argv.index("agentforge") + 1]
            if action == "exec":
                return CampaignCommandResult(20, f"run_id={run_id}\n", "")
            if action == "inspect":
                return CampaignCommandResult(0, f"run_id={run_id} lifecycle=RUNNING\n", "")
            if action == "approvals":
                return CampaignCommandResult(0, "", "")
            if action == "resume":
                return CampaignCommandResult(2, "", "resume infrastructure failure")
            return CampaignCommandResult(0, "", "")

    class ContractCampaign(Verified10Campaign):
        def _preflight_agentforge(self, workspace: Path, task) -> None:
            return None

        def _git_patch(self, workspace: Path, task) -> str:
            return "diff --git a/user.py b/user.py\n"

        @staticmethod
        def _agentforge_telemetry(workspace: Path, durable_run_id: str) -> dict[str, object]:
            return {"lifecycle": "RUNNING", "unavailable": ("model_usage",)}

    runner = FailingRunner(protocol)
    campaign = ContractCampaign(PROTOCOL, tmp_path / "out", runner=runner)
    campaign.prepare()
    state_path = tmp_path / "out" / "campaign-state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["attempts"][BenchmarkArm.AGENTFORGE.value] = [
        {
            "instance_id": task.instance_id,
            "attempt_index": 1,
            "status": "FAILED",
            "failure_class": "MODEL_FAILED",
        }
        for task in protocol.tasks[1:]
    ]
    state_path.write_text(json.dumps(state), encoding="utf-8")

    campaign.run_agentforge()

    record = json.loads(state_path.read_text(encoding="utf-8"))["attempts"][
        BenchmarkArm.AGENTFORGE.value
    ][0]
    assert record["status"] == "FAILED"
    assert record["run_id"] == run_id
    assert record["model_patch"]


def test_cli_main_uses_fake_campaign_and_stable_domain_errors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[str] = []

    class FakeCampaign:
        def __init__(self, protocol: Path, output: Path) -> None:
            calls.extend((str(protocol), str(output)))

        def prepare(self, **kwargs: object) -> None:
            raise CampaignExecutionError("stable campaign error")

        def preflight_summary(self) -> dict[str, str]:
            raise AssertionError("failed prepare must not emit a summary")

    result = main(
        [
            "prepare",
            "--harness-root",
            str(MINI_ROOT),
            "--protocol",
            str(PROTOCOL),
            "--output-dir",
            str(tmp_path / "out"),
        ],
        campaign_factory=FakeCampaign,
    )
    captured = capsys.readouterr()
    assert result != 0
    assert captured.err.strip() == "stable campaign error"
    assert "Traceback" not in captured.err
    assert calls


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["prepare", "--harness-root", str(MINI_ROOT)], "prepare"),
        (["run-agentforge"], "agentforge"),
        (["run-mini", "--mini-root", str(MINI_ROOT)], "mini"),
        (["status"], "status"),
        (["finalize-predictions", "--arm", "AGENTFORGE"], "finalize"),
        (["score", "--arm", "AGENTFORGE", "--harness-root", str(MINI_ROOT)], "score"),
        (["report"], "report"),
    ],
)
def test_cli_main_dispatches_every_campaign_command_with_fake_runner(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    expected: str,
) -> None:
    calls: list[str] = []

    class FakeCampaign:
        def __init__(self, protocol: Path, output: Path) -> None:
            return None

        def prepare(self, **kwargs: object) -> None:
            calls.append("prepare")

        def preflight_summary(self) -> dict[str, str]:
            return {
                "agentforge_admission": "10/10",
                "safe_symlink_rejections": "0",
                "protocol_sha256": "a" * 64,
            }

        def run_agentforge(self, **kwargs: object) -> None:
            calls.append("agentforge")

        def run_mini(self, **kwargs: object) -> None:
            calls.append("mini")

        def status(self) -> dict[str, object]:
            calls.append("status")
            return {"AGENTFORGE": {"planned": 10}, "MINI_SWE_AGENT": {"planned": 10}}

        def finalize_predictions(self, arm: BenchmarkArm) -> None:
            calls.append("finalize")

        def score(self, arm: BenchmarkArm, harness_root: Path) -> None:
            assert arm is BenchmarkArm.AGENTFORGE
            assert harness_root == MINI_ROOT
            calls.append("score")

        def report(self) -> None:
            calls.append("report")

    result = main(
        [*argv, "--protocol", str(PROTOCOL), "--output-dir", str(tmp_path / "out")],
        campaign_factory=FakeCampaign,
    )
    captured = capsys.readouterr()
    assert result == 0
    assert calls == [expected]
    assert "Traceback" not in captured.err
    if expected == "prepare":
        assert captured.out.splitlines() == [
            "agentforge_admission=10/10",
            "safe_symlink_rejections=0",
            f"protocol_sha256={'a' * 64}",
        ]


def test_cli_run_agentforge_accepts_mini_linear_repair_engine(
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}

    class Campaign:
        def __init__(self, protocol: Path, output: Path) -> None:
            del protocol, output

        def run_agentforge(self, **kwargs: object) -> None:
            captured.update(kwargs)

    result = main(
        [
            "run-agentforge",
            "--protocol",
            str(PROTOCOL),
            "--output-dir",
            str(tmp_path / "out"),
            "--repair-engine",
            "mini_linear",
        ],
        campaign_factory=Campaign,
    )

    assert result == 0
    assert captured["repair_engine"] == "mini_linear"


def test_cli_run_agentforge_accepts_mini_native_repair_engine(
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}

    class Campaign:
        def __init__(self, protocol: Path, output: Path) -> None:
            del protocol, output

        def run_agentforge(self, **kwargs: object) -> None:
            captured.update(kwargs)

    result = main(
        [
            "run-agentforge",
            "--protocol",
            str(PROTOCOL),
            "--output-dir",
            str(tmp_path / "out"),
            "--repair-engine",
            "mini_native",
        ],
        campaign_factory=Campaign,
    )

    assert result == 0
    assert captured["repair_engine"] == "mini_native"


def test_mini_native_canary_projection_is_deterministic(
    tmp_path: Path,
) -> None:
    from evaluation.build_verified50_protocol import build_mini_native_canary

    parent = json.loads(PROTOCOL.read_text(encoding="utf-8"))
    parent["protocol_name"] = "verified50-openai-gpt54mini"
    parent["model"] = "gpt-5.4-mini"
    parent["tasks"] = parent["tasks"] * 5
    parent_path = tmp_path / "verified50.json"
    parent_path.write_text(json.dumps(parent), encoding="utf-8")
    canary_path = tmp_path / "canary.json"

    build_mini_native_canary(parent_path, canary_path)
    canary = load_verified10_protocol(canary_path)
    assert canary.protocol_name == "verified50-openai-gpt54mini-mini-native-canary"
    assert len(canary.tasks) == 10
    assert canary.tasks == load_verified10_protocol(PROTOCOL).tasks
    assert canary.parent_protocol_sha256
    assert canary.model == "gpt-5.4-mini"
    assert canary.temperature == 0.0
    assert canary.agentforge_budget.logical_model_calls == 50
    assert canary.agentforge_budget.run_steps == 80
    assert canary.mini_budget.step_limit == 50
    campaign = Verified10Campaign(canary_path, tmp_path / "out")
    assert campaign._active_repair_engine == "mini_native"


def test_checked_in_mini_native_canary_contract() -> None:
    protocol = load_verified10_protocol(CANARY_PROTOCOL)
    parent = load_verified10_protocol(PARENT_PROTOCOL)
    assert len(protocol.tasks) == 10
    assert len(parent.tasks) == 50
    assert protocol.tasks == parent.tasks[:10]
    assert protocol.parent_protocol_sha256 == parent.protocol_sha256
    assert protocol.parent_protocol_sha256
    assert protocol.model == "gpt-5.4-mini"
    assert protocol.temperature == 0.0
    assert protocol.agentforge_budget.logical_model_calls == 50
    assert protocol.agentforge_budget.run_steps == 80
    assert protocol.agentforge_budget.wall_time_seconds == 1800
    assert protocol.agentforge_budget.provider_max_model_requests == 52
    assert protocol.mini_budget.step_limit == 50
    assert protocol.mini_budget.wall_time_seconds == 1800
    assert Verified10Campaign(CANARY_PROTOCOL, Path("tmp-canary-output"))._active_repair_engine == (
        "mini_native"
    )


def test_cli_prepare_prints_linux_preflight_contract(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    class PreparedCampaign:
        def __init__(self, protocol: Path, output: Path) -> None:
            pass

        def prepare(self, **kwargs: object) -> None:
            pass

        def preflight_summary(self) -> dict[str, str]:
            return {
                "agentforge_admission": "10/10",
                "safe_symlink_rejections": "0",
                "protocol_sha256": "a" * 64,
            }

    assert (
        main(
            [
                "prepare",
                "--harness-root",
                str(MINI_ROOT),
                "--protocol",
                str(PROTOCOL),
                "--output-dir",
                str(tmp_path / "out"),
            ],
            campaign_factory=PreparedCampaign,
        )
        == 0
    )
    assert capsys.readouterr().out.splitlines() == [
        "agentforge_admission=10/10",
        "safe_symlink_rejections=0",
        f"protocol_sha256={'a' * 64}",
    ]
