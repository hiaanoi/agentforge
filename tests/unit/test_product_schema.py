from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import UniqueConstraint, inspect, select
from sqlalchemy.exc import IntegrityError, StatementError

from agentforge.application.contracts import (
    LifecycleStatus,
    OutcomeStatus,
    ReceiptStatus,
    RunControlRequestStatus,
    RunControlRequestType,
)
from agentforge.application.kernel_errors import IncompatibleProductSchemaError
from agentforge.domain.models import Run
from agentforge.persistence.database import Database
from agentforge.persistence.product_tables import (
    PRODUCT_SCHEMA_VERSION,
    ApplicationCommandReceiptRow,
    ConversationRow,
    ProductSchemaVersionRow,
    RunControlRequestRow,
    RunLeaseRow,
    TrustedProfileRow,
    WorkspaceSourceBindingRow,
)
from agentforge.persistence.repositories import RunRepository
from agentforge.persistence.tables import MutationExecutionRow, ProcessExecutionRow, RunRow

NOW = datetime(2026, 8, 10, tzinfo=UTC)
DIGEST = "a" * 64

# Captured and frozen from the SQLAlchemy table shape at 2963eb5^ (product v3).
V3_MUTATION_EXECUTIONS_DDL = """CREATE TABLE mutation_executions (
    execution_id VARCHAR(36) NOT NULL,
    run_id VARCHAR(36) NOT NULL,
    approval_id VARCHAR(36) NOT NULL,
    tool_call_digest VARCHAR(64) NOT NULL,
    tool_name VARCHAR(100) NOT NULL,
    target_path TEXT NOT NULL,
    before_sha256 VARCHAR(64),
    expected_after_sha256 VARCHAR(64) NOT NULL,
    actual_after_sha256 VARCHAR(64),
    bytes_written INTEGER NOT NULL,
    status VARCHAR(32) NOT NULL,
    result_summary VARCHAR(500),
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    PRIMARY KEY (execution_id),
    UNIQUE (approval_id),
    UNIQUE (tool_call_digest),
    FOREIGN KEY(run_id) REFERENCES runs (run_id) ON DELETE CASCADE,
    FOREIGN KEY(approval_id) REFERENCES approval_requests (approval_id) ON DELETE CASCADE
)"""
V3_MUTATION_INDEX_DDL = (
    "CREATE INDEX ix_mutation_executions_run_id ON mutation_executions (run_id)",
    "CREATE INDEX ix_mutation_executions_status ON mutation_executions (status)",
    "CREATE INDEX ix_mutation_run_created ON mutation_executions (run_id, created_at)",
)


def _uuid() -> str:
    return str(uuid4())


def _run(run_id: str) -> RunRow:
    return RunRow(
        run_id=run_id,
        task="schema invariant probe",
        status="PENDING",
        current_step=0,
        max_steps=1,
        tool_call_count=0,
        max_tool_calls=1,
        model_provider="test",
        created_at=NOW,
        updated_at=NOW,
        total_token_usage=0,
        estimated_cost=0.0,
        error_message=None,
        final_output=None,
    )


