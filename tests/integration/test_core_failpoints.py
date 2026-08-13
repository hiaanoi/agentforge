from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from agentforge.application.approval_commands import DecideApprovalCommand
from agentforge.application.kernel_errors import StaleFenceError
from agentforge.domain.enums import (
    ApprovalStatus,
    EventType,
    RunStatus,
)
from agentforge.domain.errors import ApprovalDecisionConflictError
from agentforge.domain.models import ApprovalRequest, Run, utc_now
from agentforge.domain.mutations import MutationApprovalBinding
from agentforge.persistence.approval_workflow import ApprovalWorkflow
from agentforge.persistence.database import Database
from agentforge.persistence.event_log import RunLeaseAuthority
from agentforge.persistence.mutation_workflow import MutationWorkflow
from agentforge.persistence.mutations import MutationApprovalBindingRepository
from agentforge.persistence.product_tables import RunLeaseRow, WorkspaceSourceBindingRow
from agentforge.persistence.repositories import (
    ApprovalRepository,
    CheckpointRepository,
    EventRepository,
    RunRepository,
)
from agentforge.persistence.run_leases import RunLeaseStore
from agentforge.persistence.source_revisions import MutationRecoveryAction, WorkspaceDigester
from agentforge.testing.failpoints import FailpointController

CRASH_CODE = 86
SHA = "a" * 64


class CoreFailpointCase(StrEnum):
    APPROVAL_COMMITTED_BEFORE_ACK = "approval_committed_before_ack"
    MUTATION_WRITING = "mutation_writing"
    LEASE_TAKEOVER_STALE_WRITER = "lease_takeover_stale_writer"


@dataclass(frozen=True)
class RecoveryFacts:
    actual_classification: str
    expected_classification: str
    side_effect_count: int
    stale_write_count: int
    boundary_count: int = 0


class RecordingFailpoints:
    def __init__(self) -> None:
        self.hits: list[str] = []

    def hit(self, name: str) -> None:
        self.hits.append(name)


class HardExitAt:
    """Exit at the boundary without exception unwinding or cleanup."""

    def __init__(self, target: str, exit_code: int) -> None:
        self._target = target
        self._exit_code = exit_code

    def hit(self, name: str) -> None:
        if name == self._target:
            os._exit(self._exit_code)


def _authority(value: str) -> RunLeaseAuthority:
    payload = json.loads(value)
    return RunLeaseAuthority(
        run_id=UUID(payload["run_id"]),
        owner_id=payload["owner_id"],
        lease_token=UUID(payload["lease_token"]),
        fencing_token=payload["fencing_token"],
        version=payload["version"],
    )


def _worker(case: CoreFailpointCase, state: dict[str, str]) -> int:
    database = Database.from_path(Path(state["database_path"]))
    crash: FailpointController = HardExitAt(case.value, CRASH_CODE)
    if case is CoreFailpointCase.APPROVAL_COMMITTED_BEFORE_ACK:
        command = DecideApprovalCommand.model_validate_json(state["command"])
        ApprovalWorkflow(database, failpoints=crash).resolve_command(command)
    elif case is CoreFailpointCase.MUTATION_WRITING:
        MutationWorkflow(database, failpoints=crash).claim_resume(
            UUID(state["run_id"]),
            UUID(state["approval_id"]),
            actual_workspace_digest=state["before_digest"],
            workspace_root_identity=state["workspace_root"],
            authority=_authority(state["authority"]),
        )
    else:
        ready = Path(state["ready_path"])
        go = Path(state["go_path"])
        ready.write_text("ready", encoding="utf-8")
        deadline = time.monotonic() + 10
        while not go.exists():
            if time.monotonic() >= deadline:
                return 87
            time.sleep(0.01)
        run_id = UUID(state["run_id"])
        run = RunRepository(database).get(run_id)
        Path(state["attempt_path"]).write_text("1", encoding="utf-8")
        run.current_step += 1
        try:
            RunRepository(database).save(run, authority=_authority(state["authority"]))
        except StaleFenceError:
            os._exit(CRASH_CODE)
    return 0


