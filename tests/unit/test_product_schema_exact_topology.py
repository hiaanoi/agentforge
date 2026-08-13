from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from agentforge.application.kernel_errors import IncompatibleProductSchemaError
from agentforge.persistence import product_tables
from agentforge.persistence.database import Database, _canonical_sql
from agentforge.persistence.tables import Base

PRODUCT_TABLES = (
    "application_command_receipts",
    "approval_requests",
    "checkpoints",
    "conversations",
    "diff_validation_results",
    "evaluation_baseline_executions",
    "evaluation_campaign_events",
    "evaluation_campaigns",
    "evaluation_pilot_attempts",
    "evaluation_protocol_events",
    "evaluation_protocols",
    "evaluation_run_telemetry",
    "evaluation_slots",
    "evaluation_studies",
    "evaluation_study_campaigns",
    "evaluation_study_events",
    "events",
    "model_attempts",
    "model_runtime_states",
    "mutation_approval_bindings",
    "mutation_executions",
    "process_executions",
    "product_schema_version",
    "repair_budget_consumptions",
    "repair_evaluation_runs",
    "repair_states",
    "repair_task_policies",
    "run_control_requests",
    "run_leases",
    "runs",
    "test_approval_bindings",
    "trusted_profiles",
    "workspace_baseline_files",
    "workspace_baselines",
    "workspace_source_bindings",
)


def _create_database(path: Path) -> None:
    database = Database.from_path(path)
    database.create_schema()
    database.close()


