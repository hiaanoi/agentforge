from __future__ import annotations

import csv
import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ASSET_ROOT = PROJECT_ROOT / "evaluation" / "candidates"

REQUIRED_CANDIDATE_FIELDS = {
    "candidate_id",
    "candidate_schema_version",
    "source_kind",
    "source_dataset",
    "source_project",
    "source_bug_id",
    "source_issue_title",
    "source_urls",
    "issue_url",
    "buggy_commit",
    "fixed_commit",
    "license",
    "license_verified",
    "attribution_requirements",
    "language",
    "original_bug_summary",
    "observed_failure",
    "expected_behavior",
    "affected_files",
    "patch_file_count",
    "patch_added_lines",
    "patch_deleted_lines",
    "original_tests",
    "dependency_requirements",
    "python_version_constraints",
    "operating_system_constraints",
    "requires_network",
    "requires_external_service",
    "requires_database",
    "requires_native_build",
    "deterministic_tests",
    "estimated_test_runtime_seconds",
    "expected_fixture_file_count",
    "expected_fixture_size_bytes",
    "expected_edit_rounds",
    "cross_module_reasoning",
    "requires_test_feedback",
    "misleading_symptom",
    "visible_test_feasibility",
    "hidden_test_feasibility",
    "anti_hardcoding_feasibility",
    "windows_feasibility",
    "offline_feasibility",
    "adaptation_actions",
    "adaptation_risks",
    "provenance_confidence",
    "difficulty_recommendation",
    "difficulty_rationale",
    "audit_status",
    "exclusion_reasons",
    "score_breakdown",
    "total_score",
    "evidence_notes",
    "field_evidence",
    "audited_at",
}

SCORE_MAXIMA = {
    "reproducibility": 20,
    "runtime_compatibility": 20,
    "engineering_realism": 15,
    "hidden_test_strength": 15,
    "adaptation_cost": 10,
    "diversity_contribution": 10,
    "demo_value": 5,
    "provenance_confidence": 5,
}

CSV_FIELDS = (
    "candidate_id",
    "source_kind",
    "source_project",
    "source_bug_id",
    "audit_status",
    "difficulty_recommendation",
    "license",
    "license_verified",
    "total_score",
    "estimated_test_runtime_seconds",
    "patch_file_count",
    "expected_edit_rounds",
    "windows_feasibility",
    "offline_feasibility",
)


def _load_json(name: str) -> Any:
    return json.loads((ASSET_ROOT / name).read_text(encoding="utf-8"))


def _candidate_by_id() -> dict[str, dict[str, Any]]:
    candidates = _load_json("candidates.json")["candidates"]
    return {candidate["candidate_id"]: candidate for candidate in candidates}


def test_candidate_schema_freezes_required_fields_and_enums() -> None:
    schema = _load_json("candidate_schema.json")
    candidate_schema = schema["$defs"]["candidate"]

    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["schema_version"] == "1.0.0"
    assert set(candidate_schema["required"]) == REQUIRED_CANDIDATE_FIELDS
    assert candidate_schema["additionalProperties"] is False
    assert candidate_schema["properties"]["source_kind"]["enum"] == [
        "QUIXBUGS",
        "BUGSINPY",
        "SWE_BENCH",
        "GITHUB",
        "SELF_BUILT",
    ]
    assert candidate_schema["properties"]["audit_status"]["enum"] == [
        "ELIGIBLE",
        "CONDITIONAL",
        "REJECTED",
    ]


def test_candidate_pool_has_unique_ids_valid_fields_and_scores() -> None:
    document = _load_json("candidates.json")
    candidates = document["candidates"]
    candidate_ids = [candidate["candidate_id"] for candidate in candidates]

    assert document["candidate_schema_version"] == "1.0.0"
    assert 20 <= len(candidates) <= 24
    assert len(candidate_ids) == len(set(candidate_ids))

    for candidate in candidates:
        assert set(candidate) == REQUIRED_CANDIDATE_FIELDS
        assert candidate["candidate_schema_version"] == "1.0.0"
        assert candidate["source_kind"] in {
            "QUIXBUGS",
            "BUGSINPY",
            "SWE_BENCH",
            "GITHUB",
            "SELF_BUILT",
        }
        assert candidate["audit_status"] in {"ELIGIBLE", "CONDITIONAL", "REJECTED"}
        assert set(candidate["score_breakdown"]) == set(SCORE_MAXIMA)
        points = 0
        for category, maximum in SCORE_MAXIMA.items():
            score = candidate["score_breakdown"][category]
            assert set(score) == {"score", "max_score", "evidence", "uncertainty", "deductions"}
            assert score["max_score"] == maximum
            assert 0 <= score["score"] <= maximum
            assert score["evidence"]
            points += score["score"]
        assert candidate["total_score"] == points


