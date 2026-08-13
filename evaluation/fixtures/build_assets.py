from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
TASKS = ROOT / "tasks"


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return value


def _immutable_hashes(task_root: Path) -> dict[str, str]:
    paths = [task_root / "ATTRIBUTION.md"]
    for directory in ("tests", "reference"):
        paths.extend(path for path in (task_root / directory).rglob("*") if path.is_file())
    return {
        path.relative_to(task_root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(paths, key=lambda item: item.relative_to(task_root).as_posix())
    }


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> None:
    registry = _load_json(ROOT / "registry.json")
    task_ids = sorted(item["task_id"] for item in registry["tasks"])
    registry["tasks"] = [{"task_id": task_id} for task_id in task_ids]
    _write_json(ROOT / "registry.json", registry)
    for task_id in task_ids:
        task_root = TASKS / task_id
        manifest = _load_json(task_root / "task_manifest.json")
        if manifest["task_id"] != task_id:
            raise ValueError(f"Registry/manifest task mismatch: {task_id}")
        manifest["immutable_file_sha256"] = _immutable_hashes(task_root)
        manifest["protected_paths"] = sorted(
            {"task_manifest.json", *manifest["immutable_file_sha256"]}
        )
        _write_json(task_root / "task_manifest.json", manifest)


if __name__ == "__main__":
    main()