def _assert_rejected_without_repair(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        before = tuple(
            connection.execute(
                "SELECT type, name, sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            )
        )
    database = Database.from_path(path)
    with pytest.raises(IncompatibleProductSchemaError):
        database.validate_product_schema()
    with pytest.raises(IncompatibleProductSchemaError):
        database.create_schema()
    database.close()
    with sqlite3.connect(path) as connection:
        after = tuple(
            connection.execute(
                "SELECT type, name, sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            )
        )
    assert after == before


def _replace_table_sql(path: Path, table_name: str, old: str, new: str) -> None:
    with sqlite3.connect(path) as connection:
        original = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        ).fetchone()[0]
        assert old in original
        connection.execute("PRAGMA writable_schema=ON")
        connection.execute(
            "UPDATE sqlite_master SET sql=? WHERE type='table' AND name=?",
            (original.replace(old, new, 1), table_name),
        )
        connection.execute("PRAGMA writable_schema=OFF")


def test_v9_managed_table_inventory_is_explicit_and_complete() -> None:
    assert product_tables.PRODUCT_SCHEMA_VERSION == 9
    assert len(PRODUCT_TABLES) == 35
    assert PRODUCT_TABLES == tuple(sorted(Base.metadata.tables))


@pytest.mark.parametrize(
    ("upper", "lower"),
    [
        ("SELECT 'A''B'", "SELECT 'a''B'"),
        ('SELECT "A""B"', 'SELECT "a""B"'),
        ("SELECT `A``B`", "SELECT `a``B`"),
        ("SELECT [A]]B]", "SELECT [a]]B]"),
    ],
)
def test_sql_canonicalization_preserves_quoted_token_bytes(
    upper: str, lower: str
) -> None:
    assert _canonical_sql(upper) != _canonical_sql(lower)


def test_sql_canonicalization_normalizes_only_unquoted_syntax() -> None:
    assert _canonical_sql("  SELECT\n  value  ") == "select value"


@pytest.mark.parametrize("table_name", PRODUCT_TABLES)
def test_v9_rejects_extra_column_in_every_agentforge_table(
    tmp_path: Path, table_name: str
) -> None:
    path = tmp_path / f"extra-column-{table_name}.sqlite3"
    _create_database(path)
    with sqlite3.connect(path) as connection:
        connection.execute(f'ALTER TABLE "{table_name}" ADD COLUMN audit_extra INTEGER')

    _assert_rejected_without_repair(path)


def test_v9_rejects_receipt_missing_closed_status_check(tmp_path: Path) -> None:
    path = tmp_path / "receipt-missing-status-check.sqlite3"
    _create_database(path)
    _replace_table_sql(
        path,
        "application_command_receipts",
        "CONSTRAINT ck_receipt_status_closed CHECK (status IN "
        "('ACCEPTED', 'IN_PROGRESS', 'COMPLETED', 'FAILED', 'INDETERMINATE')), ",
        "",
    )

    _assert_rejected_without_repair(path)


@pytest.mark.parametrize(
    ("table_name", "old", "new"),
    [
        ("runs", "task TEXT NOT NULL", "task TEXT"),
        ("runs", "task TEXT NOT NULL", "task TEXT DEFAULT 'tampered' NOT NULL"),
        ("conversations", "version INTEGER NOT NULL", "version TEXT NOT NULL"),
        (
            "events",
            "global_cursor INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT",
            "global_cursor INTEGER NOT NULL UNIQUE",
        ),
        (
            "checkpoints",
            "FOREIGN KEY(run_id) REFERENCES runs (run_id) ON DELETE CASCADE",
            "FOREIGN KEY(run_id) REFERENCES runs (run_id)",
        ),
        (
            "repair_budget_consumptions",
            "UNIQUE (run_id, budget_kind, fact_id)",
            "UNIQUE (run_id, budget_kind)",
        ),
        (
            "application_command_receipts",
            "length(request_digest) = 64",
            "length(request_digest) >= 1",
        ),
        (
            "application_command_receipts",
            "command_type VARCHAR(64) NOT NULL,",
            "command_type VARCHAR(64) NOT NULL "
            "CHECK (length(command_type) > 0),",
        ),
    ],
)
def test_v9_rejects_corruption_across_constraint_families(
    tmp_path: Path, table_name: str, old: str, new: str
) -> None:
    path = tmp_path / f"constraint-{table_name}.sqlite3"
    _create_database(path)
    _replace_table_sql(path, table_name, old, new)

    _assert_rejected_without_repair(path)


def test_v9_rejects_missing_named_index(tmp_path: Path) -> None:
    path = tmp_path / "missing-index.sqlite3"
    _create_database(path)
    with sqlite3.connect(path) as connection:
        connection.execute("DROP INDEX ix_events_run_id")

    _assert_rejected_without_repair(path)


def test_v9_rejects_extra_named_index(tmp_path: Path) -> None:
    path = tmp_path / "extra-index.sqlite3"
    _create_database(path)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE INDEX ix_runs_audit_extra ON runs (task)")

    _assert_rejected_without_repair(path)


def test_v9_rejects_case_change_inside_quoted_check_literal(tmp_path: Path) -> None:
    path = tmp_path / "quoted-literal.sqlite3"
    _create_database(path)
    _replace_table_sql(path, "events", "'RUN'", "'run'")

    _assert_rejected_without_repair(path)


def test_v9_rejects_same_name_and_columns_partial_index(tmp_path: Path) -> None:
    path = tmp_path / "partial-index.sqlite3"
    _create_database(path)
    with sqlite3.connect(path) as connection:
        connection.execute("DROP INDEX ix_runs_status")
        connection.execute(
            "CREATE INDEX ix_runs_status ON runs (status) "
            "WHERE status = 'RUNNING'"
        )

    _assert_rejected_without_repair(path)


def test_v9_rejects_trigger_attached_to_managed_table(tmp_path: Path) -> None:
    path = tmp_path / "managed-trigger.sqlite3"
    _create_database(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TRIGGER abort_runs BEFORE UPDATE ON runs "
            "BEGIN SELECT RAISE(ABORT, 'blocked'); END"
        )

    _assert_rejected_without_repair(path)


def test_v9_allows_trigger_owned_by_unrelated_extension_table(tmp_path: Path) -> None:
    path = tmp_path / "extension-trigger.sqlite3"
    _create_database(path)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE application_extension (id INTEGER PRIMARY KEY)")
        connection.execute(
            "CREATE TRIGGER extension_trigger AFTER INSERT ON application_extension "
            "BEGIN UPDATE application_extension SET id = NEW.id WHERE id = NEW.id; END"
        )

    database = Database.from_path(path)
    database.validate_product_schema()
    database.create_schema()
    database.close()
