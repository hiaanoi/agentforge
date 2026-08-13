import pytest
from parcel_flow.service import ParcelService
from parcel_flow.store import ParcelStore


def test_restart_after_dispatch_does_not_store_a_second_dispatch(tmp_path) -> None:
    database = tmp_path / "parcels.sqlite3"
    first = ParcelService(ParcelStore(database))

    def interrupt_handoff() -> None:
        raise RuntimeError("service stopped during handoff")

    with pytest.raises(RuntimeError, match="stopped"):
        first.process("command-17", "parcel-42", "worker-a", after_dispatch=interrupt_handoff)

    restarted = ParcelService(ParcelStore(database))
    result = restarted.process("command-17", "parcel-42", "worker-b")

    dispatches = restarted.store.dispatches_for("command-17")
    assert result.message == "Parcel parcel-42 dispatched"
    assert len(dispatches) == 1
