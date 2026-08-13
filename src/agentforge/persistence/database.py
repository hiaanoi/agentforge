from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

from sqlalchemy import Connection, Engine, create_engine, event, insert, inspect, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from agentforge.application.kernel_errors import IncompatibleProductSchemaError
from agentforge.persistence.tables import Base


def _canonical_sql(value: object) -> str | None:
    """Normalize SQL syntax while preserving every byte inside quoted tokens."""
    if value is None:
        return None
    sql = str(value)
    canonical: list[str] = []
    quote: str | None = None
    pending_space = False
    index = 0
    while index < len(sql):
        character = sql[index]
        if quote is not None:
            canonical.append(character)
            closing = "]" if quote == "[" else quote
            if character == closing:
                if index + 1 < len(sql) and sql[index + 1] == closing:
                    canonical.append(sql[index + 1])
                    index += 1
                else:
                    quote = None
            index += 1
            continue
        if character.isspace():
            pending_space = bool(canonical)
            index += 1
            continue
        if pending_space:
            canonical.append(" ")
            pending_space = False
        if character in {"'", '"', "`", "["}:
            quote = character
            canonical.append(character)
        else:
            canonical.append(character.casefold())
        index += 1
    return "".join(canonical)


def _sqlite_schema_sql(
    connection: Connection, *, object_type: str, table_name: str
) -> frozenset[tuple[str, str | None]]:
    return frozenset(
        (str(row.name), _canonical_sql(row.sql))
        for row in connection.exec_driver_sql(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type = ? AND tbl_name = ? AND sql IS NOT NULL",
            (object_type, table_name),
        )
    )


@dataclass(frozen=True)
class _TableTopology:
    columns: tuple[tuple[str, str, bool, str | None], ...]
    primary_key: tuple[str | None, tuple[str, ...]]
    foreign_keys: frozenset[
        tuple[
            str | None,
            tuple[str, ...],
            str | None,
            tuple[str, ...],
            str | None,
        ]
    ]
    unique_constraints: frozenset[tuple[str | None, tuple[str, ...]]]
    indexes: frozenset[tuple[str | None, tuple[str, ...], bool]]
    index_sql: frozenset[tuple[str, str | None]]
    checks: frozenset[tuple[str | None, str | None]]
    triggers: frozenset[tuple[str, str | None]]
    create_sql: str | None


def _reflect_table_topology(connection: Connection, table_name: str) -> _TableTopology:
    inspector = inspect(connection)
    primary_key = inspector.get_pk_constraint(table_name)
    create_sql: str | None = None
    if connection.dialect.name == "sqlite":
        create_sql = connection.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        ).scalar_one_or_none()
    return _TableTopology(
        columns=tuple(
            (
                str(column["name"]),
                str(column["type"]).upper(),
                bool(column["nullable"]),
                _canonical_sql(column.get("default")),
            )
            for column in inspector.get_columns(table_name)
        ),
        primary_key=(
            primary_key.get("name"),
            tuple(
                str(column)
                for column in primary_key.get("constrained_columns", ()) or ()
            ),
        ),
        foreign_keys=frozenset(
            (
                item.get("name"),
                tuple(item.get("constrained_columns", ()) or ()),
                item.get("referred_table"),
                tuple(item.get("referred_columns", ()) or ()),
                (item.get("options", {}) or {}).get("ondelete"),
            )
            for item in inspector.get_foreign_keys(table_name)
        ),
        unique_constraints=frozenset(
            (
                item.get("name"),
                tuple(str(column) for column in item.get("column_names", ()) or ()),
            )
            for item in inspector.get_unique_constraints(table_name)
        ),
        indexes=frozenset(
            (
                item.get("name"),
                tuple(str(column) for column in item.get("column_names", ()) or ()),
                bool(item.get("unique", False)),
            )
            for item in inspector.get_indexes(table_name)
        ),
        index_sql=_sqlite_schema_sql(
            connection, object_type="index", table_name=table_name
        ),
        checks=frozenset(
            (item.get("name"), _canonical_sql(item.get("sqltext")))
            for item in inspector.get_check_constraints(table_name)
        ),
        triggers=_sqlite_schema_sql(
            connection, object_type="trigger", table_name=table_name
        ),
        create_sql=_canonical_sql(create_sql),
    )


