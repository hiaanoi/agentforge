from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import timedelta
from typing import Generic, TypeVar, cast
from uuid import UUID

from agentforge.application.contracts import OutcomeStatus
from agentforge.application.kernel_errors import ProductKernelError, StaleFenceError
from agentforge.persistence.event_log import RunLeaseAuthority
from agentforge.persistence.run_leases import RunLeaseStore

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class DriverOutcome(Generic[T]):
    """Public ownership outcome; UNKNOWN never fabricates a terminal Run fact."""

    outcome: OutcomeStatus
    value: T | None
    disposition: str | None = None


class RunLeaseLostError(ProductKernelError):
    def __init__(self) -> None:
        super().__init__("run ownership was lost")


@dataclass(frozen=True, slots=True)
class RunOwnership:
    """Explicit live authority handle passed through the production call chain."""

    _snapshot: Callable[[], RunLeaseAuthority]
    _expect_release: Callable[[], RunLeaseAuthority] | None = None

    @property
    def authority(self) -> RunLeaseAuthority:
        authority = self._snapshot()
        if type(authority) is not RunLeaseAuthority:
            raise RunLeaseLostError()
        return authority

    def expect_release(self) -> RunLeaseAuthority:
        """Close side-effect access and return the authority for an atomic release."""
        if self._expect_release is None:
            raise RunLeaseLostError()
        authority = self._expect_release()
        if type(authority) is not RunLeaseAuthority:
            raise RunLeaseLostError()
        return authority


class RunDriver:
    """Own the execution lease and heartbeat for one asynchronous Run drive."""

    def __init__(
        self,
        leases: RunLeaseStore,
        *,
        run_id: UUID,
        owner_id: str,
        ttl: timedelta,
        heartbeat_interval: timedelta,
    ) -> None:
        if (
            type(ttl) is not timedelta
            or ttl <= timedelta(0)
            or type(heartbeat_interval) is not timedelta
            or heartbeat_interval <= timedelta(0)
            or heartbeat_interval * 3 >= ttl
        ):
            raise ValueError("heartbeat interval must be strictly below one third of ttl")
        self._leases = leases
        self._run_id = run_id
        self._owner_id = owner_id
        self._ttl = ttl
        self._heartbeat_interval = heartbeat_interval
        self._authority: RunLeaseAuthority | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._lost = False
        self._lost_event = asyncio.Event()
        self._release_expected = False

    @property
    def authority(self) -> RunLeaseAuthority:
        if self._authority is None:
            raise RunLeaseLostError()
        return self._authority

    @property
    def lost(self) -> bool:
        return self._lost

    @property
    def owner_id(self) -> str:
        return self._owner_id

    @property
    def heartbeat_task(self) -> asyncio.Task[None] | None:
        return self._heartbeat_task

    def require_side_effect_authority(self) -> RunLeaseAuthority:
        if self._lost or self._release_expected:
            raise RunLeaseLostError()
        return self.authority

    def expect_release(self) -> RunLeaseAuthority:
        if self._lost or self._release_expected:
            raise RunLeaseLostError()
        authority = self.authority
        self._release_expected = True
        return authority

    async def run(self, operation: Callable[[RunOwnership], Awaitable[T]]) -> T:
        """Evaluator compatibility seam; product callers use ``run_outcome``."""
        if self._heartbeat_task is not None or self._authority is not None:
            raise RuntimeError("RunDriver is already active")
        lease = self._leases.acquire(self._run_id, owner_id=self._owner_id, ttl=self._ttl)
        self._authority = lease.authority
        self._lost = False
        self._lost_event.clear()
        self._release_expected = False
        ownership = RunOwnership(self.require_side_effect_authority, self.expect_release)
        task = asyncio.create_task(self._heartbeat(), name=f"run-lease-{self._run_id}")
        self._heartbeat_task = task
        try:
            return await operation(ownership)
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            self._heartbeat_task = None
            authority = self._authority
            self._authority = None
            if authority is not None and not self._lost:
                try:
                    self._leases.release(authority)
                except StaleFenceError:
                    if not self._release_expected:
                        self._lost = True
            self._release_expected = False

    async def run_outcome(
        self,
        operation: Callable[[RunOwnership], Awaitable[T]],
        *,
        authority: RunLeaseAuthority | None = None,
    ) -> DriverOutcome[T]:
        if self._heartbeat_task is not None or self._authority is not None:
            raise RuntimeError("RunDriver is already active")
        if authority is None:
            lease = self._leases.acquire(
                self._run_id,
                owner_id=self._owner_id,
                ttl=self._ttl,
            )
            authority = lease.authority
        elif authority.run_id != self._run_id or authority.owner_id != self._owner_id:
            raise StaleFenceError()
        else:
            with self._leases.session() as session:
                self._leases.require_active(session, authority)
        self._authority = authority
        self._lost = False
        self._lost_event.clear()
        self._release_expected = False
        ownership = RunOwnership(self.require_side_effect_authority, self.expect_release)
        task = asyncio.create_task(self._heartbeat(), name=f"run-lease-{self._run_id}")
        self._heartbeat_task = task
        operation_task: asyncio.Future[T] = asyncio.ensure_future(operation(ownership))
        lost_wait = asyncio.create_task(self._lost_event.wait())
        try:
            waitables = {
                cast(asyncio.Future[object], operation_task),
                cast(asyncio.Future[object], lost_wait),
            }
            done, _ = await asyncio.wait(waitables, return_when=asyncio.FIRST_COMPLETED)
            # A workflow may intentionally release its own lease as part of a
            # durable pause transaction.  If its operation already completed,
            # return that proven boundary result rather than turning it into an
            # ownership-loss UNKNOWN merely because the heartbeat observed the
            # release in the same scheduling turn.
            if operation_task in done:
                lost_wait.cancel()
                with suppress(asyncio.CancelledError):
                    await lost_wait
                return DriverOutcome(OutcomeStatus.UNVERIFIED, await operation_task)
            if lost_wait in done and self._lost:
                operation_task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await operation_task
                return DriverOutcome(OutcomeStatus.UNKNOWN, None)
            lost_wait.cancel()
            with suppress(asyncio.CancelledError):
                await lost_wait
            return DriverOutcome(OutcomeStatus.UNVERIFIED, await operation_task)
        finally:
            if not operation_task.done():
                operation_task.cancel()
                with suppress(asyncio.CancelledError):
                    await operation_task
            if not lost_wait.done():
                lost_wait.cancel()
                with suppress(asyncio.CancelledError):
                    await lost_wait
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            self._heartbeat_task = None
            authority = self._authority
            self._authority = None
            if authority is not None and not self._lost:
                try:
                    self._leases.release(authority)
                except StaleFenceError:
                    if not self._release_expected:
                        self._lost = True
            self._release_expected = False

    async def _heartbeat(self) -> None:
        delay = self._heartbeat_interval.total_seconds()
        while True:
            await asyncio.sleep(delay)
            authority = self._authority
            if authority is None:
                return
            try:
                renewed = await asyncio.to_thread(
                    self._leases.renew,
                    authority,
                    ttl=self._ttl,
                )
            except (StaleFenceError, ProductKernelError, ValueError):
                if self._release_expected:
                    return
                self._lost = True
                self._lost_event.set()
                return
            self._authority = renewed.authority
