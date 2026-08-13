from __future__ import annotations

from parcel_flow.models import DispatchResult
from parcel_flow.store import ParcelStore


class ParcelDispatcher:
    def __init__(self, store: ParcelStore) -> None:
        self.store = store

    def dispatch(self, command_id: str, parcel_id: str) -> DispatchResult:
        message = f"Parcel {parcel_id} dispatched"
        dispatch_id = self.store.record_dispatch(command_id, parcel_id, message)
        return DispatchResult(command_id, parcel_id, dispatch_id, message)