def test_public_sources_and_field_level_provenance_are_complete() -> None:
    candidates = _candidate_by_id().values()

    for candidate in candidates:
        evidence = candidate["field_evidence"]
        assert set(evidence) == REQUIRED_CANDIDATE_FIELDS - {"field_evidence"}
        for field_name, claim_evidence in evidence.items():
            assert claim_evidence["status"] in {"VERIFIED", "INFERRED", "UNVERIFIED"}, field_name
            assert isinstance(claim_evidence["evidence_refs"], list)
            assert claim_evidence["notes"]
        if candidate["source_kind"] != "SELF_BUILT":
            assert candidate["source_urls"]
            assert all(url.startswith("https://") for url in candidate["source_urls"])
            assert candidate["license"]
            if evidence["license"]["status"] == "VERIFIED":
                assert evidence["license"]["evidence_refs"]
            else:
                assert candidate["license_verified"] is False
            assert evidence["original_bug_summary"]["evidence_refs"]
            assert evidence["fixed_commit"]["evidence_refs"]


def test_csv_is_a_consistent_projection_of_canonical_json() -> None:
    candidates = _candidate_by_id()
    with (ASSET_ROOT / "candidate_matrix.csv").open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))

    assert tuple(rows[0]) == CSV_FIELDS
    assert {row["candidate_id"] for row in rows} == set(candidates)
    for row in rows:
        candidate = candidates[row["candidate_id"]]
        for field in CSV_FIELDS:
            expected = candidate[field]
            if isinstance(expected, bool):
                expected = str(expected).lower()
            else:
                expected = str(expected)
            assert row[field] == expected


def test_selection_lists_are_disjoint_valid_and_meet_primary_mix() -> None:
    candidates = _candidate_by_id()
    primary_document = _load_json("recommended_primary.json")
    alternate_document = _load_json("recommended_alternates.json")
    primary = primary_document["candidate_ids"]
    alternates = alternate_document["candidate_ids"]
    rejected = _load_json("rejected_candidates.json")["candidate_ids"]

    assert primary_document["selection_status"] == "HUMAN_APPROVED_FOR_B2_PREFLIGHT"
    assert primary_document["human_decision"] == {
        "replacement_candidate_ids": [],
        "main_demo_candidate_id": "self-durable-double-consumption",
        "main_demo_backup_candidate_id": "self-terminal-state-cas",
        "difficulty_mix_accepted": {"BASIC": 6, "ENGINEERING": 3, "CHALLENGE": 1},
        "conditional_validation_strategy": "SOURCE_TIERED",
        "next_phase": "M7_B2_0_PREFLIGHT",
    }
    assert alternate_document["selection_status"] == "REGISTERED_ALTERNATES"

    assert len(primary) == 10
    assert len(alternates) >= 4
    assert set(primary).isdisjoint(alternates)
    assert set(primary).isdisjoint(rejected)
    assert set(alternates).isdisjoint(rejected)
    assert set(primary) | set(alternates) | set(rejected) == set(candidates)

    for candidate_id in primary:
        candidate = candidates[candidate_id]
        assert candidate["audit_status"] == "ELIGIBLE"
        assert candidate["license_verified"] is True

    source_counts = Counter(candidates[candidate_id]["source_kind"] for candidate_id in primary)
    assert source_counts == {
        "QUIXBUGS": 2,
        "BUGSINPY": 3,
        "SWE_BENCH": 2,
        "SELF_BUILT": 3,
    }
    difficulty_counts = Counter(
        candidates[candidate_id]["difficulty_recommendation"] for candidate_id in primary
    )
    assert difficulty_counts == {"BASIC": 6, "ENGINEERING": 3, "CHALLENGE": 1}


def test_recommendations_respect_license_and_audit_gates() -> None:
    candidates = _candidate_by_id()
    recommended = set(_load_json("recommended_primary.json")["candidate_ids"])
    recommended.update(_load_json("recommended_alternates.json")["candidate_ids"])

    for candidate_id in recommended:
        candidate = candidates[candidate_id]
        assert candidate["audit_status"] != "REJECTED"
        if not candidate["license_verified"]:
            assert candidate_id not in _load_json("recommended_primary.json")["candidate_ids"]


def test_assets_contain_no_absolute_paths_or_secret_material() -> None:
    forbidden_secret_patterns = (
        re.compile(r"sk-[A-Za-z0-9_-]{16,}"),
        re.compile(r"OPENAI_API_KEY\s*="),
        re.compile(r"DEEPSEEK_API_KEY\s*="),
        re.compile(r"(?:TOKEN|SECRET|PASSWORD)\s*=", re.IGNORECASE),
    )
    windows_absolute_path = re.compile(r"(?<![A-Za-z])[A-Za-z]:[\\/]")

    for path in ASSET_ROOT.iterdir():
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        assert not windows_absolute_path.search(text), path.name
        assert not any(pattern.search(text) for pattern in forbidden_secret_patterns), path.name


def test_artifact_registry_matches_small_checked_in_assets() -> None:
    registry = _load_json("artifact_registry.json")
    registered_paths = {entry["path"] for entry in registry["artifacts"]}
    actual_paths = {
        path.relative_to(PROJECT_ROOT).as_posix()
        for path in ASSET_ROOT.iterdir()
        if path.is_file() and path.name != "artifact_registry.json"
    }

    assert registered_paths == actual_paths
    for entry in registry["artifacts"]:
        path = PROJECT_ROOT / entry["path"]
        content = path.read_bytes()
        assert entry["sha256"] == hashlib.sha256(content).hexdigest()
        assert entry["size_bytes"] == len(content)
        assert len(content) <= 1_000_000
    assert sum(entry["size_bytes"] for entry in registry["artifacts"]) <= 5_000_000
