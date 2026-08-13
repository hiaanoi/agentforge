from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import MetaData, Table, inspect, select

from agentforge.application.kernel_errors import StaleFenceError
from agentforge.domain.enums import (
    EventType,
    ProcessExecutionStatus,
)
from agentforge.domain.models import Run
from agentforge.domain.repair import (
    BudgetKind,
    RepairCompletionStatus,
    RepairTerminationReason,
)
from agentforge.evaluation.validators import DiffValidationResult
from agentforge.models.base import ModelRequest
from agentforge.persistence.approval_workflow import ApprovalWorkflow
from agentforge.persistence.database import Database
from agentforge.persistence.event_log import EventLog, RunLeaseAuthority
from agentforge.persistence.model_workflow import ModelWorkflow
from agentforge.persistence.mutation_workflow import MutationWorkflow
from agentforge.persistence.repair_workflow import RepairWorkflow
from agentforge.persistence.repositories import CheckpointRepository, RunRepository
from agentforge.persistence.run_leases import RunLeaseStore
from agentforge.persistence.source_revisions import SourceRevisionStore
from agentforge.persistence.test_execution_workflow import (
    TestExecutionWorkflow as ProcessWorkflow,
)

WriteCall = Callable[[Database, Run, RunLeaseAuthority], None]


def _run_write(database: Database, run: Run, authority: RunLeaseAuthority) -> None:
    RunRepository(database).save(run, authority=authority)


def _event_write(database: Database, run: Run, authority: RunLeaseAuthority) -> None:
    with database.session() as session:
        EventLog().append(session, authority, EventType.RUN_STARTED, {})


def _checkpoint_write(
    database: Database, run: Run, authority: RunLeaseAuthority
) -> None:
    CheckpointRepository(database).save(
        run.run_id, 1, {"phase": "stale"}, authority=authority
    )


def _approval_write(
    database: Database, run: Run, authority: RunLeaseAuthority
) -> None:
    ApprovalWorkflow(database).claim_resume(
        run.run_id, uuid4(), authority=authority
    )


def _mutation_claim(
    database: Database, run: Run, authority: RunLeaseAuthority
) -> None:
    MutationWorkflow(database).claim_resume(
        run.run_id,
        uuid4(),
        actual_workspace_digest="a" * 64,
        authority=authority,
    )


def _mutation_finalize(
    database: Database, run: Run, authority: RunLeaseAuthority
) -> None:
    MutationWorkflow(database).mark_indeterminate(
        run.run_id, uuid4(), "stale", authority=authority
    )


def _process_claim(
    database: Database, run: Run, authority: RunLeaseAuthority
) -> None:
    ProcessWorkflow(database).claim_resume(
        run.run_id, uuid4(), authority=authority
    )


def _process_finalize(
    database: Database, run: Run, authority: RunLeaseAuthority
) -> None:
    ProcessWorkflow(database).finish(
        run.run_id,
        uuid4(),
        expected_version=1,
        status=ProcessExecutionStatus.COMPLETED,
        failure_kind=None,
        exit_code=0,
        stdout_digest="a" * 64,
        stderr_digest="b" * 64,
        stdout_size=0,
        stderr_size=0,
        stdout_summary="",
        stderr_summary="",
        stdout_truncated=False,
        stderr_truncated=False,
        duration_ms=0,
        termination_reason=None,
        termination_result=None,
        authority=authority,
    )


def _process_unknown(
    database: Database, run: Run, authority: RunLeaseAuthority
) -> None:
    ProcessWorkflow(database).mark_indeterminate(
        run.run_id, uuid4(), reason="stale", authority=authority
    )


def _model_budget_attempt(
    database: Database, run: Run, authority: RunLeaseAuthority
) -> None:
    ModelWorkflow(database).prepare_attempt(
        run.run_id,
        uuid4(),
        1,
        request=ModelRequest(task=run.task, step_number=1),
        provider_identity="authority-matrix/model",
        authority=authority,
    )


def _model_retry(
    database: Database, run: Run, authority: RunLeaseAuthority
) -> None:
    ModelWorkflow(database).record_retry(
        run.run_id, 1, 0.0, authority=authority
    )


