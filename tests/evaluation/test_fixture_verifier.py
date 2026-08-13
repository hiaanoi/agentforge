from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
VERIFIER_PATH = ROOT / "evaluation" / "fixtures" / "verify_fixtures.py"
FORMAL_TASKS = ROOT / "evaluation" / "fixtures" / "tasks"


def _load_verifier() -> ModuleType:
    spec = importlib.util.spec_from_file_location("fixture_verifier", VERIFIER_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_minimal_task(root: Path) -> Path:
    task = root / "task"
    (task / "workspace" / "sample").mkdir(parents=True)
    (task / "tests" / "visible").mkdir(parents=True)
    (task / "tests" / "hidden").mkdir(parents=True)
    (task / "reference" / "fixed_files" / "sample").mkdir(parents=True)
    (task / "workspace" / "sample" / "value.py").write_text("VALUE = 1\n", encoding="utf-8")
    (task / "reference" / "fixed_files" / "sample" / "value.py").write_text(
        "VALUE = 2\n", encoding="utf-8"
    )
    test = "from sample.value import VALUE\n\ndef test_value():\n    assert VALUE == 2\n"
    (task / "tests" / "visible" / "test_value.py").write_text(test, encoding="utf-8")
    (task / "tests" / "hidden" / "test_value.py").write_text(test, encoding="utf-8")
    manifest = {
        "fixture_schema_version": "1.0.0",
        "task_id": "sample",
        "editable_paths": ["workspace/sample/value.py"],
        "protected_paths": ["tests/visible/test_value.py", "tests/hidden/test_value.py"],
        "reference_files": ["reference/fixed_files/sample/value.py"],
        "test_commands": {
            "visible": ["-m", "pytest", "tests/visible", "-q"],
            "hidden": ["-m", "pytest", "tests/hidden", "-q"],
        },
        "limits": {"timeout_seconds": 10, "max_output_chars": 2000},
    }
    (task / "task_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return task


def test_safe_join_rejects_absolute_and_parent_paths(tmp_path: Path) -> None:
    verifier = _load_verifier()

    with pytest.raises(ValueError, match="relative"):
        verifier.safe_join(tmp_path, "C:/Windows/system.ini")
    with pytest.raises(ValueError, match="traversal"):
        verifier.safe_join(tmp_path, "../outside")


def test_reference_overlay_is_applied_only_to_the_temporary_workspace(tmp_path: Path) -> None:
    verifier = _load_verifier()
    task = _write_minimal_task(tmp_path)
    original = (task / "workspace" / "sample" / "value.py").read_text(encoding="utf-8")

    result = verifier.run_attempt(task, "reference", "visible", 1)

    assert result["outcome"] == "PASS"
    assert (task / "workspace" / "sample" / "value.py").read_text(encoding="utf-8") == original


def test_buggy_attempt_fails_without_using_reference_files(tmp_path: Path) -> None:
    verifier = _load_verifier()
    task = _write_minimal_task(tmp_path)

    result = verifier.run_attempt(task, "buggy", "hidden", 1)

    assert result["outcome"] == "FAIL"
    assert result["returncode"] != 0


def test_minimal_environment_drops_host_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    verifier = _load_verifier()
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-pass")
    monkeypatch.setenv("GITHUB_TOKEN", "must-not-pass")
    monkeypatch.setenv("SSH_AUTH_SOCK", "must-not-pass")

    environment = verifier.build_environment(Path("workspace"))

    assert "OPENAI_API_KEY" not in environment
    assert "GITHUB_TOKEN" not in environment
    assert "SSH_AUTH_SOCK" not in environment
    assert environment["PYTHONNOUSERSITE"] == "1"
    assert environment["PYTHONDONTWRITEBYTECODE"] == "1"
    assert environment["PYTHONUTF8"] == "1"
    assert environment["PYTHONIOENCODING"] == "utf-8"
    assert environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"
    assert environment["PYTHONHASHSEED"] == "0"
    assert Path(environment["TEMP"]).parent == Path("workspace").resolve().parent
    assert Path(environment["TMP"]).parent == Path("workspace").resolve().parent


def test_attempt_report_is_versioned_and_output_is_bounded(tmp_path: Path) -> None:
    verifier = _load_verifier()
    task = _write_minimal_task(tmp_path)
    manifest_path = task / "task_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["limits"]["max_output_chars"] = 80
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    noisy_test = "def test_noisy():\n    print('x' * 10000)\n    assert False\n"
    (task / "tests" / "visible" / "test_value.py").write_text(noisy_test, encoding="utf-8")

    result = verifier.run_attempt(task, "buggy", "visible", 7)

    assert result["result_schema_version"] == "1.0.0"
    assert result["attempt"] == 7
    assert len(result["stdout"]) <= 80
    assert len(result["stderr"]) <= 80
    assert result["stdout_truncated"] is True
    assert "workspace_path" not in result


def test_hidden_tests_are_not_copied_under_editable_workspace(tmp_path: Path) -> None:
    verifier = _load_verifier()
    task = _write_minimal_task(tmp_path)

    prepared = verifier.prepare_attempt(task, "buggy", tmp_path / "attempt")

    assert not (prepared.workspace / "tests").exists()
    assert (prepared.execution_root / "tests" / "hidden" / "test_value.py").is_file()


def test_reference_overlay_rejects_a_target_not_declared_editable(tmp_path: Path) -> None:
    verifier = _load_verifier()
    task = _write_minimal_task(tmp_path)
    manifest_path = task / "task_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["editable_paths"] = ["workspace/sample/other.py"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="editable"):
        verifier.prepare_attempt(task, "reference", tmp_path / "attempt")


def test_timeout_is_reported_as_an_explicit_failed_attempt(tmp_path: Path) -> None:
    verifier = _load_verifier()
    task = _write_minimal_task(tmp_path)
    slow_test = "import time\n\ndef test_slow():\n    time.sleep(5)\n"
    (task / "tests" / "visible" / "test_value.py").write_text(slow_test, encoding="utf-8")
    manifest_path = task / "task_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["limits"]["timeout_seconds"] = 1
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = verifier.run_attempt(task, "buggy", "visible", 1)

    assert result["outcome"] == "FAIL"
    assert result["timed_out"] is True
    assert result["returncode"] == -1


def test_attempt_output_redacts_the_ephemeral_execution_root(tmp_path: Path) -> None:
    verifier = _load_verifier()
    execution_root = tmp_path / "agentforge-sample-secret"
    output = f"native={execution_root} posix={execution_root.as_posix()}"

    sanitized = verifier.sanitize_output(output, execution_root)

    assert sanitized == "native=<execution_root> posix=<execution_root>"
    assert "agentforge-sample-secret" not in sanitized

    python_output = f"traceback={Path(sys.base_prefix) / 'Lib' / 'module.py'}"
    sanitized_python = verifier.sanitize_output(python_output, execution_root)
    assert sanitized_python == f"traceback={Path('<python_root>') / 'Lib' / 'module.py'}"
    assert sys.base_prefix not in sanitized_python


def test_formal_manifest_validation_rejects_command_drift(tmp_path: Path) -> None:
    verifier = _load_verifier()
    source = FORMAL_TASKS / "quixbugs-shortest-path-length"
    task = tmp_path / source.name
    shutil.copytree(source, task)
    manifest_path = task / "task_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["test_commands"]["hidden"] = ["-c", "print('untrusted command')"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="fixed pytest argv"):
        verifier.validate_manifest(task, source.name)


def test_formal_manifest_validation_rejects_limit_type_and_range_drift(tmp_path: Path) -> None:
    verifier = _load_verifier()
    source = FORMAL_TASKS / "quixbugs-shortest-path-length"
    task = tmp_path / source.name
    shutil.copytree(source, task)
    manifest_path = task / "task_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["limits"] = {"timeout_seconds": 31, "max_output_chars": "unbounded"}
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="limits"):
        verifier.validate_manifest(task, source.name)


def test_formal_manifest_validation_normalizes_wrong_enum_types_to_value_error(
    tmp_path: Path,
) -> None:
    verifier = _load_verifier()
    source = FORMAL_TASKS / "quixbugs-shortest-path-length"
    task = tmp_path / source.name
    shutil.copytree(source, task)
    manifest_path = task / "task_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["source_kind"] = ["QUIXBUGS"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="source_kind"):
        verifier.validate_manifest(task, source.name)
