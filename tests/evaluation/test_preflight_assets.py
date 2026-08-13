from __future__ import annotations

import csv
import hashlib
import json
import re
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ASSET_ROOT = PROJECT_ROOT / "evaluation" / "preflight"

PRIMARY_IDS = {
    "quixbugs-shortest-path-length",
    "quixbugs-topological-ordering",
    "bugsinpy-black-21",
    "bugsinpy-pysnooper-3",
    "bugsinpy-httpie-4",
    "swebench-flask-5014",
    "swebench-pytest-10051",
    "self-durable-double-consumption",
    "self-policy-priority-shadow",
    "self-async-cancel-cleanup",
}

REQUIRED_FIELDS = {
    "candidate_id",
    "preflight_schema_version",
    "source_kind",
    "original_audit_status",
    "source_project",
    "source_bug_id",
    "license",
    "license_status",
    "attribution_requirements",
    "buggy_revision",
    "fixed_revision",
    "source_evidence",
    "source_checkout_verified",
    "bug_fix_relation_verified",
    "original_failure_reproduced",
    "reference_fix_verified",
    "failure_command",
    "passing_command",
    "test_repetitions",
    "deterministic_result",
    "minimum_test_runtime_ms",
    "maximum_test_runtime_ms",
    "dependency_inventory",
    "dependency_install_required_for_source_audit",
    "target_fixture_runtime_dependencies",
    "network_required",
    "external_service_required",
    "database_required",
    "native_build_required",
    "python_3_14_status",
    "windows_status",
    "offline_status",
    "fixed_test_profile_feasibility",
    "crop_feasibility",
    "crop_fidelity_risk",
    "required_source_files",
    "required_test_files",
    "required_support_files",
    "expected_fixture_file_count",
    "expected_fixture_size_bytes",
    "reference_patch_files",
    "reference_patch_lines",
    "reference_patch_changes_tests",
    "reference_patch_changes_dependencies",
    "visible_test_plan",
    "hidden_test_plan",
    "anti_hardcoding_plan",
    "known_incorrect_patch_examples",
    "protected_paths_plan",
    "expected_edit_rounds",
    "difficulty_before",
    "difficulty_after_preflight",
    "difficulty_change_reason",
    "demo_suitability",
    "primary_risks",
    "unresolved_questions",
    "evidence_classification",
    "final_gate",
    "gate_reasons",
    "conditional_resolution",
    "recommended_action",
    "audited_at",
}

CSV_FIELDS = (
    "candidate_id",
    "source_kind",
    "original_audit_status",
    "license_status",
    "source_checkout_verified",
    "original_failure_reproduced",
    "reference_fix_verified",
    "deterministic_result",
    "python_3_14_status",
    "windows_status",
    "offline_status",
    "crop_feasibility",
    "difficulty_before",
    "difficulty_after_preflight",
    "final_gate",
    "recommended_action",
)


def _load_json(name: str) -> Any:
    return json.loads((ASSET_ROOT / name).read_text(encoding="utf-8"))


def _results() -> list[dict[str, Any]]:
    return _load_json("primary_preflight_results.json")["results"]


def test_schema_freezes_required_fields_and_gate_enums() -> None:
    schema = _load_json("preflight_schema.json")
    result_schema = schema["$defs"]["preflight_result"]

    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["schema_version"] == "1.0.0"
    assert set(result_schema["required"]) == REQUIRED_FIELDS
    assert result_schema["additionalProperties"] is False
    assert result_schema["properties"]["final_gate"]["enum"] == [
        "GO",
        "CONDITIONAL",
        "NO_GO",
        "UNVERIFIED",
    ]


def test_all_ten_accepted_primary_candidates_have_unique_results() -> None:
    document = _load_json("primary_preflight_results.json")
    results = document["results"]
    ids = [result["candidate_id"] for result in results]

    assert document["preflight_schema_version"] == "1.0.0"
    assert document["selection_basis"] == "M7_B1_HUMAN_APPROVED_PRIMARY"
    assert len(ids) == len(set(ids)) == 10
    assert set(ids) == PRIMARY_IDS
    assert all(set(result) == REQUIRED_FIELDS for result in results)


def test_evidence_classification_is_explicit_for_every_key_conclusion() -> None:
    required_claims = {
        "license_status",
        "source_checkout_verified",
        "bug_fix_relation_verified",
        "original_failure_reproduced",
        "reference_fix_verified",
        "deterministic_result",
        "python_3_14_status",
        "windows_status",
        "offline_status",
        "fixed_test_profile_feasibility",
        "crop_feasibility",
        "final_gate",
    }
    for result in _results():
        evidence = result["evidence_classification"]
        assert set(evidence) == required_claims
        for claim in evidence.values():
            assert claim["status"] in {"VERIFIED", "INFERRED", "UNVERIFIED"}
            assert claim["evidence_refs"]
            assert claim["summary"]


def test_final_gates_obey_hard_requirements() -> None:
    for result in _results():
        gate = result["final_gate"]
        assert gate in {"GO", "CONDITIONAL", "NO_GO", "UNVERIFIED"}
        assert result["gate_reasons"]
        if gate == "GO":
            assert result["license_status"] == "VERIFIED"
            assert result["offline_status"] == "VERIFIED"
            assert result["fixed_test_profile_feasibility"] is True
            assert result["crop_feasibility"] == "VERIFIED"
            assert result["deterministic_result"] is True
            assert not result["unresolved_questions"]
            assert result["conditional_resolution"] == []
        elif gate == "CONDITIONAL":
            assert 1 <= len(result["conditional_resolution"]) <= 2
            assert result["unresolved_questions"]
        elif gate == "NO_GO":
            assert result["recommended_action"] == "REPLACE_AFTER_HUMAN_APPROVAL"
        else:
            assert any(
                claim["status"] == "UNVERIFIED"
                for claim in result["evidence_classification"].values()
            )


