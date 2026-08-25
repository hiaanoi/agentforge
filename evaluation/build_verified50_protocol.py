"""Freeze a deterministic 50-task SWE-bench Verified protocol.

This script runs on the evaluation host, where the pinned Arrow dataset is cached.
It deliberately writes only public task projections into the protocol.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

import pyarrow.ipc as ipc


def _digest(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _rank(selection_key: str, instance_id: str) -> str:
    return hashlib.sha256(f"{selection_key}\0{instance_id}".encode()).hexdigest()


def build(
    template_path: Path, dataset_arrow: Path, output_path: Path, *, selection_key: str
) -> None:
    template = json.loads(template_path.read_text(encoding="utf-8"))
    rows = ipc.open_stream(dataset_arrow).read_all().to_pylist()
    if len(rows) != 500:
        raise ValueError(f"expected 500 Verified rows, got {len(rows)}")
    ranked = sorted(rows, key=lambda row: _rank(selection_key, row["instance_id"]))
    selected = ranked[:50]
    tasks: list[dict[str, str]] = []
    for row in selected:
        public = {
            "base_commit": row["base_commit"],
            "instance_id": row["instance_id"],
            "problem_statement": row["problem_statement"],
            "repo": row["repo"],
        }
        tasks.append(
            {
                "selection_rank": _rank(selection_key, row["instance_id"]),
                "instance_id": row["instance_id"],
                "repo": row["repo"],
                "base_commit": row["base_commit"],
                "public_task_sha256": _digest(public),
            }
        )
    protocol: dict[str, Any] = dict(template)
    protocol["protocol_name"] = "verified50-openai-gpt54mini"
    protocol["model"] = "gpt-5.4-mini"
    protocol["thinking_enabled"] = False
    protocol["temperature"] = 0.0
    protocol["tasks"] = tasks
    protocol["source_selection_sha256"] = _digest(
        {"selection_key": selection_key, "tasks": tasks}
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(protocol, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"protocol={output_path}")
    print(f"tasks={len(tasks)}")
    print(f"selection_key={selection_key}")
    print(f"repo_counts={dict(sorted(Counter(task['repo'] for task in tasks).items()))}")
    print(f"source_selection_sha256={protocol['source_selection_sha256']}")


def build_mini_native_canary(parent_path: Path, output_path: Path) -> None:
    """Write the deterministic first-ten projection of a frozen 50-task protocol."""

    parent = json.loads(parent_path.read_text(encoding="utf-8"))
    if parent.get("protocol_name") != "verified50-openai-gpt54mini":
        raise ValueError("canary parent must be the frozen gpt-5.4-mini 50-task protocol")
    tasks = parent.get("tasks")
    if not isinstance(tasks, list) or len(tasks) != 50:
        raise ValueError("canary parent must contain exactly 50 tasks")
    canary = dict(parent)
    canary["protocol_name"] = "verified50-openai-gpt54mini-mini-native-canary"
    canary["parent_protocol_sha256"] = _digest(parent)
    canary["tasks"] = tasks[:10]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(canary, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--template", type=Path)
    parser.add_argument("--dataset-arrow", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--selection-key", default="agentforge-vs-mini-verified-50-v1")
    parser.add_argument("--canary-parent", type=Path)
    parser.add_argument("--canary-output", type=Path)
    args = parser.parse_args()
    if (args.canary_parent is None) != (args.canary_output is None):
        parser.error("--canary-parent and --canary-output must be supplied together")
    if args.canary_parent is not None:
        build_mini_native_canary(args.canary_parent, args.canary_output)
        return 0
    if args.template is None or args.dataset_arrow is None or args.output is None:
        parser.error("--template, --dataset-arrow, and --output are required for 50-task builds")
    build(args.template, args.dataset_arrow, args.output, selection_key=args.selection_key)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
