from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from parcel_flow.service import OwnershipUnavailable, ParcelService
from parcel_flow.store import ParcelStore


def _crash() -> None:
    raise RuntimeError("handoff interrupted")


def test_completed_result_is_reused_on_repeated_processing(tmp_path) -> None:
    service = ParcelService(ParcelStore(tmp_path / "reuse.sqlite3"))
    command = f"command-{uuid4()}"
    first = service.process(command, "parcel-a", "worker-a")
    second = service.process(command, "parcel-a", "worker-b")
    assert second == first
    assert len(service.store.dispatches_for(command)) == 1


def test_repeated_recovery_keeps_one_durable_dispatch(tmp_path) -> None:
    database = tmp_path / "recovery.sqlite3"
    command = f"command-{uuid4()}"
    with pytest.raises(RuntimeError, match="interrupted"):
        ParcelService(ParcelStore(database)).process(
            command, "parcel-b", "worker-a", after_dispatch=_crash
        )
    recovered = ParcelService(ParcelStore(database))
    first = recovered.process(command, "parcel-b", "worker-b")
    second = ParcelService(ParcelStore(database)).process(command, "parcel-b", "worker-c")
    assert first == second
    assert len(recovered.store.dispatches_for(command)) == 1


def test_stale_owner_can_be_recovered_without_repeating_dispatch(tmp_path) -> None:
    database = tmp_path / "stale.sqlite3"
    with pytest.raises(RuntimeError):
        ParcelService(ParcelStore(database)).process(
            "stale-command", "parcel-c", "departed-worker", after_dispatch=_crash
        )
    service = ParcelService(ParcelStore(database))
    service.process("stale-command", "parcel-c", "replacement-worker")
    receipt = service.store.get_receipt("stale-command")
    assert receipt.status == "COMPLETED"
    assert receipt.owner_token == "replacement-worker"
    assert len(service.store.dispatches_for("stale-command")) == 1


def test_active_owner_without_a_dispatch_cannot_be_replaced(tmp_path) -> None:
    store = ParcelStore(tmp_path / "active.sqlite3")
    store.ensure_receipt("active-command", "parcel-active")
    assert store.claim("active-command", "active-worker", None)

    with pytest.raises(OwnershipUnavailable):
        ParcelService(store).process("active-command", "parcel-active", "second-worker")

    receipt = store.get_receipt("active-command")
    assert receipt.owner_token == "active-worker"
    assert store.dispatches_for("active-command") == []


def test_command_id_cannot_be_reused_for_a_different_parcel(tmp_path) -> None:
    service = ParcelService(ParcelStore(tmp_path / "payload.sqlite3"))
    service.process("bound-command", "parcel-original", "worker-a")

    with pytest.raises(ValueError, match="different parcel"):
        service.process("bound-command", "parcel-other", "worker-b")

    assert len(service.store.dispatches_for("bound-command")) == 1


def test_concurrent_claims_cross_a_barrier_but_only_one_dispatch_is_stored(tmp_path) -> None:
    database = tmp_path / "concurrent.sqlite3"
    command = f"command-{uuid4()}"
    barrier = Barrier(2)

    def run(owner: str):
        service = ParcelService(ParcelStore(database))
        try:
            return service.process(command, "parcel-d", owner, before_claim=barrier.wait)
        except OwnershipUnavailable:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(run, ("worker-a", "worker-b")))

    store = ParcelStore(database)
    assert any(result is not None for result in outcomes)
    assert len(store.dispatches_for(command)) == 1
    assert store.get_receipt(command).status == "COMPLETED"
