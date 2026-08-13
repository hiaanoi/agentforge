import sys
from collections.abc import Sequence
from pathlib import Path

from agentforge.evaluation.real_model_pilot import RealModelPilotApplication


def main(argv: Sequence[str]) -> int:
    repository_root = Path(__file__).resolve().parents[1]
    try:
        return RealModelPilotApplication(repository_root).run(argv)
    except Exception:
        print("real-model Pilot command failed", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
