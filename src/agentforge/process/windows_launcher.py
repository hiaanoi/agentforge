import json
import subprocess
import sys
from pathlib import Path
from typing import Any


def main() -> int:
    if len(sys.argv) != 2:
        return 125
    pid_file = Path(sys.argv[1])
    try:
        raw: dict[str, Any] = json.loads(sys.stdin.readline())
        process = subprocess.Popen(
            raw["argv"],
            executable=raw["executable_path"],
            cwd=raw["cwd"],
            env=raw["environment"],
            shell=False,
        )
        pid_file.write_text(str(process.pid), encoding="ascii")
        return process.wait()
    except BaseException:
        pid_file.write_text("LAUNCH_ERROR", encoding="ascii")
        return 125


if __name__ == "__main__":
    raise SystemExit(main())
