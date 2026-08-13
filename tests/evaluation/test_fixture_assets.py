from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path, PurePosixPath
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "evaluation" / "fixtures"
TASKS = FIXTURES / "tasks"
EXPECTED_TASK_IDS = {
    "quixbugs-shortest-path-length",
    "bugsinpy-black-21",
    "swebench-pytest-10051",
    "self-durable-double-consumption",
}
EXPECTED_VISIBLE_FAILURES = {
    "bugsinpy-black-21": {
        "tests/visible/test_diagnostics.py::test_non_ascii_diagnostic_round_trips_as_utf8"
    },
    "quixbugs-shortest-path-length": {
        "tests/visible/test_shortest_path.py::test_competing_routes_choose_the_shortest_total_distance"
    },
    "self-durable-double-consumption": {
        "tests/visible/test_restart_dispatch.py::test_restart_after_dispatch_does_not_store_a_second_dispatch"
    },
    "swebench-pytest-10051": {
        "tests/visible/test_phase_clear.py::test_clear_updates_records_already_bound_to_call_phase"
    },
}


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _manifest(task_id: str) -> dict[str, Any]:
    return _load_json(TASKS / task_id / "task_manifest.json")


def _assert_safe_relative(path: str) -> None:
    parsed = PurePosixPath(path)
    assert path == parsed.as_posix()
    assert not parsed.is_absolute()
    assert ".." not in parsed.parts
    assert "." not in parsed.parts


def test_registry_contains_exactly_the_human_approved_first_batch() -> None:
    registry = _load_json(FIXTURES / "registry.json")

    assert registry["fixture_registry_version"] == "1.0.0"
    assert {item["task_id"] for item in registry["tasks"]} == EXPECTED_TASK_IDS
    assert len(registry["tasks"]) == len(EXPECTED_TASK_IDS)
    assert registry["model_evaluation_authorized"] is False


def test_fixture_schema_applies_its_manifest_definition_at_the_root() -> None:
    schema = _load_json(FIXTURES / "fixture_schema.json")

    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["schema_version"] == "1.0.0"
    assert schema["$ref"] == "#/$defs/task_manifest"


@pytest.mark.parametrize("task_id", sorted(EXPECTED_TASK_IDS))
def test_manifest_has_fixed_offline_execution_contract(task_id: str) -> None:
    manifest = _manifest(task_id)

    assert manifest["fixture_schema_version"] == "1.0.0"
    assert manifest["task_id"] == task_id
    assert manifest["network_required"] is False
    assert manifest["external_service_required"] is False
    assert manifest["expected_outcomes"] == {"buggy": "FAIL", "reference": "PASS"}
    assert manifest["limits"]["timeout_seconds"] <= 30
    assert manifest["limits"]["max_output_chars"] <= 20_000
    assert manifest["test_commands"] == {
        "visible": ["-m", "pytest", "tests/visible", "-q"],
        "hidden": ["-m", "pytest", "tests/hidden", "-q"],
    }
    assert manifest["expected_baseline_failure"] == {
        "runner": "pytest",
        "expected_exit_class": "NON_ZERO",
        "failed_node_ids": sorted(EXPECTED_VISIBLE_FAILURES[task_id]),
        "match_mode": "EXACT_SET",
        "fingerprint_version": 1,
    }


@pytest.mark.parametrize("task_id", sorted(EXPECTED_TASK_IDS))
def test_manifest_paths_are_contained_and_ownership_is_disjoint(task_id: str) -> None:
    manifest = _manifest(task_id)
    workspace_paths = set(manifest["editable_paths"])
    protected_paths = set(manifest["protected_paths"])

    for path in workspace_paths | protected_paths | set(manifest["reference_files"]):
        _assert_safe_relative(path)

    assert workspace_paths
    assert protected_paths
    assert workspace_paths.isdisjoint(protected_paths)
    assert all(path.startswith("workspace/") for path in workspace_paths)
    assert all(not path.startswith("workspace/") for path in protected_paths)
    assert all(path.startswith("reference/fixed_files/") for path in manifest["reference_files"])
    assert {"task_manifest.json", "ATTRIBUTION.md"}.issubset(protected_paths)
    assert set(manifest["reference_files"]).issubset(protected_paths)
    assert {
        path.relative_to(TASKS / task_id).as_posix()
        for path in (TASKS / task_id / "tests").rglob("*.py")
    }.issubset(protected_paths)


