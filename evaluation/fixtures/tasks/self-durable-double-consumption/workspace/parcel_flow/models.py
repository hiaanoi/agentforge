from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Receipt:
    command_id: str
    parcel_id: str
    status: str
    owner_token: str | None
    result: str | None


@dataclass(frozen=True)
class DispatchResult:
    command_id: str
    parcel_id: str
    dispatch_id: int
    message: str