@lru_cache(maxsize=1)
def _expected_sqlite_product_topology() -> dict[str, _TableTopology]:
    """Freeze the current v9 contract through the same SQLite reflection path."""
    reference = create_engine("sqlite://")
    try:
        with reference.begin() as connection:
            Base.metadata.create_all(connection)
            return {
                table_name: _reflect_table_topology(connection, table_name)
                for table_name in Base.metadata.tables
            }
    finally:
        reference.dispose()


class Database:
    def __init__(
        self,
        url: str,
        *,
        artifact_root: Path | None = None,
    ) -> None:
        self._engine: Engine = create_engine(url)
        self._artifact_root = artifact_root
        self._path: Path | None = None
        if url.startswith("sqlite"):
            event.listen(self._engine, "connect", self._enable_sqlite_foreign_keys)
        self._session_factory = sessionmaker(bind=self._engine, expire_on_commit=False)

    @classmethod
    def from_path(
        cls, path: Path, *, artifact_root: Path | None = None
    ) -> "Database":
        resolved = path.resolve()
        resolved.parent.mkdir(parents=True, exist_ok=True)
        database = cls(
            f"sqlite:///{resolved.as_posix()}",
            artifact_root=(
                artifact_root.resolve()
                if artifact_root is not None
                else resolved.parent / f".{resolved.name}.artifacts"
            ),
        )
        database._path = resolved
        return database

    @classmethod
    def read_only_from_path(cls, path: Path) -> "Database":
        """Create an inspection-only SQLite handle without materializing ``path``.

        The engine is deliberately lazy: :class:`Doctor` checks the file before
        connecting, while any eventual connection uses SQLite's ``mode=ro`` URI.
        This keeps diagnostics safe even when the configured database is absent.
        """

        resolved = path.resolve(strict=False)
        encoded_path = quote(resolved.as_posix(), safe="/:")
        database = cls(f"sqlite:///file:{encoded_path}?mode=ro&uri=true")
        database._path = resolved
        return database

    @property
    def path(self) -> Path | None:
        """Resolved durable SQLite identity, when constructed from a path."""

        return self._path

    @property
    def verification_artifact_root(self) -> Path:
        if self._artifact_root is None:
            raise IncompatibleProductSchemaError()
        return self._artifact_root.resolve(strict=False)

    @staticmethod
    def _enable_sqlite_foreign_keys(dbapi_connection: object, _: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    def create_schema(self) -> None:
        from agentforge.persistence import product_tables

        connection = self._engine.connect()
        try:
            if self._engine.dialect.name == "sqlite":
                connection.exec_driver_sql("BEGIN IMMEDIATE")
            else:
                connection.begin()
            try:
                existing_tables = inspect(connection).get_table_names()
            except SQLAlchemyError as exc:
                raise IncompatibleProductSchemaError() from exc
            if existing_tables:
                if not self._has_compatible_product_schema(connection, existing_tables):
                    raise IncompatibleProductSchemaError()
            else:
                Base.metadata.create_all(connection)
                connection.execute(
                    insert(product_tables.ProductSchemaVersionRow).values(
                        singleton_id=1, version=product_tables.PRODUCT_SCHEMA_VERSION
                    )
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _has_compatible_product_schema(
        connection: Connection, existing_tables: list[str]
    ) -> bool:
        from agentforge.persistence.product_tables import (
            PRODUCT_SCHEMA_VERSION,
            ProductSchemaVersionRow,
        )

        required_tables = set(Base.metadata.tables)
        if not required_tables.issubset(existing_tables):
            return False
        try:
            rows = connection.execute(
                select(
                    ProductSchemaVersionRow.singleton_id,
                    ProductSchemaVersionRow.version,
                )
            ).all()
        except SQLAlchemyError as exc:
            raise IncompatibleProductSchemaError() from exc
        version_matches = [(row.singleton_id, row.version) for row in rows] == [
            (1, PRODUCT_SCHEMA_VERSION)
        ]
        if not version_matches:
            return False
        return Database._has_exact_product_topology(connection)

    @staticmethod
    def _has_exact_product_topology(connection: Connection) -> bool:
        """Validate every AgentForge-owned v9 table without touching the database."""
        if connection.dialect.name != "sqlite":
            return False
        try:
            expected = _expected_sqlite_product_topology()
            return all(
                _reflect_table_topology(connection, table_name) == topology
                for table_name, topology in expected.items()
            )
        except SQLAlchemyError:
            return False

    @staticmethod
    def _has_v9_trust_source_and_capsule_topology(connection: Connection) -> bool:
        """Validate exact trust and source-binding columns, checks, and identities."""
        expected_checks = {
            "trusted_profiles": {
                ("ck_trusted_profile_uuid_length", "length(trust_id)=36"),
                ("ck_trusted_profile_version_positive", "profile_version>=1"),
                ("ck_trusted_profile_digest_length", "length(profile_digest)=64"),
                (
                    "ck_trusted_profile_executable_digest_length",
                    "length(executable_digest)=64",
                ),
                ("ck_trusted_profile_argv_digest_length", "length(argv_digest)=64"),
                (
                    "ck_trusted_profile_config_digest_length",
                    "length(config_source_digest)=64",
                ),
                (
                    "ck_trusted_profile_workspace_digest_length",
                    "length(workspace_identity)=64",
                ),
                (
                    "ck_trusted_profile_purpose_closed",
                    "purposein('development','verification','utility')",
                ),
                (
                    "ck_trusted_profile_time_topology",
                    "disabled_atisnullordisabled_at>=enabled_at",
                ),
            },
            "test_approval_bindings": {
                (
                    "ck_test_binding_source_revision_nonnegative",
                    "source_revision_number>=0",
                ),
                (
                    "ck_test_binding_source_digest_length",
                    "length(source_revision_digest)=64",
                ),
            },
            "process_executions": {
                (
                    "ck_process_execution_source_revision_nonnegative",
                    "source_revision_number>=0",
                ),
                (
                    "ck_process_execution_source_digest_length",
                    "length(source_revision_digest)=64",
                ),
                (
                    "ck_process_execution_start_digest_length",
                    "source_digest_at_startisnullorlength(source_digest_at_start)=64",
                ),
                (
                    "ck_process_execution_capsule_topology",
                    "(verification_capsule_idisnullandcapsule_stateisnull"
                    "andsource_snapshot_digestisnullandverifier_artifact_digestisnull"
                    "andexecutable_artifact_digestisnullandartifact_algorithm_versionisnull"
                    "andruntime_trust_classisnull)or(length(verification_capsule_id)=36"
                    "andcapsule_state='staging'andsource_snapshot_digestisnull"
                    "andverifier_artifact_digestisnullandexecutable_artifact_digestisnull"
                    "andartifact_algorithm_versionisnullandruntime_trust_classisnull)or"
                    "(length(verification_capsule_id)=36andcapsule_state='sealed'"
                    "andlength(source_snapshot_digest)=64andlength(verifier_artifact_digest)=64"
                    "andlength(executable_artifact_digest)=64andartifact_algorithm_version>=1"
                    "andruntime_trust_class='non_hermetic')",
                ),
            },
        }
        try:
            inspector = inspect(connection)
            for table_name, checks_contract in expected_checks.items():
                table = Base.metadata.tables[table_name]
                expected_columns = tuple(
                    (column.name, str(column.type).upper(), bool(column.nullable))
                    for column in table.columns
                )
                actual_columns = tuple(
                    (
                        column["name"],
                        str(column["type"]).upper(),
                        bool(column["nullable"]),
                    )
                    for column in inspector.get_columns(table_name)
                )
                actual_checks = {
                    (
                        item.get("name"),
                        "".join(str(item.get("sqltext", "")).split()).casefold(),
                    )
                    for item in inspector.get_check_constraints(table_name)
                }
                if actual_columns != expected_columns or actual_checks != checks_contract:
                    return False
        except (KeyError, SQLAlchemyError):
            return False
        return (
            Database._has_exact_table_identity_topology(
                connection,
                "trusted_profiles",
                primary_key=("trust_id",),
                foreign_keys=set(),
                unique_constraints={("workspace_identity", "profile_id")},
                indexes=set(),
            )
            and Database._has_exact_table_identity_topology(
                connection,
                "test_approval_bindings",
                primary_key=("approval_id",),
                foreign_keys={
                    (("approval_id",), "approval_requests", ("approval_id",), "CASCADE"),
                    (("run_id",), "runs", ("run_id",), "CASCADE"),
                    (("checkpoint_id",), "checkpoints", ("checkpoint_id",), "CASCADE"),
                },
                unique_constraints={("tool_call_digest",)},
                indexes={("ix_test_approval_bindings_run_id", ("run_id",), False)},
            )
            and Database._has_exact_table_identity_topology(
                connection,
                "process_executions",
                primary_key=("execution_id",),
                foreign_keys={
                    (("run_id",), "runs", ("run_id",), "CASCADE"),
                    (("approval_id",), "approval_requests", ("approval_id",), "CASCADE"),
                },
                unique_constraints={
                    ("approval_id",),
                    ("tool_call_digest",),
                    ("run_id", "attempt_number"),
                },
                indexes={
                    (
                        "ix_process_execution_run_created",
                        ("run_id", "created_at"),
                        False,
                    ),
                    ("ix_process_executions_run_id", ("run_id",), False),
                    ("ix_process_executions_status", ("status",), False),
                },
            )
        )

    @staticmethod
    def _has_v6_model_attempt_topology(connection: Connection) -> bool:
        """Validate the closed provider-attempt journal introduced in version 6."""
        expected_columns = (
            ("attempt_id", "VARCHAR(36)", False),
            ("run_id", "VARCHAR(36)", False),
            ("logical_call_id", "VARCHAR(36)", False),
            ("attempt_number", "INTEGER", False),
            ("status", "VARCHAR(32)", False),
            ("request_digest", "VARCHAR(64)", False),
            ("provider_identity", "VARCHAR(200)", False),
            ("budget_digest", "VARCHAR(64)", False),
            ("error_type", "VARCHAR(64)", True),
            ("retryable", "BOOLEAN", True),
            ("duration_ms", "INTEGER", True),
            ("usage", "JSON", True),
            ("created_at", "DATETIME", False),
            ("dispatched_at", "DATETIME", True),
            ("completed_at", "DATETIME", True),
        )
        try:
            inspector = inspect(connection)
            columns = tuple(
                (
                    column["name"],
                    str(column["type"]).upper(),
                    bool(column["nullable"]),
                )
                for column in inspector.get_columns("model_attempts")
            )
            checks = {
                (
                    item.get("name"),
                    "".join(str(item.get("sqltext", "")).split()).casefold(),
                )
                for item in inspector.get_check_constraints("model_attempts")
            }
        except SQLAlchemyError:
            return False
        checks_match = checks == {
            (
                "ck_model_attempt_status_closed",
                "statusin('prepared','dispatching','completed','failed',"
                "'indeterminate')",
            ),
            (
                "ck_model_attempt_digest_lengths",
                "length(request_digest)=64andlength(budget_digest)=64",
            ),
            (
                "ck_model_attempt_provider_identity",
                "length(provider_identity)>0andlength(provider_identity)<=200",
            ),
            (
                "ck_model_attempt_time_topology",
                "(status='prepared'anddispatched_atisnullandcompleted_atisnull)or"
                "(status='dispatching'anddispatched_atisnotnullandcompleted_atisnull)or"
                "(statusin('completed','failed','indeterminate')and"
                "dispatched_atisnotnullandcompleted_atisnotnull)",
            ),
            (
                "ck_model_attempt_terminal_facts",
                "(statusin('prepared','dispatching','indeterminate')and"
                "error_typeisnullandretryableisnullandduration_msisnulland"
                "usageisnull)or(status='completed'anderror_typeisnulland"
                "retryableisnullandduration_msisnotnull)or(status='failed'and"
                "error_typeisnotnullandretryableisnotnullandusageisnull)",
            ),
        }
        return (
            columns == expected_columns
            and checks_match
            and Database._has_exact_table_identity_topology(
                connection,
                "model_attempts",
                primary_key=("attempt_id",),
                foreign_keys={
                    (("run_id",), "runs", ("run_id",), "CASCADE"),
                },
                unique_constraints={
                    ("run_id", "logical_call_id", "attempt_number"),
                },
                indexes={
                    (
                        "ix_model_attempts_logical_call_id",
                        ("logical_call_id",),
                        False,
                    ),
                    ("ix_model_attempts_run_id", ("run_id",), False),
                },
            )
        )

    @staticmethod
    def _has_exact_table_identity_topology(
        connection: Connection,
        table_name: str,
        *,
        primary_key: tuple[str, ...],
        foreign_keys: set[
            tuple[tuple[str, ...], str | None, tuple[str, ...], str | None]
        ],
        unique_constraints: set[tuple[str, ...]],
        indexes: set[tuple[str | None, tuple[str, ...], bool]],
    ) -> bool:
        """Compare durable identity edges exactly, including named indexes."""
        try:
            inspector = inspect(connection)
            actual_primary_key = tuple(
                inspector.get_pk_constraint(table_name).get(
                    "constrained_columns", ()
                )
                or ()
            )
            actual_foreign_keys = {
                (
                    tuple(item.get("constrained_columns", ()) or ()),
                    item.get("referred_table"),
                    tuple(item.get("referred_columns", ()) or ()),
                    (item.get("options", {}) or {}).get("ondelete"),
                )
                for item in inspector.get_foreign_keys(table_name)
            }
            actual_uniques = {
                tuple(item.get("column_names", ()) or ())
                for item in inspector.get_unique_constraints(table_name)
            }
            actual_indexes = {
                (
                    item.get("name"),
                    tuple(item.get("column_names", ()) or ()),
                    bool(item.get("unique", False)),
                )
                for item in inspector.get_indexes(table_name)
            }
        except SQLAlchemyError:
            return False
        return (
            actual_primary_key == primary_key
            and actual_foreign_keys == foreign_keys
            and actual_uniques == unique_constraints
            and actual_indexes == indexes
        )

    @staticmethod
    def _has_v5_lease_topology(connection: Connection) -> bool:
        """Validate the version-5 lease columns and time constraints fail closed."""
        expected_columns = (
            ("run_id", "VARCHAR(36)", False),
            ("owner_id", "VARCHAR(200)", False),
            ("lease_token", "VARCHAR(36)", False),
            ("fencing_token", "INTEGER", False),
            ("version", "INTEGER", False),
            ("acquired_at", "DATETIME", False),
            ("heartbeat_at", "DATETIME", False),
            ("expires_at", "DATETIME", False),
            ("released_at", "DATETIME", True),
        )
        try:
            inspector = inspect(connection)
            columns = tuple(
                (
                    column["name"],
                    str(column["type"]).upper(),
                    bool(column["nullable"]),
                )
                for column in inspector.get_columns("run_leases")
            )
            checks = {
                (
                    item.get("name"),
                    "".join(str(item.get("sqltext", "")).split()).casefold(),
                )
                for item in inspector.get_check_constraints("run_leases")
            }
        except SQLAlchemyError:
            return False
        return columns == expected_columns and {
            (
                "ck_run_lease_time_topology",
                "acquired_at<=heartbeat_atandheartbeat_at<expires_at",
            ),
            (
                "ck_run_lease_release_topology",
                "released_atisnullorreleased_at>=heartbeat_at",
            ),
        }.issubset(checks)

    @staticmethod
    def _has_v4_mutation_topology(connection: Connection) -> bool:
        """Validate the version-4 mutation table contract without changing DDL."""
        expected_columns = (
            ("execution_id", "VARCHAR(36)", False),
            ("run_id", "VARCHAR(36)", False),
            ("approval_id", "VARCHAR(36)", False),
            ("tool_call_digest", "VARCHAR(64)", False),
            ("tool_name", "VARCHAR(100)", False),
            ("target_path", "TEXT", False),
            ("before_sha256", "VARCHAR(64)", True),
            ("expected_after_sha256", "VARCHAR(64)", False),
            ("before_workspace_digest", "VARCHAR(64)", False),
            ("expected_after_workspace_digest", "VARCHAR(64)", False),
            ("actual_after_sha256", "VARCHAR(64)", True),
            ("bytes_written", "INTEGER", False),
            ("status", "VARCHAR(32)", False),
            ("result_summary", "VARCHAR(500)", True),
            ("created_at", "DATETIME", False),
            ("updated_at", "DATETIME", False),
        )
        try:
            inspector = inspect(connection)
            columns = tuple(
                (
                    column["name"],
                    str(column["type"]).upper(),
                    bool(column["nullable"]),
                )
                for column in inspector.get_columns("mutation_executions")
            )
            checks = {
                (
                    item.get("name"),
                    "".join(str(item.get("sqltext", "")).split()).casefold(),
                )
                for item in inspector.get_check_constraints("mutation_executions")
            }
        except SQLAlchemyError:
            return False
        return (
            columns == expected_columns
            and Database._has_exact_table_identity_topology(
                connection,
                "mutation_executions",
                primary_key=("execution_id",),
                foreign_keys={
                    (("run_id",), "runs", ("run_id",), "CASCADE"),
                    (
                        ("approval_id",),
                        "approval_requests",
                        ("approval_id",),
                        "CASCADE",
                    ),
                },
                unique_constraints={("approval_id",), ("tool_call_digest",)},
                indexes={
                    ("ix_mutation_executions_run_id", ("run_id",), False),
                    ("ix_mutation_executions_status", ("status",), False),
                    ("ix_mutation_run_created", ("run_id", "created_at"), False),
                },
            )
            and checks
            == {
                (
                    "ck_mutation_before_workspace_digest_length",
                    "length(before_workspace_digest)=64",
                ),
                (
                    "ck_mutation_after_workspace_digest_length",
                    "length(expected_after_workspace_digest)=64",
                ),
            }
        )

    def validate_product_schema(self) -> None:
        connection = self._engine.connect()
        try:
            existing_tables = inspect(connection).get_table_names()
            if not existing_tables or not self._has_compatible_product_schema(
                connection, existing_tables
            ):
                raise IncompatibleProductSchemaError()
        except IncompatibleProductSchemaError:
            raise
        except SQLAlchemyError as exc:
            raise IncompatibleProductSchemaError() from exc
        finally:
            connection.close()

    def validate_product_schema_read_only(self) -> None:
        """Validate a durable SQLite database without allowing SQLite writes."""
        path = self._path
        if path is None:
            raise IncompatibleProductSchemaError()
        encoded_path = quote(path.resolve(strict=True).as_posix(), safe="/:")
        engine = create_engine(f"sqlite:///file:{encoded_path}?mode=ro&uri=true")
        try:
            connection = engine.connect()
            try:
                existing_tables = inspect(connection).get_table_names()
                if not existing_tables or not self._has_compatible_product_schema(
                    connection, existing_tables
                ):
                    raise IncompatibleProductSchemaError()
            except IncompatibleProductSchemaError:
                raise
            except (OSError, SQLAlchemyError) as exc:
                raise IncompatibleProductSchemaError() from exc
            finally:
                connection.close()
        finally:
            engine.dispose()

    def new_session(self) -> Session:
        """Return an uncommitted Session for an explicit application UoW."""
        return Session(
            bind=self._engine,
            expire_on_commit=False,
            close_resets_only=False,
        )

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self._session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def close(self) -> None:
        self._engine.dispose()
