from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID, uuid4

from agentforge.application.app import AgentApplication
from agentforge.application.bootstrap import ProductApplicationFactory
from agentforge.application.commands import DecideApproval, ResumeRun, StartRun, TrustProfile
from agentforge.application.config import (
    ProductConfig,
    ProductConfigLoader,
    UnsafeConfigurationError,
)
from agentforge.application.contracts import OutcomeStatus, ProfilePurpose
from agentforge.application.doctor import Doctor
from agentforge.application.errors import ApplicationFailure, application_error_from_exception
from agentforge.application.events import ApprovalRequestedPayload, ProductEvent, RunFinishedPayload
from agentforge.application.queries import (
    ExportRunDetails,
    PendingApprovals,
    ProfileTrustDetails,
    RunDetails,
)
from agentforge.application.run_commands import ResumeRecoveryChoice
from agentforge.application.views import ProfileTrustDetailsView
from agentforge.cli.exit_codes import ExitCode
from agentforge.cli.parser import build_parser
from agentforge.cli.render import (
    render_configuration_error,
    render_error,
    render_event,
    render_trust_review,
    render_view,
)
from agentforge.domain.enums import ApprovalStatus, RejectionStrategy


def build_application(args: argparse.Namespace) -> AgentApplication:
    config = _load_config(args)
    return ProductApplicationFactory().build(
        args.workspace,
        config=config,
        mini_native_container=args.mini_native_container,
        mini_native_container_workspace=args.mini_native_container_workspace,
    )


def _load_config(args: argparse.Namespace) -> ProductConfig:
    return ProductConfigLoader().load(
        args.workspace,
        cli={
            "database_path": args.database_path,
            "model": args.model,
            "max_steps": args.max_steps,
            "profile_ids": tuple(args.profile_ids) if args.profile_ids is not None else None,
        },
    )


def build_doctor(args: argparse.Namespace) -> Doctor:
    config = _load_config(args)
    return ProductApplicationFactory().build_doctor(args.workspace, config=config)


@dataclass
class _RunIdHolder:
    value: UUID | None = None


async def dispatch(
    application: AgentApplication, args: argparse.Namespace, run_id: _RunIdHolder
) -> ExitCode:
    command = args.command
    if command == "exec":
        return await _stream(
            application,
            StartRun(command_id=uuid4(), task=args.task, workspace=args.workspace),
            after_cursor=args.after_cursor,
            run_id=run_id,
        )
    if command == "approve":
        return await _stream(
            application,
            DecideApproval(
                command_id=uuid4(),
                approval_id=_uuid(args.approval_id),
                status=ApprovalStatus.APPROVED,
                note=args.note,
            ),
            run_id=run_id,
        )
    if command == "reject":
        return await _stream(
            application,
            DecideApproval(
                command_id=uuid4(),
                approval_id=_uuid(args.approval_id),
                status=ApprovalStatus.REJECTED,
                strategy=RejectionStrategy(args.strategy),
                note=args.note,
            ),
            run_id=run_id,
        )
    if command == "resume":
        return await _stream(
            application,
            ResumeRun(
                command_id=uuid4(),
                run_id=_uuid(args.run_id),
                recovery_choice=ResumeRecoveryChoice(args.recovery_choice),
            ),
            after_cursor=args.after_cursor,
            run_id=run_id,
        )
    if command == "inspect":
        query = (
            ExportRunDetails(run_id=_uuid(args.run_id))
            if args.export
            else RunDetails(run_id=_uuid(args.run_id))
        )
        render_view(application.query(query))
        return ExitCode.OK
    if command == "approvals":
        render_view(
            application.query(
                PendingApprovals(run_id=_uuid(args.run_id) if args.run_id is not None else None)
            )
        )
        return ExitCode.OK
    if command == "trust":
        purpose = ProfilePurpose(args.purpose) if args.purpose is not None else None
        details = application.query(
            ProfileTrustDetails(
                workspace=args.workspace,
                profile_id=args.profile_id,
                purpose=purpose,
            )
        )
        if not isinstance(details, ProfileTrustDetailsView):
            raise ValueError("profile query returned an invalid view")
        if args.show:
            render_trust_review(details)
            return ExitCode.OK
        if not args.yes:
            raise ValueError("trust requires an explicit confirmation")
        render_trust_review(details)
        return await _stream(
            application,
            TrustProfile(
                command_id=uuid4(),
                workspace_identity=details.workspace_identity,
                purpose=details.purpose,
                identity=details.trusted_identity(),
            ),
            run_id=run_id,
        )
    raise ValueError("closed CLI command is invalid")


async def _stream(
    application: AgentApplication,
    command: object,
    *,
    after_cursor: int | None = None,
    run_id: _RunIdHolder,
) -> ExitCode:
    facts: list[ProductEvent] = []
    async for event in application.stream(command, after_cursor=after_cursor):  # type: ignore[arg-type]
        facts.append(event)
        if event.run_id is not None:
            run_id.value = event.run_id
        render_event(event)
    if any(isinstance(event.payload, ApprovalRequestedPayload) for event in facts):
        return ExitCode.APPROVAL_REQUIRED
    finished = next(
        (
            event.payload
            for event in reversed(facts)
            if isinstance(event.payload, RunFinishedPayload)
        ),
        None,
    )
    if finished is None:
        return ExitCode.OK
    if finished.outcome_status is OutcomeStatus.UNKNOWN:
        return ExitCode.UNKNOWN
    if finished.outcome_status is OutcomeStatus.VERIFIED:
        return ExitCode.OK
    return ExitCode.FAILED


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:
        return exc.code if type(exc.code) is int else int(ExitCode.USAGE)
    holder = _RunIdHolder()
    try:
        if args.command == "doctor":
            render_view(build_doctor(args).report(args.workspace))
            return int(ExitCode.OK)
        application = build_application(args)
        return int(asyncio.run(_run_application(application, args, holder)))
    except ApplicationFailure as failure:
        render_error(failure.error)
        return failure.error.exit_code
    except UnsafeConfigurationError:
        render_configuration_error()
        return int(ExitCode.CONFIGURATION_ERROR)
    except KeyboardInterrupt:
        if holder.value is not None:
            sys.stderr.write(f"run_id={holder.value} detached=true\n")
        else:
            sys.stderr.write("detached=true\n")
        return int(ExitCode.PAUSED)
    except BaseException as exc:
        error = application_error_from_exception(exc)
        render_error(error)
        return error.exit_code


def _uuid(value: object) -> UUID:
    if type(value) is not str:
        raise ValueError("identifier must be a UUID")
    try:
        return UUID(value)
    except ValueError:
        raise ValueError("identifier must be a UUID") from None


async def _run_application(
    application: AgentApplication, args: argparse.Namespace, run_id: _RunIdHolder
) -> ExitCode:
    try:
        return await dispatch(application, args, run_id)
    finally:
        await application.aclose()