def _receipt(command_id: str, **overrides: object) -> ApplicationCommandReceiptRow:
    values: dict[str, object] = {
        "command_id": command_id,
        "command_type": "OPEN_COMMAND_NAME",
        "request_digest": DIGEST,
        "status": ReceiptStatus.ACCEPTED.value,
        "result_scope_type": None,
        "result_scope_id": None,
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(overrides)
    return ApplicationCommandReceiptRow(**values)


def _assert_product_database_reopens(path: Path) -> None:
    reopened = Database.from_path(path)
    reopened.validate_product_schema()
    reopened.close()


def _column_contract(row_type: type[object]) -> dict[str, tuple[bool, bool]]:
    return {
        column.name: (column.nullable, column.primary_key)
        for column in row_type.__table__.columns  # type: ignore[attr-defined]
    }


def _unique_column_sets(row_type: type[object]) -> set[tuple[str, ...]]:
    table = row_type.__table__  # type: ignore[attr-defined]
    return {
        tuple(column.name for column in constraint.columns)
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }


def test_product_contract_enums_are_closed_and_stable() -> None:
    assert PRODUCT_SCHEMA_VERSION == 9
    assert [status.value for status in ReceiptStatus] == [
        "ACCEPTED",
        "IN_PROGRESS",
        "COMPLETED",
        "FAILED",
        "INDETERMINATE",
    ]
    assert [status.value for status in LifecycleStatus] == [
        "CREATED",
        "RUNNING",
        "PAUSED",
        "TERMINAL",
    ]
    assert [status.value for status in OutcomeStatus] == [
        "VERIFIED",
        "UNVERIFIED",
        "FAILED",
        "UNKNOWN",
    ]
    assert [kind.value for kind in RunControlRequestType] == ["CANCEL"]
    assert [status.value for status in RunControlRequestStatus] == [
        "REQUESTED",
        "CANCELLED",
        "INDETERMINATE",
    ]


def test_product_rows_have_the_approved_columns_and_nullability() -> None:
    assert _column_contract(ProductSchemaVersionRow) == {
        "singleton_id": (False, True),
        "version": (False, False),
    }
    assert _column_contract(ConversationRow) == {
        "conversation_id": (False, True),
        "next_ordinal": (False, False),
        "version": (False, False),
    }
    assert _column_contract(ApplicationCommandReceiptRow) == {
        "command_id": (False, True),
        "command_type": (False, False),
        "request_digest": (False, False),
        "status": (False, False),
        "result_scope_type": (True, False),
        "result_scope_id": (True, False),
        "created_at": (False, False),
        "updated_at": (False, False),
    }
    assert _column_contract(RunLeaseRow) == {
        "run_id": (False, True),
        "owner_id": (False, False),
        "lease_token": (False, False),
        "fencing_token": (False, False),
        "version": (False, False),
        "acquired_at": (False, False),
        "heartbeat_at": (False, False),
        "expires_at": (False, False),
        "released_at": (True, False),
    }
    assert _column_contract(WorkspaceSourceBindingRow) == {
        "run_id": (False, True),
        "workspace_root_identity": (False, False),
        "git_head": (True, False),
        "initial_source_digest": (False, False),
        "expected_source_digest": (False, False),
        "source_revision_number": (False, False),
        "digest_algorithm_version": (False, False),
        "config_digest": (False, False),
        "profile_digest": (False, False),
        "created_at": (False, False),
        "updated_at": (False, False),
    }
    assert _column_contract(TrustedProfileRow) == {
        "trust_id": (False, True),
        "workspace_identity": (False, False),
        "profile_id": (False, False),
        "profile_version": (False, False),
        "profile_digest": (False, False),
        "executable_digest": (False, False),
        "argv_digest": (False, False),
        "cwd_identity": (False, False),
        "config_source_digest": (False, False),
        "purpose": (False, False),
        "enabled_at": (False, False),
        "disabled_at": (True, False),
    }
    assert _column_contract(RunControlRequestRow) == {
        "control_request_id": (False, True),
        "run_id": (False, False),
        "command_id": (False, False),
        "request_type": (False, False),
        "status": (False, False),
        "requested_at": (False, False),
        "updated_at": (False, False),
    }
    mutation_columns = _column_contract(MutationExecutionRow)
    assert mutation_columns["before_workspace_digest"] == (False, False)
    assert mutation_columns["expected_after_workspace_digest"] == (False, False)
    assert _column_contract(ProcessExecutionRow)["source_digest_at_start"] == (
        True,
        False,
    )
    process_columns = _column_contract(ProcessExecutionRow)
    for name in (
        "verification_capsule_id",
        "capsule_state",
        "source_snapshot_digest",
        "verifier_artifact_digest",
        "executable_artifact_digest",
        "artifact_algorithm_version",
        "runtime_trust_class",
    ):
        assert process_columns[name] == (True, False)


def test_real_v8_capsule_topology_is_rejected_without_ddl(tmp_path: Path) -> None:
    path = tmp_path / "real-v8.sqlite3"
    database = Database.from_path(path)
    database.create_schema()
    RunRepository(database).create(Run(task="preserve real v8 topology"))
    database.close()
    new_markers = {
        "verification_capsule_id VARCHAR(36)",
        "capsule_state VARCHAR(16)",
        "source_snapshot_digest VARCHAR(64)",
        "verifier_artifact_digest VARCHAR(64)",
        "executable_artifact_digest VARCHAR(64)",
        "artifact_algorithm_version INTEGER",
        "runtime_trust_class VARCHAR(32)",
        "ck_process_execution_capsule_topology",
    }
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("PRAGMA legacy_alter_table=ON")
        current = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='process_executions'"
        ).fetchone()[0]
        historical = "\n".join(
            line
            for line in current.splitlines()
            if not any(marker in line for marker in new_markers)
        )
        indexes = [
            row[0]
            for row in connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' "
                "AND tbl_name='process_executions' AND sql IS NOT NULL ORDER BY name"
            )
        ]
        connection.execute(
            "ALTER TABLE process_executions RENAME TO process_executions_v9"
        )
        connection.execute(historical)
        columns = [
            row[1] for row in connection.execute("PRAGMA table_info(process_executions)")
        ]
        common = ", ".join(columns)
        connection.execute(
            f"INSERT INTO process_executions ({common}) "
            f"SELECT {common} FROM process_executions_v9"
        )
        connection.execute("DROP TABLE process_executions_v9")
        for index_sql in indexes:
            connection.execute(index_sql)
        connection.execute("UPDATE product_schema_version SET version=8")
        before_master = list(
            connection.execute(
                "SELECT type, name, sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            )
        )
        before_runs = connection.execute(
            "SELECT run_id, task, status FROM runs ORDER BY run_id"
        ).fetchall()

    reopened = Database.from_path(path)
    with pytest.raises(IncompatibleProductSchemaError):
        reopened.validate_product_schema()
    with pytest.raises(IncompatibleProductSchemaError):
        reopened.create_schema()
    reopened.close()
    with sqlite3.connect(path) as connection:
        after_master = list(
            connection.execute(
                "SELECT type, name, sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            )
        )
        after_runs = connection.execute(
            "SELECT run_id, task, status FROM runs ORDER BY run_id"
        ).fetchall()
        version = connection.execute(
            "SELECT singleton_id, version FROM product_schema_version"
        ).fetchall()
    assert after_master == before_master
    assert after_runs == before_runs
    assert version == [(1, 8)]


def test_real_v7_process_topology_is_rejected_without_ddl(tmp_path: Path) -> None:
    path = tmp_path / "real-v7.sqlite3"
    database = Database.from_path(path)
    database.create_schema()
    RunRepository(database).create(Run(task="preserve real v7 topology"))
    database.close()
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("PRAGMA legacy_alter_table=ON")
        current = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='process_executions'"
        ).fetchone()[0]
        v8_and_v9_markers = {
            "source_digest_at_start",
            "ck_process_execution_start_digest_length",
            "verification_capsule_id",
            "capsule_state",
            "source_snapshot_digest",
            "verifier_artifact_digest",
            "executable_artifact_digest",
            "artifact_algorithm_version",
            "runtime_trust_class",
            "ck_process_execution_capsule_topology",
        }
        historical = "\n".join(
            line
            for line in current.splitlines()
            if not any(marker in line for marker in v8_and_v9_markers)
        )
        indexes = [
            row[0]
            for row in connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' "
                "AND tbl_name='process_executions' AND sql IS NOT NULL ORDER BY name"
            )
        ]
        connection.execute(
            "ALTER TABLE process_executions RENAME TO process_executions_v8"
        )
        connection.execute(historical)
        columns = [
            row[1] for row in connection.execute("PRAGMA table_info(process_executions)")
        ]
        common = ", ".join(columns)
        connection.execute(
            f"INSERT INTO process_executions ({common}) "
            f"SELECT {common} FROM process_executions_v8"
        )
        connection.execute("DROP TABLE process_executions_v8")
        for index_sql in indexes:
            connection.execute(index_sql)
        connection.execute("UPDATE product_schema_version SET version=7")
        assert "source_digest_at_start" not in columns
        before_master = list(
            connection.execute(
                "SELECT type, name, sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            )
        )
        before_version = connection.execute(
            "SELECT singleton_id, version FROM product_schema_version"
        ).fetchall()
        before_runs = connection.execute(
            "SELECT run_id, task, status FROM runs ORDER BY run_id"
        ).fetchall()

    reopened = Database.from_path(path)
    with pytest.raises(IncompatibleProductSchemaError):
        reopened.validate_product_schema()
    with pytest.raises(IncompatibleProductSchemaError):
        reopened.create_schema()
    reopened.close()

    with sqlite3.connect(path) as connection:
        after_master = list(
            connection.execute(
                "SELECT type, name, sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            )
        )
        after_version = connection.execute(
            "SELECT singleton_id, version FROM product_schema_version"
        ).fetchall()
        after_runs = connection.execute(
            "SELECT run_id, task, status FROM runs ORDER BY run_id"
        ).fetchall()
    assert after_master == before_master
    assert after_version == before_version == [(1, 7)]
    assert after_runs == before_runs


def test_v3_mutation_shape_is_rejected_without_schema_or_data_changes(
    tmp_path: Path,
) -> None:
    path = tmp_path / "v3-mutations.sqlite3"
    database = Database.from_path(path)
    database.create_schema()
    database.close()
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("ALTER TABLE mutation_executions RENAME TO mutation_executions_v4")
        connection.execute(V3_MUTATION_EXECUTIONS_DDL)
        connection.execute("DROP TABLE mutation_executions_v4")
        for index_ddl in V3_MUTATION_INDEX_DDL:
            connection.execute(index_ddl)
        old_row = (
            str(uuid4()),
            str(uuid4()),
            str(uuid4()),
            "f" * 64,
            "edit_file",
            "src/app.py",
            "a" * 64,
            "b" * 64,
            None,
            0,
            "PREPARED",
            None,
            NOW.isoformat(),
            NOW.isoformat(),
        )
        connection.execute(
            "INSERT INTO mutation_executions VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            old_row,
        )
        connection.execute("UPDATE product_schema_version SET version = 3")
        persisted_old_ddl = connection.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'table' AND name = 'mutation_executions'"
        ).fetchone()
        persisted_old_indexes = tuple(
            row[0]
            for row in connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'index' "
                "AND tbl_name = 'mutation_executions' AND sql IS NOT NULL ORDER BY name"
            )
        )
        assert persisted_old_ddl == (V3_MUTATION_EXECUTIONS_DDL,)
        assert persisted_old_indexes == V3_MUTATION_INDEX_DDL
        foreign_keys = {
            (row[2], row[3], row[4], row[6])
            for row in connection.execute(
                "PRAGMA foreign_key_list(mutation_executions)"
            )
        }
        assert foreign_keys == {
            ("runs", "run_id", "run_id", "CASCADE"),
            ("approval_requests", "approval_id", "approval_id", "CASCADE"),
        }
        explicit_indexes = {
            row[1]: tuple(
                item[2]
                for item in connection.execute(f"PRAGMA index_info('{row[1]}')")
            )
            for row in connection.execute("PRAGMA index_list(mutation_executions)")
            if row[3] == "c"
        }
        assert explicit_indexes == {
            "ix_mutation_executions_run_id": ("run_id",),
            "ix_mutation_executions_status": ("status",),
            "ix_mutation_run_created": ("run_id", "created_at"),
        }
        before = tuple(
            connection.execute(
                "SELECT type, name, sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            )
        )
        before_data = tuple(connection.execute("SELECT * FROM mutation_executions"))

    incompatible = Database.from_path(path)
    with pytest.raises(IncompatibleProductSchemaError):
        incompatible.validate_product_schema()
    with pytest.raises(IncompatibleProductSchemaError):
        incompatible.create_schema()
    incompatible.close()

    with sqlite3.connect(path) as connection:
        after = tuple(
            connection.execute(
                "SELECT type, name, sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            )
        )
        columns = tuple(
            row[1] for row in connection.execute("PRAGMA table_info(mutation_executions)")
        )
        version = connection.execute(
            "SELECT version FROM product_schema_version WHERE singleton_id = 1"
        ).fetchone()
        after_data = tuple(connection.execute("SELECT * FROM mutation_executions"))
    assert after == before
    assert after_data == before_data == (old_row,)
    assert "before_workspace_digest" not in columns
    assert "expected_after_workspace_digest" not in columns
    assert version == (3,)


def test_v4_missing_mutation_index_is_rejected_without_repair(tmp_path: Path) -> None:
    path = tmp_path / "v4-missing-index.sqlite3"
    database = Database.from_path(path)
    database.create_schema()
    database.close()
    with sqlite3.connect(path) as connection:
        connection.execute("DROP INDEX ix_mutation_run_created")
        before = tuple(
            connection.execute(
                "SELECT type, name, sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            )
        )

    incompatible = Database.from_path(path)
    with pytest.raises(IncompatibleProductSchemaError):
        incompatible.validate_product_schema()
    with pytest.raises(IncompatibleProductSchemaError):
        incompatible.create_schema()
    incompatible.close()

    with sqlite3.connect(path) as connection:
        after = tuple(
            connection.execute(
                "SELECT type, name, sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            )
        )
    assert after == before


@pytest.mark.parametrize(
    ("old_fragment", "new_fragment"),
    [
        (
            "before_workspace_digest VARCHAR(64) NOT NULL",
            "before_workspace_digest VARCHAR(64)",
        ),
        (
            "CONSTRAINT ck_mutation_before_workspace_digest_length "
            "CHECK (length(before_workspace_digest) = 64)",
            "CONSTRAINT ck_mutation_before_workspace_digest_length CHECK (1 = 1)",
        ),
        (
            "FOREIGN KEY(run_id) REFERENCES runs (run_id) ON DELETE CASCADE",
            "FOREIGN KEY(run_id) REFERENCES runs (run_id)",
        ),
    ],
)
def test_v4_weakened_mutation_topology_is_rejected_without_repair(
    tmp_path: Path, old_fragment: str, new_fragment: str
) -> None:
    path = tmp_path / "v4-weakened.sqlite3"
    database = Database.from_path(path)
    database.create_schema()
    database.close()
    with sqlite3.connect(path) as connection:
        original_sql = connection.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'table' AND name = 'mutation_executions'"
        ).fetchone()[0]
        assert old_fragment in original_sql
        connection.execute("PRAGMA writable_schema=ON")
        connection.execute(
            "UPDATE sqlite_master SET sql = ? "
            "WHERE type = 'table' AND name = 'mutation_executions'",
            (original_sql.replace(old_fragment, new_fragment),),
        )
        connection.execute("PRAGMA writable_schema=OFF")
        before = tuple(
            connection.execute(
                "SELECT type, name, sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            )
        )

    incompatible = Database.from_path(path)
    with pytest.raises(IncompatibleProductSchemaError):
        incompatible.validate_product_schema()
    with pytest.raises(IncompatibleProductSchemaError):
        incompatible.create_schema()
    incompatible.close()

    with sqlite3.connect(path) as connection:
        after = tuple(
            connection.execute(
                "SELECT type, name, sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            )
        )
    assert after == before


def test_v4_renamed_mutation_column_is_rejected_without_repair(tmp_path: Path) -> None:
    path = tmp_path / "v4-renamed.sqlite3"
    database = Database.from_path(path)
    database.create_schema()
    database.close()
    with sqlite3.connect(path) as connection:
        connection.execute(
            "ALTER TABLE mutation_executions RENAME COLUMN "
            "before_workspace_digest TO renamed_workspace_digest"
        )
        before = tuple(
            connection.execute(
                "SELECT type, name, sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            )
        )

    incompatible = Database.from_path(path)
    with pytest.raises(IncompatibleProductSchemaError):
        incompatible.validate_product_schema()
    with pytest.raises(IncompatibleProductSchemaError):
        incompatible.create_schema()
    incompatible.close()

    with sqlite3.connect(path) as connection:
        after = tuple(
            connection.execute(
                "SELECT type, name, sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            )
        )
    assert after == before


def test_product_rows_have_explicit_identity_constraints() -> None:
    assert _unique_column_sets(ApplicationCommandReceiptRow) == set()
    assert _unique_column_sets(RunLeaseRow) == {("lease_token",)}
    assert _unique_column_sets(WorkspaceSourceBindingRow) == set()
    assert _unique_column_sets(TrustedProfileRow) == {
        ("workspace_identity", "profile_id")
    }
    assert _unique_column_sets(RunControlRequestRow) == {
        ("run_id",),
        ("command_id",),
    }


def test_fresh_database_records_product_schema_version(tmp_path: Path) -> None:
    database = Database.from_path(tmp_path / "product.sqlite3")

    database.create_schema()
    database.validate_product_schema()

    with database.session() as session:
        rows = session.scalars(select(ProductSchemaVersionRow)).all()
    assert [(row.singleton_id, row.version) for row in rows] == [(1, 9)]
    with database.session() as session:
        assert set(inspect(session.get_bind()).get_table_names()) >= {
            "product_schema_version",
            "application_command_receipts",
            "run_leases",
            "workspace_source_bindings",
            "trusted_profiles",
            "run_control_requests",
            "conversations",
        }
    database.close()


def test_create_schema_is_idempotent_for_a_versioned_database(tmp_path: Path) -> None:
    database = Database.from_path(tmp_path / "product.sqlite3")
    database.create_schema()
    database.create_schema()

    with database.session() as session:
        assert session.scalar(select(ProductSchemaVersionRow.version)) == 9
    database.close()


@pytest.mark.parametrize(
    ("next_ordinal", "version"),
    [(0, 1), (1, 0)],
)
def test_conversation_counters_are_positive_database_invariants(
    tmp_path: Path,
    next_ordinal: int,
    version: int,
) -> None:
    database = Database.from_path(tmp_path / "conversation-checks.sqlite3")
    database.create_schema()
    with pytest.raises(IntegrityError), database.session() as session:
        session.add(
            ConversationRow(
                conversation_id=str(uuid4()),
                next_ordinal=next_ordinal,
                version=version,
            )
        )
    database.close()


def test_v1_database_with_old_event_shape_is_rejected_without_ddl(
    tmp_path: Path,
) -> None:
    path = tmp_path / "v1-old-events.sqlite3"
    database = Database.from_path(path)
    database.create_schema()
    database.close()
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("ALTER TABLE events RENAME TO events_v2")
        connection.execute(
            "CREATE TABLE events ("
            "event_id VARCHAR(36) PRIMARY KEY, "
            "run_id VARCHAR(36) NOT NULL, "
            "event_type VARCHAR(64) NOT NULL, "
            "sequence_number INTEGER NOT NULL, "
            "payload JSON NOT NULL, "
            "created_at DATETIME NOT NULL)"
        )
        connection.execute("DROP TABLE events_v2")
        connection.execute("UPDATE product_schema_version SET version = 1")
        before = {
            (row[0], row[1])
            for row in connection.execute(
                "SELECT name, sql FROM sqlite_master WHERE type IN ('table', 'index') ORDER BY name"
            )
        }

    reopened = Database.from_path(path)
    with pytest.raises(IncompatibleProductSchemaError, match="incompatible product schema"):
        reopened.validate_product_schema()
    with pytest.raises(IncompatibleProductSchemaError, match="incompatible product schema"):
        reopened.create_schema()
    reopened.close()

    with sqlite3.connect(path) as connection:
        after = {
            (row[0], row[1])
            for row in connection.execute(
                "SELECT name, sql FROM sqlite_master WHERE type IN ('table', 'index') ORDER BY name"
            )
        }
        assert connection.execute(
            "SELECT version FROM product_schema_version WHERE singleton_id = 1"
        ).fetchone() == (1,)
        event_columns = [row[1] for row in connection.execute("PRAGMA table_info(events)")]
    assert after == before
    assert event_columns == [
        "event_id",
        "run_id",
        "event_type",
        "sequence_number",
        "payload",
        "created_at",
    ]


def test_v2_database_without_event_topology_checks_is_rejected_without_ddl(
    tmp_path: Path,
) -> None:
    path = tmp_path / "v2-events-without-topology-checks.sqlite3"
    database = Database.from_path(path)
    database.create_schema()
    database.close()
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("ALTER TABLE events RENAME TO events_v3")
        connection.execute(
            "CREATE TABLE events ("
            "global_cursor INTEGER PRIMARY KEY AUTOINCREMENT, "
            "schema_version INTEGER NOT NULL, "
            "event_id VARCHAR(36) NOT NULL UNIQUE, "
            "scope_type VARCHAR(32) NOT NULL, "
            "scope_id VARCHAR(200) NOT NULL, "
            "run_id VARCHAR(36), "
            "event_type VARCHAR(64) NOT NULL, "
            "sequence_number INTEGER, "
            "payload JSON NOT NULL, "
            "created_at DATETIME NOT NULL, "
            "UNIQUE(run_id, sequence_number), "
            "CONSTRAINT ck_event_schema_version_one CHECK (schema_version = 1), "
            "FOREIGN KEY(run_id) REFERENCES runs(run_id) ON DELETE CASCADE)"
        )
        connection.execute("DROP TABLE events_v3")
        connection.execute("CREATE INDEX ix_events_scope_type ON events(scope_type)")
        connection.execute("CREATE INDEX ix_events_scope_id ON events(scope_id)")
        connection.execute("CREATE INDEX ix_events_run_id ON events(run_id)")
        connection.execute("CREATE INDEX ix_events_event_type ON events(event_type)")
        connection.execute("UPDATE product_schema_version SET version = 2")
        before = {
            (row[0], row[1])
            for row in connection.execute(
                "SELECT name, sql FROM sqlite_master WHERE type IN ('table', 'index') ORDER BY name"
            )
        }

    reopened = Database.from_path(path)
    with pytest.raises(IncompatibleProductSchemaError, match="incompatible product schema"):
        reopened.validate_product_schema()
    with pytest.raises(IncompatibleProductSchemaError, match="incompatible product schema"):
        reopened.create_schema()
    reopened.close()

    with sqlite3.connect(path) as connection:
        after = {
            (row[0], row[1])
            for row in connection.execute(
                "SELECT name, sql FROM sqlite_master WHERE type IN ('table', 'index') ORDER BY name"
            )
        }
        assert connection.execute(
            "SELECT version FROM product_schema_version WHERE singleton_id = 1"
        ).fetchone() == (2,)
        event_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'events'"
        ).fetchone()[0]
    assert after == before
    assert "ck_event_schema_version_one" in event_sql
    assert "ck_event_scope_topology" not in event_sql


def test_real_v5_model_attempt_topology_is_rejected_without_ddl(
    tmp_path: Path,
) -> None:
    path = tmp_path / "v5-model-attempts.sqlite3"
    database = Database.from_path(path)
    database.create_schema()
    database.close()
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("DROP TABLE model_attempts")
        connection.execute(
            "CREATE TABLE model_attempts ("
            "attempt_id VARCHAR(36) PRIMARY KEY, "
            "run_id VARCHAR(36) NOT NULL, "
            "logical_call_id VARCHAR(36) NOT NULL, "
            "attempt_number INTEGER NOT NULL, "
            "status VARCHAR(32) NOT NULL, "
            "error_type VARCHAR(64), retryable BOOLEAN, duration_ms INTEGER, "
            "usage JSON, created_at DATETIME NOT NULL, completed_at DATETIME, "
            "UNIQUE(run_id, logical_call_id, attempt_number), "
            "FOREIGN KEY(run_id) REFERENCES runs(run_id) ON DELETE CASCADE)"
        )
        connection.execute(
            "CREATE INDEX ix_model_attempts_run_id ON model_attempts(run_id)"
        )
        connection.execute(
            "CREATE INDEX ix_model_attempts_logical_call_id "
            "ON model_attempts(logical_call_id)"
        )
        connection.execute("UPDATE product_schema_version SET version = 5")
        before = {
            (row[0], row[1])
            for row in connection.execute(
                "SELECT name, sql FROM sqlite_master "
                "WHERE type IN ('table', 'index') ORDER BY name"
            )
        }

    reopened = Database.from_path(path)
    with pytest.raises(IncompatibleProductSchemaError):
        reopened.validate_product_schema()
    with pytest.raises(IncompatibleProductSchemaError):
        reopened.create_schema()
    reopened.close()

    with sqlite3.connect(path) as connection:
        after = {
            (row[0], row[1])
            for row in connection.execute(
                "SELECT name, sql FROM sqlite_master "
                "WHERE type IN ('table', 'index') ORDER BY name"
            )
        }
        version = connection.execute(
            "SELECT version FROM product_schema_version WHERE singleton_id = 1"
        ).fetchone()
        columns = [
            row[1] for row in connection.execute("PRAGMA table_info(model_attempts)")
        ]
    assert after == before
    assert version == (5,)
    assert "request_digest" not in columns


@pytest.mark.parametrize(
    "corruption",
    [
        "primary_key",
        "foreign_key_cascade",
        "unique_attempt_identity",
        "run_index",
        "logical_call_index",
        "nullable_provider_identity",
    ],
)
def test_pseudo_v6_model_attempt_topology_is_rejected_before_ddl(
    tmp_path: Path,
    corruption: str,
) -> None:
    path = tmp_path / f"pseudo-v6-{corruption}.sqlite3"
    database = Database.from_path(path)
    database.create_schema()
    RunRepository(database).create(Run(task=f"preserve {corruption}"))
    database.close()
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        if corruption == "run_index":
            connection.execute("DROP INDEX ix_model_attempts_run_id")
        elif corruption == "logical_call_index":
            connection.execute("DROP INDEX ix_model_attempts_logical_call_id")
        else:
            original = connection.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type = 'table' AND name = 'model_attempts'"
            ).fetchone()[0]
            replacements = {
                "primary_key": ("PRIMARY KEY (attempt_id),", ""),
                "foreign_key_cascade": (
                    "FOREIGN KEY(run_id) REFERENCES runs (run_id) ON DELETE CASCADE",
                    "FOREIGN KEY(run_id) REFERENCES runs (run_id) ON DELETE NO ACTION",
                ),
                "unique_attempt_identity": (
                    "UNIQUE (run_id, logical_call_id, attempt_number),",
                    "",
                ),
                "nullable_provider_identity": (
                    "provider_identity VARCHAR(200) NOT NULL",
                    "provider_identity VARCHAR(200)",
                ),
            }
            old, new = replacements[corruption]
            corrupted = original.replace(old, new)
            assert corrupted != original
            connection.execute("ALTER TABLE model_attempts RENAME TO model_attempts_old")
            connection.execute(corrupted)
            connection.execute("DROP TABLE model_attempts_old")
            connection.execute(
                "CREATE INDEX ix_model_attempts_run_id ON model_attempts(run_id)"
            )
            connection.execute(
                "CREATE INDEX ix_model_attempts_logical_call_id "
                "ON model_attempts(logical_call_id)"
            )
        before_master = list(
            connection.execute(
                "SELECT type, name, sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            )
        )
        before_version = connection.execute(
            "SELECT singleton_id, version FROM product_schema_version"
        ).fetchall()
        before_runs = connection.execute(
            "SELECT run_id, task, status FROM runs ORDER BY run_id"
        ).fetchall()

    reopened = Database.from_path(path)
    with pytest.raises(IncompatibleProductSchemaError):
        reopened.validate_product_schema()
    with pytest.raises(IncompatibleProductSchemaError):
        reopened.create_schema()
    reopened.close()

    with sqlite3.connect(path) as connection:
        after_master = list(
            connection.execute(
                "SELECT type, name, sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            )
        )
        after_version = connection.execute(
            "SELECT singleton_id, version FROM product_schema_version"
        ).fetchall()
        after_runs = connection.execute(
            "SELECT run_id, task, status FROM runs ORDER BY run_id"
        ).fetchall()
    assert after_master == before_master
    assert after_version == before_version == [(1, 9)]
    assert after_runs == before_runs


def test_v6_product_schema_is_rejected_without_ddl(tmp_path: Path) -> None:
    path = tmp_path / "real-v6.sqlite3"
    database = Database.from_path(path)
    database.create_schema()
    with database.session() as session:
        session.add(
            TrustedProfileRow(
                trust_id=_uuid(),
                workspace_identity=DIGEST,
                profile_id="historical-v6",
                profile_version=1,
                profile_digest=DIGEST,
                executable_digest=DIGEST,
                argv_digest=DIGEST,
                cwd_identity="workspace",
                config_source_digest=DIGEST,
                purpose="development",
                enabled_at=NOW,
                disabled_at=None,
            )
        )
    database.close()

    def v6_sql(table_name: str, current: str) -> str:
        removed_markers = {
            "trusted_profiles": {
                "purpose VARCHAR(32)",
                "ck_trusted_profile_workspace_digest_length",
                "ck_trusted_profile_purpose_closed",
                "ck_trusted_profile_time_topology",
            },
            "test_approval_bindings": {
                "source_revision_number INTEGER",
                "source_revision_digest VARCHAR(64)",
                "ck_test_binding_source_revision_nonnegative",
                "ck_test_binding_source_digest_length",
            },
            "process_executions": {
                "source_revision_number INTEGER",
                "source_revision_digest VARCHAR(64)",
                "source_digest_at_start VARCHAR(64)",
                "verification_capsule_id VARCHAR(36)",
                "capsule_state VARCHAR(16)",
                "source_snapshot_digest VARCHAR(64)",
                "verifier_artifact_digest VARCHAR(64)",
                "executable_artifact_digest VARCHAR(64)",
                "artifact_algorithm_version INTEGER",
                "runtime_trust_class VARCHAR(32)",
                "ck_process_execution_source_revision_nonnegative",
                "ck_process_execution_source_digest_length",
                "ck_process_execution_start_digest_length",
                "ck_process_execution_capsule_topology",
            },
        }[table_name]
        lines = [
            line
            for line in current.splitlines()
            if not any(marker in line for marker in removed_markers)
        ]
        if table_name == "trusted_profiles":
            lines = [
                line.replace(
                    "UNIQUE (workspace_identity, profile_id)",
                    "UNIQUE (workspace_identity, profile_id, profile_version, profile_digest)",
                )
                for line in lines
            ]
        for index in range(len(lines) - 2, 0, -1):
            if lines[index].strip():
                lines[index] = lines[index].rstrip().removesuffix(",")
                break
        return "\n".join(lines)

    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("PRAGMA legacy_alter_table=ON")
        for table_name in (
            "trusted_profiles",
            "test_approval_bindings",
            "process_executions",
        ):
            original = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table_name,),
            ).fetchone()[0]
            historical = v6_sql(table_name, original)
            indexes = [
                row[0]
                for row in connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type = 'index' "
                    "AND tbl_name = ? AND sql IS NOT NULL ORDER BY name",
                    (table_name,),
                )
            ]
            connection.execute(f"ALTER TABLE {table_name} RENAME TO {table_name}_v7")
            connection.execute(historical)
            columns = [
                row[1]
                for row in connection.execute(f"PRAGMA table_info({table_name})")
            ]
            common = ", ".join(columns)
            connection.execute(
                f"INSERT INTO {table_name} ({common}) "
                f"SELECT {common} FROM {table_name}_v7"
            )
            connection.execute(f"DROP TABLE {table_name}_v7")
            for index_sql in indexes:
                connection.execute(index_sql)
        connection.execute("UPDATE product_schema_version SET version = 6")
        assert [
            row[1]
            for row in connection.execute("PRAGMA table_info(trusted_profiles)")
        ] == [
            "trust_id",
            "workspace_identity",
            "profile_id",
            "profile_version",
            "profile_digest",
            "executable_digest",
            "argv_digest",
            "cwd_identity",
            "config_source_digest",
            "enabled_at",
            "disabled_at",
        ]
        assert "source_revision_number" not in {
            row[1]
            for table_name in ("test_approval_bindings", "process_executions")
            for row in connection.execute(f"PRAGMA table_info({table_name})")
        }
        trusted_uniques = {
            tuple(
                item[2]
                for item in connection.execute(f"PRAGMA index_info('{row[1]}')")
            )
            for row in connection.execute("PRAGMA index_list(trusted_profiles)")
            if row[3] == "u"
        }
        assert trusted_uniques == {
            (
                "workspace_identity",
                "profile_id",
                "profile_version",
                "profile_digest",
            )
        }
        before = list(
            connection.execute(
                "SELECT type, name, sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            )
        )
        before_trust = connection.execute(
            "SELECT * FROM trusted_profiles"
        ).fetchall()

    reopened = Database.from_path(path)
    with pytest.raises(IncompatibleProductSchemaError):
        reopened.validate_product_schema()
    with pytest.raises(IncompatibleProductSchemaError):
        reopened.create_schema()
    reopened.close()

    with sqlite3.connect(path) as connection:
        after = list(
            connection.execute(
                "SELECT type, name, sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            )
        )
        assert connection.execute(
            "SELECT version FROM product_schema_version"
        ).fetchall() == [(6,)]
        after_trust = connection.execute(
            "SELECT * FROM trusted_profiles"
        ).fetchall()
    assert after == before
    assert after_trust == before_trust


@pytest.mark.parametrize(
    ("table_name", "old", "new"),
    [
        (
            "trusted_profiles",
            "purpose VARCHAR(32) NOT NULL",
            "purpose VARCHAR(32)",
        ),
        (
            "trusted_profiles",
            "UNIQUE (workspace_identity, profile_id),",
            "",
        ),
        (
            "test_approval_bindings",
            "CONSTRAINT ck_test_binding_source_digest_length "
            "CHECK (length(source_revision_digest) = 64),",
            "",
        ),
        (
            "process_executions",
            "CONSTRAINT ck_process_execution_source_revision_nonnegative "
            "CHECK (source_revision_number >= 0),",
            "",
        ),
    ],
)
def test_pseudo_v9_trust_and_source_topology_is_rejected_without_ddl(
    tmp_path: Path,
    table_name: str,
    old: str,
    new: str,
) -> None:
    path = tmp_path / f"pseudo-v9-{table_name}-{abs(hash(old))}.sqlite3"
    database = Database.from_path(path)
    database.create_schema()
    database.close()
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("PRAGMA legacy_alter_table=ON")
        original = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        ).fetchone()[0]
        corrupted = original.replace(old, new)
        assert corrupted != original
        indexes = [
            row[0]
            for row in connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'index' "
                "AND tbl_name = ? AND sql IS NOT NULL ORDER BY name",
                (table_name,),
            )
        ]
        connection.execute(f"ALTER TABLE {table_name} RENAME TO {table_name}_old")
        connection.execute(corrupted)
        connection.execute(f"DROP TABLE {table_name}_old")
        for index_sql in indexes:
            connection.execute(index_sql)
        before = list(
            connection.execute(
                "SELECT type, name, sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            )
        )

    reopened = Database.from_path(path)
    with pytest.raises(IncompatibleProductSchemaError):
        reopened.validate_product_schema()
    with pytest.raises(IncompatibleProductSchemaError):
        reopened.create_schema()
    reopened.close()

    with sqlite3.connect(path) as connection:
        after = list(
            connection.execute(
                "SELECT type, name, sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            )
        )
        version = connection.execute(
            "SELECT version FROM product_schema_version"
        ).fetchall()
    assert after == before
    assert version == [(9,)]


def test_concurrent_database_instances_initialize_one_schema(tmp_path: Path) -> None:
    path = tmp_path / "concurrent.sqlite3"
    barrier = Barrier(4)

    def initialize() -> None:
        database = Database.from_path(path)
        barrier.wait()
        database.create_schema()
        database.validate_product_schema()
        database.close()

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(initialize) for _ in range(4)]
        for future in futures:
            future.result()

    reopened = Database.from_path(path)
    reopened.create_schema()
    reopened.validate_product_schema()
    with reopened.session() as session:
        assert session.scalar(select(ProductSchemaVersionRow.version)) == 9
        assert len(session.scalars(select(ProductSchemaVersionRow)).all()) == 1
    reopened.close()


def test_schema_version_table_enforces_singleton_identity(tmp_path: Path) -> None:
    database = Database.from_path(tmp_path / "product.sqlite3")
    database.create_schema()

    with pytest.raises(IntegrityError), database.session() as session:
        session.add(ProductSchemaVersionRow(singleton_id=2, version=1))
    database.close()


def test_application_rejects_missing_schema(tmp_path: Path) -> None:
    database = Database.from_path(tmp_path / "missing.sqlite3")
    with pytest.raises(IncompatibleProductSchemaError, match="incompatible product schema"):
        database.validate_product_schema()
    database.close()


def test_application_rejects_wrong_schema(tmp_path: Path) -> None:
    database = Database.from_path(tmp_path / "wrong.sqlite3")
    database.create_schema()
    with database.session() as session:
        row = session.get(ProductSchemaVersionRow, 1)
        assert row is not None
        row.version = 999

    with pytest.raises(IncompatibleProductSchemaError, match="incompatible product schema"):
        database.validate_product_schema()
    database.close()


def test_create_schema_does_not_modify_wrong_version_database(tmp_path: Path) -> None:
    path = tmp_path / "wrong-version.sqlite3"
    database = Database.from_path(path)
    database.create_schema()
    with database.session() as session:
        row = session.get(ProductSchemaVersionRow, 1)
        assert row is not None
        row.version = 999
    with database.session() as session:
        before = set(inspect(session.get_bind()).get_table_names())

    with pytest.raises(IncompatibleProductSchemaError, match="incompatible product schema"):
        database.create_schema()
    with database.session() as session:
        after = set(inspect(session.get_bind()).get_table_names())
        assert session.scalar(select(ProductSchemaVersionRow.version)) == 999
    assert after == before
    database.close()


def test_create_schema_does_not_silently_adopt_an_unversioned_database(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE legacy_data (id INTEGER PRIMARY KEY)")
        before = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }

    database = Database.from_path(path)
    with pytest.raises(IncompatibleProductSchemaError, match="incompatible product schema"):
        database.create_schema()
    database.close()

    with sqlite3.connect(path) as connection:
        after = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    assert after == before == {"legacy_data"}


def test_partial_product_schema_is_rejected_without_pollution(tmp_path: Path) -> None:
    path = tmp_path / "partial.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE product_schema_version "
            "(singleton_id INTEGER PRIMARY KEY, version INTEGER NOT NULL)"
        )
        before = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}

    database = Database.from_path(path)
    with pytest.raises(IncompatibleProductSchemaError, match="incompatible product schema"):
        database.create_schema()
    database.close()

    with sqlite3.connect(path) as connection:
        after = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
    assert after == before == {"product_schema_version"}


def test_version_row_without_required_tables_is_rejected_without_pollution(
    tmp_path: Path,
) -> None:
    path = tmp_path / "version-only.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE product_schema_version "
            "(singleton_id INTEGER PRIMARY KEY, version INTEGER NOT NULL)"
        )
        connection.execute(
            "INSERT INTO product_schema_version (singleton_id, version) VALUES (1, 1)"
        )
        before = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}

    database = Database.from_path(path)
    with pytest.raises(IncompatibleProductSchemaError, match="incompatible product schema"):
        database.validate_product_schema()
    with pytest.raises(IncompatibleProductSchemaError, match="incompatible product schema"):
        database.create_schema()
    database.close()

    with sqlite3.connect(path) as connection:
        after = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
    assert after == before == {"product_schema_version"}


def test_versioned_schema_missing_one_required_table_is_not_repaired(tmp_path: Path) -> None:
    path = tmp_path / "missing-required.sqlite3"
    database = Database.from_path(path)
    database.create_schema()
    with database.session() as session:
        TrustedProfileRow.__table__.drop(session.connection())
    database.close()
    with sqlite3.connect(path) as connection:
        before = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
    assert "trusted_profiles" not in before

    reopened = Database.from_path(path)
    with pytest.raises(IncompatibleProductSchemaError, match="incompatible product schema"):
        reopened.validate_product_schema()
    with pytest.raises(IncompatibleProductSchemaError, match="incompatible product schema"):
        reopened.create_schema()
    reopened.close()

    with sqlite3.connect(path) as connection:
        after = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
    assert after == before
    assert "trusted_profiles" not in after


def test_complete_schema_allows_unrelated_extra_table(tmp_path: Path) -> None:
    path = tmp_path / "extra-table.sqlite3"
    database = Database.from_path(path)
    database.create_schema()
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE application_extension (id INTEGER PRIMARY KEY)")

    database.create_schema()
    database.validate_product_schema()
    with sqlite3.connect(path) as connection:
        assert "application_extension" in {
            row[0] for row in connection.execute("SELECT name FROM sqlite_master")
        }
    database.close()


@pytest.mark.parametrize(
    "overrides",
    [
        {"status": "NOT_CLOSED"},
        {"result_scope_type": "RUN", "result_scope_id": None},
        {"result_scope_type": None, "result_scope_id": _uuid()},
        {"command_id": "short"},
        {"request_digest": "short"},
    ],
)
def test_receipt_database_checks_reject_invalid_contracts(
    tmp_path: Path, overrides: dict[str, object]
) -> None:
    path = tmp_path / "checks.sqlite3"
    database = Database.from_path(path)
    database.create_schema()
    invalid = dict(overrides)
    command_id = str(invalid.pop("command_id", _uuid()))

    with pytest.raises(IntegrityError), database.session() as session:
        session.add(_receipt(command_id, **invalid))

    database.validate_product_schema()
    database.close()
    _assert_product_database_reopens(path)


@pytest.mark.parametrize(
    ("fencing_token", "version"),
    [(0, 1), (1, 0)],
)
def test_lease_database_checks_reject_nonpositive_counters(
    tmp_path: Path, fencing_token: int, version: int
) -> None:
    database = Database.from_path(tmp_path / f"lease-{fencing_token}-{version}.sqlite3")
    database.create_schema()
    run_id = _uuid()
    with pytest.raises(IntegrityError), database.session() as session:
        session.add(_run(run_id))
        session.flush()
        session.add(
            RunLeaseRow(
                run_id=run_id,
                owner_id="owner",
                lease_token=_uuid(),
                fencing_token=fencing_token,
                version=version,
                acquired_at=NOW,
                heartbeat_at=NOW,
                expires_at=NOW + timedelta(seconds=30),
            )
        )
    database.close()


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("initial_source_digest", "short"),
        ("expected_source_digest", "short"),
        ("source_revision_number", -1),
        ("digest_algorithm_version", 0),
        ("config_digest", "short"),
        ("profile_digest", "short"),
    ],
)
def test_source_binding_checks_reject_invalid_versions_and_digests(
    tmp_path: Path, field: str, invalid: object
) -> None:
    database = Database.from_path(tmp_path / f"source-{field}.sqlite3")
    database.create_schema()
    run_id = _uuid()
    values: dict[str, object] = {
        "run_id": run_id,
        "workspace_root_identity": "workspace",
        "git_head": None,
        "initial_source_digest": DIGEST,
        "expected_source_digest": DIGEST,
        "source_revision_number": 0,
        "digest_algorithm_version": 1,
        "config_digest": DIGEST,
        "profile_digest": DIGEST,
        "created_at": NOW,
        "updated_at": NOW,
    }
    values[field] = invalid
    with pytest.raises(IntegrityError), database.session() as session:
        session.add(_run(run_id))
        session.flush()
        session.add(WorkspaceSourceBindingRow(**values))
    database.close()


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("profile_version", 0),
        ("profile_digest", "short"),
        ("executable_digest", "short"),
        ("argv_digest", "short"),
        ("config_source_digest", "short"),
    ],
)
def test_trusted_profile_checks_reject_invalid_versions_and_digests(
    tmp_path: Path, field: str, invalid: object
) -> None:
    database = Database.from_path(tmp_path / f"profile-{field}.sqlite3")
    database.create_schema()
    values: dict[str, object] = {
        "trust_id": _uuid(),
        "workspace_identity": DIGEST,
        "profile_id": "tests",
        "profile_version": 1,
        "profile_digest": DIGEST,
        "executable_digest": DIGEST,
        "argv_digest": DIGEST,
        "cwd_identity": "workspace",
        "config_source_digest": DIGEST,
        "purpose": "development",
        "enabled_at": NOW,
        "disabled_at": None,
    }
    values[field] = invalid
    with pytest.raises(IntegrityError), database.session() as session:
        session.add(TrustedProfileRow(**values))
    database.close()