def _repair_state(
    database: Database, run: Run, authority: RunLeaseAuthority
) -> None:
    RepairWorkflow(database).mark_final_answer_received(
        run.run_id, 1, authority=authority
    )


def _repair_budget(
    database: Database, run: Run, authority: RunLeaseAuthority
) -> None:
    RepairWorkflow(database).consume_budget(
        run.run_id, BudgetKind.MODEL, "stale", authority=authority
    )


def _repair_diff(
    database: Database, run: Run, authority: RunLeaseAuthority
) -> None:
    result = DiffValidationResult(
        compliant=True,
        baseline_digest="a" * 64,
        final_workspace_digest="b" * 64,
        diff_digest="c" * 64,
        modified_files=(),
        created_files=(),
        deleted_files=(),
        renamed_files=(),
        type_changed_files=(),
        changed_file_count=0,
        total_changed_bytes=0,
        violations=(),
        suspicious_findings=(),
    )
    RepairWorkflow(database).record_diff_validation(
        run.run_id, result, authority=authority
    )


def _repair_final(
    database: Database, run: Run, authority: RunLeaseAuthority
) -> None:
    RepairWorkflow(database).transition_terminal(
        run.run_id,
        expected_version=1,
        status=RepairCompletionStatus.RUNTIME_FAILURE,
        reason=RepairTerminationReason.RUNTIME_FAILURE,
        authority=authority,
    )


def _source_revision(
    database: Database, run: Run, authority: RunLeaseAuthority
) -> None:
    with database.session() as session:
        SourceRevisionStore().advance(
            session,
            run.run_id,
            before_digest="a" * 64,
            expected_after_digest="b" * 64,
            expected_revision_number=0,
            authority=authority,
        )


_WRITE_PATHS: tuple[tuple[str, WriteCall], ...] = (
    ("run", _run_write),
    ("event", _event_write),
    ("checkpoint", _checkpoint_write),
    ("approval", _approval_write),
    ("mutation_claim", _mutation_claim),
    ("mutation_finalize", _mutation_finalize),
    ("process_claim", _process_claim),
    ("process_finalize", _process_finalize),
    ("process_unknown", _process_unknown),
    ("model_budget_attempt", _model_budget_attempt),
    ("model_retry", _model_retry),
    ("repair_state", _repair_state),
    ("repair_budget", _repair_budget),
    ("repair_diff", _repair_diff),
    ("repair_final", _repair_final),
    ("source_revision", _source_revision),
)


def _business_fingerprint(
    database: Database,
) -> tuple[tuple[str, tuple[tuple[object, ...], ...]], ...]:
    ignored = {"run_leases"}
    with database.session() as session:
        connection = session.connection()
        names = sorted(set(inspect(connection).get_table_names()) - ignored)
        values: list[tuple[str, tuple[tuple[object, ...], ...]]] = []
        for name in names:
            table = Table(name, MetaData(), autoload_with=connection)
            ordering = tuple(table.primary_key.columns) or tuple(table.columns)
            rows = session.execute(select(table).order_by(*ordering)).all()
            values.append((name, tuple(tuple(row) for row in rows)))
        return tuple(values)


@pytest.mark.parametrize(
    ("write_path", "invoke"),
    _WRITE_PATHS,
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_every_owned_write_rejects_stale_fence_without_business_changes(
    tmp_path: Path,
    write_path: str,
    invoke: WriteCall,
) -> None:
    database = Database.from_path(tmp_path / f"{write_path}.sqlite3")
    database.create_schema()
    run = RunRepository(database).create(Run(task=f"stale {write_path}"))
    leases = RunLeaseStore(database)
    stale = leases.acquire(
        run.run_id, owner_id="old-owner", ttl=timedelta(seconds=30)
    )
    leases.release(stale.authority)
    replacement = leases.acquire(
        run.run_id, owner_id="new-owner", ttl=timedelta(seconds=30)
    )
    assert replacement.fencing_token == stale.fencing_token + 1
    before = _business_fingerprint(database)

    with pytest.raises(StaleFenceError):
        invoke(database, run, stale.authority)

    assert _business_fingerprint(database) == before
    database.close()
