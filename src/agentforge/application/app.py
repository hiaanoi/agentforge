from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import timedelta
from pathlib import Path
from typing import Self, cast, overload
from uuid import UUID, uuid4

from sqlalchemy import func, select

from agentforge.application.approval_commands import DecideApprovalCommand
from agentforge.application.commands import (
    APPLICATION_COMMAND_ADAPTER,
    ApplicationCommand,
    DecideApproval,
    ResumeRun,
    StartRun,
    TrustProfile,
)
from agentforge.application.contracts import ReceiptStatus
from agentforge.application.doctor import Doctor
from agentforge.application.errors import ApplicationFailure, application_error_from_exception
from agentforge.application.events import ProductEvent
from agentforge.application.kernel_errors import (
    IdempotencyConflictError,
    ProfileTrustMismatchError,
)
from agentforge.application.product_workspace import ProductWorkspaceCapture
from agentforge.application.projections import ProductProjector
from agentforge.application.queries import (
    APPLICATION_QUERY_ADAPTER,
    DoctorReport,
    ExportRunDetails,
    PendingApprovals,
    ProfileTrustDetails,
    RunDetails,
)
from agentforge.application.run_commands import ResumeRun as ResumeRunCommand
from agentforge.application.run_creation import ProductStartRunAssembler, RunCreationWorkflow
from agentforge.application.run_driver import DriverOutcome
from agentforge.application.runtime_factory import RuntimeComponents
from agentforge.application.views import (
    DoctorReportView,
    ExportRunDetailsView,
    LocalRunDetailsView,
    PendingApprovalsView,
    ProfileTrustDetailsView,
    RunProjectionFacts,
)
from agentforge.domain.enums import RunStatus
from agentforge.domain.errors import RunNotFoundError
from agentforge.domain.models import PersistedEvent
from agentforge.domain.repair import RepairCompletionStatus
from agentforge.persistence.approval_workflow import ApprovalWorkflow
from agentforge.persistence.database import Database
from agentforge.persistence.event_log import EventScopeType
from agentforge.persistence.product_tables import (
    ApplicationCommandReceiptRow,
    WorkspaceSourceBindingRow,
)
from agentforge.persistence.profile_trust import ProfileKernel, TrustedProfileIdentity
from agentforge.persistence.repositories import ApprovalRepository, RunRepository
from agentforge.persistence.resume_workflow import ResumeRunWorkflow
from agentforge.persistence.run_leases import RunLeaseStore
from agentforge.persistence.tables import Base, EventRow, RepairStateRow, RunRow
from agentforge.runtime.engine import AgentRuntime
from agentforge.tools.testing.profiles import TestProfileRegistry