@pytest.mark.parametrize("task_id", sorted(EXPECTED_TASK_IDS))
def test_manifest_hashes_cover_all_immutable_files(task_id: str) -> None:
    task_root = TASKS / task_id
    manifest = _manifest(task_id)
    hashes = manifest["immutable_file_sha256"]
    expected = {
        path.relative_to(task_root).as_posix()
        for directory in ("tests", "reference")
        for path in (task_root / directory).rglob("*")
        if path.is_file()
    }
    expected.add("ATTRIBUTION.md")

    assert set(hashes) == expected
    for relative_path, expected_digest in hashes.items():
        content = (task_root / relative_path).read_bytes()
        assert hashlib.sha256(content).hexdigest() == expected_digest


@pytest.mark.parametrize("task_id", sorted(EXPECTED_TASK_IDS))
def test_reference_overlay_targets_only_declared_editable_files(task_id: str) -> None:
    manifest = _manifest(task_id)
    editable = set(manifest["editable_paths"])

    for reference_path in manifest["reference_files"]:
        target = reference_path.removeprefix("reference/fixed_files/")
        assert f"workspace/{target}" in editable


def test_public_fixtures_record_license_and_pinned_provenance() -> None:
    for task_id in EXPECTED_TASK_IDS - {"self-durable-double-consumption"}:
        manifest = _manifest(task_id)

        assert manifest["license"] == "MIT"
        assert manifest["source_urls"]
        assert all(url.startswith("https://") for url in manifest["source_urls"])
        assert manifest["buggy_revision"]
        assert manifest["fixed_revision"]


def test_self_built_fixture_is_recorded_as_original_work() -> None:
    manifest = _manifest("self-durable-double-consumption")

    assert manifest["source_kind"] == "SELF_BUILT"
    assert manifest["license"] == "AgentForge project original"
    assert manifest["source_urls"] == []
    assert manifest["buggy_revision"] is None
    assert manifest["fixed_revision"] is None


def test_fixture_python_does_not_use_network_shell_subprocess_or_sleep() -> None:
    forbidden_imports = {
        "asyncio",
        "builtins",
        "ctypes",
        "http",
        "importlib",
        "multiprocessing",
        "os",
        "requests",
        "socket",
        "subprocess",
        "sys",
        "urllib",
    }
    forbidden_names = {"__import__", "compile", "eval", "exec", "popen", "system"}
    forbidden_attributes = {
        "create_subprocess_exec",
        "create_subprocess_shell",
        "execv",
        "execve",
        "fork",
        "popen",
        "spawnl",
        "spawnv",
        "startfile",
        "system",
        "__import__",
    }

    for path in TASKS.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_roots = {alias.name.split(".")[0] for alias in node.names}
                assert imported_roots.isdisjoint(forbidden_imports)
            elif isinstance(node, ast.ImportFrom):
                assert (node.module or "").split(".")[0] not in forbidden_imports
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in forbidden_names
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr not in forbidden_attributes
                if isinstance(node.func.value, ast.Name):
                    assert f"{node.func.value.id}.{node.func.attr}" != "time.sleep"


def test_verification_report_records_three_fresh_attempts_per_state_and_suite() -> None:
    report = _load_json(FIXTURES / "verification_report.json")

    assert report["verification_schema_version"] == "1.0.0"
    assert report["repeat_count"] == 3
    assert report["task_count"] == 4
    assert report["accepted"] is True
    assert report["model_evaluation_performed"] is False
    verifier = _load_fixture_verifier()
    assert report["fixture_asset_digest"] == verifier.compute_fixture_asset_digest()
    assert len(report["results"]) == 4 * 2 * 2 * 3

    observed = {
        (result["task_id"], result["state"], result["suite"], result["attempt"])
        for result in report["results"]
    }
    expected = {
        (task_id, state, suite, attempt)
        for task_id in EXPECTED_TASK_IDS
        for state in ("buggy", "reference")
        for suite in ("visible", "hidden")
        for attempt in (1, 2, 3)
    }
    assert observed == expected
    assert all(not result["timed_out"] for result in report["results"])
    assert all(
        result["outcome"] == ("FAIL" if result["state"] == "buggy" else "PASS")
        for result in report["results"]
    )
    for result in report["results"]:
        if result["state"] == "buggy" and result["suite"] == "visible":
            assert set(result["failed_node_ids"]) == EXPECTED_VISIBLE_FAILURES[result["task_id"]]


def _load_fixture_verifier() -> Any:
    import importlib.util

    path = FIXTURES / "verify_fixtures.py"
    spec = importlib.util.spec_from_file_location("fixture_report_verifier", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
