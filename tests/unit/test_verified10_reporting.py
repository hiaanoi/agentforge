from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import agentforge.evaluation.verified10_reporting as reporting_module
from agentforge.evaluation.swebench_prediction import (
    SWEbenchInstanceBinding,
    SWEbenchPrediction,
)
from agentforge.evaluation.verified10_campaign import (
    AttemptFailureClass,
    AttemptStatus,
    BenchmarkArm,
    BenchmarkAttemptRecord,
    finalize_verified10_campaign,
    load_verified10_protocol,
)
from agentforge.evaluation.verified10_reporting import (
    PinnedHarnessVerifier,
    ReportingError,
    Verified10Reporting,
    choose_decision,
)
from agentforge.evaluation.verified10_support import (
    CampaignCommand,
    CampaignCommandResult,
    CampaignState,
    WorkspaceRecord,
)

ROOT = Path(__file__).parents[2]
PROTOCOL_PATH = ROOT / "evaluation" / "protocols" / "verified10-deepseek-flash-pass1.json"


class _AcceptHarness:
    def verify(self, root: Path) -> None:
        assert root == root.resolve(strict=True)


class _HarnessRunner:
    def __init__(self, report: dict[str, object]) -> None:
        self.report = report
        self.commands: list[CampaignCommand] = []

    def run(self, command: CampaignCommand) -> CampaignCommandResult:
        self.commands.append(command)
        report_dir = Path(command.argv[command.argv.index("--report_dir") + 1])
        run_id = command.argv[command.argv.index("--run_id") + 1]
        predictions = json.loads(
            Path(command.argv[command.argv.index("--predictions_path") + 1]).read_text(
                encoding="utf-8"
            )
        )
        model = predictions[0]["model_name_or_path"].replace("/", "__")
        report_dir.mkdir(parents=True, exist_ok=True)
        (report_dir / f"{model}.{run_id}.json").write_text(
            json.dumps(self.report), encoding="utf-8"
        )
        return CampaignCommandResult(0, "official harness completed\n", "")


def _official_report(*, resolved: tuple[str, ...] = ()) -> dict[str, object]:
    protocol = load_verified10_protocol(PROTOCOL_PATH)
    expected = tuple(task.instance_id for task in protocol.tasks)
    unresolved = tuple(item for item in expected if item not in resolved)
    return {
        "schema_version": 2,
        "total_instances": 10,
        "submitted_instances": 10,
        "completed_instances": 10,
        "resolved_instances": len(resolved),
        "unresolved_instances": len(unresolved),
        "infra_failure_instances": 0,
        "ambiguous_failure_instances": 0,
        "empty_patch_instances": 0,
        "error_instances": 0,
        "completed_ids": list(expected),
        "incomplete_ids": [],
        "empty_patch_ids": [],
        "submitted_ids": list(expected),
        "resolved_ids": list(resolved),
        "unresolved_ids": list(unresolved),
        "infra_failure_ids": [],
        "ambiguous_failure_ids": [],
        "failure_reasons": {},
        "error_ids": [],
        "unstopped_instances": 0,
        "unstopped_containers": [],
        "unremoved_images": [],
    }


def _finalized_arm(root: Path, arm: BenchmarkArm, *, patch_first: bool = False) -> None:
    protocol = load_verified10_protocol(PROTOCOL_PATH)
    copied_protocol = root / "protocol.json"
    if not copied_protocol.exists():
        copied_protocol.write_bytes(PROTOCOL_PATH.read_bytes())
    state_path = root / "campaign-state.json"
    if state_path.exists():
        state = CampaignState.model_validate_json(state_path.read_bytes())
    else:
        workspaces = {
            f"{candidate.value}:{task.instance_id}": WorkspaceRecord(
                path=f"workspaces/{candidate.value.lower()}/{task.instance_id}",
                image_tag=f"docker.io/swebench/{task.instance_id}:latest",
                image_digest=f"docker.io/swebench/{task.instance_id}@sha256:{'a' * 64}",
                head=task.base_commit,
                workspace_digest="b" * 64,
                symlink_count=0,
                disk_bytes=1,
            )
            for candidate in BenchmarkArm
            for task in protocol.tasks
        }
        state = CampaignState(
            protocol_digest=protocol.protocol_digest,
            prepared=True,
            admission_count=10,
            public_tasks={task.instance_id: "bound in runner" for task in protocol.tasks},
            workspaces=workspaces,
        )
    namespace = "agentforge" if arm is BenchmarkArm.AGENTFORGE else "mini-swe-agent"
    attempts: list[BenchmarkAttemptRecord] = []
    predictions: list[SWEbenchPrediction] = []
    for index, task in enumerate(protocol.tasks):
        binding = SWEbenchInstanceBinding(
            instance_id=task.instance_id,
            repo=task.repo,
            base_commit=task.base_commit,
        )
        patch = "diff --git a/a.py b/a.py\n" if patch_first and index == 0 else ""
        prediction = (
            SWEbenchPrediction(
                instance_id=task.instance_id,
                model_name_or_path=f"{namespace}:{protocol.model}",
                model_patch=patch,
                base_commit=task.base_commit,
                patch_sha256=hashlib.sha256(patch.encode()).hexdigest(),
            )
            if patch
            else SWEbenchPrediction.empty(binding, protocol.model, namespace=namespace)
        )
        predictions.append(prediction)
        attempts.append(
            BenchmarkAttemptRecord(
                protocol_sha256=protocol.protocol_sha256,
                arm=arm,
                instance_id=task.instance_id,
                attempt_index=1,
                status=AttemptStatus.COMPLETED,
                failure_class=(
                    AttemptFailureClass.NONE if patch else AttemptFailureClass.EMPTY
                ),
                model_calls=50,
                steps=50,
                provider_total_tokens=1000 + index,
                approval_count=1,
                edit_count=1 if patch else 0,
                test_count=1,
                wall_time_seconds=10.0 + index,
                prediction_patch_sha256=prediction.patch_sha256,
            )
        )
    finalize_verified10_campaign(
        protocol,
        attempts,
        predictions,
        root / f"{arm.value.lower()}-predictions.json",
        root / f"{arm.value.lower()}-ledger.json",
        arm=arm,
    )
    state = state.model_copy(update={"finalized_arms": (*state.finalized_arms, arm.value)})
    state_path.write_text(state.model_dump_json(indent=2) + "\n", encoding="utf-8")


