"""Official SWE-bench scoring and paired reporting for the frozen Verified-10 run."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, cast
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agentforge.evaluation.verified10_campaign import (
    BenchmarkArm,
    BenchmarkAttemptRecord,
    Verified10Protocol,
    load_attempt_ledger,
    load_verified10_protocol,
)
from agentforge.evaluation.verified10_support import (
    CampaignCommand,
    CampaignState,
    CommandRunner,
    SubprocessCommandRunner,
)

SWE_BENCH_COMMIT: Literal["4e6126978a16bdfebc6538db8f28cacc2c8b77dc"] = (
    "4e6126978a16bdfebc6538db8f28cacc2c8b77dc"
)
_SHA256 = r"^[0-9a-f]{64}$"
class ReportingError(RuntimeError):
    """Stable, non-secret failure at the official scoring/report boundary."""


class HarnessVerifier(Protocol):
    def verify(self, root: Path) -> None: ...


class PinnedHarnessVerifier:
    """Require the exact clean SWE-bench source used by the frozen protocol."""

    def verify(self, root: Path) -> None:
        requested = root.absolute()
        try:
            if requested.is_symlink() or requested.resolve(strict=True) != requested:
                raise ReportingError("SWE-bench harness path is unsafe")
            metadata = requested.lstat()
            if not stat.S_ISDIR(metadata.st_mode) or not (requested / ".git").is_dir():
                raise ReportingError("SWE-bench harness is not a local Git checkout")
            if self._git(requested, "rev-parse", "--verify", "HEAD") != SWE_BENCH_COMMIT:
                raise ReportingError("SWE-bench harness commit does not match protocol")
            if self._git(requested, "status", "--porcelain=v1", "--untracked-files=no"):
                raise ReportingError("SWE-bench harness tracked tree must be clean")
            entry = requested / "swebench" / "harness" / "run_evaluation.py"
            item = entry.lstat()
            if entry.is_symlink() or not stat.S_ISREG(item.st_mode):
                raise ReportingError("SWE-bench harness entry point is unavailable")
        except ReportingError:
            raise
        except OSError:
            raise ReportingError("SWE-bench harness verification failed") from None

    @staticmethod
    def _git(root: Path, *arguments: str) -> str:
        try:
            result = subprocess.run(
                ("git", "-C", str(root), *arguments),
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
                shell=False,
            )
        except (OSError, subprocess.SubprocessError):
            raise ReportingError("SWE-bench harness Git verification failed") from None
        if result.returncode != 0:
            raise ReportingError("SWE-bench harness Git verification failed")
        return result.stdout.strip()


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class OfficialInstanceResult(_FrozenModel):
    instance_id: str
    classification: Literal[
        "RESOLVED",
        "UNRESOLVED",
        "INFRA_FAILURE",
        "AMBIGUOUS_FAILURE",
        "EMPTY_PATCH",
        "ERROR",
        "INCOMPLETE",
    ]
    resolved: bool


class OfficialReport(_FrozenModel):
    raw: dict[str, object]
    instances: tuple[OfficialInstanceResult, ...]
    resolved_instances: int = Field(ge=0, le=500)


class ScoreMetadata(_FrozenModel):
    schema_version: Literal[1] = 1
    arm: BenchmarkArm
    protocol_sha256: str = Field(pattern=_SHA256)
    harness_commit: Literal["4e6126978a16bdfebc6538db8f28cacc2c8b77dc"]
    run_id: str
    predictions_sha256: str = Field(pattern=_SHA256)
    ledger_sha256: str = Field(pattern=_SHA256)
    report_sha256: str = Field(pattern=_SHA256)
    log_sha256: str = Field(pattern=_SHA256)
    report_path: str
    log_path: str


@dataclass(frozen=True, slots=True)
class ScoreResult:
    arm: BenchmarkArm
    resolved_instances: int
    report_path: Path
    log_path: Path
    report_sha256: str


@dataclass(frozen=True, slots=True)
class ComparisonResult:
    comparison_json: Path
    comparison_markdown: Path
    artifact_manifest: Path


class Verified10Reporting:
    """Score finalized arms and produce one hash-bound paired comparison."""

    def __init__(
        self,
        protocol_path: str | Path,
        output_dir: str | Path,
        *,
        runner: CommandRunner | None = None,
        harness_verifier: HarnessVerifier | None = None,
    ) -> None:
        try:
            self.protocol_path = Path(protocol_path).resolve(strict=True)
            self.protocol = load_verified10_protocol(self.protocol_path)
            self.root = Path(output_dir).resolve(strict=True)
            if not self.root.is_dir() or self.root.is_symlink():
                raise OSError
            copied = load_verified10_protocol(self.root / "protocol.json")
            if copied.protocol_sha256 != self.protocol.protocol_sha256:
                raise ReportingError("Campaign protocol digest mismatch")
        except ReportingError:
            raise
        except (OSError, ValueError):
            raise ReportingError("Unable to load finalized Verified-10 campaign") from None
        self.runner = runner or SubprocessCommandRunner()
        self.harness_verifier = harness_verifier or PinnedHarnessVerifier()

    def score(self, arm: BenchmarkArm, harness_root: str | Path) -> ScoreResult:
        harness = Path(harness_root).resolve(strict=True)
        self.harness_verifier.verify(harness)
        _, prediction_path, ledger_path, predictions, attempts = self._load_arm(arm)
        prediction_digest_before = _sha256_file(prediction_path)
        ledger_digest_before = _sha256_file(ledger_path)
        score_root = self.root / "official" / arm.value.lower()
        if score_root.exists() or score_root.is_symlink():
            raise ReportingError("Official score for arm already exists")
        score_root.mkdir(parents=True)
        run_id = f"verified10-{self.protocol.protocol_sha256[:16]}-{arm.value.lower()}"
        harness_dataset = self.root / "harness-dataset.json"
        dataset_name = str(harness_dataset) if harness_dataset.is_file() else self.protocol.dataset_name
        command = CampaignCommand(
            (
                "uv",
                "run",
                "--project",
                str(harness),
                "--frozen",
                "python",
                "-m",
                "swebench.harness.run_evaluation",
                "--dataset_name",
                dataset_name,
                "--split",
                self.protocol.dataset_split,
                "--instance_ids",
                *(task.instance_id for task in self.protocol.tasks),
                "--predictions_path",
                str(prediction_path),
                "--max_workers",
                "4",
                "--timeout",
                "1800",
                "--run_id",
                run_id,
                "--report_dir",
                str(score_root),
            ),
            cwd=harness,
            timeout_seconds=10 * 1800,
        )
        try:
            result = self.runner.run(command)
        except (OSError, subprocess.SubprocessError):
            raise ReportingError("Official SWE-bench harness could not run") from None
        log_path = score_root / "harness.log"
        log_bytes = (result.stdout + result.stderr).encode("utf-8", errors="replace")
        _atomic_new(log_path, log_bytes)
        if result.returncode != 0:
            raise ReportingError("Official SWE-bench harness failed")
        model_name = predictions[0]["model_name_or_path"].replace("/", "__")
        report_path = score_root / f"{model_name}.{run_id}.json"
        official = _load_official_report(report_path, self.protocol)
        prediction_digest = _sha256_file(prediction_path)
        ledger_digest = _sha256_file(ledger_path)
        if (
            prediction_digest != prediction_digest_before
            or ledger_digest != ledger_digest_before
        ):
            raise ReportingError("Official harness mutated finalized generation artifacts")
        report_digest = _sha256_file(report_path)
        log_digest = _sha256_file(log_path)
        # Ensure scoring did not mutate finalized generation artifacts.
        if any(record.protocol_sha256 != self.protocol.protocol_sha256 for record in attempts):
            raise ReportingError("Attempt ledger protocol digest mismatch")
        metadata = ScoreMetadata(
            arm=arm,
            protocol_sha256=self.protocol.protocol_sha256,
            harness_commit=SWE_BENCH_COMMIT,
            run_id=run_id,
            predictions_sha256=prediction_digest,
            ledger_sha256=ledger_digest,
            report_sha256=report_digest,
            log_sha256=log_digest,
            report_path=report_path.relative_to(self.root).as_posix(),
            log_path=log_path.relative_to(self.root).as_posix(),
        )
        _atomic_new(
            score_root / "score-metadata.json",
            metadata.model_dump_json(indent=2).encode("utf-8") + b"\n",
        )
        return ScoreResult(
            arm=arm,
            resolved_instances=official.resolved_instances,
            report_path=report_path,
            log_path=log_path,
            report_sha256=report_digest,
        )

    def report(self) -> ComparisonResult:
        targets = (
            self.root / "comparison.json",
            self.root / "comparison.md",
            self.root / "artifact-manifest.json",
        )
        if any(path.exists() or path.is_symlink() for path in targets):
            raise ReportingError("Verified-10 comparison already exists")
        state = self._load_state()
        if state.admission_count != len(self.protocol.tasks) or set(state.finalized_arms) != {
            arm.value for arm in BenchmarkArm
        }:
            raise ReportingError("Campaign is not fully admitted and finalized")
        arm_data = {
            arm: self._arm_report_data(arm, state.admission_count) for arm in BenchmarkArm
        }
        agentforge = arm_data[BenchmarkArm.AGENTFORGE]
        mini = arm_data[BenchmarkArm.MINI_SWE_AGENT]
        agentforge_resolved = _required_int(agentforge, "resolved")
        mini_resolved = _required_int(mini, "resolved")
        agentforge_instances = cast(dict[str, dict[str, object]], agentforge["by_instance"])
        mini_instances = cast(dict[str, dict[str, object]], mini["by_instance"])
        decision = choose_decision(
            state.admission_count,
            agentforge_resolved,
            mini_resolved,
            expected_admission=len(self.protocol.tasks),
        )
        instances: list[dict[str, object]] = []
        for task in self.protocol.tasks:
            instances.append(
                {
                    "instance_id": task.instance_id,
                    "agentforge": agentforge_instances[task.instance_id],
                    "mini_swe_agent": mini_instances[task.instance_id],
                }
            )
        comparison = {
            "schema_version": 1,
            "protocol_sha256": self.protocol.protocol_sha256,
            "decision": decision,
            "agentforge": {key: value for key, value in agentforge.items() if key != "by_instance"},
            "mini_swe_agent": {key: value for key, value in mini.items() if key != "by_instance"},
            "instances": instances,
            "prior_14_call_baseline": self.protocol.prior_baseline.model_dump(mode="json"),
            "paired_delta": {
                "agentforge_resolved": agentforge_resolved
                - self.protocol.prior_baseline.agentforge_resolved,
                "mini_resolved": mini_resolved
                - self.protocol.prior_baseline.mini_resolved,
                "agentforge_admission": state.admission_count
                - self.protocol.prior_baseline.agentforge_admission,
            },
            "score_source": "official swebench.harness.run_evaluation only",
            "cost_normalized": False,
        }
        lock = self.root / ".comparison.finalize.lock"
        try:
            lock_fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except OSError:
            raise ReportingError(
                "Verified-10 comparison publication is already in progress"
            ) from None
        published: list[Path] = []
        try:
            comparison_bytes = _canonical_json(comparison)
            markdown_bytes = _comparison_markdown(comparison).encode("utf-8")
            _atomic_new(targets[0], comparison_bytes)
            published.append(targets[0])
            _atomic_new(targets[1], markdown_bytes)
            published.append(targets[1])
            artifacts = self._manifest_artifacts((*self._score_files(), targets[0], targets[1]))
            manifest = {
                "schema_version": 1,
                "protocol_sha256": self.protocol.protocol_sha256,
                "artifacts": artifacts,
            }
            # The manifest is the comparison publication commit marker.
            _atomic_new(targets[2], _canonical_json(manifest))
            published.append(targets[2])
        except Exception:
            for path in reversed(published):
                path.unlink(missing_ok=True)
            raise
        finally:
            os.close(lock_fd)
            lock.unlink(missing_ok=True)
        return ComparisonResult(*targets)

    def _load_state(self) -> CampaignState:
        try:
            state = CampaignState.model_validate_json(
                (self.root / "campaign-state.json").read_bytes()
            )
        except (OSError, ValueError, ValidationError):
            raise ReportingError("Campaign state is missing or invalid") from None
        if state.protocol_digest != self.protocol.protocol_sha256:
            raise ReportingError("Campaign state protocol digest mismatch")
        return state

    def _load_arm(
        self, arm: BenchmarkArm
    ) -> tuple[
        CampaignState,
        Path,
        Path,
        tuple[dict[str, str], ...],
        tuple[BenchmarkAttemptRecord, ...],
    ]:
        state = self._load_state()
        if arm.value not in state.finalized_arms:
            raise ReportingError("Campaign arm is not finalized")
        prediction_path = self.root / f"{arm.value.lower()}-predictions.json"
        ledger_path = self.root / f"{arm.value.lower()}-ledger.json"
        predictions = _load_predictions(prediction_path, self.protocol, arm)
        try:
            attempts = load_attempt_ledger(ledger_path)
        except (OSError, ValueError):
            raise ReportingError("Campaign attempt ledger is invalid") from None
        expected = tuple(task.instance_id for task in self.protocol.tasks)
        if (
            len(attempts) != len(expected)
            or tuple(record.instance_id for record in attempts) != expected
            or any(
                record.arm is not arm
                or record.protocol_sha256 != self.protocol.protocol_sha256
                or record.prediction_patch_sha256
                != hashlib.sha256(predictions[index]["model_patch"].encode()).hexdigest()
                for index, record in enumerate(attempts)
            )
        ):
            raise ReportingError("Campaign attempt ledger denominator is invalid")
        return state, prediction_path, ledger_path, predictions, attempts

    def _arm_report_data(self, arm: BenchmarkArm, admission: int) -> dict[str, object]:
        _, prediction_path, ledger_path, predictions, attempts = self._load_arm(arm)
        metadata_path = self.root / "official" / arm.value.lower() / "score-metadata.json"
        try:
            metadata = ScoreMetadata.model_validate_json(metadata_path.read_bytes())
        except (OSError, ValueError, ValidationError):
            raise ReportingError("Official score metadata is missing or invalid") from None
        if (
            metadata.arm is not arm
            or metadata.protocol_sha256 != self.protocol.protocol_sha256
            or metadata.predictions_sha256 != _sha256_file(prediction_path)
            or metadata.ledger_sha256 != _sha256_file(ledger_path)
        ):
            raise ReportingError("Official score artifact binding mismatch")
        report_path = _safe_relative_file(self.root, metadata.report_path)
        log_path = _safe_relative_file(self.root, metadata.log_path)
        if (
            metadata.report_sha256 != _sha256_file(report_path)
            or metadata.log_sha256 != _sha256_file(log_path)
        ):
            raise ReportingError("Official score hash binding mismatch")
        official = _load_official_report(report_path, self.protocol)
        by_official = {item.instance_id: item for item in official.instances}
        by_instance: dict[str, dict[str, object]] = {}
        for prediction, attempt in zip(predictions, attempts, strict=True):
            official_item = by_official[attempt.instance_id]
            by_instance[attempt.instance_id] = {
                "official_classification": official_item.classification,
                "official_resolved": official_item.resolved,
                "non_empty_patch": bool(prediction["model_patch"]),
                "model_calls": attempt.model_calls,
                "steps": attempt.steps,
                "provider_prompt_tokens": attempt.provider_prompt_tokens,
                "provider_completion_tokens": attempt.provider_completion_tokens,
                "provider_total_tokens": attempt.provider_total_tokens,
                "approvals": attempt.approval_count,
                "edits": attempt.edit_count,
                "tests": attempt.test_count,
                "wall_time_seconds": attempt.wall_time_seconds,
                "failure_class": attempt.failure_class.value,
                "trajectory_sha256": attempt.trajectory_sha256,
                "prediction_patch_sha256": attempt.prediction_patch_sha256,
            }
        non_empty = sum(bool(item["model_patch"]) for item in predictions)
        resolved = official.resolved_instances
        return {
            "admission": admission,
            "non_empty_patches": non_empty,
            "resolved": resolved,
            "model_calls": _sum_optional(record.model_calls for record in attempts),
            "provider_total_tokens": _sum_optional(
                record.provider_total_tokens for record in attempts
            ),
            "predictions_sha256": metadata.predictions_sha256,
            "ledger_sha256": metadata.ledger_sha256,
            "official_report_sha256": metadata.report_sha256,
            "harness_log_sha256": metadata.log_sha256,
            "evidence_gates": {
                "admission_10_of_10": admission == 10,
                "admission_all_tasks": admission == len(self.protocol.tasks),
                "non_empty_patches_gt_0": non_empty > 0,
                "resolved_gt_0": resolved > 0,
            },
            "by_instance": by_instance,
        }

    def _score_files(self) -> tuple[Path, ...]:
        paths: list[Path] = [
            self.root / "protocol.json",
            self.root / "campaign-state.json",
        ]
        for arm in BenchmarkArm:
            paths.extend(
                (
                    self.root / f"{arm.value.lower()}-predictions.json",
                    self.root / f"{arm.value.lower()}-ledger.json",
                    self.root / "official" / arm.value.lower() / "score-metadata.json",
                )
            )
            metadata = ScoreMetadata.model_validate_json(paths[-1].read_bytes())
            attempts = load_attempt_ledger(
                self.root / f"{arm.value.lower()}-ledger.json"
            )
            paths.extend(
                _safe_relative_file(self.root, attempt.trajectory_path)
                for attempt in attempts
                if attempt.trajectory_path is not None
            )
            paths.extend(
                (
                    _safe_relative_file(self.root, metadata.report_path),
                    _safe_relative_file(self.root, metadata.log_path),
                )
            )
        return tuple(paths)

    def _manifest_artifacts(self, paths: tuple[Path, ...]) -> list[dict[str, str]]:
        unique = sorted(set(paths), key=lambda item: item.relative_to(self.root).as_posix())
        return [
            {
                "path": path.relative_to(self.root).as_posix(),
                "sha256": _sha256_file(path),
            }
            for path in unique
        ]


def choose_decision(
    admission: int,
    agentforge_resolved: int,
    mini_resolved: int,
    *,
    expected_admission: int = 10,
) -> str:
    if admission != expected_admission:
        return "COMPATIBILITY_INCOMPLETE"
    if mini_resolved > agentforge_resolved and agentforge_resolved <= 1:
        return "DESIGN_REPAIR_ENGINE_MIGRATION"
    if agentforge_resolved > 0 and agentforge_resolved + 1 >= mini_resolved:
        return "KEEP_AND_IMPROVE_AGENTFORGE_LOOP"
    return "RUN_MODEL_CONTROL_EXPERIMENT"


def _load_predictions(
    path: Path, protocol: Verified10Protocol, arm: BenchmarkArm
) -> tuple[dict[str, str], ...]:
    try:
        decoded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ReportingError("Official predictions artifact is missing or invalid") from None
    expected_ids = tuple(task.instance_id for task in protocol.tasks)
    expected_model = (
        "agentforge:" if arm is BenchmarkArm.AGENTFORGE else "mini-swe-agent:"
    ) + protocol.model
    if not isinstance(decoded, list) or len(decoded) != len(expected_ids):
        raise ReportingError("Official predictions denominator must contain ten records")
    rows: list[dict[str, str]] = []
    for expected_id, value in zip(expected_ids, decoded, strict=True):
        if (
            not isinstance(value, dict)
            or set(value) != {"instance_id", "model_name_or_path", "model_patch"}
            or value.get("instance_id") != expected_id
            or value.get("model_name_or_path") != expected_model
            or not isinstance(value.get("model_patch"), str)
        ):
            raise ReportingError("Official predictions denominator is invalid")
        rows.append(
            {
                "instance_id": expected_id,
                "model_name_or_path": expected_model,
                "model_patch": cast(str, value["model_patch"]),
            }
        )
    return tuple(rows)


def _load_official_report(path: Path, protocol: Verified10Protocol) -> OfficialReport:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ReportingError("Official SWE-bench report is missing or invalid") from None
    if not isinstance(raw, dict):
        raise ReportingError("Official SWE-bench report is invalid")
    expected = tuple(task.instance_id for task in protocol.tasks)
    expected_set = set(expected)
    fields = {
        "RESOLVED": "resolved_ids",
        "UNRESOLVED": "unresolved_ids",
        "INFRA_FAILURE": "infra_failure_ids",
        "AMBIGUOUS_FAILURE": "ambiguous_failure_ids",
        "EMPTY_PATCH": "empty_patch_ids",
        "ERROR": "error_ids",
        "INCOMPLETE": "incomplete_ids",
    }
    classified: dict[str, str] = {}
    try:
        if (
            any(
                type(raw[name]) is not int
                for name in (
                    "total_instances",
                    "submitted_instances",
                    "completed_instances",
                    "resolved_instances",
                    "unresolved_instances",
                    "infra_failure_instances",
                    "ambiguous_failure_instances",
                    "empty_patch_instances",
                    "error_instances",
                )
            )
            or raw["total_instances"] != len(expected)
            or raw["submitted_instances"] != len(expected)
            or set(raw["submitted_ids"]) != expected_set
            or len(raw["submitted_ids"]) != len(expected)
        ):
            raise ValueError
        for classification, field in fields.items():
            values = raw[field]
            if not isinstance(values, list) or any(type(value) is not str for value in values):
                raise ValueError
            count_name = {
                "RESOLVED": "resolved_instances",
                "UNRESOLVED": "unresolved_instances",
                "INFRA_FAILURE": "infra_failure_instances",
                "AMBIGUOUS_FAILURE": "ambiguous_failure_instances",
                "EMPTY_PATCH": "empty_patch_instances",
                "ERROR": "error_instances",
                "INCOMPLETE": None,
            }[classification]
            if count_name is not None and raw[count_name] != len(values):
                raise ValueError
            for instance_id in values:
                if instance_id not in expected_set or instance_id in classified:
                    raise ValueError
                classified[instance_id] = classification
        if set(classified) != expected_set:
            raise ValueError
        completed = set(raw["completed_ids"])
        if completed != {
            instance_id
            for instance_id, classification in classified.items()
            if classification in {"RESOLVED", "UNRESOLVED"}
        }:
            raise ValueError
        if raw["completed_instances"] != len(completed):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise ReportingError("Official report classification denominator is invalid") from None
    instances = tuple(
        OfficialInstanceResult(
            instance_id=instance_id,
            classification=classified[instance_id],  # type: ignore[arg-type]
            resolved=classified[instance_id] == "RESOLVED",
        )
        for instance_id in expected
    )
    return OfficialReport(
        raw=raw,
        instances=instances,
        resolved_instances=sum(item.resolved for item in instances),
    )


def _safe_relative_file(root: Path, relative: str) -> Path:
    if not relative or relative.startswith(('/', '\\')) or "\\" in relative:
        raise ReportingError("Official artifact path is unsafe")
    parts = relative.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ReportingError("Official artifact path is unsafe")
    target = root.joinpath(*parts)
    try:
        if target.is_symlink() or not target.resolve(strict=True).is_file():
            raise ReportingError("Official artifact path is unavailable")
        target.resolve(strict=True).relative_to(root.resolve(strict=True))
    except (OSError, ValueError):
        raise ReportingError("Official artifact path is unavailable") from None
    return target


def _atomic_new(path: Path, payload: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() or path.is_symlink():
            raise ReportingError("Reporting artifact already exists")
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        # Link provides a no-clobber publication primitive; remove the private temp after.
        os.link(temporary, path)
        temporary.unlink()
        if os.name != "nt":
            descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    except ReportingError:
        temporary.unlink(missing_ok=True)
        raise
    except OSError:
        temporary.unlink(missing_ok=True)
        raise ReportingError("Unable to publish reporting artifact") from None


def _sha256_file(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        raise ReportingError("Unable to hash reporting artifact") from None


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _sum_optional(values: Iterable[int | None]) -> int | None:
    sequence: tuple[int | None, ...] = tuple(values)
    if any(value is None for value in sequence):
        return None
    return sum(value for value in sequence if value is not None)


def _required_int(values: dict[str, object], key: str) -> int:
    value = values[key]
    if type(value) is not int:
        raise ReportingError("Comparison aggregate is invalid")
    return value


def _comparison_markdown(comparison: dict[str, object]) -> str:
    agentforge = comparison["agentforge"]
    mini = comparison["mini_swe_agent"]
    assert isinstance(agentforge, dict) and isinstance(mini, dict)
    return "\n".join(
        (
            "# AgentForge vs mini-SWE-agent: Verified-10 Pass 1",
            "",
            f"- Protocol: `{comparison['protocol_sha256']}`",
            f"- Decision: **{comparison['decision']}**",
            "- Scoring source: official `swebench.harness.run_evaluation` only.",
            "- This comparison is paired but not cost-normalized across calls, steps, "
            "tokens, or USD.",
            "",
            "| Arm | Admission | Non-empty patches | Official resolved | Model calls | Tokens |",
            "|---|---:|---:|---:|---:|---:|",
            f"| AgentForge | {agentforge['admission']} | {agentforge['non_empty_patches']} | "
            f"{agentforge['resolved']} | {agentforge['model_calls']} | "
            f"{agentforge['provider_total_tokens']} |",
            f"| mini-SWE-agent | {mini['admission']} | {mini['non_empty_patches']} | "
            f"{mini['resolved']} | {mini['model_calls']} | {mini['provider_total_tokens']} |",
            "",
            "The JSON companion contains all ten per-instance classifications and artifact hashes.",
            "",
        )
    )
