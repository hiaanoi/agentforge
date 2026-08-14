from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from agentforge.application.kernel_errors import StaleFenceError
from agentforge.application.run_driver import RunDriver, RunLeaseLostError, RunOwnership
from agentforge.domain.enums import RunStatus
from agentforge.domain.models import ApprovalRequest, Checkpoint, Run
from agentforge.persistence.approval_workflow import ApprovalWorkflow
from agentforge.persistence.database import Database
from agentforge.persistence.event_log import RunLeaseAuthority
from agentforge.persistence.repositories import RunRepository
from agentforge.persistence.run_leases import (
    LeaseAcquireDisposition,
    RunLease,
    RunLeaseStore,
)


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 8, 10, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def kernel(tmp_path: Path) -> tuple[Database, UUID, Clock, RunLeaseStore]:
    database = Database.from_path(tmp_path / "leases.sqlite3")
    database.create_schema()
    run_id = RunRepository(database).create(Run(task="lease probe")).run_id
    clock = Clock()
    return database, run_id, clock, RunLeaseStore(database, clock=clock)


def test_acquire_renew_release_and_expired_takeover_are_monotonic(tmp_path: Path) -> None:
    database, run_id, clock, leases = kernel(tmp_path)
    first = leases.acquire(run_id, owner_id="worker-one", ttl=timedelta(seconds=3))
    same = leases.acquire(run_id, owner_id="worker-one", ttl=timedelta(seconds=3))
    assert same.lease_token == first.lease_token
    assert same.fencing_token == first.fencing_token == 1
    assert same.version == first.version + 1

    with pytest.raises(StaleFenceError, match="stale"):
        leases.acquire(run_id, owner_id="worker-two", ttl=timedelta(seconds=3))

    clock.advance(4)
    second = leases.acquire(run_id, owner_id="worker-two", ttl=timedelta(seconds=3))
    assert second.lease_token != first.lease_token
    assert second.fencing_token == first.fencing_token + 1
    assert second.version == same.version + 1
    with pytest.raises(StaleFenceError, match="stale"):
        leases.renew(first.authority, ttl=timedelta(seconds=3))

    leases.release(second.authority)
    assert leases.current(run_id) is None
    third = leases.acquire(run_id, owner_id="worker-three", ttl=timedelta(seconds=3))
    assert third.fencing_token == second.fencing_token + 1
    assert third.version == second.version + 2
    database.close()


def test_release_accepts_current_epoch_after_heartbeat_version_advances(tmp_path: Path) -> None:
    database, run_id, _, leases = kernel(tmp_path)
    lease = leases.acquire(run_id, owner_id="worker", ttl=timedelta(seconds=30))

    # A driver heartbeat can advance only the CAS version while a synchronous
    # pause transaction is preparing to release the same ownership epoch.
    leases.renew(lease.authority, ttl=timedelta(seconds=30))

    leases.release(lease.authority)

    assert leases.current(run_id) is None
    database.close()


def test_two_database_atomic_acquire_or_observe_has_exactly_one_owner(
    tmp_path: Path,
) -> None:
    database, run_id, _, _ = kernel(tmp_path)
    # Separate engine/session factories simulate two independently-created app
    # processes, rather than two calls through one Database object.
    competing_database = Database.from_path(tmp_path / "leases.sqlite3")
    barrier = Barrier(2)
    attempt_owners = (f"app-a:{uuid4()}", f"app-b:{uuid4()}")

    def acquire(index: int) -> tuple[LeaseAcquireDisposition, RunLease, str]:
        barrier.wait()
        result = RunLeaseStore((database, competing_database)[index]).acquire_or_observe(
            run_id, owner_id=attempt_owners[index], ttl=timedelta(seconds=30)
        )
        return result.disposition, result.lease, attempt_owners[index]

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(acquire, range(2)))

    assert sorted(item[0] for item in results) == [
        LeaseAcquireDisposition.OBSERVER,
        LeaseAcquireDisposition.OWNER,
    ]
    owner = next(
        lease
        for disposition, lease, _ in results
        if disposition is LeaseAcquireDisposition.OWNER
    )
    observer = next(
        lease
        for disposition, lease, _ in results
        if disposition is LeaseAcquireDisposition.OBSERVER
    )
    assert observer.authority == owner.authority
    assert owner.owner_id in attempt_owners
    assert len(set(attempt_owners)) == 2
    with competing_database.session() as session:
        RunLeaseStore(None).claim_write(session, owner.authority)
    database.close()
    competing_database.close()


