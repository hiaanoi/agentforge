"""Run the complete offline suite in process-isolated groups."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTEST_ARGS = ("-m", "not live", "-q")


def test_groups() -> list[tuple[str, ...]]:
    integration = sorted((ROOT / "tests" / "integration").glob("test_*.py"))
    return [
        ("tests/unit", "--ignore=tests/unit/test_run_leases.py"),
        ("tests/unit/test_run_leases.py",),
        (
            "tests/cli",
            "tests/docs",
            "tests/evaluation",
            "tests/process",
            "tests/security",
        ),
        *((path.relative_to(ROOT).as_posix(),) for path in integration),
    ]


def main() -> int:
    for group in test_groups():
        print(f"\n=== pytest {' '.join(group)} ===", flush=True)
        completed = subprocess.run(
            [sys.executable, "-m", "pytest", *group, *PYTEST_ARGS],
            cwd=ROOT,
            check=False,
        )
        if completed.returncode != 0:
            return completed.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