def test_unique_lease_profile_and_control_identities_are_enforced(tmp_path: Path) -> None:
    path = tmp_path / "unique.sqlite3"
    database = Database.from_path(path)
    database.create_schema()
    first_run, second_run, third_run = _uuid(), _uuid(), _uuid()
    first_command, second_command = _uuid(), _uuid()
    lease_token = _uuid()
    with database.session() as session:
        session.add_all([_run(first_run), _run(second_run), _run(third_run)])
        session.add_all([_receipt(first_command), _receipt(second_command)])
        session.flush()
        session.add(
            RunLeaseRow(
                run_id=first_run,
                owner_id="first",
                lease_token=lease_token,
                fencing_token=1,
                version=1,
                acquired_at=NOW,
                heartbeat_at=NOW,
                expires_at=NOW + timedelta(seconds=30),
            )
        )
        session.add(
            TrustedProfileRow(
                trust_id=_uuid(),
                workspace_identity=DIGEST,
                profile_id="tests",
                profile_version=1,
                profile_digest=DIGEST,
                executable_digest=DIGEST,
                argv_digest=DIGEST,
                cwd_identity="workspace",
                config_source_digest=DIGEST,
                purpose="development",
                enabled_at=NOW,
                disabled_at=None,
            )
        )
        session.add(
            RunControlRequestRow(
                control_request_id=_uuid(),
                run_id=first_run,
                command_id=first_command,
                request_type="CANCEL",
                status="REQUESTED",
                requested_at=NOW,
                updated_at=NOW,
            )
        )

    with pytest.raises(IntegrityError), database.session() as session:
        session.add(
            RunLeaseRow(
                run_id=second_run,
                owner_id="second",
                lease_token=lease_token,
                fencing_token=1,
                version=1,
                acquired_at=NOW,
                heartbeat_at=NOW,
                expires_at=NOW + timedelta(seconds=30),
            )
        )
    with pytest.raises(IntegrityError), database.session() as session:
        session.add(
            TrustedProfileRow(
                trust_id=_uuid(),
                workspace_identity=DIGEST,
                profile_id="tests",
                profile_version=1,
                profile_digest=DIGEST,
                executable_digest="b" * 64,
                argv_digest="b" * 64,
                cwd_identity="other",
                config_source_digest="b" * 64,
                purpose="development",
                enabled_at=NOW,
                disabled_at=None,
            )
        )
    with pytest.raises(IntegrityError), database.session() as session:
        session.add(
            RunControlRequestRow(
                control_request_id=_uuid(),
                run_id=first_run,
                command_id=second_command,
                request_type="CANCEL",
                status="REQUESTED",
                requested_at=NOW,
                updated_at=NOW,
            )
        )
    with pytest.raises(IntegrityError), database.session() as session:
        session.add(
            RunControlRequestRow(
                control_request_id=_uuid(),
                run_id=third_run,
                command_id=first_command,
                request_type="CANCEL",
                status="REQUESTED",
                requested_at=NOW,
                updated_at=NOW,
            )
        )
    database.validate_product_schema()
    database.close()
    _assert_product_database_reopens(path)