def test_authority_requires_run_token_fence_and_version_and_active_expiry(tmp_path: Path) -> None:
    database, run_id, clock, leases = kernel(tmp_path)
    lease = leases.acquire(run_id, owner_id="owner", ttl=timedelta(seconds=2))
    with database.session() as session:
        leases.require_active(session, lease.authority)
        for authority in (
            RunLeaseAuthority(run_id, "owner", uuid4(), lease.fencing_token, lease.version),
            RunLeaseAuthority(
                run_id, "owner", lease.lease_token, lease.fencing_token + 1, lease.version
            ),
            RunLeaseAuthority(
                run_id, "owner", lease.lease_token, lease.fencing_token, lease.version + 1
            ),
        ):
            with pytest.raises(StaleFenceError, match="stale"):
                leases.require_active(session, authority)
    clock.advance(3)
    with database.session() as session, pytest.raises(StaleFenceError, match="stale"):
        leases.require_active(session, lease.authority)
    database.close()


@pytest.mark.parametrize("ttl", [timedelta(0), timedelta(seconds=-1), True, 3])
def test_lease_rejects_invalid_ttl(tmp_path: Path, ttl: object) -> None:
    database, run_id, _, _ = kernel(tmp_path)
    leases = RunLeaseStore(database)
    with pytest.raises(ValueError):
        leases.acquire(run_id, owner_id="owner", ttl=ttl)  # type: ignore[arg-type]
    assert leases.current(run_id) is None
    database.close()


@pytest.mark.parametrize(
    "overrides",
    [
        {"fencing_token": True},
        {"version": True},
        {"heartbeat_at": datetime(2026, 8, 10)},
        {"expires_at": datetime(2026, 8, 10)},
    ],
)
def test_run_lease_is_strict_and_requires_time_topology(overrides: dict[str, object]) -> None:
    now = datetime(2026, 8, 10, tzinfo=UTC)
    values: dict[str, object] = {
        "run_id": uuid4(),
        "owner_id": "owner",
        "lease_token": uuid4(),
        "fencing_token": 1,
        "version": 1,
        "acquired_at": now,
        "heartbeat_at": now,
        "expires_at": now + timedelta(seconds=3),
    }
    values.update(overrides)
    with pytest.raises(ValidationError):
        RunLease.model_validate(values)


@pytest.mark.asyncio
async def test_driver_heartbeats_and_cleans_up_task(tmp_path: Path) -> None:
    database, run_id, _, leases = kernel(tmp_path)
    driver = RunDriver(
        leases,
        run_id=run_id,
        owner_id="driver",
        ttl=timedelta(seconds=3),
        heartbeat_interval=timedelta(milliseconds=20),
    )
    observed_versions: list[int] = []

    async def operation(ownership: RunOwnership) -> str:
        for _ in range(12):
            await asyncio.sleep(0.03)
            observed_versions.append(ownership.authority.version)
        return "done"

    assert await driver.run(operation) == "done"
    assert max(observed_versions) > min(observed_versions)
    assert driver.heartbeat_task is None
    database.close()


@pytest.mark.asyncio
async def test_driver_stops_new_side_effect_after_heartbeat_loss(tmp_path: Path) -> None:
    database, run_id, clock, leases = kernel(tmp_path)
    driver = RunDriver(
        leases,
        run_id=run_id,
        owner_id="driver",
        ttl=timedelta(seconds=1),
        heartbeat_interval=timedelta(milliseconds=20),
    )

    async def operation(ownership: RunOwnership) -> None:
        captured = ownership.authority
        clock.advance(2)
        leases.acquire(run_id, owner_id="replacement", ttl=timedelta(seconds=3))
        await asyncio.sleep(0.08)
        assert driver.lost is True
        with pytest.raises(RunLeaseLostError):
            driver.require_side_effect_authority()
        with database.session() as session, pytest.raises(StaleFenceError):
            leases.require_active(session, captured)

    result = await driver.run_outcome(operation)
    assert result.outcome.value == "UNKNOWN"
    assert driver.heartbeat_task is None
    database.close()