def test_score_invokes_exact_pinned_harness_contract_and_preserves_report(
    tmp_path: Path,
) -> None:
    _finalized_arm(tmp_path, BenchmarkArm.AGENTFORGE, patch_first=True)
    report = _official_report(resolved=("sympy__sympy-16886",))
    runner = _HarnessRunner(report)
    reporting = Verified10Reporting(
        PROTOCOL_PATH,
        tmp_path,
        runner=runner,
        harness_verifier=_AcceptHarness(),
    )
    harness = tmp_path / "harness"
    harness.mkdir()

    result = reporting.score(BenchmarkArm.AGENTFORGE, harness)

    assert result.resolved_instances == 1
    command = runner.commands[0]
    assert command.argv[:7] == (
        "uv",
        "run",
        "--project",
        str(harness),
        "--frozen",
        "python",
        "-m",
    )
    assert "swebench.harness.run_evaluation" in command.argv
    assert command.argv[command.argv.index("--max_workers") + 1] == "1"
    assert command.argv[command.argv.index("--timeout") + 1] == "1800"
    start = command.argv.index("--instance_ids") + 1
    end = command.argv.index("--predictions_path")
    assert command.argv[start:end] == tuple(task.instance_id for task in reporting.protocol.tasks)
    assert result.report_sha256 == hashlib.sha256(result.report_path.read_bytes()).hexdigest()
    assert result.log_path.read_text(encoding="utf-8") == "official harness completed\n"


def test_score_rejects_wrong_prediction_denominator_before_harness(tmp_path: Path) -> None:
    _finalized_arm(tmp_path, BenchmarkArm.AGENTFORGE)
    path = tmp_path / "agentforge-predictions.json"
    rows = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps(rows[:-1]), encoding="utf-8")
    runner = _HarnessRunner(_official_report())
    reporting = Verified10Reporting(
        PROTOCOL_PATH,
        tmp_path,
        runner=runner,
        harness_verifier=_AcceptHarness(),
    )
    harness = tmp_path / "harness"
    harness.mkdir()

    with pytest.raises(ReportingError, match=r"ten|denominator"):
        reporting.score(BenchmarkArm.AGENTFORGE, harness)
    assert runner.commands == []


def test_score_rejects_harness_mutation_of_finalized_predictions(tmp_path: Path) -> None:
    _finalized_arm(tmp_path, BenchmarkArm.AGENTFORGE)

    class MutatingRunner(_HarnessRunner):
        def run(self, command: CampaignCommand) -> CampaignCommandResult:
            result = super().run(command)
            target = Path(command.argv[command.argv.index("--predictions_path") + 1])
            target.write_bytes(target.read_bytes() + b" ")
            return result

    reporting = Verified10Reporting(
        PROTOCOL_PATH,
        tmp_path,
        runner=MutatingRunner(_official_report()),
        harness_verifier=_AcceptHarness(),
    )
    harness = tmp_path / "harness"
    harness.mkdir()

    with pytest.raises(ReportingError, match="mutated"):
        reporting.score(BenchmarkArm.AGENTFORGE, harness)


def test_official_report_rejects_overlap_or_missing_denominator(tmp_path: Path) -> None:
    _finalized_arm(tmp_path, BenchmarkArm.AGENTFORGE)
    report = _official_report()
    expected = report["submitted_ids"]
    assert isinstance(expected, list)
    report["resolved_ids"] = [expected[0]]
    report["resolved_instances"] = 1
    # Leave the same ID in unresolved_ids to create an ambiguous official denominator.
    runner = _HarnessRunner(report)
    reporting = Verified10Reporting(
        PROTOCOL_PATH,
        tmp_path,
        runner=runner,
        harness_verifier=_AcceptHarness(),
    )
    harness = tmp_path / "harness"
    harness.mkdir()

    with pytest.raises(ReportingError, match=r"classification|denominator"):
        reporting.score(BenchmarkArm.AGENTFORGE, harness)


