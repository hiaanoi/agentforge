"""Executable, durable orchestration for the frozen Verified-10 protocol."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from pydantic import ValidationError

from agentforge.application.bootstrap import (
    ProductApplicationFactory,
    ProductRuntimeDefinitionLoader,
)
from agentforge.application.config import ProductConfigLoader
from agentforge.evaluation.swebench_prediction import SWEbenchInstanceBinding, SWEbenchPrediction
from agentforge.evaluation.verified10_campaign import (
    AttemptFailureClass,
    AttemptStatus,
    BenchmarkArm,
    BenchmarkAttemptRecord,
    CampaignArtifactResult,
    CampaignExecutionError,
    Verified10Task,
    finalize_verified10_campaign,
    load_verified10_protocol,
    validate_public_task,
)
from agentforge.evaluation.verified10_support import (
    _TERMINAL_RETURN_CODES,
    _UUID_PATTERN,
    CampaignAttempt,
    CampaignCommand,
    CampaignCommandResult,
    CampaignState,
    CommandRunner,
    DockerImageBinding,
    MiniSourceVerifier,
    SourceVerifier,
    SubprocessCommandRunner,
    WorkspaceRecord,
    _relative,
    _resolve_under_root,
    agentforge_config,
    agentforge_runtime,
    mini_config,
    select_and_validate_public_rows,
    swebench_image_name,
)
from agentforge.persistence.database import Database
from agentforge.persistence.model_workflow import ModelWorkflow
from agentforge.persistence.repair_workflow import RepairWorkflow
from agentforge.persistence.repositories import ApprovalRepository, EventRepository, RunRepository


class Verified10Campaign:
    """Durable matched-arm orchestration; never performs scoring."""

    def __init__(
        self,
        protocol_path: str | Path,
        output_dir: str | Path,
        *,
        runner: CommandRunner | None = None,
        mini_source_verifier: SourceVerifier | None = None,
    ) -> None:
        try:
            self.protocol_path = Path(protocol_path).resolve(strict=True)
            self.protocol = load_verified10_protocol(self.protocol_path)
        except (OSError, ValueError):
            raise CampaignExecutionError("Unable to load campaign protocol") from None
        self.root = self._safe_root(Path(output_dir))
        self.runner = runner or SubprocessCommandRunner()
        self.mini_source_verifier = mini_source_verifier or MiniSourceVerifier()

    @staticmethod
    def _safe_root(requested: Path) -> Path:
        absolute = requested.absolute()
        if absolute == Path(absolute.anchor):
            raise CampaignExecutionError("Output directory cannot be a filesystem root")
        current = Path(absolute.anchor)
        try:
            for part in absolute.parts[1:]:
                if part in {"", ".", ".."}:
                    raise CampaignExecutionError("Output directory path is unsafe")
                current /= part
                if current.exists() or current.is_symlink():
                    metadata = current.lstat()
                    if (
                        stat.S_ISLNK(metadata.st_mode)
                        or getattr(metadata, "st_file_attributes", 0) & 0x400
                    ):
                        raise CampaignExecutionError("Output directory path is unsafe")
            return absolute
        except OSError:
            raise CampaignExecutionError("Output directory path is unsafe") from None

    @property
    def _state_path(self) -> Path:
        return self.root / "campaign-state.json"

    @property
    def _lock_path(self) -> Path:
        return self.root / ".campaign-state.lock"

    def _bootstrap_or_load(self) -> CampaignState:
        try:
            if self.root.exists() or self.root.is_symlink():
                raise CampaignExecutionError("New campaign output directory must not exist")
            self.root.parent.mkdir(parents=True, exist_ok=True)
            self.root.mkdir(exist_ok=False)
            descriptor = self._acquire_lock()
            try:
                self._atomic(self.root / "protocol.json", self.protocol_path.read_bytes())
                state = CampaignState(protocol_digest=self.protocol.protocol_digest)
                self._write_state_unlocked(state)
                return state
            finally:
                self._release_lock(descriptor)
        except CampaignExecutionError:
            raise
        except OSError:
            raise CampaignExecutionError("Unable to initialize campaign state") from None

    def _acquire_lock(self) -> int:
        try:
            return os.open(self._lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except OSError:
            raise CampaignExecutionError("Campaign state update is already in progress") from None

    def _release_lock(self, descriptor: int) -> None:
        try:
            os.close(descriptor)
        finally:
            try:
                self._lock_path.unlink(missing_ok=True)
            except OSError:
                pass

    def _load(self) -> CampaignState:
        return self._load_unlocked()

    def _load_unlocked(self) -> CampaignState:
        try:
            copied = load_verified10_protocol(self.root / "protocol.json")
            state = CampaignState.model_validate_json(self._state_path.read_bytes())
        except (OSError, UnicodeError, ValueError, ValidationError):
            raise CampaignExecutionError("Campaign state is missing or invalid") from None
        if (
            copied.protocol_digest != self.protocol.protocol_digest
            or state.protocol_digest != self.protocol.protocol_digest
        ):
            raise CampaignExecutionError("Campaign protocol digest mismatch")
        if state.prepared:
            if tuple(state.public_tasks) != tuple(task.instance_id for task in self.protocol.tasks):
                raise CampaignExecutionError("Campaign public task order is invalid")
            try:
                for task in self.protocol.tasks:
                    validate_public_task(
                        {
                            "instance_id": task.instance_id,
                            "repo": task.repo,
                            "base_commit": task.base_commit,
                            "problem_statement": state.public_tasks[task.instance_id],
                        },
                        task,
                    )
            except (KeyError, TypeError, ValueError):
                raise CampaignExecutionError("Campaign public task binding is invalid") from None
        for workspace_record in state.workspaces.values():
            _resolve_under_root(self.root, workspace_record.path)
        for records in state.attempts.values():
            for attempt_record in records:
                if attempt_record.trajectory_path is not None:
                    _resolve_under_root(self.root, attempt_record.trajectory_path)
        return state

    def _update(self, change: Callable[[CampaignState], CampaignState]) -> CampaignState:
        descriptor = self._acquire_lock()
        try:
            before = self._load_unlocked()
            after = CampaignState.model_validate(change(before).model_dump(mode="python"))
            self._write_state_unlocked(after)
            return after
        finally:
            self._release_lock(descriptor)

    def _write_state_unlocked(self, state: CampaignState) -> None:
        validated = CampaignState.model_validate(state.model_dump(mode="python"))
        self._atomic(self._state_path, validated.model_dump_json(indent=2).encode() + b"\n")

    @staticmethod
    def _atomic(path: Path, payload: bytes) -> None:
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with temporary.open("xb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            if os.name != "nt":
                descriptor = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        except OSError:
            temporary.unlink(missing_ok=True)
            raise CampaignExecutionError("Unable to write campaign artifact") from None

    def _run(self, command: CampaignCommand, *, label: str) -> CampaignCommandResult:
        try:
            result = self.runner.run(command)
        except subprocess.TimeoutExpired:
            raise CampaignExecutionError(label + " command timed out") from None
        except (OSError, subprocess.SubprocessError, TimeoutError):
            raise CampaignExecutionError(label + " command failed") from None
        if result.returncode not in command.acceptable_returncodes:
            raise CampaignExecutionError(label + " command failed")
        return result

    @staticmethod
    def _dataset_command(dataset_python: Path | None = None) -> CampaignCommand:
        script = (
            "import json; from datasets import load_dataset; "
            "d=load_dataset('princeton-nlp/SWE-bench_Verified', split='test'); "
            "print(json.dumps([{k:r[k] for k in "
            "('instance_id','repo','base_commit','problem_statement')} for r in d]))"
        )
        environment = dict(os.environ)
        environment.setdefault("HF_ENDPOINT", "https://huggingface.co")
        argv = (str(dataset_python or sys.executable), "-c", script)
        return CampaignCommand(argv, environment=environment, timeout_seconds=600)

    def _inspect_image(self, tag: str, *, pull_if_missing: bool) -> DockerImageBinding:
        inspect = CampaignCommand(
            ("docker", "image", "inspect", "--format", "{{json .RepoDigests}}", tag),
            acceptable_returncodes=frozenset(range(256)),
        )
        result = self._run(inspect, label="docker image inspect")
        if result.returncode != 0:
            message = (result.stdout + result.stderr).casefold()
            if not pull_if_missing or not any(
                term in message for term in ("not found", "no such image")
            ):
                raise CampaignExecutionError("Docker image inspect command failed")
            self._run(
                CampaignCommand(("docker", "pull", tag), timeout_seconds=1800),
                label="docker pull",
            )
            result = self._run(
                CampaignCommand(
                    ("docker", "image", "inspect", "--format", "{{json .RepoDigests}}", tag)
                ),
                label="docker image inspect",
            )
        return DockerImageBinding.from_inspect(tag, result.stdout)

    def prepare(
        self,
        harness_root: str | Path | None = None,
        dataset_python: str | Path | None = None,
    ) -> None:
        state = self._bootstrap_or_load()
        if state.prepared:
            return
        harness: Path | None = None
        if harness_root is not None:
            from agentforge.evaluation.verified10_reporting import PinnedHarnessVerifier

            harness = Path(harness_root).resolve(strict=True)
            PinnedHarnessVerifier().verify(harness)
        elif isinstance(self.runner, SubprocessCommandRunner):
            raise CampaignExecutionError("prepare requires a pinned --harness-root")
        executable: Path | None = None
        if dataset_python is not None:
            executable = Path(dataset_python).absolute()
        elif harness is not None:
            executable = (harness / ".venv" / "bin" / "python").absolute()
        if executable is not None and not executable.is_file():
            raise CampaignExecutionError("Verified dataset Python is unavailable")
        try:
            decoded = json.loads(
                self._run(self._dataset_command(executable), label="dataset").stdout
            )
            if not isinstance(decoded, list) or any(not isinstance(row, dict) for row in decoded):
                raise ValueError
            rows = select_and_validate_public_rows(decoded, self.protocol)
        except (json.JSONDecodeError, TypeError, ValueError):
            raise CampaignExecutionError("Public Verified dataset validation failed") from None
        self._run(CampaignCommand(("docker", "version", "--format", "{{json .}}")), label="docker")
        records: dict[str, WorkspaceRecord] = {}
        bindings: dict[str, DockerImageBinding] = {}
        for task in self.protocol.tasks:
            tag = swebench_image_name(task.instance_id)
            self._run(
                CampaignCommand(("docker", "pull", tag), timeout_seconds=1800),
                label="docker pull",
            )
            bindings[task.instance_id] = self._inspect_image(tag, pull_if_missing=False)
        for arm in BenchmarkArm:
            for task in self.protocol.tasks:
                binding = bindings[task.instance_id]
                workspace = self.root / "workspaces" / arm.value.lower() / task.instance_id
                if workspace.exists() or workspace.is_symlink():
                    raise CampaignExecutionError("Materialized workspace already exists")
                try:
                    workspace.parent.mkdir(parents=True, exist_ok=True)
                except OSError:
                    raise CampaignExecutionError(
                        "Workspace admission path is unavailable"
                    ) from None
                created = self._run(
                    CampaignCommand(("docker", "create", binding.digest_reference)),
                    label="docker create",
                ).stdout.strip()
                if not created:
                    raise CampaignExecutionError("Docker create returned no container")
                try:
                    self._run(
                        CampaignCommand(("docker", "cp", created + ":/testbed/.", str(workspace))),
                        label="docker copy",
                    )
                finally:
                    try:
                        self.runner.run(CampaignCommand(("docker", "rm", created)))
                    except (OSError, subprocess.SubprocessError):
                        pass
                self._run(
                    CampaignCommand(
                        ("git", "-C", str(workspace), "reset", "--hard", task.base_commit)
                    ),
                    label="git reset",
                )
                self._run(
                    CampaignCommand(("git", "-C", str(workspace), "clean", "-fd")),
                    label="git clean",
                )
                head = self._git_head(workspace)
                if head != task.base_commit:
                    raise CampaignExecutionError("Materialized workspace base commit mismatch")
                try:
                    capture = self._capture_workspace(workspace, task.instance_id)
                except (OSError, TypeError, ValueError, RuntimeError):
                    raise CampaignExecutionError("Workspace admission validation failed") from None
                records[f"{arm.value}:{task.instance_id}"] = WorkspaceRecord(
                    path=_relative(self.root, workspace),
                    image_tag=binding.tag,
                    image_digest=binding.digest_reference,
                    head=head,
                    workspace_digest=capture[0],
                    symlink_count=capture[1],
                    disk_bytes=capture[2],
                )
        statements = {row["instance_id"]: row["problem_statement"] for row in rows}

        def admit(current: CampaignState) -> CampaignState:
            if current.prepared:
                return current
            return current.model_copy(
                update={
                    "prepared": True,
                    "admission_count": 10,
                    "public_tasks": statements,
                    "workspaces": records,
                }
            )

        self._update(admit)

    def _git_head(self, workspace: Path) -> str:
        return self._run(
            CampaignCommand(("git", "-C", str(workspace), "rev-parse", "--verify", "HEAD")),
            label="git",
        ).stdout.strip()

    @staticmethod
    def _capture_workspace(workspace: Path, task_id: str) -> tuple[str, int, int]:
        from agentforge.application.product_workspace import ProductWorkspaceCapture

        capture = ProductWorkspaceCapture().capture(workspace, task_id=task_id, command_id=uuid4())
        files = capture.baseline.files
        return (
            capture.baseline.root_digest,
            sum(item.is_symlink for item in files),
            sum(item.size_bytes for item in files),
        )

    def _require_prepared(self, state: CampaignState) -> None:
        if not state.prepared or state.admission_count != 10 or len(state.workspaces) != 20:
            raise CampaignExecutionError("Campaign is not admitted; run prepare successfully first")

    def _validate_prepared_bindings(self, state: CampaignState, arm: BenchmarkArm) -> None:
        self._require_prepared(state)
        for task in self.protocol.tasks:
            record = state.workspaces[f"{arm.value}:{task.instance_id}"]
            workspace = _resolve_under_root(self.root, record.path)
            if self._git_head(workspace) != record.head or record.head != task.base_commit:
                raise CampaignExecutionError("Prepared workspace HEAD binding changed")
            digest, links, size = self._capture_workspace(workspace, task.instance_id)
            if (digest, links, size) != (
                record.workspace_digest,
                record.symlink_count,
                record.disk_bytes,
            ):
                raise CampaignExecutionError("Prepared workspace digest binding changed")
            image = self._inspect_image(record.image_tag, pull_if_missing=False)
            if image.digest_reference != record.image_digest:
                raise CampaignExecutionError("Prepared Docker image digest binding changed")

    @staticmethod
    def _records(state: CampaignState, arm: BenchmarkArm) -> dict[str, CampaignAttempt]:
        return {record.instance_id: record for record in state.attempts.get(arm.value, ())}

    def _store_attempt(self, arm: BenchmarkArm, replacement: CampaignAttempt) -> CampaignState:
        def replace(state: CampaignState) -> CampaignState:
            records = self._records(state, arm)
            existing = records.get(replacement.instance_id)
            if existing is not None and existing.status in {
                AttemptStatus.COMPLETED,
                AttemptStatus.FAILED,
            }:
                if existing != replacement:
                    raise CampaignExecutionError("Terminal campaign attempt cannot be overwritten")
                return state
            records[replacement.instance_id] = replacement
            attempts = dict(state.attempts)
            attempts[arm.value] = tuple(
                records[task.instance_id]
                for task in self.protocol.tasks
                if task.instance_id in records
            )
            return state.model_copy(update={"attempts": attempts})

        return self._update(replace)

    def run_agentforge(self, *, recover_running: bool = False, retry_failed: bool = False) -> None:
        self._run_arm(
            BenchmarkArm.AGENTFORGE,
            recover_running=recover_running,
            retry_failed=retry_failed,
            mini_root=None,
        )

    def run_mini(
        self,
        *,
        mini_root: str | Path,
        recover_running: bool = False,
        retry_failed: bool = False,
    ) -> None:
        state = self._load()
        self._require_prepared(state)
        root = Path(mini_root).absolute()
        self.mini_source_verifier.verify(root)
        self._run_arm(
            BenchmarkArm.MINI_SWE_AGENT,
            recover_running=recover_running,
            retry_failed=retry_failed,
            mini_root=root,
        )

    def _run_arm(
        self,
        arm: BenchmarkArm,
        *,
        recover_running: bool,
        retry_failed: bool,
        mini_root: Path | None,
    ) -> None:
        if retry_failed:
            raise CampaignExecutionError("Protocol permits one attempt; --retry-failed is invalid")
        state = self._load()
        self._validate_prepared_bindings(state, arm)
        for task in self.protocol.tasks:
            state = self._load()
            existing = self._records(state, arm).get(task.instance_id)
            if existing is not None and existing.status in {
                AttemptStatus.COMPLETED,
                AttemptStatus.FAILED,
            }:
                continue
            if existing is not None and existing.status is AttemptStatus.RUNNING:
                if not recover_running:
                    raise CampaignExecutionError("RUNNING attempt requires --recover-running")
                if arm is BenchmarkArm.MINI_SWE_AGENT or existing.run_id is None:
                    self._store_attempt(
                        arm,
                        existing.model_copy(
                            update={
                                "status": AttemptStatus.FAILED,
                                "failure_class": AttemptFailureClass.INTERRUPTED,
                                "terminal_reason": "INTERRUPTED_NO_DURABLE_RESUME",
                            }
                        ),
                    )
                    continue
                try:
                    attempt = self._recover_agentforge(task, state, existing)
                except CampaignExecutionError as exc:
                    attempt = self._preserve_failed_agentforge(
                        task, existing, str(exc), time.monotonic()
                    )
                self._store_attempt(arm, attempt)
                continue
            running = CampaignAttempt(
                instance_id=task.instance_id,
                attempt_index=1,
                status=AttemptStatus.RUNNING,
            )
            self._store_attempt(arm, running)
            started = time.monotonic()
            try:
                attempt = self._execute_attempt(arm, task, self._load(), mini_root, started)
            except CampaignExecutionError as exc:
                durable = self._records(self._load(), arm).get(task.instance_id)
                if (
                    arm is BenchmarkArm.AGENTFORGE
                    and durable is not None
                    and durable.run_id is not None
                ):
                    attempt = self._preserve_failed_agentforge(task, durable, str(exc), started)
                else:
                    attempt = CampaignAttempt(
                        instance_id=task.instance_id,
                        attempt_index=1,
                        status=AttemptStatus.FAILED,
                        failure_class=self._classify_failure(str(exc)),
                        terminal_reason=str(exc),
                        wall_time_seconds=max(0.0, time.monotonic() - started),
                        telemetry_unavailable=("model_usage", "repair_counters", "events"),
                    )
            self._store_attempt(arm, attempt)

    @staticmethod
    def _classify_failure(message: str) -> AttemptFailureClass:
        lowered = message.casefold()
        if "timed out" in lowered or "timeout" in lowered:
            return AttemptFailureClass.TIMEOUT
        if "policy" in lowered or "approval" in lowered:
            return AttemptFailureClass.POLICY_FAILED
        if "model" in lowered or "provider" in lowered:
            return AttemptFailureClass.MODEL_FAILED
        if "config" in lowered or "runtime" in lowered or "compat" in lowered:
            return AttemptFailureClass.COMPATIBILITY_FAILED
        return AttemptFailureClass.INFRASTRUCTURE_FAILED

    def _execute_attempt(
        self,
        arm: BenchmarkArm,
        task: Verified10Task,
        state: CampaignState,
        mini_root: Path | None,
        started: float,
    ) -> CampaignAttempt:
        workspace_record = state.workspaces[f"{arm.value}:{task.instance_id}"]
        workspace = _resolve_under_root(self.root, workspace_record.path)
        if arm is BenchmarkArm.MINI_SWE_AGENT:
            if mini_root is None:
                raise CampaignExecutionError("mini root is required")
            return self._execute_mini(task, mini_root, workspace_record, started)
        return self._execute_agentforge(
            task, workspace, state.public_tasks[task.instance_id], started
        )

    _mini_config = staticmethod(mini_config)

    def _execute_mini(
        self,
        task: Verified10Task,
        mini_root: Path,
        workspace_record: WorkspaceRecord,
        started: float,
    ) -> CampaignAttempt:
        secret = os.environ.get("DEEPSEEK_API_KEY")
        if not secret:
            raise CampaignExecutionError("Mini model credential is unavailable")
        output = self.root / "mini-output" / task.instance_id
        output.mkdir(parents=True, exist_ok=True)
        config = self.root / "configs" / "mini" / f"{task.instance_id}.yaml"
        self._atomic(config, self._mini_config().encode())
        builtin = mini_root / "src" / "minisweagent" / "config" / "benchmarks" / "swebench.yaml"
        environment = dict(os.environ)
        environment["OPENAI_API_KEY"] = secret
        pattern = "^" + re.escape(task.instance_id) + "$"
        command = CampaignCommand(
            (
                "uv",
                "run",
                "--project",
                str(mini_root),
                "--frozen",
                "python",
                "-m",
                "minisweagent.run.benchmarks.swebench",
                "--subset",
                "verified",
                "--split",
                "test",
                "--filter",
                pattern,
                "--output",
                str(output),
                "--workers",
                "1",
                "--model",
                "openai/deepseek-v4-flash",
                "--config",
                str(builtin),
                "--config",
                str(config),
            ),
            environment=environment,
            timeout_seconds=1800,
            acceptable_returncodes=frozenset(range(256)),
        )
        result = self._run(command, label="mini")
        trajectory = output / task.instance_id / f"{task.instance_id}.traj.json"
        attempt = self._parse_mini_result(task, output, trajectory, result, started)
        image = self._inspect_image(workspace_record.image_tag, pull_if_missing=False)
        if image.digest_reference != workspace_record.image_digest:
            return attempt.model_copy(
                update={
                    "status": AttemptStatus.FAILED,
                    "failure_class": AttemptFailureClass.INFRASTRUCTURE_FAILED,
                    "terminal_reason": "Prepared Docker image digest changed during mini run",
                }
            )
        return attempt

    def _parse_mini_result(
        self,
        task: Verified10Task,
        output: Path,
        trajectory: Path,
        result: CampaignCommandResult,
        started: float,
    ) -> CampaignAttempt:
        result_failure: AttemptFailureClass | None = None
        result_reason: str | None = None
        patch = ""
        try:
            predictions = json.loads((output / "preds.json").read_text(encoding="utf-8"))
            if not isinstance(predictions, dict) or set(predictions) != {task.instance_id}:
                raise ValueError("predictions must contain exactly the expected instance")
            item = predictions[task.instance_id]
            if not isinstance(item, dict) or set(item) != {
                "model_name_or_path",
                "instance_id",
                "model_patch",
            }:
                raise ValueError("prediction record shape is invalid")
            if item.get("instance_id") != task.instance_id:
                raise ValueError("prediction instance does not match")
            patch = item["model_patch"]
            if (
                not isinstance(patch, str)
                or item.get("model_name_or_path") != "openai/deepseek-v4-flash"
            ):
                raise ValueError("prediction model identity is invalid")
        except FileNotFoundError:
            result_failure = AttemptFailureClass.INFRASTRUCTURE_FAILED
            result_reason = "mini predictions artifact is missing"
        except (OSError, UnicodeError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            result_failure = AttemptFailureClass.PROTOCOL_FAILED
            result_reason = "mini predictions artifact is malformed"
        info: Mapping[str, object] = {}
        try:
            decoded = json.loads(trajectory.read_text(encoding="utf-8"))
            if not isinstance(decoded, dict) or decoded.get("instance_id") != task.instance_id:
                raise ValueError("trajectory instance does not match")
            raw_info = decoded.get("info")
            messages = decoded.get("messages")
            if not isinstance(raw_info, dict) or not isinstance(messages, list):
                raise ValueError("trajectory shape is invalid")
            user_contents = [
                message.get("content")
                for message in messages
                if isinstance(message, dict) and message.get("role") == "user"
            ]
            if not user_contents or not isinstance(user_contents[0], str):
                raise ValueError("trajectory user prompt is missing")
            match = re.search(
                r"Consider the following PR description:\n(.*?)\n</pr_description>",
                user_contents[0],
                re.DOTALL,
            )
            if match is None:
                raise ValueError("trajectory public task prompt is malformed")
            validate_public_task(
                {
                    "instance_id": task.instance_id,
                    "repo": task.repo,
                    "base_commit": task.base_commit,
                    "problem_statement": match.group(1),
                },
                task,
            )
            info = raw_info
        except FileNotFoundError:
            result_failure = result_failure or AttemptFailureClass.INFRASTRUCTURE_FAILED
            result_reason = result_reason or "mini trajectory artifact is missing"
        except (OSError, UnicodeError, TypeError, ValueError, json.JSONDecodeError):
            result_failure = AttemptFailureClass.PROTOCOL_FAILED
            result_reason = "mini trajectory artifact is malformed or input binding drifted"
        exit_status = info.get("exit_status")
        stats = info.get("model_stats") if isinstance(info.get("model_stats"), dict) else {}
        assert isinstance(stats, dict)
        model_calls = _nonnegative_int(stats.get("api_calls"))
        prompt = _first_int(stats, "prompt_tokens", "input_tokens")
        completion = _first_int(stats, "completion_tokens", "output_tokens")
        total = _first_int(stats, "total_tokens")
        terminal_ok = (
            result_failure is None
            and result.returncode == 0
            and isinstance(exit_status, str)
            and exit_status.casefold()
            in {
                "submitted",
                "completed",
                "success",
            }
        )
        if terminal_ok:
            failure = AttemptFailureClass.NONE if patch else AttemptFailureClass.EMPTY
        elif result_failure is not None:
            failure = result_failure
        elif result.returncode == 0 and isinstance(exit_status, str):
            failure = (
                AttemptFailureClass.TIMEOUT
                if "timeout" in exit_status.casefold()
                else AttemptFailureClass.MODEL_FAILED
            )
        else:
            failure = self._classify_failure(str(exit_status or result.stderr))
        unavailable = (
            "steps",
            *(
                name
                for name, value in (
                    ("provider_prompt_tokens", prompt),
                    ("provider_completion_tokens", completion),
                    ("provider_total_tokens", total),
                )
                if value is None
            ),
        )
        return self._terminal_attempt(
            task.instance_id,
            patch,
            trajectory,
            status=AttemptStatus.COMPLETED if terminal_ok else AttemptStatus.FAILED,
            failure=failure,
            reason=result_reason
            or (str(exit_status) if exit_status is not None else f"returncode={result.returncode}"),
            model_calls=model_calls,
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=total,
            wall=max(0.0, time.monotonic() - started),
            unavailable=unavailable,
        )

    _agentforge_config = staticmethod(agentforge_config)
    _agentforge_runtime = staticmethod(agentforge_runtime)

    def _preflight_agentforge(self, workspace: Path, task: Verified10Task) -> None:
        directory = workspace / ".agentforge"
        directory.mkdir(exist_ok=True)
        self._atomic(directory / "config.toml", self._agentforge_config().encode())
        self._atomic(
            directory / "runtime.toml", self._agentforge_runtime(task.instance_id).encode()
        )
        try:
            loader = ProductConfigLoader(user_root=self.root / ".no-user-config")
            config = loader.load(
                workspace,
                cli={
                    "database_path": ".agentforge/agentforge.db",
                    "model": "deepseek-v4-flash",
                    "max_steps": 80,
                    "profile_ids": ("compile", "verify"),
                },
            )
            ProductRuntimeDefinitionLoader().load(workspace, config=config)
            database = Database.from_path(config.database_path)
            try:
                database.create_schema()
            finally:
                database.close()
            report = (
                ProductApplicationFactory().build_doctor(workspace, config=config).report(workspace)
            )
            if not report.ready:
                failures = {check.check for check in report.checks if check.status.value == "FAIL"}
                if failures:
                    raise CampaignExecutionError("AgentForge doctor preflight failed")
        except CampaignExecutionError:
            raise
        except Exception:
            raise CampaignExecutionError(
                "AgentForge generated configuration is incompatible"
            ) from None

    @staticmethod
    def _af_common(workspace: Path) -> tuple[str, ...]:
        return (
            "--workspace",
            str(workspace),
            "--database-path",
            ".agentforge/agentforge.db",
            "--model",
            "deepseek-v4-flash",
            "--max-steps",
            "80",
            "--profile-id",
            "compile",
            "--profile-id",
            "verify",
        )

    def _execute_agentforge(
        self, task: Verified10Task, workspace: Path, problem_statement: str, started: float
    ) -> CampaignAttempt:
        self._preflight_agentforge(workspace, task)
        common = self._af_common(workspace)
        for profile, purpose in (("compile", "development"), ("verify", "verification")):
            self._run(
                CampaignCommand(
                    (
                        "uv",
                        "run",
                        "--frozen",
                        "agentforge",
                        "trust",
                        *common,
                        "--purpose",
                        purpose,
                        "--yes",
                        profile,
                    )
                ),
                label="agentforge trust",
            )
        result = self._run(
            CampaignCommand(
                (
                    "uv",
                    "run",
                    "--frozen",
                    "agentforge",
                    "exec",
                    *common,
                    problem_statement,
                ),
                timeout_seconds=1800,
                acceptable_returncodes=_TERMINAL_RETURN_CODES,
            ),
            label="agentforge exec",
        )
        run_id = self._run_id(result.stdout + result.stderr)
        if run_id is None:
            raise CampaignExecutionError("AgentForge did not report a durable run_id")
        running = CampaignAttempt(
            instance_id=task.instance_id,
            attempt_index=1,
            status=AttemptStatus.RUNNING,
            run_id=run_id,
        )
        self._store_attempt(BenchmarkArm.AGENTFORGE, running)
        final, approval_count, transcript = self._drive_agentforge(
            workspace, run_id, result, started
        )
        return self._finish_agentforge(
            task, workspace, run_id, final, approval_count, transcript, started
        )

    def _recover_agentforge(
        self, task: Verified10Task, state: CampaignState, existing: CampaignAttempt
    ) -> CampaignAttempt:
        assert existing.run_id is not None
        workspace = _resolve_under_root(
            self.root, state.workspaces[f"{BenchmarkArm.AGENTFORGE.value}:{task.instance_id}"].path
        )
        started = time.monotonic()
        inspect = self._inspect_run(workspace, existing.run_id, export=False)
        final, approvals, transcript = self._drive_agentforge(
            workspace, existing.run_id, inspect, started
        )
        return self._finish_agentforge(
            task, workspace, existing.run_id, final, approvals, transcript, started
        )

    def _preserve_failed_agentforge(
        self,
        task: Verified10Task,
        existing: CampaignAttempt,
        reason: str,
        started: float,
    ) -> CampaignAttempt:
        assert existing.run_id is not None
        state = self._load()
        workspace = _resolve_under_root(
            self.root,
            state.workspaces[f"{BenchmarkArm.AGENTFORGE.value}:{task.instance_id}"].path,
        )
        transcript = reason + "\n"
        for export in (False, True):
            try:
                inspected = self._inspect_run(workspace, existing.run_id, export=export)
                transcript += inspected.stdout + inspected.stderr
            except CampaignExecutionError:
                transcript += "inspect_unavailable=true\n"
        try:
            patch = self._git_patch(workspace, task)
        except CampaignExecutionError:
            patch = ""
        try:
            telemetry = self._agentforge_telemetry(workspace, existing.run_id)
        except Exception:
            telemetry = {
                "unavailable": (
                    "run_lifecycle",
                    "repair_counters",
                    "model_usage",
                    "approvals",
                    "events",
                )
            }
        trajectory = self.root / "trajectories" / "agentforge" / f"{task.instance_id}.log"
        try:
            self._atomic(trajectory, transcript.encode("utf-8", errors="replace"))
        except CampaignExecutionError:
            pass
        return self._terminal_attempt(
            task.instance_id,
            patch,
            trajectory,
            status=AttemptStatus.FAILED,
            failure=self._classify_failure(reason),
            reason=str(telemetry.get("terminal_reason") or reason),
            run_id=existing.run_id,
            model_calls=_as_int(telemetry.get("model_calls")),
            steps=_as_int(telemetry.get("steps")),
            prompt_tokens=_as_int(telemetry.get("prompt_tokens")),
            completion_tokens=_as_int(telemetry.get("completion_tokens")),
            total_tokens=_as_int(telemetry.get("total_tokens")),
            approval_count=_as_int(telemetry.get("approval_count")),
            edit_count=_as_int(telemetry.get("edit_count")),
            test_count=_as_int(telemetry.get("test_count")),
            event_count=_as_int(telemetry.get("event_count")),
            wall=max(0.0, time.monotonic() - started),
            unavailable=_string_tuple(telemetry.get("unavailable")),
        )

    def _drive_agentforge(
        self,
        workspace: Path,
        run_id: str,
        latest: CampaignCommandResult,
        started: float,
    ) -> tuple[CampaignCommandResult, int, str]:
        common = self._af_common(workspace)
        approval_count = 0
        transcript = latest.stdout + latest.stderr
        for _ in range(160):
            if time.monotonic() - started >= 1800:
                raise CampaignExecutionError("AgentForge wall timeout reached")
            inspection = self._inspect_run(workspace, run_id, export=False)
            transcript += inspection.stdout + inspection.stderr
            lifecycle = self._field(inspection.stdout, "lifecycle")
            if lifecycle == "TERMINAL":
                return inspection, approval_count, transcript
            approvals = self._run(
                CampaignCommand(
                    (
                        "uv",
                        "run",
                        "--frozen",
                        "agentforge",
                        "approvals",
                        *common,
                        "--run-id",
                        run_id,
                    )
                ),
                label="agentforge approvals",
            )
            ids = tuple(dict.fromkeys(re.findall(r"approval_id=([0-9a-f-]{36})", approvals.stdout)))
            for approval_id in ids:
                UUID(approval_id)
                self._run(
                    CampaignCommand(
                        (
                            "uv",
                            "run",
                            "--frozen",
                            "agentforge",
                            "approve",
                            *common,
                            approval_id,
                        )
                    ),
                    label="agentforge approve",
                )
                approval_count += 1
            resumed = self._run(
                CampaignCommand(
                    (
                        "uv",
                        "run",
                        "--frozen",
                        "agentforge",
                        "resume",
                        *common,
                        run_id,
                    ),
                    timeout_seconds=max(1, 1800 - int(time.monotonic() - started)),
                    acceptable_returncodes=_TERMINAL_RETURN_CODES,
                ),
                label="agentforge resume",
            )
            transcript += approvals.stdout + approvals.stderr + resumed.stdout + resumed.stderr
        raise CampaignExecutionError("AgentForge durable loop exceeded its bound")

    def _inspect_run(self, workspace: Path, run_id: str, *, export: bool) -> CampaignCommandResult:
        arguments = (
            "uv",
            "run",
            "--frozen",
            "agentforge",
            "inspect",
            *self._af_common(workspace),
            *(("--export",) if export else ()),
            run_id,
        )
        return self._run(CampaignCommand(arguments), label="agentforge inspect")

    def _finish_agentforge(
        self,
        task: Verified10Task,
        workspace: Path,
        run_id: str,
        final: CampaignCommandResult,
        approval_count: int,
        transcript: str,
        started: float,
    ) -> CampaignAttempt:
        exported = self._inspect_run(workspace, run_id, export=True)
        transcript += exported.stdout + exported.stderr
        trajectory = self.root / "trajectories" / "agentforge" / f"{task.instance_id}.log"
        self._atomic(trajectory, transcript.encode("utf-8", errors="replace"))
        patch = self._git_patch(workspace, task)
        telemetry = self._agentforge_telemetry(workspace, run_id)
        lifecycle = self._field(final.stdout, "lifecycle") or telemetry.get("lifecycle")
        outcome = self._field(final.stdout, "outcome") or telemetry.get("outcome")
        terminal_reason = telemetry.get("terminal_reason") or outcome or lifecycle or "UNKNOWN"
        completed = lifecycle == "TERMINAL" and outcome in {"VERIFIED", "UNVERIFIED"}
        failure = (
            AttemptFailureClass.NONE
            if completed and patch
            else (
                AttemptFailureClass.EMPTY
                if completed
                else (
                    AttemptFailureClass.RUNTIME_FAILED
                    if outcome == "UNKNOWN"
                    else self._classify_failure(str(terminal_reason))
                )
            )
        )
        return self._terminal_attempt(
            task.instance_id,
            patch,
            trajectory,
            status=AttemptStatus.COMPLETED if completed else AttemptStatus.FAILED,
            failure=failure,
            reason=str(terminal_reason),
            run_id=run_id,
            model_calls=_as_int(telemetry.get("model_calls")),
            steps=_as_int(telemetry.get("steps")),
            prompt_tokens=_as_int(telemetry.get("prompt_tokens")),
            completion_tokens=_as_int(telemetry.get("completion_tokens")),
            total_tokens=_as_int(telemetry.get("total_tokens")),
            approval_count=_as_int(telemetry.get("approval_count")) or approval_count,
            edit_count=_as_int(telemetry.get("edit_count")),
            test_count=_as_int(telemetry.get("test_count")),
            event_count=_as_int(telemetry.get("event_count")),
            wall=max(0.0, time.monotonic() - started),
            unavailable=_string_tuple(telemetry.get("unavailable")),
        )

    @staticmethod
    def _field(text: str, name: str) -> str | None:
        match = re.search(r"(?:^|\s)" + re.escape(name) + r"=([^\s]+)", text)
        return match.group(1) if match else None

    @staticmethod
    def _run_id(text: str) -> str | None:
        match = re.search(r"run_id=([0-9a-f-]{36})", text, re.IGNORECASE)
        if match is None or _UUID_PATTERN.fullmatch(match.group(1)) is None:
            return None
        try:
            return str(UUID(match.group(1)))
        except ValueError:
            return None

    def _git_patch(self, workspace: Path, task: Verified10Task) -> str:
        from agentforge.evaluation.swebench_prediction import SWEbenchPredictionExporter

        binding = SWEbenchInstanceBinding(
            instance_id=task.instance_id, repo=task.repo, base_commit=task.base_commit
        )
        try:
            prediction = SWEbenchPredictionExporter().capture(
                workspace,
                binding=binding,
                model_identity=self.protocol.model,
                excluded_untracked_prefixes=(".agentforge",),
            )
            return prediction.model_patch
        except Exception as exc:
            if "empty" in str(exc).casefold():
                return ""
            raise CampaignExecutionError("AgentForge patch export failed") from None

    @staticmethod
    def _agentforge_telemetry(workspace: Path, run_id: str) -> dict[str, object]:
        database = Database.read_only_from_path(workspace / ".agentforge" / "agentforge.db")
        unavailable: list[str] = []
        values: dict[str, object] = {}
        identity = UUID(run_id)
        try:
            run = RunRepository(database).get(identity)
            values.update(
                lifecycle=run.status.value,
                steps=run.current_step,
            )
        except Exception:
            unavailable.append("run_lifecycle")
        try:
            repair = RepairWorkflow(database).get_state(identity)
            values.update(
                terminal_reason=repair.failure_reason.value if repair.failure_reason else None,
                edit_count=repair.edit_attempts_used,
                test_count=repair.test_runs_used,
            )
        except Exception:
            unavailable.append("repair_counters")
        try:
            model = ModelWorkflow(database).get_state(identity)
            values.update(
                model_calls=model.model_request_count,
                prompt_tokens=model.input_tokens,
                completion_tokens=model.output_tokens,
                total_tokens=model.total_tokens,
            )
        except Exception:
            unavailable.append("model_usage")
        try:
            values["approval_count"] = len(ApprovalRepository(database).list_for_run(identity))
        except Exception:
            unavailable.append("approvals")
        try:
            values["event_count"] = len(EventRepository(database).list_for_run(identity))
        except Exception:
            unavailable.append("events")
        finally:
            database.close()
        values["unavailable"] = tuple(unavailable)
        return values

    def _terminal_attempt(
        self,
        instance_id: str,
        patch: str,
        trajectory: Path,
        *,
        status: AttemptStatus,
        failure: AttemptFailureClass,
        reason: str,
        run_id: str | None = None,
        model_calls: int | None = None,
        steps: int | None = None,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        total_tokens: int | None = None,
        approval_count: int | None = None,
        edit_count: int | None = None,
        test_count: int | None = None,
        event_count: int | None = None,
        wall: float | None = None,
        unavailable: tuple[str, ...] = (),
    ) -> CampaignAttempt:
        raw = trajectory.read_bytes() if trajectory.is_file() else b""
        return CampaignAttempt(
            instance_id=instance_id,
            attempt_index=1,
            status=status,
            failure_class=failure,
            model_patch=patch,
            run_id=run_id,
            terminal_reason=reason,
            model_calls=model_calls,
            steps=steps,
            provider_prompt_tokens=prompt_tokens,
            provider_completion_tokens=completion_tokens,
            provider_total_tokens=total_tokens,
            approval_count=approval_count,
            edit_count=edit_count,
            test_count=test_count,
            event_count=event_count,
            wall_time_seconds=wall,
            trajectory_path=_relative(self.root, trajectory) if trajectory.exists() else None,
            trajectory_sha256=hashlib.sha256(raw).hexdigest() if trajectory.exists() else None,
            telemetry_unavailable=unavailable,
        )

    def status(self) -> dict[str, dict[str, int]]:
        state = self._load()
        result: dict[str, dict[str, int]] = {}
        for arm in BenchmarkArm:
            counts = {
                "planned": 0,
                "running": 0,
                "completed": 0,
                "failed": 0,
                "admission": state.admission_count,
                "telemetry_unavailable": 0,
            }
            records = state.attempts.get(arm.value, ())
            for record in records:
                counts[record.status.value.lower()] += 1
                if record.telemetry_unavailable:
                    counts["telemetry_unavailable"] += 1
            counts["planned"] = 10 - counts["running"] - counts["completed"] - counts["failed"]
            result[arm.value] = counts
        return result

    def preflight_summary(self) -> dict[str, str]:
        state = self._load()
        self._require_prepared(state)
        return {
            "agentforge_admission": f"{state.admission_count}/10",
            "safe_symlink_rejections": "0",
            "protocol_sha256": self.protocol.protocol_sha256,
        }

    def finalize_predictions(self, arm: BenchmarkArm) -> CampaignArtifactResult:
        state = self._load()
        self._require_prepared(state)
        if arm.value in state.finalized_arms:
            raise CampaignExecutionError("Predictions for this arm are already finalized")
        records = self._records(state, arm)
        expected = {task.instance_id for task in self.protocol.tasks}
        if set(records) != expected or any(
            record.status not in {AttemptStatus.COMPLETED, AttemptStatus.FAILED}
            for record in records.values()
        ):
            raise CampaignExecutionError("Finalization requires ten terminal attempts")
        namespace: Literal["agentforge", "mini-swe-agent"] = (
            "agentforge" if arm is BenchmarkArm.AGENTFORGE else "mini-swe-agent"
        )
        predictions: list[SWEbenchPrediction] = []
        attempts: list[BenchmarkAttemptRecord] = []
        for task in self.protocol.tasks:
            record = records[task.instance_id]
            binding = SWEbenchInstanceBinding(
                instance_id=task.instance_id, repo=task.repo, base_commit=task.base_commit
            )
            if record.model_patch:
                prediction = SWEbenchPrediction(
                    instance_id=task.instance_id,
                    model_name_or_path=f"{namespace}:{self.protocol.model}",
                    model_patch=record.model_patch,
                    base_commit=task.base_commit,
                    patch_sha256=hashlib.sha256(record.model_patch.encode()).hexdigest(),
                )
            else:
                prediction = SWEbenchPrediction.empty(
                    binding, self.protocol.model, namespace=namespace
                )
            predictions.append(prediction)
            attempts.append(
                BenchmarkAttemptRecord(
                    protocol_sha256=self.protocol.protocol_sha256,
                    arm=arm,
                    instance_id=task.instance_id,
                    attempt_index=1,
                    status=record.status,
                    failure_class=record.failure_class,
                    model_calls=record.model_calls,
                    steps=record.steps,
                    provider_prompt_tokens=record.provider_prompt_tokens,
                    provider_completion_tokens=record.provider_completion_tokens,
                    provider_total_tokens=record.provider_total_tokens,
                    approval_count=record.approval_count,
                    edit_count=record.edit_count,
                    test_count=record.test_count,
                    event_count=record.event_count,
                    wall_time_seconds=record.wall_time_seconds,
                    terminal_reason=record.terminal_reason,
                    telemetry_unavailable=record.telemetry_unavailable,
                    provider_capabilities=record.provider_capabilities,
                    trajectory_path=record.trajectory_path,
                    trajectory_sha256=record.trajectory_sha256,
                    prediction_patch_sha256=prediction.patch_sha256,
                )
            )
        result = finalize_verified10_campaign(
            self.protocol,
            attempts,
            predictions,
            self.root / f"{arm.value.lower()}-predictions.json",
            self.root / f"{arm.value.lower()}-ledger.json",
            arm=arm,
        )

        def mark_final(current: CampaignState) -> CampaignState:
            if arm.value in current.finalized_arms:
                raise CampaignExecutionError("Predictions for this arm are already finalized")
            return current.model_copy(
                update={"finalized_arms": (*current.finalized_arms, arm.value)}
            )

        self._update(mark_final)
        return result

    def score(self, arm: BenchmarkArm, harness_root: str | Path) -> object:
        from agentforge.evaluation.verified10_reporting import Verified10Reporting

        return Verified10Reporting(
            self.protocol_path,
            self.root,
            runner=self.runner,
        ).score(arm, harness_root)

    def report(self) -> object:
        from agentforge.evaluation.verified10_reporting import Verified10Reporting

        return Verified10Reporting(
            self.protocol_path,
            self.root,
            runner=self.runner,
        ).report()


def _nonnegative_int(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _first_int(values: Mapping[str, object], *names: str) -> int | None:
    for name in names:
        found = _nonnegative_int(values.get(name))
        if found is not None:
            return found
    return None


def _as_int(value: object) -> int | None:
    return _nonnegative_int(value)


def _string_tuple(value: object) -> tuple[str, ...]:
    if isinstance(value, (list, tuple)) and all(isinstance(item, str) for item in value):
        return tuple(value)
    return ()


# Complete the legacy import surface when this module was imported first and
# therefore caused the protocol module to skip its eager compatibility import.
_protocol_module = sys.modules["agentforge.evaluation.verified10_campaign"]

_protocol_module.CampaignCommand = CampaignCommand  # type: ignore[attr-defined]
_protocol_module.CampaignCommandResult = CampaignCommandResult  # type: ignore[attr-defined]
_protocol_module.CampaignState = CampaignState  # type: ignore[attr-defined]
_protocol_module.CommandRunner = CommandRunner  # type: ignore[attr-defined]
_protocol_module.MiniSourceVerifier = MiniSourceVerifier  # type: ignore[attr-defined]
_protocol_module.SubprocessCommandRunner = SubprocessCommandRunner  # type: ignore[attr-defined]
_protocol_module.Verified10Campaign = Verified10Campaign  # type: ignore[attr-defined]
