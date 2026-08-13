from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, NamedTuple
from urllib.parse import urlparse

FIXTURE_ROOT = Path(__file__).resolve().parent
RESULT_SCHEMA_VERSION = "1.0.0"
APPROVED_TASK_IDS = {
    "bugsinpy-black-21",
    "quixbugs-shortest-path-length",
    "self-durable-double-consumption",
    "swebench-pytest-10051",
}
FAILED_NODE_PATTERN = re.compile(
    r"(?m)^FAILED[ \t]+(?P<node>\S+?)(?:[ \t]+-[ \t]+.*)?\r?$"
)


class PreparedAttempt(NamedTuple):
    execution_root: Path
    workspace: Path


class ProcessCapture(NamedTuple):
    returncode: int
    stdout: str
    stderr: str
    stdout_truncated: bool
    stderr_truncated: bool
    timed_out: bool


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path.name}")
    return value


def safe_join(root: Path, relative_path: str) -> Path:
    parsed = PurePosixPath(relative_path)
    if parsed.is_absolute() or (parsed.parts and ":" in parsed.parts[0]):
        raise ValueError("Fixture paths must be relative")
    if ".." in parsed.parts or "." in parsed.parts:
        raise ValueError("Fixture path traversal is forbidden")
    candidate = root.joinpath(*parsed.parts)
    resolved_root = root.resolve()
    resolved_candidate = candidate.resolve()
    if resolved_candidate != resolved_root and resolved_root not in resolved_candidate.parents:
        raise ValueError("Fixture path escapes its root")
    return candidate


def build_environment(workspace: Path) -> dict[str, str]:
    attempt_temp = workspace.resolve().parent / "tmp"
    attempt_temp.mkdir(parents=True, exist_ok=True)
    environment: dict[str, str] = {
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "PYTHONHASHSEED": "0",
        "PYTHONPATH": str(workspace.resolve()),
        "TEMP": str(attempt_temp),
        "TMP": str(attempt_temp),
    }
    for name in ("SYSTEMROOT", "WINDIR"):
        value = os.environ.get(name)
        if value:
            environment[name] = value
    return environment


def prepare_attempt(task_root: Path, state: str, execution_root: Path) -> PreparedAttempt:
    if state not in {"buggy", "reference"}:
        raise ValueError(f"Unknown fixture state: {state}")
    manifest = load_json(task_root / "task_manifest.json")
    workspace = execution_root / "workspace"
    shutil.copytree(task_root / "workspace", workspace)
    shutil.copytree(task_root / "tests", execution_root / "tests")

    if state == "reference":
        prefix = "reference/fixed_files/"
        editable_paths = set(manifest["editable_paths"])
        for relative_path in manifest["reference_files"]:
            if not relative_path.startswith(prefix):
                raise ValueError("Reference file is outside the fixed-files overlay")
            target_relative = relative_path.removeprefix(prefix)
            if f"workspace/{target_relative}" not in editable_paths:
                raise ValueError("Reference overlay target is not declared editable")
            source = safe_join(task_root, relative_path)
            target = safe_join(workspace, target_relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)

    return PreparedAttempt(execution_root=execution_root, workspace=workspace)


def _drain_bounded(stream: BinaryIO, limit: int, target: bytearray, truncated: list[bool]) -> None:
    try:
        while chunk := stream.read(8192):
            remaining = limit - len(target)
            if remaining > 0:
                target.extend(chunk[:remaining])
            if len(chunk) > max(remaining, 0):
                truncated[0] = True
    except (OSError, ValueError):
        return