@pytest.mark.parametrize(
    ("request_type", "status"),
    [("UNKNOWN", "REQUESTED"), ("CANCEL", "UNKNOWN")],
)
def test_control_request_checks_and_foreign_keys_are_enforced(
    tmp_path: Path, request_type: str, status: str
) -> None:
    path = tmp_path / f"control-{request_type}-{status}.sqlite3"
    database = Database.from_path(path)
    database.create_schema()
    run_id = _uuid()
    command_id = _uuid()
    with pytest.raises(IntegrityError), database.session() as session:
        session.add(_run(run_id))
        session.add(_receipt(command_id))
        session.flush()
        session.add(
            RunControlRequestRow(
                control_request_id=_uuid(),
                run_id=run_id,
                command_id=command_id,
                request_type=request_type,
                status=status,
                requested_at=NOW,
                updated_at=NOW,
            )
        )
    with pytest.raises(IntegrityError), database.session() as session:
        session.add(
            RunControlRequestRow(
                control_request_id=_uuid(),
                run_id=_uuid(),
                command_id=_uuid(),
                request_type="CANCEL",
                status="REQUESTED",
                requested_at=NOW,
                updated_at=NOW,
            )
        )
    database.validate_product_schema()
    database.close()
    _assert_product_database_reopens(path)


