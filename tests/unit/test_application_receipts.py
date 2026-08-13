from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import InvalidRequestError, SQLAlchemyError

from agentforge.application.contracts import ReceiptStatus
from agentforge.application.kernel_errors import (
    IdempotencyConflictError,
    InvalidReceiptTransitionError,
    PersistenceBoundaryError,
    UnitOfWorkStateError,
)
from agentforge.application.run_creation import StartRun, SubmitMessageRun
from agentforge.domain.models import utc_now
from agentforge.domain.repair import BudgetProfile, RepairDifficulty, RepairTaskPolicy
from agentforge.models.domain import ModelBudget
from agentforge.persistence.application_uow import ApplicationUnitOfWork
from agentforge.persistence.database import Database
from agentforge.persistence.product_tables import ApplicationCommandReceiptRow
from agentforge.persistence.receipts import ReceiptStore, request_digest

SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64


def _policy(task_id: str = "receipt-task") -> RepairTaskPolicy:
    return RepairTaskPolicy(
        task_id=task_id,
        policy_version=1,
        difficulty=RepairDifficulty.BASIC,
        budget_profile=BudgetProfile.BASIC,
        allowed_write_paths=("src/**",),
        protected_paths=("tests/**",),
        allowed_development_test_profiles=("unit",),
        final_verification_profile_id="hidden",
        allow_file_creation=False,
        max_created_files=0,
        max_changed_files=2,
        max_total_changed_bytes=4096,
        max_single_file_changed_bytes=2048,
        path_case_sensitive=False,
    )


def _command(command_id: UUID | None = None, *, task: str = "fix it") -> StartRun:
    return StartRun(
        command_id=command_id or uuid4(),
        task=task,
        max_steps=7,
        max_tool_calls=8,
        model_provider="mock",
        model_budget=ModelBudget(max_model_requests=6, max_retries=2),
        workspace_root_identity="workspace-1",
        git_head=SHA_A,
        initial_source_digest=SHA_B,
        digest_algorithm_version=1,
        config_digest=SHA_C,
        profile_digest=SHA_A,
        repair_policy=_policy(),
        baseline_id=UUID(int=9),
        baseline_digest=SHA_B,
    )


def _database(path: Path) -> Database:
    database = Database.from_path(path)
    database.create_schema()
    return database


def test_request_digest_excludes_command_id_but_covers_every_semantic_field() -> None:
    first = _command(UUID(int=1))
    second = first.model_copy(update={"command_id": UUID(int=2)})
    assert request_digest(first) == request_digest(second)

    changed = {
        "task": "different",
        "max_steps": 9,
        "max_tool_calls": 10,
        "model_provider": "other",
        "workspace_root_identity": "workspace-2",
        "git_head": SHA_C,
        "initial_source_digest": SHA_A,
        "digest_algorithm_version": 2,
        "config_digest": SHA_A,
        "profile_digest": SHA_B,
        "baseline_id": UUID(int=10),
        "baseline_digest": SHA_C,
        "model_budget": ModelBudget(max_model_requests=7),
        "repair_policy": _policy("changed"),
    }
    for field, value in changed.items():
        assert request_digest(first.model_copy(update={field: value})) != request_digest(first)


@pytest.mark.parametrize("invalid", [True, 1.0, "1", 2])
def test_command_schema_version_requires_exact_integer_one(invalid: object) -> None:
    payload = _command().model_dump(mode="python")
    payload["schema_version"] = invalid
    with pytest.raises(ValueError):
        StartRun.model_validate(payload)


def test_request_digest_revalidates_models_that_bypass_assignment_validation() -> None:
    bypassed = _command().model_copy(update={"schema_version": True})
    with pytest.raises(ValueError):
        request_digest(bypassed)


@pytest.mark.parametrize(
    ("field", "invalid"),
    [("max_steps", True), ("max_tool_calls", 2.0), ("digest_algorithm_version", "1")],
)
def test_command_integer_fields_reject_bool_float_and_string(
    field: str, invalid: object
) -> None:
    payload = _command().model_dump(mode="python")
    payload[field] = invalid
    with pytest.raises(ValueError):
        StartRun.model_validate(payload)