def run_process_bounded(
    command: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    timeout_seconds: int,
    output_limit: int,
) -> ProcessCapture:
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdout is not None
    assert process.stderr is not None
    stdout_buffer = bytearray()
    stderr_buffer = bytearray()
    stdout_truncated = [False]
    stderr_truncated = [False]
    readers = (
        threading.Thread(
            target=_drain_bounded,
            args=(process.stdout, output_limit, stdout_buffer, stdout_truncated),
            daemon=True,
        ),
        threading.Thread(
            target=_drain_bounded,
            args=(process.stderr, output_limit, stderr_buffer, stderr_truncated),
            daemon=True,
        ),
    )
    for reader in readers:
        reader.start()
    timed_out = False
    try:
        returncode = process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        process.stdout.close()
        process.stderr.close()
        returncode = -1
        timed_out = True
    finally:
        for reader in readers:
            reader.join(timeout=1 if timed_out else None)
    return ProcessCapture(
        returncode=returncode,
        stdout=stdout_buffer.decode("utf-8", errors="replace"),
        stderr=stderr_buffer.decode("utf-8", errors="replace"),
        stdout_truncated=stdout_truncated[0],
        stderr_truncated=stderr_truncated[0],
        timed_out=timed_out,
    )


def sanitize_output(value: str, execution_root: Path) -> str:
    sanitized = value
    replacements = (
        (execution_root, "<execution_root>"),
        (Path(sys.base_prefix), "<python_root>"),
        (Path(sys.prefix), "<python_root>"),
    )
    for root, replacement in replacements:
        for rendered_root in {str(root), root.as_posix()}:
            sanitized = sanitized.replace(rendered_root, replacement)
    return sanitized


def failed_node_ids(stdout: str, stderr: str) -> list[str]:
    combined = "\n".join(item for item in (stdout, stderr) if item)
    return sorted(
        {
            match.group("node").replace("\\", "/")
            for match in FAILED_NODE_PATTERN.finditer(combined)
        }
    )


def run_attempt(task_root: Path, state: str, suite: str, attempt: int) -> dict[str, Any]:
    manifest = load_json(task_root / "task_manifest.json")
    if suite not in {"visible", "hidden"}:
        raise ValueError(f"Unknown test suite: {suite}")
    command = [sys.executable, *manifest["test_commands"][suite]]
    timeout_seconds = int(manifest["limits"]["timeout_seconds"])
    output_limit = int(manifest["limits"]["max_output_chars"])
    started = time.monotonic()

    with tempfile.TemporaryDirectory(prefix=f"agentforge-{manifest['task_id']}-") as temp:
        prepared = prepare_attempt(task_root, state, Path(temp))
        capture = run_process_bounded(
            command,
            cwd=prepared.execution_root,
            environment=build_environment(prepared.workspace),
            timeout_seconds=timeout_seconds,
            output_limit=output_limit,
        )

    return {
        "result_schema_version": RESULT_SCHEMA_VERSION,
        "task_id": manifest["task_id"],
        "state": state,
        "suite": suite,
        "attempt": attempt,
        "outcome": "PASS" if capture.returncode == 0 else "FAIL",
        "returncode": capture.returncode,
        "timed_out": capture.timed_out,
        "duration_ms": round((time.monotonic() - started) * 1000),
        "stdout": sanitize_output(capture.stdout, prepared.execution_root),
        "stderr": sanitize_output(capture.stderr, prepared.execution_root),
        "stdout_truncated": capture.stdout_truncated,
        "stderr_truncated": capture.stderr_truncated,
        "failed_node_ids": failed_node_ids(capture.stdout, capture.stderr),
    }


def validate_immutable_files(task_root: Path, manifest: dict[str, Any]) -> None:
    for relative_path, expected_digest in manifest["immutable_file_sha256"].items():
        content = safe_join(task_root, relative_path).read_bytes()
        actual_digest = hashlib.sha256(content).hexdigest()
        if actual_digest != expected_digest:
            raise ValueError(f"Immutable fixture digest mismatch: {relative_path}")


def _relative_files(directory: Path, task_root: Path) -> set[str]:
    return {
        path.relative_to(task_root).as_posix()
        for path in directory.rglob("*")
        if path.is_file()
    }


