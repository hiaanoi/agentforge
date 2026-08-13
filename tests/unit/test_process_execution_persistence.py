from datetime import timedelta
from pathlib import Path

import pytest

from agentforge.domain.errors import DuplicateProcessExecutionError
from agentforge.domain.models import ApprovalRequest, Run
from agentforge.domain.test_execution import (
    ProcessExecutionRecord,
)
from agentforge.domain.test_execution import (
    TestApprovalBinding as ApprovalBinding,
)
from agentforge.persistence.database import Database
from agentforge.persistence.repositories import (
    ApprovalRepository,
    CheckpointRepository,
    RunRepository,
)
from agentforge.persistence.run_leases import RunLeaseStore
from agentforge.persistence.test_executions import (
    ProcessExecutionRepository,
)
from agentforge.persistence.test_executions import (
    TestApprovalBindingRepository as BindingRepository,
)

SHA = "a" * 64


def create_binding(database: Database, run: Run, suffix: str) -> ApprovalBinding:
    lease = RunLeaseStore(database).acquire(
        run.run_id,
        owner_id=f"test:process:{suffix}",
        ttl=timedelta(seconds=30),
    )
    checkpoint = CheckpointRepository(database).save(
        run.run_id,
        1,
        {"phase": suffix},
        authority=lease.authority,
    )
    RunLeaseStore(database).release(lease.authority)
    approval = ApprovalRepository(database).create(
        ApprovalRequest(
            run_id=run.run_id,
            checkpoint_id=checkpoint.checkpoint_id,
            tool_name="run_tests",
            sanitized_arguments={"profile_id": "unit_tests"},
            request_digest=(suffix * 64)[:64],
        )
    )
    return ApprovalBinding(
        approval_id=approval.approval_id,
        run_id=run.run_id,
        checkpoint_id=checkpoint.checkpoint_id,
        tool_call_digest=approval.request_digest,
        profile_id="unit_tests",
        profile_version=1,
        profile_digest=SHA,
        executable_path="C:\\Python\\python.exe",
        argv_digest=SHA,
        cwd="C:\\workspace",
        environment_digest=SHA,
    )


def record_for(binding: ApprovalBinding, attempt: int) -> ProcessExecutionRecord:
    return ProcessExecutionRecord(
        run_id=binding.run_id,
        approval_id=binding.approval_id,
        tool_call_digest=binding.tool_call_digest,
        attempt_number=attempt,
        profile_id=binding.profile_id,
        profile_version=binding.profile_version,
        profile_digest=binding.profile_digest,
        executable_path=binding.executable_path,
        argv_digest=binding.argv_digest,
        cwd=binding.cwd,
        environment_digest=binding.environment_digest,
    )


def test_binding_and_execution_round_trip_after_database_recreation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "runtime.db"
    database = Database.from_path(path)
    database.create_schema()
    run = RunRepository(database).create(Run(task="test persistence"))
    binding = create_binding(database, run, "b")
    BindingRepository(database).create(binding)
    record = ProcessExecutionRepository(database).create(record_for(binding, 1))
    database.close()

    reopened = Database.from_path(path)
    reopened.create_schema()
    loaded_binding = BindingRepository(reopened).get_for_approval(binding.approval_id)
    loaded_record = ProcessExecutionRepository(reopened).get(record.execution_id)

    assert loaded_binding == binding
    assert loaded_record == record
    assert loaded_record.tool_call_digest == binding.tool_call_digest
    assert loaded_record.record_version == 1
    assert loaded_record.result_schema_version == 1


def test_execution_uniqueness_covers_approval_digest_and_run_attempt(
    tmp_path: Path,
) -> None:
    database = Database.from_path(tmp_path / "runtime.db")
    database.create_schema()
    run = RunRepository(database).create(Run(task="unique attempts"))
    first = create_binding(database, run, "b")
    second = create_binding(database, run, "c")
    bindings = BindingRepository(database)
    bindings.create(first)
    bindings.create(second)
    executions = ProcessExecutionRepository(database)
    executions.create(record_for(first, 1))

    with pytest.raises(DuplicateProcessExecutionError):
        executions.create(record_for(first, 2))
    with pytest.raises(DuplicateProcessExecutionError):
        executions.create(record_for(second, 1))


def test_execution_listing_is_ordered_and_isolated_by_run(tmp_path: Path) -> None:
    database = Database.from_path(tmp_path / "runtime.db")
    database.create_schema()
    runs = RunRepository(database)
    run_a = runs.create(Run(task="run a"))
    run_b = runs.create(Run(task="run b"))
    binding_a1 = create_binding(database, run_a, "b")
    binding_a2 = create_binding(database, run_a, "c")
    binding_b1 = create_binding(database, run_b, "d")
    bindings = BindingRepository(database)
    executions = ProcessExecutionRepository(database)
    for binding in (binding_a1, binding_a2, binding_b1):
        bindings.create(binding)
    record_a1 = executions.create(record_for(binding_a1, 1))
    record_a2 = executions.create(record_for(binding_a2, 2))
    executions.create(record_for(binding_b1, 1))

    assert executions.list_for_run(run_a.run_id) == [record_a1, record_a2]
    assert executions.get_for_approval(binding_a2.approval_id) == record_a2
    assert bindings.find_for_approval(binding_b1.approval_id) == binding_b1