class CrashHarness:
    expected_crash_code = CRASH_CODE

    def __init__(self, root: Path) -> None:
        self.root = root
        self.state: dict[str, str] = {}

    @staticmethod
    def _authority_json(authority: RunLeaseAuthority) -> str:
        return json.dumps(
            {
                "run_id": str(authority.run_id),
                "owner_id": authority.owner_id,
                "lease_token": str(authority.lease_token),
                "fencing_token": authority.fencing_token,
                "version": authority.version,
            }
        )

    def run_worker(self, case: CoreFailpointCase) -> subprocess.CompletedProcess[str]:
        self.state = self._prepare(case)
        command = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--core-worker",
            case.value,
            json.dumps(self.state),
        ]
        if case is not CoreFailpointCase.LEASE_TAKEOVER_STALE_WRITER:
            return subprocess.run(command, text=True, capture_output=True, timeout=20)
        process = subprocess.Popen(
            command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        ready = Path(self.state["ready_path"])
        deadline = time.monotonic() + 10
        while not ready.exists():
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
            if time.monotonic() >= deadline:
                process.kill()
                stdout, stderr = process.communicate()
                return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
            time.sleep(0.01)
        database = Database.from_path(Path(self.state["database_path"]))
        try:
            with database.session() as session:
                row = session.get(RunLeaseRow, self.state["run_id"])
                assert row is not None
                row.acquired_at -= timedelta(seconds=40)
                row.heartbeat_at -= timedelta(seconds=35)
                row.expires_at -= timedelta(seconds=31)
            RunLeaseStore(database).acquire(
                UUID(self.state["run_id"]), owner_id="replacement", ttl=timedelta(seconds=30)
            )
        finally:
            database.close()
        Path(self.state["go_path"]).write_text("go", encoding="utf-8")
        stdout, stderr = process.communicate(timeout=20)
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)

    def recreate_and_recover(self, case: CoreFailpointCase) -> RecoveryFacts:
        database = Database.from_path(Path(self.state["database_path"]))
        try:
            if case is CoreFailpointCase.APPROVAL_COMMITTED_BEFORE_ACK:
                command = DecideApprovalCommand.model_validate_json(self.state["command"])
                replayed = ApprovalWorkflow(database).resolve_command(command)
                events = EventRepository(database).list_for_run(UUID(self.state["run_id"]))
                count = sum(event.event_type is EventType.APPROVAL_GRANTED for event in events)
                return RecoveryFacts(
                    replayed.status.value,
                    ApprovalStatus.APPROVED.value,
                    count,
                    0,
                    boundary_count=count,
                )
            if case is CoreFailpointCase.MUTATION_WRITING:
                action = MutationWorkflow(database).recover_writing(
                    UUID(self.state["run_id"]),
                    UUID(self.state["approval_id"]),
                    actual_workspace_digest=self.state["before_digest"],
                    workspace_root_identity=self.state["workspace_root"],
                    authority=_authority(self.state["authority"]),
                )
                events = EventRepository(database).list_for_run(UUID(self.state["run_id"]))
                boundary_count = sum(
                    event.event_type is EventType.MUTATION_STARTED for event in events
                )
                side_effect_count = int((Path(self.state["workspace_root"]) / "new.txt").exists())
                return RecoveryFacts(
                    action.value,
                    MutationRecoveryAction.RETRY.value,
                    side_effect_count,
                    0,
                    boundary_count=boundary_count,
                )
            row = RunRepository(database).get(UUID(self.state["run_id"]))
            attempt_count = int(Path(self.state["attempt_path"]).read_text(encoding="utf-8"))
            return RecoveryFacts(
                "STALE_FENCED",
                "STALE_FENCED",
                attempt_count,
                row.current_step,
                boundary_count=attempt_count,
            )
        finally:
            database.close()

    def _prepare(self, case: CoreFailpointCase) -> dict[str, str]:
        database_path = self.root / f"{case.value}.db"
        database = Database.from_path(database_path)
        database.create_schema()
        try:
            run = RunRepository(database).create(Run(task=case.value))
            lease = RunLeaseStore(database).acquire(
                run.run_id, owner_id="setup", ttl=timedelta(seconds=30)
            )
            if case is CoreFailpointCase.LEASE_TAKEOVER_STALE_WRITER:
                return {
                    "database_path": str(database_path),
                    "run_id": str(run.run_id),
                    "authority": self._authority_json(lease.authority),
                    "ready_path": str(self.root / "ready"),
                    "go_path": str(self.root / "go"),
                    "attempt_path": str(self.root / "attempt"),
                    "cleanup_path": str(self.root / "cleanup"),
                }
            run.transition_to(RunStatus.RUNNING)
            RunRepository(database).save(run, authority=lease.authority)
            checkpoint = CheckpointRepository(database).save(
                run.run_id, 1, {"phase": case.value}, authority=lease.authority
            )
            approval = ApprovalRepository(database).create(
                ApprovalRequest(
                    run_id=run.run_id,
                    checkpoint_id=checkpoint.checkpoint_id,
                    tool_name="write_file"
                    if case is CoreFailpointCase.MUTATION_WRITING
                    else "probe",
                    sanitized_arguments={},
                    request_digest=SHA,
                )
            )
            if case is CoreFailpointCase.MUTATION_WRITING:
                workspace = self.root / "workspace"
                workspace.mkdir()
                before = WorkspaceDigester().digest(workspace)
                now = utc_now()
                with database.session() as session:
                    session.add(
                        WorkspaceSourceBindingRow(
                            run_id=str(run.run_id),
                            workspace_root_identity=str(workspace.resolve()),
                            git_head=None,
                            initial_source_digest=before,
                            expected_source_digest=before,
                            source_revision_number=0,
                            digest_algorithm_version=1,
                            config_digest="d" * 64,
                            profile_digest="e" * 64,
                            created_at=now,
                            updated_at=now,
                        )
                    )
                MutationApprovalBindingRepository(database).create(
                    MutationApprovalBinding(
                        approval_id=approval.approval_id,
                        run_id=run.run_id,
                        checkpoint_id=checkpoint.checkpoint_id,
                        tool_call_digest=approval.request_digest,
                        tool_name="write_file",
                        target_path="new.txt",
                        target_existed=False,
                        before_sha256=None,
                        expected_after_sha256="b" * 64,
                        bytes_written=1,
                    )
                )
            run.transition_to(RunStatus.WAITING_APPROVAL)
            RunRepository(database).save(run, authority=lease.authority)
            RunLeaseStore(database).release(lease.authority)
            command = DecideApprovalCommand(
                command_id=uuid4(), approval_id=approval.approval_id, status=ApprovalStatus.APPROVED
            )
            if case is CoreFailpointCase.APPROVAL_COMMITTED_BEFORE_ACK:
                return {
                    "database_path": str(database_path),
                    "run_id": str(run.run_id),
                    "command": command.model_dump_json(),
                    "cleanup_path": str(self.root / "cleanup"),
                }
            ApprovalWorkflow(database).resolve_command(command)
            mutation_lease = RunLeaseStore(database).acquire(
                run.run_id, owner_id="mutation-worker", ttl=timedelta(seconds=30)
            )
            before = WorkspaceDigester().digest(workspace)
            MutationWorkflow(database).ensure_prepared(
                approval.approval_id,
                before_workspace_digest=before,
                expected_after_workspace_digest="c" * 64,
                authority=mutation_lease.authority,
            )
            return {
                "database_path": str(database_path),
                "run_id": str(run.run_id),
                "approval_id": str(approval.approval_id),
                "before_digest": before,
                "workspace_root": str(workspace.resolve()),
                "authority": self._authority_json(mutation_lease.authority),
                "cleanup_path": str(self.root / "cleanup"),
            }
        finally:
            database.close()