def compute_fixture_asset_digest() -> str:
    paths = [
        FIXTURE_ROOT / name
        for name in (
            "build_assets.py",
            "fixture_schema.json",
            "registry.json",
            "verify_fixtures.py",
        )
    ]
    paths.extend(path for path in (FIXTURE_ROOT / "tasks").rglob("*") if path.is_file())
    digest = hashlib.sha256()
    for path in sorted(paths, key=lambda item: item.relative_to(FIXTURE_ROOT).as_posix()):
        relative_path = path.relative_to(FIXTURE_ROOT).as_posix()
        digest.update(relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def _require_string_list(value: Any, field: str, *, allow_empty: bool = False) -> list[str]:
    if not isinstance(value, list) or (not value and not allow_empty):
        raise ValueError(f"Manifest field {field} must be a string list")
    if any(not isinstance(item, str) or not item for item in value):
        raise ValueError(f"Manifest field {field} must contain non-empty strings")
    if len(value) != len(set(value)):
        raise ValueError(f"Manifest field {field} must not contain duplicates")
    return value


def validate_manifest(task_root: Path, expected_task_id: str) -> dict[str, Any]:
    manifest = load_json(task_root / "task_manifest.json")
    schema = load_json(FIXTURE_ROOT / "fixture_schema.json")
    definition = schema["$defs"]["task_manifest"]
    required = set(definition["required"])
    allowed = set(definition["properties"])
    keys = set(manifest)
    if missing := required - keys:
        raise ValueError(f"Manifest is missing required fields: {sorted(missing)}")
    if extra := keys - allowed:
        raise ValueError(f"Manifest has unsupported fields: {sorted(extra)}")
    if manifest["task_id"] != expected_task_id:
        raise ValueError("Registry and manifest task IDs differ")
    if manifest["fixture_schema_version"] != "1.0.0":
        raise ValueError("Unsupported fixture schema version")
    if not isinstance(manifest["task_id"], str) or re.fullmatch(
        r"[a-z0-9-]+", manifest["task_id"]
    ) is None:
        raise ValueError("Manifest task_id has an invalid format")
    if not isinstance(manifest["source_kind"], str) or manifest["source_kind"] not in {
        "QUIXBUGS",
        "BUGSINPY",
        "SWE_BENCH",
        "SELF_BUILT",
    }:
        raise ValueError("Manifest source_kind is unsupported")
    if not isinstance(manifest["difficulty"], str) or manifest["difficulty"] not in {
        "BASIC",
        "ENGINEERING",
        "CHALLENGE",
    }:
        raise ValueError("Manifest difficulty is unsupported")
    if not isinstance(manifest["license"], str) or not manifest["license"]:
        raise ValueError("Manifest license must be a non-empty string")
    source_urls = _require_string_list(
        manifest["source_urls"], "source_urls", allow_empty=manifest["source_kind"] == "SELF_BUILT"
    )
    if any(urlparse(url).scheme != "https" or not urlparse(url).netloc for url in source_urls):
        raise ValueError("Manifest source_urls must contain absolute HTTPS URLs")
    for revision_field in ("buggy_revision", "fixed_revision"):
        revision = manifest[revision_field]
        if revision is not None and (not isinstance(revision, str) or not revision):
            raise ValueError(f"Manifest {revision_field} must be a non-empty string or null")
    if (
        manifest["network_required"] is not False
        or manifest["external_service_required"] is not False
    ):
        raise ValueError("Formal fixtures must be offline and local")
    repair_prompt = manifest["repair_prompt"]
    if not isinstance(repair_prompt, dict) or set(repair_prompt) != {
        "schema_version",
        "title",
        "description",
        "success_conditions",
    }:
        raise ValueError("Manifest repair_prompt has an invalid shape")
    prompt_conditions = _require_string_list(
        repair_prompt["success_conditions"],
        "repair_prompt.success_conditions",
    )
    if (
        repair_prompt["schema_version"] != 1
        or not isinstance(repair_prompt["title"], str)
        or not 1 <= len(repair_prompt["title"]) <= 200
        or not isinstance(repair_prompt["description"], str)
        or not 1 <= len(repair_prompt["description"]) <= 4_000
        or len(prompt_conditions) > 20
        or any(len(item) > 1_000 for item in prompt_conditions)
    ):
        raise ValueError("Manifest repair_prompt is outside accepted bounds")
    serialized_prompt = json.dumps(repair_prompt, ensure_ascii=False).casefold()
    if "reference/fixed_files" in serialized_prompt or "tests/hidden" in serialized_prompt:
        raise ValueError("Manifest repair_prompt exposes evaluator-only paths")
    if manifest["task_id"] == "self-durable-double-consumption" and any(
        phrase in serialized_prompt
        for phrase in (
            "idempotent",
            "exactly once",
            "double consumption",
            "compare-and-set",
            "crash window",
            "concurrent",
            "receipts table",
        )
    ):
        raise ValueError("Primary Demo repair_prompt exposes private repair guidance")
    if manifest["expected_outcomes"] != {"buggy": "FAIL", "reference": "PASS"}:
        raise ValueError("Fixture outcomes must require buggy FAIL and reference PASS")
    expected_baseline = manifest["expected_baseline_failure"]
    if not isinstance(expected_baseline, dict) or set(expected_baseline) != {
        "runner",
        "expected_exit_class",
        "failed_node_ids",
        "match_mode",
        "fingerprint_version",
    }:
        raise ValueError("Manifest expected_baseline_failure has an invalid shape")
    baseline_nodes = _require_string_list(
        expected_baseline["failed_node_ids"], "expected_baseline_failure.failed_node_ids"
    )
    if (
        expected_baseline["runner"] != "pytest"
        or expected_baseline["expected_exit_class"] != "NON_ZERO"
        or expected_baseline["match_mode"] != "EXACT_SET"
        or expected_baseline["fingerprint_version"] != 1
        or any(
            not node.startswith("tests/visible/") or "::" not in node
            for node in baseline_nodes
        )
    ):
        raise ValueError("Manifest expected baseline fingerprint is unsupported")
    expected_commands = {
        "visible": ["-m", "pytest", "tests/visible", "-q"],
        "hidden": ["-m", "pytest", "tests/hidden", "-q"],
    }
    if manifest["test_commands"] != expected_commands:
        raise ValueError("Fixture test commands must use the fixed pytest argv")
    limits = manifest["limits"]
    if not isinstance(limits, dict) or set(limits) != {"timeout_seconds", "max_output_chars"}:
        raise ValueError("Manifest limits must contain timeout_seconds and max_output_chars")
    timeout = limits["timeout_seconds"]
    output_limit = limits["max_output_chars"]
    if (
        not isinstance(timeout, int)
        or isinstance(timeout, bool)
        or not 1 <= timeout <= 30
        or not isinstance(output_limit, int)
        or isinstance(output_limit, bool)
        or not 1 <= output_limit <= 20_000
    ):
        raise ValueError("Manifest limits are outside the accepted integer ranges")

    editable_list = _require_string_list(manifest["editable_paths"], "editable_paths")
    protected_list = _require_string_list(manifest["protected_paths"], "protected_paths")
    reference_list = _require_string_list(manifest["reference_files"], "reference_files")
    immutable_map = manifest["immutable_file_sha256"]
    if not isinstance(immutable_map, dict) or any(
        not isinstance(path, str)
        or not isinstance(file_digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", file_digest) is None
        for path, file_digest in immutable_map.items()
    ):
        raise ValueError("Manifest immutable_file_sha256 has an invalid shape")

    for path in task_root.rglob("*"):
        if path.is_symlink():
            raise ValueError("Fixture assets may not contain symbolic links")

    editable = set(editable_list)
    expected_editable = _relative_files(task_root / "workspace", task_root)
    if editable != expected_editable:
        raise ValueError("Editable paths must exactly cover workspace files")

    immutable = set(immutable_map)
    expected_immutable = {"ATTRIBUTION.md"}
    expected_immutable.update(_relative_files(task_root / "tests", task_root))
    expected_immutable.update(_relative_files(task_root / "reference", task_root))
    if immutable != expected_immutable:
        raise ValueError("Immutable hash map does not exactly cover evaluator assets")
    expected_protected = immutable | {"task_manifest.json"}
    if set(protected_list) != expected_protected:
        raise ValueError("Protected paths do not exactly cover evaluator assets")

    for relative_path in editable | expected_protected:
        candidate = safe_join(task_root, relative_path)
        if not candidate.is_file():
            raise ValueError(f"Declared fixture file is missing: {relative_path}")
    for reference_path in reference_list:
        prefix = "reference/fixed_files/"
        if not reference_path.startswith(prefix):
            raise ValueError("Reference file is outside the fixed-files overlay")
        if f"workspace/{reference_path.removeprefix(prefix)}" not in editable:
            raise ValueError("Reference overlay target is not declared editable")

    validate_immutable_files(task_root, manifest)
    return manifest


def validate_registry(registry: dict[str, Any]) -> list[str]:
    if set(registry) != {"fixture_registry_version", "model_evaluation_authorized", "tasks"}:
        raise ValueError("Fixture registry has an unsupported shape")
    if registry["fixture_registry_version"] != "1.0.0":
        raise ValueError("Unsupported fixture registry version")
    if registry["model_evaluation_authorized"] is not False:
        raise ValueError("M7-B2.1 does not authorize model evaluation")
    if not isinstance(registry["tasks"], list) or any(
        not isinstance(item, dict) or set(item) != {"task_id"} for item in registry["tasks"]
    ):
        raise ValueError("Fixture registry entries may contain only task_id")
    task_ids = [item["task_id"] for item in registry["tasks"]]
    if len(task_ids) != len(set(task_ids)) or set(task_ids) != APPROVED_TASK_IDS:
        raise ValueError("Fixture registry must contain exactly the approved first batch")
    return task_ids


def verify_all(repeat: int) -> dict[str, Any]:
    if not isinstance(repeat, int) or isinstance(repeat, bool) or repeat < 1:
        raise ValueError("Repeat count must be a positive integer")
    registry = load_json(FIXTURE_ROOT / "registry.json")
    results: list[dict[str, Any]] = []
    task_ids = validate_registry(registry)
    for task_id in task_ids:
        task_root = FIXTURE_ROOT / "tasks" / task_id
        validate_manifest(task_root, task_id)
        for attempt in range(1, repeat + 1):
            for state in ("buggy", "reference"):
                for suite in ("visible", "hidden"):
                    results.append(run_attempt(task_root, state, suite, attempt))

    expected_nodes = {
        task_id: set(
            load_json(FIXTURE_ROOT / "tasks" / task_id / "task_manifest.json")[
                "expected_baseline_failure"
            ]["failed_node_ids"]
        )
        for task_id in task_ids
    }
    accepted = all(
        result["outcome"]
        == ("FAIL" if result["state"] == "buggy" else "PASS")
        and not result["timed_out"]
        and (
            result["state"] != "buggy"
            or result["suite"] != "visible"
            or set(result["failed_node_ids"]) == expected_nodes[result["task_id"]]
        )
        for result in results
    )
    return {
        "verification_schema_version": RESULT_SCHEMA_VERSION,
        "repeat_count": repeat,
        "task_count": len(task_ids),
        "accepted": accepted,
        "fixture_asset_digest": compute_fixture_asset_digest(),
        "model_evaluation_performed": False,
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify formal AgentForge repair fixtures")
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument(
        "--output", type=Path, default=FIXTURE_ROOT / "verification_report.json"
    )
    args = parser.parse_args()
    if args.repeat < 1:
        parser.error("--repeat must be at least 1")

    report = verify_all(args.repeat)
    args.output.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(
        f"verified {report['task_count']} fixtures x {args.repeat} repetitions: "
        f"{'PASS' if report['accepted'] else 'FAIL'}"
    )
    return 0 if report["accepted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