def test_execution_and_test_design_evidence_is_bounded_and_complete() -> None:
    for result in _results():
        assert result["test_repetitions"] >= 0
        assert result["minimum_test_runtime_ms"] >= 0
        assert result["maximum_test_runtime_ms"] >= result["minimum_test_runtime_ms"]
        assert result["visible_test_plan"]
        assert result["hidden_test_plan"]
        assert result["anti_hardcoding_plan"]
        assert len(result["known_incorrect_patch_examples"]) >= 1
        assert result["protected_paths_plan"]
        assert result["expected_fixture_file_count"] >= 1
        assert result["expected_fixture_size_bytes"] >= 1
        assert result["reference_patch_lines"] >= 0
        if result["source_kind"] == "SELF_BUILT":
            assert result["test_repetitions"] == 0
            assert result["failure_command"] is None
            assert result["passing_command"] is None
            assert len(result["known_incorrect_patch_examples"]) >= 2
        else:
            assert result["source_checkout_verified"] is True


def test_csv_is_consistent_projection() -> None:
    by_id = {result["candidate_id"]: result for result in _results()}
    with (ASSET_ROOT / "primary_preflight_matrix.csv").open(
        encoding="utf-8", newline=""
    ) as handle:
        rows = list(csv.DictReader(handle))

    assert tuple(rows[0]) == CSV_FIELDS
    assert {row["candidate_id"] for row in rows} == PRIMARY_IDS
    for row in rows:
        result = by_id[row["candidate_id"]]
        for field in CSV_FIELDS:
            expected = result[field]
            if isinstance(expected, bool):
                expected = str(expected).lower()
            assert row[field] == str(expected)


def test_replacements_are_advisory_and_use_registered_alternates_only() -> None:
    candidate_assets = _load_json("../candidates/recommended_alternates.json")
    registered = set(candidate_assets["candidate_ids"])
    document = _load_json("replacement_recommendations.json")

    assert document["automatic_replacement_performed"] is False
    assert document["accepted_primary_candidate_ids"] == list(
        _load_json("primary_preflight_results.json")["candidate_ids"]
    )
    for recommendation in document["recommendations"]:
        assert recommendation["alternate_candidate_id"] in registered
        assert recommendation["requires_human_approval"] is True


def test_decision_outputs_preserve_demo_and_first_batch_boundaries() -> None:
    demo = _load_json("demo_preflight.json")
    summary = _load_json("gate_summary.json")
    replacements = _load_json("replacement_recommendations.json")

    assert demo["primary_demo_candidate_id"] == "self-durable-double-consumption"
    assert demo["backup_demo_candidate_id"] == "self-terminal-state-cas"
    assert demo["automatic_switch_performed"] is False
    assert len(summary["recommended_first_fixture_candidate_ids"]) == 4
    assert set(summary["recommended_first_fixture_candidate_ids"]) <= PRIMARY_IDS
    assert summary["human_decision"] == {
        "decision_status": "M7_B2_1_AUTHORIZED",
        "approved_first_fixture_candidate_ids": summary[
            "recommended_first_fixture_candidate_ids"
        ],
        "conditional_candidate_strategy": "BUILD_WITH_ADMISSION_GATES",
        "current_replacement_decision": "NO_REPLACEMENTS",
        "model_runs_authorized": False,
        "decided_at": "2026-07-17T00:00:00+08:00",
    }
    assert replacements["human_decision"] == "NO_REPLACEMENTS_ACCEPTED"
    assert summary["formal_fixture_construction_started"] is True
    assert summary["formal_fixture_verification_passed"] is True
    fixture_report = json.loads(
        (PROJECT_ROOT / "evaluation" / "fixtures" / "verification_report.json").read_text(
            encoding="utf-8"
        )
    )
    assert summary["formal_fixture_asset_digest"] == fixture_report["fixture_asset_digest"]
    assert summary["model_evaluation_started"] is False
    assert summary["m7_b2_1_started"] is True
    assert summary["m7_b2_1_completed"] is True


def test_assets_contain_no_absolute_paths_secrets_or_large_third_party_files() -> None:
    windows_path = re.compile(r"(?<![A-Za-z])[A-Za-z]:[\\/]")
    secret_patterns = (
        re.compile(r"sk-[A-Za-z0-9_-]{16,}"),
        re.compile(r"OPENAI_API_KEY\s*="),
        re.compile(r"(?:TOKEN|SECRET|PASSWORD)\s*=", re.IGNORECASE),
    )
    for path in ASSET_ROOT.iterdir():
        if not path.is_file():
            continue
        content = path.read_text(encoding="utf-8")
        assert not windows_path.search(content), path.name
        assert not any(pattern.search(content) for pattern in secret_patterns), path.name
        assert path.stat().st_size <= 1_000_000


def test_artifact_registry_matches_generated_assets() -> None:
    registry = _load_json("artifact_registry.json")
    registered = {entry["path"] for entry in registry["artifacts"]}
    actual = {
        path.relative_to(PROJECT_ROOT).as_posix()
        for path in ASSET_ROOT.iterdir()
        if path.is_file() and path.name != "artifact_registry.json"
    }
    assert registered == actual
    for entry in registry["artifacts"]:
        content = (PROJECT_ROOT / entry["path"]).read_bytes()
        assert entry["sha256"] == hashlib.sha256(content).hexdigest()
        assert entry["size_bytes"] == len(content)
    assert sum(entry["size_bytes"] for entry in registry["artifacts"]) <= 5_000_000