@pytest.mark.asyncio
async def test_driver_returns_completed_operation_when_it_intentionally_releases_lease(
    tmp_path: Path,
) -> None:
    database, run_id, _, leases = kernel(tmp_path)
    driver = RunDriver(
        leases,
        run_id=run_id,
        owner_id="intentional-pause",
        ttl=timedelta(seconds=3),
        heartbeat_interval=timedelta(milliseconds=20),
    )

    async def pause_at_command_boundary(ownership: RunOwnership) -> str:
        leases.release(ownership.expect_release())
        await asyncio.sleep(0)
        return "PAUSED"

    result = await driver.run_outcome(pause_at_command_boundary)

    assert result.outcome.value == "UNVERIFIED"
    assert result.value == "PAUSED"
    database.close()


@pytest.mark.asyncio
async def test_driver_waits_for_operation_after_expected_boundary_release(
    tmp_path: Path,
) -> None:
    database, run_id, _, leases = kernel(tmp_path)
    driver = RunDriver(
        leases,
        run_id=run_id,
        owner_id="intentional-pause",
        ttl=timedelta(seconds=3),
        heartbeat_interval=timedelta(milliseconds=20),
    )

    async def pause_at_command_boundary(ownership: RunOwnership) -> str:
        leases.release(ownership.expect_release())
        heartbeat = driver.heartbeat_task
        assert heartbeat is not None
        await asyncio.shield(heartbeat)
        return "PAUSED"

    result = await asyncio.wait_for(
        driver.run_outcome(pause_at_command_boundary), timeout=2
    )

    assert result.outcome.value == "UNVERIFIED"
    assert result.value == "PAUSED"
    database.close()


@pytest.mark.asyncio
async def test_latest_ownership_completes_write_after_many_heartbeats(
    tmp_path: Path,
) -> None:
    database = Database.from_path(tmp_path / "live-heartbeats.sqlite3")
    database.create_schema()
    runs = RunRepository(database)
    run_id = runs.create(Run(task="live heartbeat probe")).run_id
    leases = RunLeaseStore(database)
    driver = RunDriver(
        leases,
        run_id=run_id,
        owner_id="driver",
        ttl=timedelta(seconds=3),
        heartbeat_interval=timedelta(milliseconds=10),
    )

    async def operation(ownership: RunOwnership) -> RunStatus:
        await asyncio.sleep(0.14)
        run = runs.get(run_id)
        run.transition_to(RunStatus.RUNNING)
        runs.save(run, authority=ownership.authority)
        return runs.get(run_id).status

    result = await driver.run_outcome(operation)

    assert result.outcome.value == "UNVERIFIED"
    assert result.value is RunStatus.RUNNING
    database.close()


def test_independent_connections_choose_at_most_one_owner(tmp_path: Path) -> None:
    database, run_id, _, _ = kernel(tmp_path)
    barrier = Barrier(2)

    def acquire(owner: str) -> str:
        barrier.wait()
        try:
            RunLeaseStore(database).acquire(
                run_id,
                owner_id=owner,
                ttl=timedelta(seconds=30),
            )
        except StaleFenceError:
            return "REJECTED"
        return "ACQUIRED"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(acquire, ("one", "two")))

    assert sorted(outcomes) == ["ACQUIRED", "REJECTED"]
    database.close()


def test_approval_pause_releases_execution_lease_in_same_workflow(tmp_path: Path) -> None:
    database, run_id, _, _ = kernel(tmp_path)
    leases = RunLeaseStore(database)
    runs = RunRepository(database)
    run = runs.get(run_id)
    lease = leases.acquire(run_id, owner_id="executor", ttl=timedelta(seconds=30))
    run.transition_to(RunStatus.RUNNING)
    runs.save(run, authority=lease.authority)
    checkpoint = Checkpoint(run_id=run_id, step_number=1, runtime_state={})
    approval = ApprovalRequest(
        run_id=run_id,
        checkpoint_id=checkpoint.checkpoint_id,
        tool_name="write_file",
        sanitized_arguments={"path": "safe.txt"},
        request_digest="a" * 64,
    )

    ApprovalWorkflow(database).pause_for_approval(
        run, checkpoint, approval, authority=lease.authority
    )

    assert leases.current(run_id) is None
    database.close()