def test_report_builds_paired_artifacts_and_approved_decision(tmp_path: Path) -> None:
    _finalized_arm(tmp_path, BenchmarkArm.AGENTFORGE, patch_first=True)
    _finalized_arm(tmp_path, BenchmarkArm.MINI_SWE_AGENT, patch_first=True)
    harness = tmp_path / "harness"
    harness.mkdir()
    af = Verified10Reporting(
        PROTOCOL_PATH,
        tmp_path,
        runner=_HarnessRunner(_official_report(resolved=("sympy__sympy-16886",))),
        harness_verifier=_AcceptHarness(),
    )
    af.score(BenchmarkArm.AGENTFORGE, harness)
    mini = Verified10Reporting(
        PROTOCOL_PATH,
        tmp_path,
        runner=_HarnessRunner(
            _official_report(
                resolved=("sympy__sympy-16886", "django__django-12419")
            )
        ),
        harness_verifier=_AcceptHarness(),
    )
    mini.score(BenchmarkArm.MINI_SWE_AGENT, harness)

    result = mini.report()

    comparison = json.loads(result.comparison_json.read_text(encoding="utf-8"))
    assert comparison["decision"] == "DESIGN_REPAIR_ENGINE_MIGRATION"
    assert comparison["agentforge"]["evidence_gates"] == {
        "admission_10_of_10": True,
        "non_empty_patches_gt_0": True,
        "resolved_gt_0": True,
    }
    assert len(comparison["instances"]) == 10
    assert comparison["prior_14_call_baseline"]["agentforge_resolved"] == 0
    assert result.comparison_markdown.is_file()
    manifest = json.loads(result.artifact_manifest.read_text(encoding="utf-8"))
    assert manifest["protocol_sha256"] == mini.protocol.protocol_sha256
    assert all(len(item["sha256"]) == 64 for item in manifest["artifacts"])


def test_report_removes_partial_outputs_when_manifest_build_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _finalized_arm(tmp_path, BenchmarkArm.AGENTFORGE, patch_first=True)
    _finalized_arm(tmp_path, BenchmarkArm.MINI_SWE_AGENT, patch_first=True)
    harness = tmp_path / "harness"
    harness.mkdir()
    for arm in BenchmarkArm:
        Verified10Reporting(
            PROTOCOL_PATH,
            tmp_path,
            runner=_HarnessRunner(_official_report()),
            harness_verifier=_AcceptHarness(),
        ).score(arm, harness)
    original = reporting_module._atomic_new

    def fail_manifest(path: Path, payload: bytes) -> None:
        if path.name == "artifact-manifest.json":
            raise ReportingError("injected manifest failure")
        original(path, payload)

    monkeypatch.setattr(reporting_module, "_atomic_new", fail_manifest)
    with pytest.raises(ReportingError, match="injected"):
        Verified10Reporting(PROTOCOL_PATH, tmp_path).report()
    assert not (tmp_path / "comparison.json").exists()
    assert not (tmp_path / "comparison.md").exists()
    assert not (tmp_path / "artifact-manifest.json").exists()
    assert not (tmp_path / ".comparison.finalize.lock").exists()


@pytest.mark.parametrize(
    ("admission", "agentforge_resolved", "mini_resolved", "expected"),
    [
        (9, 5, 5, "COMPATIBILITY_INCOMPLETE"),
        (10, 0, 1, "DESIGN_REPAIR_ENGINE_MIGRATION"),
        (10, 1, 2, "DESIGN_REPAIR_ENGINE_MIGRATION"),
        (10, 2, 3, "KEEP_AND_IMPROVE_AGENTFORGE_LOOP"),
        (10, 0, 0, "RUN_MODEL_CONTROL_EXPERIMENT"),
    ],
)
def test_decision_rules_are_fixed(
    admission: int,
    agentforge_resolved: int,
    mini_resolved: int,
    expected: str,
) -> None:
    assert choose_decision(admission, agentforge_resolved, mini_resolved) == expected


def test_pinned_harness_verifier_rejects_wrong_or_dirty_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / "swebench" / "harness").mkdir(parents=True)
    (tmp_path / "swebench" / "harness" / "run_evaluation.py").write_text(
        "# fixture", encoding="utf-8"
    )

    def wrong(root: Path, *args: str) -> str:
        return "f" * 40 if args[0] == "rev-parse" else ""

    monkeypatch.setattr(PinnedHarnessVerifier, "_git", staticmethod(wrong))
    with pytest.raises(ReportingError, match="commit"):
        PinnedHarnessVerifier().verify(tmp_path)

    def dirty(root: Path, *args: str) -> str:
        return (
            "4e6126978a16bdfebc6538db8f28cacc2c8b77dc"
            if args[0] == "rev-parse"
            else " M swebench/harness/run_evaluation.py"
        )

    monkeypatch.setattr(PinnedHarnessVerifier, "_git", staticmethod(dirty))
    with pytest.raises(ReportingError, match="clean"):
        PinnedHarnessVerifier().verify(tmp_path)