def test_product_datetimes_round_trip_as_utc_and_remain_comparable(tmp_path: Path) -> None:
    path = tmp_path / "utc.sqlite3"
    database = Database.from_path(path)
    database.create_schema()
    offset_time = datetime(2026, 8, 10, 16, tzinfo=timezone(timedelta(hours=8)))
    run_id = _uuid()
    command_id = _uuid()
    with database.session() as session:
        session.add(_run(run_id))
        session.flush()
        session.add(_receipt(command_id, created_at=offset_time, updated_at=offset_time))
        session.add(
            RunLeaseRow(
                run_id=run_id,
                owner_id="owner",
                lease_token=_uuid(),
                fencing_token=1,
                version=1,
                acquired_at=offset_time,
                heartbeat_at=offset_time,
                expires_at=offset_time + timedelta(seconds=30),
            )
        )
    database.close()

    reopened = Database.from_path(path)
    reopened.validate_product_schema()
    with reopened.session() as session:
        receipt = session.get(ApplicationCommandReceiptRow, command_id)
        lease = session.get(RunLeaseRow, run_id)
        assert receipt is not None
        assert lease is not None
        assert receipt.created_at == datetime(2026, 8, 10, 8, tzinfo=UTC)
        assert receipt.created_at.tzinfo is UTC
        assert lease.expires_at > datetime(2026, 8, 10, 8, tzinfo=UTC)
    reopened.close()


def test_product_datetimes_reject_naive_values(tmp_path: Path) -> None:
    database = Database.from_path(tmp_path / "naive.sqlite3")
    database.create_schema()
    naive = datetime(2026, 8, 10, 8)
    with pytest.raises(StatementError), database.session() as session:
        session.add(_receipt(_uuid(), created_at=naive, updated_at=naive))
    database.validate_product_schema()
    database.close()
