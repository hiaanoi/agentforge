import hashlib
from datetime import timedelta
from pathlib import Path

import pytest

from agentforge.domain.errors import DuplicateMutationExecutionError
from agentforge.domain.models import ApprovalRequest, Run
from agentforge.domain.mutations import (
    MutationApprovalBinding,
    MutationExecutionRecord,
)
from agentforge.persistence.database import Database
from agentforge.persistence.mutations import (
    MutationApprovalBindingRepository,
    MutationExecutionRepository,
)
from agentforge.persistence.repositories import (
    ApprovalRepository,
    CheckpointRepository,
    RunRepository,
)
from agentforge.persistence.run_leases import RunLeaseStore

SHA_A = "a" * 64
SHA_B = "b" * 64


def create_approval(database: Database, task: str) -> ApprovalRequest:
    run = Run(task=task)
    RunRepository(database).create(run)
    authority = (
        RunLeaseStore(database)
        .acquire(run.run_id, owner_id=f"test:mutation:{task}", ttl=timedelta(seconds=30))
        .authority
    )
    checkpoint = CheckpointRepository(database).save(
        run.run_id,
        1,
        {"history": [], "resume_phase": "READY_FOR_MODEL"},
        authority=authority,
    )
    approval = ApprovalRequest(
        run_id=run.run_id,
        checkpoint_id=checkpoint.checkpoint_id,
        tool_name="write_file",
        sanitized_arguments={"path": "src/app.py", "content": "<redacted>"},
        request_digest=hashlib.sha256(str(run.run_id).encode()).hexdigest(),
    )
    ApprovalRepository(database).create(approval)
    return approval


def make_binding(approval: ApprovalRequest) -> MutationApprovalBinding:
    return MutationApprovalBinding(
        approval_id=approval.approval_id,
        run_id=approval.run_id,
        checkpoint_id=approval.checkpoint_id,
        tool_call_digest=approval.request_digest,
        tool_name=approval.tool_name,
        target_path="src/app.py",
        target_existed=True,
        before_sha256=SHA_A,
        expected_after_sha256=SHA_B,
        bytes_written=24,
    )


def make_record(binding: MutationApprovalBinding) -> MutationExecutionRecord:
    return MutationExecutionRecord(
        run_id=binding.run_id,
        approval_id=binding.approval_id,
        tool_call_digest=binding.tool_call_digest,
        tool_name=binding.tool_name,
        target_path=binding.target_path,
        before_sha256=binding.before_sha256,
        expected_after_sha256=binding.expected_after_sha256,
        before_workspace_digest=SHA_A,
        expected_after_workspace_digest=SHA_B,
    )


def test_mutation_binding_and_execution_round_trip(tmp_path: Path) -> None:
    database = Database.from_path(tmp_path / "runtime.db")
    database.create_schema()
    approval = create_approval(database, "round trip")
    binding = make_binding(approval)
    record = make_record(binding)

    binding_repository = MutationApprovalBindingRepository(database)
    execution_repository = MutationExecutionRepository(database)
    binding_repository.create(binding)
    execution_repository.create(record)

    assert binding_repository.get_for_approval(approval.approval_id) == binding
    assert execution_repository.get(record.execution_id) == record
    assert execution_repository.get_for_approval(approval.approval_id) == record


def test_mutation_execution_is_unique_per_approval_and_digest(tmp_path: Path) -> None:
    database = Database.from_path(tmp_path / "runtime.db")
    database.create_schema()
    approval = create_approval(database, "unique")
    binding = make_binding(approval)
    repository = MutationExecutionRepository(database)
    repository.create(make_record(binding))

    with pytest.raises(DuplicateMutationExecutionError):
        repository.create(make_record(binding))


def test_mutation_records_survive_restart_and_stay_run_scoped(tmp_path: Path) -> None:
    path = tmp_path / "runtime.db"
    database = Database.from_path(path)
    database.create_schema()
    first = create_approval(database, "first")
    second = create_approval(database, "second")
    binding_repository = MutationApprovalBindingRepository(database)
    execution_repository = MutationExecutionRepository(database)
    for approval in (first, second):
        binding = make_binding(approval)
        binding_repository.create(binding)
        execution_repository.create(make_record(binding))
    database.close()

    reopened = Database.from_path(path)
    reopened.create_schema()
    first_records = MutationExecutionRepository(reopened).list_for_run(first.run_id)
    second_records = MutationExecutionRepository(reopened).list_for_run(second.run_id)

    assert [record.run_id for record in first_records] == [first.run_id]
    assert [record.run_id for record in second_records] == [second.run_id]