@pytest.mark.parametrize(
    ("section", "field"),
    [("model_budget", "max_model_requests"), ("repair_policy", "max_changed_files")],
)
def test_nested_command_integer_fields_reject_bool(section: str, field: str) -> None:
    payload = _command().model_dump(mode="python")
    payload[section][field] = True
    with pytest.raises(ValueError):
        StartRun.model_validate(payload)


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("allow_file_creation", "false"),
        ("allow_file_creation", 0),
        ("allow_file_creation", 1),
        ("path_case_sensitive", "true"),
        ("path_case_sensitive", 0),
        ("path_case_sensitive", 1),
    ],
)
def test_nested_repair_policy_booleans_require_exact_bool(
    field: str, invalid: object
) -> None:
    payload = _command().model_dump(mode="python")
    payload["repair_policy"][field] = invalid
    with pytest.raises(ValueError):
        StartRun.model_validate(payload)


def test_digest_revalidates_nested_policy_model_copy_bypass() -> None:
    bypassed_policy = _policy().model_copy(update={"allow_file_creation": 0})
    bypassed = _command().model_copy(update={"repair_policy": bypassed_policy})
    with pytest.raises(ValueError):
        request_digest(bypassed)


def test_git_object_id_accepts_sha1_and_sha256_and_normalizes_case() -> None:
    base = _command()
    sha1_upper = "ABCDEF0123456789ABCDEF0123456789ABCDEF01"
    sha1 = base.model_copy(update={"git_head": sha1_upper})
    normalized = StartRun.model_validate(sha1.model_dump(mode="python"))
    assert normalized.git_head == sha1_upper.lower()
    assert request_digest(normalized) == request_digest(
        normalized.model_copy(update={"git_head": sha1_upper.lower()})
    )
    assert StartRun.model_validate(base.model_dump(mode="python")).git_head == SHA_A


@pytest.mark.parametrize(
    "invalid",
    ["", "a" * 39, "a" * 41, "a" * 63, "a" * 65, "g" * 40, 123],
)
def test_git_object_id_rejects_nonstandard_values(invalid: object) -> None:
    payload = _command().model_dump(mode="python")
    payload["git_head"] = invalid
    with pytest.raises(ValueError):
        StartRun.model_validate(payload)


def test_submit_message_digest_covers_message_identity_and_content() -> None:
    start = _command()
    submit = SubmitMessageRun(
        **start.model_dump(mode="python"),
        conversation_id=UUID(int=101),
        client_message_id=UUID(int=102),
        content="repair this",
    )
    for field, changed in {
        "conversation_id": UUID(int=103),
        "client_message_id": UUID(int=104),
        "content": "different",
    }.items():
        assert request_digest(submit.model_copy(update={field: changed})) != request_digest(
            submit
        )
    assert submit.command_type == "SUBMIT_MESSAGE"


def test_same_command_is_replayed_and_changed_request_conflicts(tmp_path: Path) -> None:
    database = _database(tmp_path / "receipt.sqlite3")
    store = ReceiptStore()
    command = _command(UUID(int=1))
    with ApplicationUnitOfWork(database) as uow:
        first = store.accept(uow.session, command)
        repeated = store.accept(uow.session, command)
        assert repeated == first
        with pytest.raises(IdempotencyConflictError):
            store.accept(uow.session, _command(UUID(int=1), task="changed"))
        uow.commit()
    database.close()


def test_receipt_state_machine_is_closed_and_timestamp_is_caller_owned(tmp_path: Path) -> None:
    database = _database(tmp_path / "terminal.sqlite3")
    store = ReceiptStore()
    command = _command()
    terminal_at = utc_now() + timedelta(seconds=1)
    with ApplicationUnitOfWork(database) as uow:
        store.accept(uow.session, command)
        store.mark_in_progress(uow.session, command.command_id, UUID(int=33))
        terminal = store.complete(uow.session, command.command_id, at=terminal_at)
        assert terminal.status is ReceiptStatus.COMPLETED
        assert terminal.updated_at == terminal_at
        with pytest.raises(InvalidReceiptTransitionError):
            store.fail(uow.session, command.command_id, at=terminal_at)
        uow.commit()
    database.close()