@pytest.fixture
def crash_harness(tmp_path: Path) -> CrashHarness:
    return CrashHarness(tmp_path)


@pytest.mark.parametrize("case", list(CoreFailpointCase))
def test_core_failpoint_has_expected_facts(
    case: CoreFailpointCase, crash_harness: CrashHarness
) -> None:
    crashed = crash_harness.run_worker(case)
    assert crashed.returncode == crash_harness.expected_crash_code, crashed.stderr
    assert not Path(crash_harness.state["cleanup_path"]).exists()
    result = crash_harness.recreate_and_recover(case)
    assert result.actual_classification == result.expected_classification
    assert result.side_effect_count <= 1
    assert result.stale_write_count == 0
    assert result.boundary_count <= 1


def test_failed_approval_cas_does_not_hit_commit_before_ack_failpoint(
    tmp_path: Path,
) -> None:
    harness = CrashHarness(tmp_path)
    state = harness._prepare(CoreFailpointCase.APPROVAL_COMMITTED_BEFORE_ACK)
    database = Database.from_path(Path(state["database_path"]))
    try:
        approved = DecideApprovalCommand.model_validate_json(state["command"])
        ApprovalWorkflow(database).resolve_command(approved)
        rejected = DecideApprovalCommand(
            command_id=uuid4(),
            approval_id=approved.approval_id,
            status=ApprovalStatus.REJECTED,
        )
        recorder = RecordingFailpoints()

        with pytest.raises(ApprovalDecisionConflictError):
            ApprovalWorkflow(database, failpoints=recorder).resolve_command(rejected)

        assert recorder.hits == []
        same_decision = DecideApprovalCommand(
            command_id=uuid4(),
            approval_id=approved.approval_id,
            status=ApprovalStatus.APPROVED,
        )
        ApprovalWorkflow(database, failpoints=recorder).resolve_command(same_decision)
        assert recorder.hits == []
    finally:
        database.close()


if __name__ == "__main__" and len(sys.argv) == 4 and sys.argv[1] == "--core-worker":
    os._exit(_worker(CoreFailpointCase(sys.argv[2]), json.loads(sys.argv[3])))
