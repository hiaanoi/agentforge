from __future__ import annotations

from collections.abc import Callable

from parcel_flow.dispatcher import ParcelDispatcher
from parcel_flow.models import DispatchResult
from parcel_flow.store import ParcelStore


class OwnershipUnavailable(RuntimeError):
    pass


class ParcelService:
    def __init__(self, store: ParcelStore) -> None:
        self.store = store
        self.dispatcher = ParcelDispatcher(store)

    def process(
        self,
        command_id: str,
        parcel_id: str,
        owner_token: str,
        *,
        before_claim: Callable[[], None] | None = None,
        after_dispatch: Callable[[], None] | None = None,
    ) -> DispatchResult:
        self.store.ensure_receipt(command_id, parcel_id)
        receipt = self.store.get_receipt(command_id)
        if receipt.status == "COMPLETED" and receipt.result is not None:
            dispatch = self.store.dispatches_for(command_id)[0]
            return DispatchResult(
                command_id, parcel_id, int(dispatch["dispatch_id"]), receipt.result
            )
        if before_claim is not None:
            before_claim()
        if not self.store.claim(command_id, owner_token, receipt.owner_token):
            current = self.store.get_receipt(command_id)
            if current.status == "COMPLETED" and current.result is not None:
                dispatch = self.store.dispatches_for(command_id)[0]
                return DispatchResult(
                    command_id, parcel_id, int(dispatch["dispatch_id"]), current.result
                )
            raise OwnershipUnavailable(command_id)
        result = self.dispatcher.dispatch(command_id, parcel_id)
        if after_dispatch is not None:
            after_dispatch()
        self.store.complete(command_id, owner_token, result.message)
        return result
