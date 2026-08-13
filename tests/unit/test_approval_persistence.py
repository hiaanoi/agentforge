from datetime import UTC, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from agentforge.domain.errors import (
    ApprovalNotFoundError,
    CheckpointNotFoundError,
    DuplicateApprovalError,
)
from agentforge.domain.models import ApprovalRequest, Run
from agentforge.persistence.database import Database
from agentforge.persistence.repositories import (
    ApprovalRepository,
    CheckpointRepository,
    RunRepository,
)
from agentforge.persistence.run_leases import RunLeaseStore


def make_database(path: Path) -> Database:
    database = Database.from_path(path)
    database.create_schema()
    return database


def create_approval(
    database: Database,
    *,
    task: str = "approval persistence",
) -> tuple[Run, ApprovalRequest]:
    run = RunRepository(database).create(Run(task=task))
    authority = (
        RunLeaseStore(database)
        .acquire(run.run_id, owner_id=f"test:approval:{task}", ttl=timedelta(seconds=30))
        .authority
    )
    checkpoint = CheckpointRepository(database).save(
        run.run_id, 1, {"history": []}, authority=authority
    )
    approval = ApprovalRepository(database).create(
        ApprovalRequest(
            run_id=run.run_id,
            checkpoint_id=checkpoint.checkpoint_id,
            tool_name="approval_probe",
            sanitized_arguments={"value": "<redacted>"},
            request_digest="a" * 64,
        )
    )
    return run, approval


def test_approval_survives_database_reopen(tmp_path: Path) -> None:
    path = tmp_path / "approvals.sqlite3"
    database = make_database(path)
    run, approval = create_approval(database)
    database.close()

    reopened = make_database(path)
    loaded = ApprovalRepository(reopened).get(approval.approval_id)
    checkpoint = CheckpointRepository(reopened).get(approval.checkpoint_id)

    assert loaded == approval
    assert loaded.requested_at.tzinfo is UTC
    assert checkpoint.run_id == run.run_id
    reopened.close()


def test_pending_approvals_are_sorted_and_isolated_by_run(tmp_path: Path) -> None:
    database = make_database(tmp_path / "pending.sqlite3")
    first_run, first = create_approval(database, task="first")
    second_run, second = create_approval(database, task="second")
    repository = ApprovalRepository(database)

    assert [item.approval_id for item in repository.list_pending()] == [
        first.approval_id,
        second.approval_id,
    ]
    assert repository.list_pending(first_run.run_id) == [first]
    assert repository.list_for_run(second_run.run_id) == [second]
    database.close()


def test_duplicate_run_checkpoint_approval_is_rejected(tmp_path: Path) -> None:
    database = make_database(tmp_path / "duplicate.sqlite3")
    _, approval = create_approval(database)

    with pytest.raises(DuplicateApprovalError):
        ApprovalRepository(database).create(approval.model_copy(update={"approval_id": uuid4()}))
    database.close()


def test_missing_approval_and_checkpoint_raise_domain_errors(tmp_path: Path) -> None:
    database = make_database(tmp_path / "missing.sqlite3")
    missing = ApprovalRequest(
        run_id=Run(task="detached").run_id,
        checkpoint_id=Run(task="checkpoint id").run_id,
        tool_name="approval_probe",
        sanitized_arguments={},
        request_digest="a" * 64,
    )

    with pytest.raises(ApprovalNotFoundError):
        ApprovalRepository(database).get(missing.approval_id)
    with pytest.raises(CheckpointNotFoundError):
        CheckpointRepository(database).get(missing.checkpoint_id)
    database.close()
