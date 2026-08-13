from datetime import UTC, timedelta
from pathlib import Path

from agentforge.domain.enums import EventType, RunStatus
from agentforge.domain.models import Run
from agentforge.persistence.database import Database
from agentforge.persistence.legacy_evaluator import LegacyEvaluatorEventRepository
from agentforge.persistence.repositories import (
    CheckpointRepository,
    EventRepository,
    RunRepository,
)
from agentforge.persistence.run_leases import RunLeaseStore


def make_database(path: Path) -> Database:
    database = Database.from_path(path)
    database.create_schema()
    return database


def test_run_survives_database_reopen(tmp_path: Path) -> None:
    path = tmp_path / "agentforge.sqlite3"
    database = make_database(path)
    repository = RunRepository(database)
    run = repository.create(Run(task="persist me"))
    run.transition_to(RunStatus.RUNNING)
    authority = (
        RunLeaseStore(database)
        .acquire(run.run_id, owner_id="test:persistence", ttl=timedelta(seconds=30))
        .authority
    )
    repository.save(run, authority=authority)
    database.close()

    reopened = make_database(path)
    loaded = RunRepository(reopened).get(run.run_id)

    assert loaded.task == "persist me"
    assert loaded.status is RunStatus.RUNNING
    assert loaded.created_at.tzinfo is UTC
    assert loaded.updated_at.tzinfo is UTC
    reopened.close()


def test_event_sequence_is_monotonic_and_isolated_per_run(tmp_path: Path) -> None:
    database = make_database(tmp_path / "events.sqlite3")
    runs = RunRepository(database)
    events = LegacyEvaluatorEventRepository(database)
    first = runs.create(Run(task="first"))
    second = runs.create(Run(task="second"))

    first_created = events.append(first.run_id, EventType.RUN_CREATED)
    second_created = events.append(second.run_id, EventType.RUN_CREATED)
    first_started = events.append(first.run_id, EventType.RUN_STARTED)

    assert first_created.sequence_number == 1
    assert first_started.sequence_number == 2
    assert second_created.sequence_number == 1
    assert [event.run_id for event in events.list_for_run(first.run_id)] == [
        first.run_id,
        first.run_id,
    ]
    database.close()


def test_latest_checkpoint_is_persisted(tmp_path: Path) -> None:
    path = tmp_path / "checkpoints.sqlite3"
    database = make_database(path)
    run = RunRepository(database).create(Run(task="checkpoint"))
    authority = (
        RunLeaseStore(database)
        .acquire(run.run_id, owner_id="test:checkpoint", ttl=timedelta(seconds=30))
        .authority
    )
    checkpoints = CheckpointRepository(database)
    checkpoints.save(
        run.run_id,
        1,
        {"history": [{"message": "first"}]},
        authority=authority,
    )
    expected = checkpoints.save(
        run.run_id,
        2,
        {"history": [{"message": "second"}]},
        authority=authority,
    )
    database.close()

    reopened = make_database(path)
    actual = CheckpointRepository(reopened).latest(run.run_id)

    assert actual is not None
    assert actual.checkpoint_id == expected.checkpoint_id
    assert actual.step_number == 2
    assert actual.runtime_state["history"] == [{"message": "second"}]
    reopened.close()


def test_run_event_and_checkpoint_survive_the_same_database_reopen(tmp_path: Path) -> None:
    path = tmp_path / "durable.sqlite3"
    database = make_database(path)
    runs = RunRepository(database)
    events = LegacyEvaluatorEventRepository(database)
    checkpoints = CheckpointRepository(database)
    run = runs.create(Run(task="durable aggregate"))
    authority = (
        RunLeaseStore(database)
        .acquire(run.run_id, owner_id="test:durable", ttl=timedelta(seconds=30))
        .authority
    )
    event = events.append(run.run_id, EventType.RUN_CREATED, {"source": "test"})
    checkpoint = checkpoints.save(run.run_id, 0, {"history": []}, authority=authority)
    database.close()

    reopened = make_database(path)
    loaded_run = RunRepository(reopened).get(run.run_id)
    loaded_events = EventRepository(reopened).list_for_run(run.run_id)
    loaded_checkpoint = CheckpointRepository(reopened).latest(run.run_id)

    assert loaded_run.run_id == run.run_id
    assert [loaded.event_id for loaded in loaded_events] == [event.event_id]
    assert loaded_events[0].created_at.tzinfo is UTC
    assert loaded_checkpoint is not None
    assert loaded_checkpoint.checkpoint_id == checkpoint.checkpoint_id
    assert loaded_checkpoint.created_at.tzinfo is UTC
    reopened.close()