@pytest.mark.parametrize(
    ("method", "terminal"),
    [
        ("complete", ReceiptStatus.COMPLETED),
        ("fail", ReceiptStatus.FAILED),
        ("mark_indeterminate", ReceiptStatus.INDETERMINATE),
    ],
)
def test_each_terminal_primitive_uses_cas(
    tmp_path: Path, method: str, terminal: ReceiptStatus
) -> None:
    database = _database(tmp_path / f"{terminal.value}.sqlite3")
    store = ReceiptStore()
    command = _command()
    at = utc_now()
    with ApplicationUnitOfWork(database) as uow:
        store.accept(uow.session, command)
        store.mark_in_progress(uow.session, command.command_id, UUID(int=4))
        result = getattr(store, method)(uow.session, command.command_id, at=at)
        assert result.status is terminal
        assert result.updated_at == at
        uow.commit()
    database.close()


def test_uow_rolls_back_without_commit_and_rejects_ambiguous_reuse(tmp_path: Path) -> None:
    database = _database(tmp_path / "uow.sqlite3")
    command = _command()
    store = ReceiptStore()
    with ApplicationUnitOfWork(database) as uow:
        store.accept(uow.session, command)
    with database.session() as session:
        assert session.scalar(select(ApplicationCommandReceiptRow)) is None

    with ApplicationUnitOfWork(database) as committed:
        store.accept(committed.session, command)
        retained_session = committed.session
        committed.commit()
        with pytest.raises(UnitOfWorkStateError):
            committed.commit()
        with pytest.raises(InvalidRequestError):
            retained_session.scalar(select(ApplicationCommandReceiptRow))
    with pytest.raises(UnitOfWorkStateError):
        _ = committed.session
    database.close()


class _FakeDialect:
    name = "other"


class _FakeConnection:
    dialect = _FakeDialect()


class _FailingSession:
    def __init__(self, *failures: str) -> None:
        self.failures = set(failures)
        self.rollback_calls = 0
        self.close_calls = 0

    def connection(self) -> _FakeConnection:
        if "begin" in self.failures:
            raise RuntimeError("secret database path")
        return _FakeConnection()

    def commit(self) -> None:
        if "commit" in self.failures:
            raise RuntimeError("secret SQL parameters")

    def rollback(self) -> None:
        self.rollback_calls += 1
        if "rollback" in self.failures:
            raise RuntimeError("secret rollback")

    def close(self) -> None:
        self.close_calls += 1
        if "close" in self.failures:
            raise RuntimeError("secret close")


class _FakeDatabase:
    def __init__(self, session: _FailingSession) -> None:
        self.fake_session = session

    def new_session(self) -> _FailingSession:
        return self.fake_session


@pytest.mark.parametrize("failure", ["begin", "commit", "rollback", "close"])
def test_uow_maps_boundary_failures_and_always_attempts_close(failure: str) -> None:
    session = _FailingSession(failure)
    uow = ApplicationUnitOfWork(_FakeDatabase(session))  # type: ignore[arg-type]
    with pytest.raises(PersistenceBoundaryError) as raised:
        with uow:
            if failure == "commit":
                uow.commit()
    assert str(raised.value) == "kernel persistence operation failed"
    assert "secret" not in str(raised.value)
    assert session.close_calls == 1
    with pytest.raises(UnitOfWorkStateError):
        _ = uow.session


def test_uow_cleanup_failure_never_overrides_business_exception() -> None:
    session = _FailingSession("rollback", "close")
    uow = ApplicationUnitOfWork(_FakeDatabase(session))  # type: ignore[arg-type]
    business = RuntimeError("business failure")
    with pytest.raises(RuntimeError) as raised:
        with uow:
            raise business
    assert raised.value is business
    assert session.rollback_calls == 1
    assert session.close_calls == 1


def test_uow_maps_session_factory_failure() -> None:
    class BrokenDatabase:
        def new_session(self) -> object:
            raise RuntimeError("secret engine URL")

    with pytest.raises(PersistenceBoundaryError) as raised:
        with ApplicationUnitOfWork(BrokenDatabase()):  # type: ignore[arg-type]
            pytest.fail("unreachable")
    assert str(raised.value) == "kernel persistence operation failed"


def test_receipt_store_maps_sqlalchemy_failure_without_leaking_details() -> None:
    class SqlFailSession:
        def execute(self, statement: object) -> None:
            del statement
            raise SQLAlchemyError("secret SQL /tmp/private.sqlite3")

    with pytest.raises(PersistenceBoundaryError) as raised:
        ReceiptStore().accept(SqlFailSession(), _command())  # type: ignore[arg-type]
    assert str(raised.value) == "kernel persistence operation failed"
    assert "secret" not in str(raised.value)