class AgentApplication:
    """The product boundary: commands are durable, streams only observe facts."""

    _BOOKKEEPING_LIMIT = 64

    def __init__(
        self,
        database: Database,
        runtime: AgentRuntime | None,
        *,
        start_assembler: ProductStartRunAssembler | None = None,
        profiles: TestProfileRegistry | None = None,
        runtime_builder: (
            Callable[[], tuple[RuntimeComponents, ProductStartRunAssembler]] | None
        ) = None,
        database_path: Path | None = None,
        poll_interval: float = 0.02,
    ) -> None:
        if poll_interval <= 0:
            raise ValueError("poll interval must be positive")
        if (runtime is None) != (start_assembler is None):
            raise ValueError("runtime and product start assembly must be supplied together")
        if runtime is None and runtime_builder is None:
            raise ValueError("an application requires a runtime or a runtime builder")
        if runtime is not None and start_assembler is not None:
            if runtime is not start_assembler.components.runtime:
                raise ValueError("runtime does not match product start assembly")
            if database is not start_assembler.components.database:
                raise ValueError("database does not match product start assembly")
            if profiles is not None and profiles is not start_assembler.profiles:
                raise ValueError("profiles do not match product start assembly")
        self._database = database
        self._runtime = runtime
        self._start_assembler = start_assembler
        self._runtime_builder = runtime_builder
        self._database_path = database_path or (
            start_assembler.config.database_path if start_assembler is not None else None
        )
        self._creation = RunCreationWorkflow(database)
        self._approvals = ApprovalWorkflow(database)
        self._profiles = profiles or (
            start_assembler.profiles if start_assembler is not None else None
        )
        self._projector = ProductProjector()
        self._doctor = Doctor(database, profiles)
        self._poll_interval = poll_interval
        # This is intentionally process-instance, not command, identity.  A
        # command can be replayed by another application process while its
        # original owner is still live.
        self._instance_id = uuid4()
        self._drivers: OrderedDict[UUID, asyncio.Task[None]] = OrderedDict()
        self._driver_errors: OrderedDict[UUID, ApplicationFailure] = OrderedDict()
        self._resume_receipts = ResumeRunWorkflow(database)
        self._resume_locks: dict[UUID, asyncio.Lock] = {}
        self._resume_tasks: OrderedDict[UUID, asyncio.Task[None]] = OrderedDict()
        self._resume_task_runs: dict[UUID, UUID] = {}
        self._command_errors: OrderedDict[UUID, ApplicationFailure] = OrderedDict()
        self._closed = False
        self._close_lock = asyncio.Lock()

    @property
    def database_path(self) -> Path | None:
        return self._database_path

    def _ensure_runtime(self) -> None:
        if self._runtime is not None and self._start_assembler is not None:
            return
        if self._runtime_builder is None:
            raise RuntimeError("product runtime is unavailable")
        components, assembler = self._runtime_builder()
        if components.database is not self._database or assembler.components is not components:
            raise RuntimeError("product runtime builder returned a mismatched assembly")
        if self._profiles is not None and assembler.profiles is not self._profiles:
            raise RuntimeError("product runtime builder returned mismatched profiles")
        self._runtime = components.runtime
        self._start_assembler = assembler
        self._profiles = assembler.profiles

    async def __aenter__(self) -> Self:
        if self._closed:
            raise RuntimeError("application is closed")
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Stop only tasks owned by this application instance, idempotently."""
        async with self._close_lock:
            if self._closed:
                return
            self._closed = True
            tasks = tuple({*self._drivers.values(), *self._resume_tasks.values()})
            current = asyncio.current_task()
            for task in tasks:
                if task is not current and not task.done():
                    task.cancel()
            await asyncio.gather(
                *(task for task in tasks if task is not current), return_exceptions=True
            )
            self._drivers.clear()
            self._resume_tasks.clear()
            self._resume_task_runs.clear()
            self._resume_locks.clear()
            self._driver_errors.clear()
            self._command_errors.clear()

    async def stream(
        self, command: ApplicationCommand, *, after_cursor: int | None = None
    ) -> AsyncIterator[ProductEvent]:
        try:
            command = APPLICATION_COMMAND_ADAPTER.validate_python(
                command.model_dump(mode="python")
            )
            if after_cursor is not None and (
                type(after_cursor) is not int or after_cursor < 0
            ):
                raise ValueError("after_cursor must be a non-negative exact integer")
            scope_type, scope_id = await self._accept(command)
        except ApplicationFailure:
            raise
        except BaseException as exc:
            raise ApplicationFailure(application_error_from_exception(exc)) from None
        cursor = after_cursor or 0
        try:
            while True:
                if isinstance(command, ResumeRun):
                    boundary = self._resume_stream_boundary(command)
                    if boundary is None:
                        if self._stream_is_quiescent(command, scope_type, scope_id):
                            self._raise_stream_failure(command, scope_type, scope_id)
                            return
                        await asyncio.sleep(self._poll_interval)
                        continue
                    # A Resume stream is a command slice, not a replay of the
                    # whole Run.  The durable marker itself is the first fact
                    # in that slice.
                    cursor = max(cursor, boundary - 1)
                facts = self._events_after(scope_type, scope_id, cursor)
                if facts:
                    for fact in facts:
                        event = self._projector.event(fact)
                        cursor = event.cursor
                        yield event
                    if self._stream_is_quiescent(command, scope_type, scope_id):
                        self._raise_stream_failure(command, scope_type, scope_id)
                        return
                elif self._stream_is_quiescent(command, scope_type, scope_id):
                    self._raise_stream_failure(command, scope_type, scope_id)
                    return
                await asyncio.sleep(self._poll_interval)
        finally:
            # A consumer disconnect never owns nor cancels a durable Run driver.
            pass

    async def _accept(self, command: ApplicationCommand) -> tuple[str, str]:
        if self._closed:
            raise RuntimeError("application is closed")
        if isinstance(command, StartRun):
            self._ensure_runtime()
            assert self._start_assembler is not None
            replay = self._accepted_start(command)
            if replay is not None:
                return replay
            prepared = self._start_assembler.prepare(command)
            result = self._creation.create(
                prepared.command, prepared_workspace=prepared.workspace
            )
            # Capture predates the SQLite write transaction. Recheck after that
            # commit, before any model/tool driver is allowed to observe source.
            if result.recovery in {"CREATED", "REATTACH_CREATED"}:
                if not ProductWorkspaceCapture().matches_source(
                    command.workspace, prepared.workspace.source_digest
                ):
                    self._terminalize_start_source_drift(
                        result.run_id, command.command_id
                    )
                    raise RuntimeError("workspace changed before run driver start")
                self._ensure_start_driver(result.run_id, command.command_id)
            return "RUN", str(result.run_id)
        if isinstance(command, DecideApproval):
            approval = self._approvals.resolve_command(
                DecideApprovalCommand(**command.model_dump(exclude={"type"}))
            )
            return "RUN", str(approval.run_id)
        if isinstance(command, ResumeRun):
            self._ensure_runtime()
            internal = ResumeRunCommand(
                command_id=command.command_id,
                run_id=command.run_id,
                recovery_choice=command.recovery_choice,
            )
            receipt = self._resume_receipts.accept(internal)
            if receipt.status in {ReceiptStatus.ACCEPTED, ReceiptStatus.IN_PROGRESS}:
                self._start_resume_task(internal)
            return "RUN", str(command.run_id)
        if isinstance(command, TrustProfile):
            if self._profiles is None:
                raise ValueError("profile registry is unavailable")
            kernel = ProfileKernel(self._database, self._profiles)
            challenge = kernel.challenge(command.identity.profile_id, purpose=command.purpose)
            expected_identity = TrustedProfileIdentity.model_validate(
                challenge.model_dump(include=set(TrustedProfileIdentity.model_fields))
            )
            if (
                command.workspace_identity != challenge.workspace_identity
                or command.identity != expected_identity
            ):
                raise ValueError("profile identity no longer matches")
            kernel.trust(challenge, command_id=command.command_id)
            return "WORKSPACE", command.workspace_identity
        raise ValueError("closed application command is invalid")

    def _terminalize_start_source_drift(self, run_id: UUID, command_id: UUID) -> None:
        """Turn a post-commit source mismatch into replayable Start facts."""
        leases = RunLeaseStore(self._database)
        acquired = leases.acquire_or_observe(
            run_id,
            owner_id=f"agent-app:{self._instance_id}:source-drift:{command_id}",
            ttl=timedelta(seconds=30),
        )
        authority = acquired.authority
        if authority is None:
            # A different live application owns the Run and must make its own
            # fenced terminal decision; this observer never mutates its receipt.
            return
        try:
            self._creation.transition_terminal(
                command_id, ReceiptStatus.FAILED, authority=authority
            )
        finally:
            current = leases.current(run_id)
            if (
                current is not None
                and current.authority.owner_id == authority.owner_id
                and current.authority.lease_token == authority.lease_token
            ):
                leases.release(authority)

    def _accepted_start(self, command: StartRun) -> tuple[str, str] | None:
        """Replay a durable product acceptance without recapturing mutable source."""

        with self._database.session() as session:
            receipt = session.get(ApplicationCommandReceiptRow, str(command.command_id))
            if receipt is None:
                return None
            if (
                receipt.command_type != "START_RUN"
                or receipt.result_scope_type != "RUN"
                or receipt.result_scope_id is None
            ):
                raise IdempotencyConflictError()
            run_id = UUID(receipt.result_scope_id)
            run = RunRepository(self._database).get(run_id)
            source = session.get(WorkspaceSourceBindingRow, str(run_id))
            root = command.workspace.resolve(strict=True)
            if (
                run.task != command.task
                or source is None
                or source.workspace_root_identity != str(root)
            ):
                raise IdempotencyConflictError()
            if run.status in {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}:
                # A prior process can crash after committing the Run terminal
                # fact but before closing its Start receipt.  The terminal Run
                # is sufficient durable evidence to reconcile that receipt.
                self._creation.finalize_observed_run(run_id)
            if run.status in {RunStatus.CREATED, RunStatus.RUNNING}:
                if (
                    run.status is RunStatus.CREATED
                    and not ProductWorkspaceCapture().matches_source(
                        root, source.expected_source_digest
                    )
                ):
                    raise RuntimeError("workspace changed before run driver start")
                self._ensure_start_driver(run_id, command.command_id)
            return "RUN", str(run_id)

    def _ensure_start_driver(self, run_id: UUID, command_id: UUID) -> None:
        existing = self._drivers.get(run_id)
        if existing is not None and not existing.done():
            return
        leases = RunLeaseStore(self._database)

        async def operation() -> object:
            # A process that did not obtain the current epoch remains a
            # read-only observer, but keeps checking the durable lease and
            # receipt.  The capped backoff makes takeover responsive after an
            # owner crash without busy-looping while a healthy owner runs.
            delay = self._poll_interval
            while True:
                if self._creation.finalize_observed_run(run_id) is not None:
                    return None
                acquisition = leases.acquire_or_observe(
                    run_id,
                    owner_id=(
                        f"agent-app:{self._instance_id}:start:{command_id}:attempt:{uuid4()}"
                    ),
                    ttl=timedelta(seconds=30),
                )
                authority = acquisition.authority
                if authority is None:
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 0.25)
                    continue
                try:
                    # The receipt may have become terminal between observing it
                    # and acquiring the released/expired epoch.
                    if self._creation.finalize_observed_run(run_id) is not None:
                        return None
                    observed = RunRepository(self._database).get(run_id)
                    if observed.status is RunStatus.RUNNING:
                        # The only safe recovery for an expired owner that has
                        # crossed RUN_STARTED is an explicit unknown outcome;
                        # never replay the provider from an in-memory point.
                        return self._creation.transition_terminal(
                            command_id,
                            ReceiptStatus.INDETERMINATE,
                            authority=authority,
                        )
                    assert self._runtime is not None
                    return await self._runtime.execute(run_id, authority=authority)
                except BaseException:
                    if self._creation.finalize_observed_run(run_id) is None:
                        self._creation.transition_terminal(
                            command_id,
                            ReceiptStatus.INDETERMINATE,
                            authority=authority,
                        )
                    raise
                finally:
                    self._creation.finalize_observed_run(run_id)
                    current = leases.current(run_id)
                    if (
                        current is not None
                        and current.authority.owner_id == authority.owner_id
                        and current.authority.lease_token == authority.lease_token
                    ):
                        leases.release(current.authority)

        self._start_driver(run_id, operation)

    def _start_driver(
        self,
        run_id: UUID,
        operation: Callable[[], Awaitable[object]],
    ) -> None:
        if run_id in self._drivers and not self._drivers[run_id].done():
            return

        async def drive() -> None:
            try:
                await operation()
            except BaseException as exc:
                self._remember_error(
                    self._driver_errors,
                    run_id,
                    ApplicationFailure(application_error_from_exception(exc, run_id=run_id)),
                )

        task = asyncio.create_task(drive(), name=f"agentforge-product-{run_id}")
        self._drivers[run_id] = task
        task.add_done_callback(lambda completed: self._forget_driver(run_id, completed))

    def _forget_driver(self, run_id: UUID, task: asyncio.Task[None]) -> None:
        # Keep a small completed-task window for stream/error observation while
        # preventing long-lived applications from retaining every command.
        if self._drivers.get(run_id) is task:
            self._trim_completed_tasks(self._drivers)

    def _start_resume_task(self, command: ResumeRunCommand) -> None:
        runtime = self._runtime
        if runtime is None:
            raise RuntimeError("product runtime is unavailable")
        existing = self._resume_tasks.get(command.command_id)
        if existing is not None and not existing.done():
            return
        lock = self._resume_locks.setdefault(command.run_id, asyncio.Lock())
        self._resume_task_runs[command.command_id] = command.run_id

        async def drive() -> None:
            async with lock:
                current = asyncio.current_task()
                assert current is not None
                self._drivers[command.run_id] = current
                failure: BaseException | None = None
                try:
                    while True:
                        result = await runtime.resume(command)
                        if (
                            isinstance(result, DriverOutcome)
                            and result.disposition
                            == "REPLAY_WAIT_FOREIGN_ACTIVE"
                        ):
                            # Only a durable foreign active lease permits an
                            # observer retry.  Once it expires, prepare
                            # classifies the persisted pre/post-watermark facts.
                            if self._resume_receipts.replay(command) is not None:
                                return
                            await asyncio.sleep(self._poll_interval)
                            continue
                        return
                except BaseException as exc:
                    failure = exc
                    self._remember_error(
                        self._command_errors,
                        command.command_id,
                        ApplicationFailure(
                            application_error_from_exception(exc, run_id=command.run_id)
                        ),
                    )
                finally:
                    try:
                        self._creation.finalize_observed_run(command.run_id)
                    except BaseException as exc:
                        # Cleanup observes only Start facts and is not allowed to
                        # mask a Resume error or turn a detached driver into an
                        # unobserved task exception.
                        if failure is None:
                            self._remember_error(
                                self._command_errors,
                                command.command_id,
                                ApplicationFailure(
                                    application_error_from_exception(
                                        exc, run_id=command.run_id
                                    )
                                ),
                            )

        task = asyncio.create_task(
            drive(), name=f"agentforge-resume-{command.run_id}-{command.command_id}"
        )
        self._resume_tasks[command.command_id] = task
        task.add_done_callback(
            lambda completed: self._forget_resume_task(
                command.command_id, command.run_id, completed
            )
        )

    def _forget_resume_task(
        self, command_id: UUID, run_id: UUID, task: asyncio.Task[None]
    ) -> None:
        if self._resume_tasks.get(command_id) is task:
            for completed in self._trim_completed_tasks(self._resume_tasks):
                self._resume_task_runs.pop(completed, None)
        if self._drivers.get(run_id) is task:
            self._trim_completed_tasks(self._drivers)
        self._resume_task_runs.pop(command_id, None)
        if not any(
            task_run_id == run_id and not self._resume_tasks[task_id].done()
            for task_id, task_run_id in self._resume_task_runs.items()
            if task_id in self._resume_tasks
        ):
            self._resume_locks.pop(run_id, None)

    @classmethod
    def _trim_completed_tasks(
        cls, tasks: OrderedDict[UUID, asyncio.Task[None]]
    ) -> tuple[UUID, ...]:
        removed: list[UUID] = []
        while len(tasks) > cls._BOOKKEEPING_LIMIT:
            completed = next(
                (key for key, task in tasks.items() if task.done()), None
            )
            if completed is None:
                break
            tasks.pop(completed, None)
            removed.append(completed)
        return tuple(removed)

    @classmethod
    def _remember_error(
        cls,
        errors: OrderedDict[UUID, ApplicationFailure],
        key: UUID,
        error: ApplicationFailure,
    ) -> None:
        errors[key] = error
        errors.move_to_end(key)
        while len(errors) > cls._BOOKKEEPING_LIMIT:
            errors.popitem(last=False)

    def _events_after(
        self, scope_type: str, scope_id: str, cursor: int
    ) -> tuple[PersistedEvent, ...]:
        with self._database.session() as session:
            rows = session.scalars(
                select(EventRow)
                .where(
                    EventRow.scope_type == scope_type,
                    EventRow.scope_id == scope_id,
                    EventRow.global_cursor > cursor,
                )
                .order_by(EventRow.global_cursor)
            ).all()
        return tuple(
            PersistedEvent(
                schema_version=row.schema_version,
                event_id=UUID(row.event_id),
                global_cursor=row.global_cursor,
                scope_type=cast(EventScopeType, row.scope_type),
                scope_id=row.scope_id,
                run_id=UUID(row.run_id) if row.run_id else None,
                sequence_number=row.sequence_number,
                event_type=row.event_type,
                payload=row.payload,
                created_at=row.created_at,
            )
            for row in rows
        )

    def _resume_stream_boundary(self, command: ResumeRun) -> int | None:
        """Find this accepted command's immutable event watermark."""
        with self._database.session() as session:
            receipt = session.get(ApplicationCommandReceiptRow, str(command.command_id))
            if receipt is None:
                return None
            events = session.scalars(
                select(EventRow)
                .where(
                    EventRow.run_id == str(command.run_id),
                    EventRow.event_type == "RUN_RESUMED",
                    EventRow.created_at >= receipt.created_at,
                )
                .order_by(EventRow.global_cursor)
            )
            marker = next(
                (
                    event
                    for event in events
                    if event.payload.get("command_id") == str(command.command_id)
                ),
                None,
            )
            return None if marker is None else marker.global_cursor

    def _scope_is_quiescent(self, scope_type: str, scope_id: str) -> bool:
        if scope_type != "RUN":
            return True
        run_id = UUID(scope_id)
        task = self._drivers.get(run_id)
        if task is not None and not task.done():
            return False
        run = RunRepository(self._database).get(run_id)
        if run.status in {
            RunStatus.WAITING_APPROVAL,
            RunStatus.PAUSED,
            RunStatus.COMPLETED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        }:
            return True
        return task is not None and task.done()

    def _stream_is_quiescent(
        self, command: ApplicationCommand, scope_type: str, scope_id: str
    ) -> bool:
        if isinstance(command, ResumeRun):
            with self._database.session() as session:
                receipt = session.get(
                    ApplicationCommandReceiptRow, str(command.command_id)
                )
                terminal = receipt is not None and receipt.status in {
                    ReceiptStatus.COMPLETED.value,
                    ReceiptStatus.FAILED.value,
                    ReceiptStatus.INDETERMINATE.value,
                }
            task = self._resume_tasks.get(command.command_id)
            return terminal and (task is None or task.done())
        return self._scope_is_quiescent(scope_type, scope_id)

    def _raise_stream_failure(
        self, command: ApplicationCommand, scope_type: str, scope_id: str
    ) -> None:
        if isinstance(command, ResumeRun):
            failure = self._command_errors.pop(command.command_id, None)
        elif scope_type == "RUN":
            failure = self._driver_errors.pop(UUID(scope_id), None)
        else:
            failure = None
        if failure is not None:
            raise failure

    @overload
    def query(self, query: RunDetails) -> LocalRunDetailsView: ...
    @overload
    def query(self, query: ExportRunDetails) -> ExportRunDetailsView: ...
    @overload
    def query(self, query: PendingApprovals) -> PendingApprovalsView: ...
    @overload
    def query(self, query: DoctorReport) -> DoctorReportView: ...
    @overload
    def query(self, query: ProfileTrustDetails) -> ProfileTrustDetailsView: ...
    def query(self, query: object) -> object:
        try:
            query = APPLICATION_QUERY_ADAPTER.validate_python(
                query.model_dump(mode="python")  # type: ignore[attr-defined]
            )
        except BaseException as exc:
            raise ApplicationFailure(application_error_from_exception(exc)) from None
        try:
            if isinstance(query, DoctorReport):
                return self._doctor.report(query.workspace)
            if isinstance(query, PendingApprovals):
                return self._projector.pending_approvals(
                    ApprovalRepository(self._database).list_pending(query.run_id)
                )
            if isinstance(query, (RunDetails, ExportRunDetails)):
                facts = self._run_facts(query.run_id)
                return (
                    self._projector.export_run(facts)
                    if isinstance(query, ExportRunDetails)
                    else self._projector.run_details(facts)
                )
            if isinstance(query, ProfileTrustDetails):
                if self._profiles is None:
                    raise ValueError("profile registry is unavailable")
                if query.workspace.resolve(strict=True) != self._profiles.workspace_root:
                    raise ValueError("profile query workspace does not match registry")
                challenge = ProfileKernel(self._database, self._profiles).challenge(
                    query.profile_id, purpose=query.purpose
                )
                profile = self._profiles.get(query.profile_id)
                try:
                    ProfileKernel(self._database, self._profiles).resolve_trusted(
                        query.profile_id, purpose=query.purpose
                    )
                    trusted = True
                except ProfileTrustMismatchError:
                    trusted = False
                return ProfileTrustDetailsView(
                    **challenge.model_dump(), cwd=profile.cwd, trusted=trusted
                )
            raise ValueError("closed application query is invalid")
        except ApplicationFailure:
            raise
        except BaseException as exc:
            raise ApplicationFailure(application_error_from_exception(exc)) from None

    def _run_facts(self, run_id: UUID) -> RunProjectionFacts:
        with self._database.session() as session:
            row = session.get(RunRow, str(run_id))
            if row is None:
                raise RunNotFoundError(f"Run {run_id} does not exist")
            run = RunRepository._to_domain(row)
            repair = session.get(RepairStateRow, str(run_id))
            source = session.get(WorkspaceSourceBindingRow, str(run_id))
            count, last = session.execute(
                select(func.count(EventRow.global_cursor), func.max(EventRow.global_cursor)).where(
                    EventRow.run_id == str(run_id)
                )
            ).one()
        if repair is None:
            raise ValueError("repair state is unavailable")
        return RunProjectionFacts(
            run=run,
            repair_status=RepairCompletionStatus(repair.status),
            event_count=int(count),
            last_cursor=int(last) if last is not None else None,
            source_digest=source.initial_source_digest if source else None,
            config_digest=source.config_digest if source else None,
            profile_digest=source.profile_digest if source else None,
        )

    def business_row_counts(self) -> dict[str, int]:
        with self._database.session() as session:
            return {
                table.name: int(
                    session.scalar(select(func.count()).select_from(table)) or 0
                )
                for table in Base.metadata.sorted_tables
            }
