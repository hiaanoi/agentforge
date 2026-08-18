"""Command-line boundary for the durable public Verified-10 campaign."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from agentforge.evaluation.verified10_campaign import (
    BenchmarkArm,
    CampaignExecutionError,
    Verified10Campaign,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="run_verified10_comparison")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("prepare", "run-agentforge", "run-mini", "status", "finalize-predictions"):
        item = commands.add_parser(name)
        item.add_argument("--protocol", required=True, type=Path)
        item.add_argument("--output-dir", required=True, type=Path)
        if name in {"run-agentforge", "run-mini"}:
            item.add_argument("--recover-running", action="store_true")
            item.add_argument("--retry-failed", action="store_true")
        if name == "run-mini":
            item.add_argument(
                "--mini-root", type=Path, default=os.environ.get("MINI_SWE_AGENT_ROOT")
            )
        if name == "finalize-predictions":
            item.add_argument(
                "--arm", required=True, choices=tuple(item.value for item in BenchmarkArm)
            )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        campaign = Verified10Campaign(args.protocol, args.output_dir)
        if args.command == "prepare":
            campaign.prepare()
        elif args.command == "run-agentforge":
            campaign.run_agentforge(
                recover_running=args.recover_running, retry_failed=args.retry_failed
            )
        elif args.command == "run-mini":
            if args.mini_root is None:
                raise CampaignExecutionError(
                    "run-mini requires --mini-root or MINI_SWE_AGENT_ROOT"
                )
            campaign.run_mini(
                mini_root=args.mini_root,
                recover_running=args.recover_running,
                retry_failed=args.retry_failed,
            )
        elif args.command == "status":
            print(campaign.status())
        else:
            campaign.finalize_predictions(BenchmarkArm(args.arm))
        return 0
    except CampaignExecutionError as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
