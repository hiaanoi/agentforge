"""Stable command-line boundary for the executable Verified-10 runner."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol

from agentforge.evaluation.verified10_campaign import (
    BenchmarkArm,
    CampaignArtifactError,
    CampaignExecutionError,
    Verified10ProtocolError,
)
from agentforge.evaluation.verified10_runner import Verified10Campaign


class CampaignFactory(Protocol):
    def __call__(self, protocol: Path, output: Path) -> object: ...


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="run_verified10_comparison")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("prepare", "run-agentforge", "run-mini", "status", "finalize-predictions"):
        item = commands.add_parser(name)
        item.add_argument("--protocol", required=True, type=Path)
        item.add_argument("--output-dir", required=True, type=Path)
        if name in {"run-agentforge", "run-mini"}:
            item.add_argument("--recover-running", action="store_true")
            item.add_argument(
                "--retry-failed",
                action="store_true",
                help="rejected by this attempts=1 protocol; retained for explicit diagnostics",
            )
        if name == "run-mini":
            item.add_argument(
                "--mini-root", type=Path, default=os.environ.get("MINI_SWE_AGENT_ROOT")
            )
        if name == "finalize-predictions":
            item.add_argument(
                "--arm", required=True, choices=tuple(arm.value for arm in BenchmarkArm)
            )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    campaign_factory: Callable[[Path, Path], object] = Verified10Campaign,
) -> int:
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    try:
        campaign = campaign_factory(args.protocol, args.output_dir)
        if args.command == "prepare":
            campaign.prepare()  # type: ignore[attr-defined]
            print("admission=10/10")
        elif args.command == "run-agentforge":
            campaign.run_agentforge(  # type: ignore[attr-defined]
                recover_running=args.recover_running, retry_failed=args.retry_failed
            )
        elif args.command == "run-mini":
            if args.mini_root is None:
                raise CampaignExecutionError("run-mini requires --mini-root or MINI_SWE_AGENT_ROOT")
            campaign.run_mini(  # type: ignore[attr-defined]
                mini_root=args.mini_root,
                recover_running=args.recover_running,
                retry_failed=args.retry_failed,
            )
        elif args.command == "status":
            print(json.dumps(campaign.status(), sort_keys=True, separators=(",", ":")))  # type: ignore[attr-defined]
        else:
            campaign.finalize_predictions(BenchmarkArm(args.arm))  # type: ignore[attr-defined]
        return 0
    except (CampaignExecutionError, CampaignArtifactError, Verified10ProtocolError, OSError) as exc:
        message = str(exc).strip() or "Verified-10 campaign failed"
        print(message, file=sys.stderr)
        return 2
