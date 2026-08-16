from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from pydantic import ValidationError

from agentforge.evaluation.swebench_prediction import (
    SWEbenchInstanceBinding,
    SWEbenchPredictionError,
    SWEbenchPredictionExporter,
    save_swebench_prediction,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Export one base-commit-bound SWE-bench prediction.",
    )
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--base-commit", required=True)
    parser.add_argument("--model-identity", required=True)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        binding = SWEbenchInstanceBinding(
            instance_id=arguments.instance_id,
            repo=arguments.repo,
            base_commit=arguments.base_commit,
        )
        prediction = SWEbenchPredictionExporter().capture(
            arguments.workspace,
            binding=binding,
            model_identity=arguments.model_identity,
        )
        save_swebench_prediction(arguments.output, prediction)
    except (SWEbenchPredictionError, ValidationError) as exc:
        print(f"prediction export failed: {exc}", file=sys.stderr)
        return 2
    print(f"prediction_sha256={prediction.patch_sha256}")
    print(f"output={arguments.output.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
