from __future__ import annotations

import argparse
from pathlib import Path

from agentforge.application.run_commands import ResumeRecoveryChoice
from agentforge.domain.enums import RejectionStrategy


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentforge")
    subcommands = parser.add_subparsers(dest="command", required=True)

    execute = subcommands.add_parser("exec", help="start a durable repair run")
    _add_workspace_options(execute)
    execute.add_argument("task", help="repair task")
    execute.add_argument("--after-cursor", type=_non_negative_integer)

    inspect = subcommands.add_parser("inspect", help="inspect a durable run")
    _add_workspace_options(inspect)
    inspect.add_argument("run_id")
    inspect.add_argument("--export", action="store_true")

    doctor = subcommands.add_parser("doctor", help="run read-only product diagnostics")
    _add_workspace_options(doctor)

    approvals = subcommands.add_parser("approvals", help="list pending approvals")
    _add_workspace_options(approvals)
    approvals.add_argument("--run-id")

    approve = subcommands.add_parser("approve", help="approve a pending operation")
    _add_workspace_options(approve)
    approve.add_argument("approval_id")
    approve.add_argument("--note", default=None)

    reject = subcommands.add_parser("reject", help="reject a pending operation")
    _add_workspace_options(reject)
    reject.add_argument("approval_id")
    reject.add_argument(
        "--strategy",
        choices=tuple(item.value for item in RejectionStrategy),
        default=RejectionStrategy.CONTINUE.value,
    )
    reject.add_argument("--note", default=None)

    resume = subcommands.add_parser("resume", help="resume one durable Run phase")
    _add_workspace_options(resume)
    resume.add_argument("run_id")
    resume.add_argument(
        "--recovery-choice",
        choices=tuple(item.value for item in ResumeRecoveryChoice),
        default=ResumeRecoveryChoice.AUTO.value,
    )
    resume.add_argument("--after-cursor", type=_non_negative_integer)

    trust = subcommands.add_parser("trust", help="trust an exact configured profile")
    _add_workspace_options(trust)
    trust.add_argument("profile_id")
    trust.add_argument("--purpose", choices=("development", "verification", "utility"))
    confirmation = trust.add_mutually_exclusive_group()
    confirmation.add_argument("--show", action="store_true", help="show the safe trust review")
    confirmation.add_argument(
        "--yes", action="store_true", help="confirm this exact trust identity"
    )
    return parser


def _add_workspace_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--database-path")
    parser.add_argument("--model")
    parser.add_argument("--max-steps", type=_positive_integer)
    parser.add_argument("--profile-id", action="append", dest="profile_ids")
    parser.add_argument("--mini-native-container")
    parser.add_argument("--mini-native-container-workspace")


def _positive_integer(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def _non_negative_integer(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return parsed
