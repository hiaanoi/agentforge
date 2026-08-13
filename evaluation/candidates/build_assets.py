# ruff: noqa: E501

from __future__ import annotations

import csv
import hashlib
import json
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
SCHEMA_VERSION = "1.0.0"
AUDITED_AT = "2026-07-16T00:00:00+08:00"

HUMAN_DECISION = {
    "replacement_candidate_ids": [],
    "main_demo_candidate_id": "self-durable-double-consumption",
    "main_demo_backup_candidate_id": "self-terminal-state-cas",
    "difficulty_mix_accepted": {"BASIC": 6, "ENGINEERING": 3, "CHALLENGE": 1},
    "conditional_validation_strategy": "SOURCE_TIERED",
    "next_phase": "M7_B2_0_PREFLIGHT",
}

REQUIRED_FIELDS = (
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
)

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

PUBLIC_VERIFIED_FIELDS = {
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
    "audited_at",
}

SELF_VERIFIED_FIELDS = {
    "candidate_id",
    "candidate_schema_version",
    "source_kind",
    "source_dataset",
    "source_project",
    "source_bug_id",
    "source_urls",
    "issue_url",
    "buggy_commit",
    "fixed_commit",
    "license",
    "license_verified",
    "attribution_requirements",
    "language",
    "audited_at",
}

PRIMARY_IDS = (
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
)

ALTERNATE_IDS = (
    "quixbugs-flatten",
    "bugsinpy-cookiecutter-2",
    "bugsinpy-fastapi-3",
    "swebench-requests-5414",
    "swebench-pylint-6386",
    "swebench-pytest-5840",
    "self-terminal-state-cas",
    "self-adapter-default-propagation",
)

REJECTED_IDS = (
    "quixbugs-bitcount",
    "bugsinpy-tqdm-8",
)


def _write_json(name: str, value: Any) -> None:
    (ROOT / name).write_text(
        json.dumps(value, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _score_breakdown(
    scores: Sequence[int],
    *,
    evidence: str,
    uncertainty: str,
    deductions: str,
) -> dict[str, dict[str, Any]]:
    if len(scores) != len(SCORE_MAXIMA):
        raise ValueError("one score is required for every rubric category")
    return {
        category: {
            "score": score,
            "max_score": maximum,
            "evidence": evidence,
            "uncertainty": uncertainty,
            "deductions": deductions,
        }
        for (category, maximum), score in zip(SCORE_MAXIMA.items(), scores, strict=True)
    }


def _field_evidence(
    fields: Iterable[str],
    *,
    evidence_refs: list[str],
    verified_fields: set[str],
    unverified_fields: set[str],
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for field in fields:
        if field in verified_fields:
            result[field] = {
                "status": "VERIFIED",
                "evidence_refs": evidence_refs,
                "notes": f"{field} was checked against the cited first-party evidence.",
            }
        elif field in unverified_fields:
            result[field] = {
                "status": "UNVERIFIED",
                "evidence_refs": [],
                "notes": f"{field} remains unresolved and requires a B2 gate decision or experiment.",
            }
        else:
            result[field] = {
                "status": "INFERRED",
                "evidence_refs": evidence_refs,
                "notes": f"{field} is an M7-B1 adaptation estimate, not a completed fixture result.",
            }
    return result


def _candidate(
    *,
    candidate_id: str,
    source_kind: str,
    source_dataset: str,
    source_project: str,
    source_bug_id: str,
    source_issue_title: str,
    source_urls: list[str],
    issue_url: str | None,
    buggy_commit: str | None,
    fixed_commit: str | None,
    license_name: str,
    license_verified: bool,
    attribution_requirements: str,
    summary: str,
    observed_failure: str,
    expected_behavior: str,
    affected_files: list[str],
    patch_stats: tuple[int, int, int],
    original_tests: list[str],
    dependencies: list[str],
    python_constraints: str,
    os_constraints: list[str],
    requires_network: bool,
    requires_external_service: bool,
    requires_database: bool,
    requires_native_build: bool,
    deterministic_tests: bool,
    estimated_runtime: int,
    fixture_files: int,
    fixture_size: int,
    edit_rounds: int,
    cross_module_reasoning: str,
    requires_test_feedback: bool,
    misleading_symptom: bool,
    visible_tests: str,
    hidden_tests: str,
    anti_hardcoding: str,
    windows_feasibility: str,
    offline_feasibility: str,
    adaptation_actions: list[str],
    adaptation_risks: list[str],
    provenance_confidence: str,
    difficulty: str,
    difficulty_rationale: str,
    audit_status: str,
    exclusion_reasons: list[str],
    scores: Sequence[int],
    verified_fields: set[str] | None = None,
    unverified_fields: set[str] | None = None,
) -> dict[str, Any]:
    score_breakdown = _score_breakdown(
        scores,
        evidence="First-party patch/test evidence plus the frozen AgentForge compatibility rubric.",
        uncertainty="No formal M7-B2 fixture or adapted test profile has been built.",
        deductions="Deductions reflect the listed adaptation, platform, dependency, and provenance risks.",
    )
    candidate: dict[str, Any] = {
        "candidate_id": candidate_id,
        "candidate_schema_version": SCHEMA_VERSION,
        "source_kind": source_kind,
        "source_dataset": source_dataset,
        "source_project": source_project,
        "source_bug_id": source_bug_id,
        "source_issue_title": source_issue_title,
        "source_urls": source_urls,
        "issue_url": issue_url,
        "buggy_commit": buggy_commit,
        "fixed_commit": fixed_commit,
        "license": license_name,
        "license_verified": license_verified,
        "attribution_requirements": attribution_requirements,
        "language": "Python",
        "original_bug_summary": summary,
        "observed_failure": observed_failure,
        "expected_behavior": expected_behavior,
        "affected_files": affected_files,
        "patch_file_count": patch_stats[0],
        "patch_added_lines": patch_stats[1],
        "patch_deleted_lines": patch_stats[2],
        "original_tests": original_tests,
        "dependency_requirements": dependencies,
        "python_version_constraints": python_constraints,
        "operating_system_constraints": os_constraints,
        "requires_network": requires_network,
        "requires_external_service": requires_external_service,
        "requires_database": requires_database,
        "requires_native_build": requires_native_build,
        "deterministic_tests": deterministic_tests,
        "estimated_test_runtime_seconds": estimated_runtime,
        "expected_fixture_file_count": fixture_files,
        "expected_fixture_size_bytes": fixture_size,
        "expected_edit_rounds": edit_rounds,
        "cross_module_reasoning": cross_module_reasoning,
        "requires_test_feedback": requires_test_feedback,
        "misleading_symptom": misleading_symptom,
        "visible_test_feasibility": visible_tests,
        "hidden_test_feasibility": hidden_tests,
        "anti_hardcoding_feasibility": anti_hardcoding,
        "windows_feasibility": windows_feasibility,
        "offline_feasibility": offline_feasibility,
        "adaptation_actions": adaptation_actions,
        "adaptation_risks": adaptation_risks,
        "provenance_confidence": provenance_confidence,
        "difficulty_recommendation": difficulty,
        "difficulty_rationale": difficulty_rationale,
        "audit_status": audit_status,
        "exclusion_reasons": exclusion_reasons,
        "score_breakdown": score_breakdown,
        "total_score": sum(item["score"] for item in score_breakdown.values()),
        "evidence_notes": [
            {
                "status": "VERIFIED",
                "claim": "The public source facts marked VERIFIED were checked against first-party URLs.",
                "evidence_refs": source_urls,
            },
            {
                "status": "INFERRED",
                "claim": "Fixture size, runtime compatibility, hidden-test strength, and difficulty are audit estimates.",
                "evidence_refs": source_urls,
            },
            {
                "status": "UNVERIFIED",
                "claim": "No adapted fixture, hidden tests, or reference fix were built or executed in M7-B1.",
                "evidence_refs": [],
            },
        ],
        "audited_at": AUDITED_AT,
    }
    candidate["field_evidence"] = _field_evidence(
        (field for field in REQUIRED_FIELDS if field != "field_evidence"),
        evidence_refs=source_urls,
        verified_fields=verified_fields or PUBLIC_VERIFIED_FIELDS,
        unverified_fields=unverified_fields or set(),
    )
    if set(candidate) != set(REQUIRED_FIELDS):
        raise ValueError(f"candidate shape mismatch: {candidate_id}")
    return candidate


def _schema() -> dict[str, Any]:
    string = {"type": "string"}
    nullable_string = {"type": ["string", "null"]}
    string_list = {"type": "array", "items": string}
    properties: dict[str, Any] = {
        field: string for field in REQUIRED_FIELDS
    }
    for field in (
        "source_urls",
        "affected_files",
        "original_tests",
        "dependency_requirements",
        "operating_system_constraints",
        "adaptation_actions",
        "adaptation_risks",
        "exclusion_reasons",
    ):
        properties[field] = string_list
    for field in ("issue_url", "buggy_commit", "fixed_commit"):
        properties[field] = nullable_string
    for field in (
        "license_verified",
        "requires_network",
        "requires_external_service",
        "requires_database",
        "requires_native_build",
        "deterministic_tests",
        "requires_test_feedback",
        "misleading_symptom",
    ):
        properties[field] = {"type": "boolean"}
    for field in (
        "patch_file_count",
        "patch_added_lines",
        "patch_deleted_lines",
        "estimated_test_runtime_seconds",
        "expected_fixture_file_count",
        "expected_fixture_size_bytes",
        "expected_edit_rounds",
        "total_score",
    ):
        properties[field] = {"type": "integer", "minimum": 0}
    properties["source_kind"] = {
        "enum": ["QUIXBUGS", "BUGSINPY", "SWE_BENCH", "GITHUB", "SELF_BUILT"]
    }
    properties["audit_status"] = {"enum": ["ELIGIBLE", "CONDITIONAL", "REJECTED"]}
    properties["difficulty_recommendation"] = {
        "enum": ["BASIC", "ENGINEERING", "CHALLENGE"]
    }
    properties["provenance_confidence"] = {"enum": ["HIGH", "MEDIUM", "LOW"]}
    properties["candidate_schema_version"] = {"const": SCHEMA_VERSION}
    properties["score_breakdown"] = {
        "type": "object",
        "required": list(SCORE_MAXIMA),
        "additionalProperties": False,
    }
    properties["evidence_notes"] = {
        "type": "array",
        "items": {
            "type": "object",
            "required": ["status", "claim", "evidence_refs"],
            "additionalProperties": False,
        },
    }
    properties["field_evidence"] = {
        "type": "object",
        "required": [field for field in REQUIRED_FIELDS if field != "field_evidence"],
        "additionalProperties": False,
    }
    candidate_schema = {
        "type": "object",
        "required": list(REQUIRED_FIELDS),
        "additionalProperties": False,
        "properties": properties,
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://agentforge.example/schemas/repair-candidate-1.0.0.json",
        "schema_version": SCHEMA_VERSION,
        "type": "object",
        "required": ["candidate_schema_version", "candidates"],
        "additionalProperties": False,
        "properties": {
            "candidate_schema_version": {"const": SCHEMA_VERSION},
            "candidates": {"type": "array", "items": {"$ref": "#/$defs/candidate"}},
        },
        "$defs": {"candidate": candidate_schema},
    }


QUIX_REVISION = "4257f44b0ff1181dedaedee6a447e133219fcebf"
QUIX_ROOT = f"https://github.com/jkoppel/QuixBugs/blob/{QUIX_REVISION}"
BUGSINPY_REVISION = "11c5f1eea954a42132cfd06bf257766a7963e0fd"
BUGSINPY_ROOT = f"https://github.com/soarsmu/BugsInPy/blob/{BUGSINPY_REVISION}"
SWE_DATASET_URL = "https://huggingface.co/datasets/SWE-bench/SWE-bench_Verified"


def _quix_candidate(
    *,
    name: str,
    title: str,
    summary: str,
    failure: str,
    expected: str,
    tests: str,
    cross_module: str,
    hidden_tests: str,
    risks: list[str],
    difficulty: str,
    difficulty_rationale: str,
    audit_status: str,
    exclusion_reasons: list[str],
    scores: Sequence[int],
    runtime: int = 2,
) -> dict[str, Any]:
    source_urls = [
        f"{QUIX_ROOT}/python_programs/{name}.py",
        f"{QUIX_ROOT}/correct_python_programs/{name}.py",
        f"{QUIX_ROOT}/python_testcases/test_{name}.py",
        f"{QUIX_ROOT}/LICENSE",
    ]
    return _candidate(
        candidate_id=f"quixbugs-{name.replace('_', '-')}",
        source_kind="QUIXBUGS",
        source_dataset="QuixBugs",
        source_project="jkoppel/QuixBugs",
        source_bug_id=name,
        source_issue_title=title,
        source_urls=source_urls,
        issue_url=None,
        buggy_commit=QUIX_REVISION,
        fixed_commit=QUIX_REVISION,
        license_name="MIT",
        license_verified=True,
        attribution_requirements="Retain the QuixBugs MIT copyright and permission notice.",
        summary=summary,
        observed_failure=failure,
        expected_behavior=expected,
        affected_files=[f"python_programs/{name}.py"],
        patch_stats=(1, 1, 1),
        original_tests=[tests],
        dependencies=["pytest supplied by the fixed TestProfile"],
        python_constraints="Original benchmark is Python 3; proposed fixture targets Python >=3.11.",
        os_constraints=["No documented OS restriction after removing benchmark harness coupling."],
        requires_network=False,
        requires_external_service=False,
        requires_database=False,
        requires_native_build=False,
        deterministic_tests=True,
        estimated_runtime=runtime,
        fixture_files=5,
        fixture_size=20000,
        edit_rounds=2,
        cross_module_reasoning=cross_module,
        requires_test_feedback=True,
        misleading_symptom=True,
        visible_tests="Expose one representative failure and preserve the function contract.",
        hidden_tests=hidden_tests,
        anti_hardcoding="Generate multiple graph/input shapes and assert semantic properties, not one example.",
        windows_feasibility="HIGH after a standalone pytest adaptation; not yet executed on Windows.",
        offline_feasibility="HIGH because the Python implementation and tests have no network dependency.",
        adaptation_actions=[
            "Copy only the relevant MIT-licensed function, support type, and attribution into a small fixture.",
            "Replace the benchmark import switch with visible and hidden fixed TestProfiles.",
            "Keep the original defect semantics while adding non-example hidden inputs.",
        ],
        adaptation_risks=risks,
        provenance_confidence="HIGH",
        difficulty=difficulty,
        difficulty_rationale=difficulty_rationale,
        audit_status=audit_status,
        exclusion_reasons=exclusion_reasons,
        scores=scores,
    )


def _bugsinpy_candidate(
    *,
    candidate_id: str,
    project_path: str,
    project_repo: str,
    bug_id: str,
    title: str,
    issue_url: str | None,
    buggy_commit: str,
    fixed_commit: str,
    license_name: str,
    license_verified: bool,
    license_url: str,
    attribution: str,
    summary: str,
    failure: str,
    expected: str,
    affected_files: list[str],
    patch_stats: tuple[int, int, int],
    original_tests: list[str],
    dependencies: list[str],
    python_constraints: str,
    os_constraints: list[str],
    cross_module: str,
    hidden_tests: str,
    anti_hardcoding: str,
    windows_feasibility: str,
    adaptation_actions: list[str],
    adaptation_risks: list[str],
    difficulty: str,
    difficulty_rationale: str,
    audit_status: str,
    exclusion_reasons: list[str],
    scores: Sequence[int],
    runtime: int,
    fixture_files: int,
    fixture_size: int,
    unverified_fields: set[str] | None = None,
) -> dict[str, Any]:
    metadata_root = f"{BUGSINPY_ROOT}/projects/{project_path}/bugs/{bug_id}"
    source_urls = [
        f"{metadata_root}/bug.info",
        f"{metadata_root}/bug_patch.txt",
        f"https://github.com/{project_repo}/commit/{fixed_commit}",
        license_url,
    ]
    if issue_url is not None:
        source_urls.append(issue_url)
    verified_fields = set(PUBLIC_VERIFIED_FIELDS)
    if not license_verified:
        verified_fields -= {"license", "license_verified", "attribution_requirements"}
    return _candidate(
        candidate_id=candidate_id,
        source_kind="BUGSINPY",
        source_dataset="BugsInPy",
        source_project=project_repo,
        source_bug_id=bug_id,
        source_issue_title=title,
        source_urls=source_urls,
        issue_url=issue_url,
        buggy_commit=buggy_commit,
        fixed_commit=fixed_commit,
        license_name=license_name,
        license_verified=license_verified,
        attribution_requirements=attribution,
        summary=summary,
        observed_failure=failure,
        expected_behavior=expected,
        affected_files=affected_files,
        patch_stats=patch_stats,
        original_tests=original_tests,
        dependencies=dependencies,
        python_constraints=python_constraints,
        os_constraints=os_constraints,
        requires_network=False,
        requires_external_service=False,
        requires_database=False,
        requires_native_build=False,
        deterministic_tests=True,
        estimated_runtime=runtime,
        fixture_files=fixture_files,
        fixture_size=fixture_size,
        edit_rounds=2,
        cross_module_reasoning=cross_module,
        requires_test_feedback=True,
        misleading_symptom=True,
        visible_tests="Retain one upstream regression behavior without exposing the repaired line.",
        hidden_tests=hidden_tests,
        anti_hardcoding=anti_hardcoding,
        windows_feasibility=windows_feasibility,
        offline_feasibility="HIGH after dependencies are pre-provisioned and network-oriented tests are excluded.",
        adaptation_actions=adaptation_actions,
        adaptation_risks=adaptation_risks,
        provenance_confidence="HIGH" if license_verified else "MEDIUM",
        difficulty=difficulty,
        difficulty_rationale=difficulty_rationale,
        audit_status=audit_status,
        exclusion_reasons=exclusion_reasons,
        scores=scores,
        verified_fields=verified_fields,
        unverified_fields=unverified_fields,
    )


def _swe_candidate(
    *,
    candidate_id: str,
    project_repo: str,
    instance_id: str,
    title: str,
    issue_url: str,
    pull_url: str,
    base_commit: str,
    fixed_commit: str,
    license_name: str,
    license_url: str,
    attribution: str,
    summary: str,
    failure: str,
    expected: str,
    affected_files: list[str],
    patch_stats: tuple[int, int, int],
    original_tests: list[str],
    dependencies: list[str],
    python_constraints: str,
    os_constraints: list[str],
    cross_module: str,
    hidden_tests: str,
    anti_hardcoding: str,
    windows_feasibility: str,
    adaptation_actions: list[str],
    adaptation_risks: list[str],
    difficulty: str,
    difficulty_rationale: str,
    audit_status: str,
    scores: Sequence[int],
    runtime: int,
    fixture_files: int,
    fixture_size: int,
) -> dict[str, Any]:
    source_urls = [
        SWE_DATASET_URL,
        issue_url,
        pull_url,
        f"https://github.com/{project_repo}/commit/{fixed_commit}",
        license_url,
    ]
    return _candidate(
        candidate_id=candidate_id,
        source_kind="SWE_BENCH",
        source_dataset="SWE-bench Verified",
        source_project=project_repo,
        source_bug_id=instance_id,
        source_issue_title=title,
        source_urls=source_urls,
        issue_url=issue_url,
        buggy_commit=base_commit,
        fixed_commit=fixed_commit,
        license_name=license_name,
        license_verified=True,
        attribution_requirements=attribution,
        summary=summary,
        observed_failure=failure,
        expected_behavior=expected,
        affected_files=affected_files,
        patch_stats=patch_stats,
        original_tests=original_tests,
        dependencies=dependencies,
        python_constraints=python_constraints,
        os_constraints=os_constraints,
        requires_network=False,
        requires_external_service=False,
        requires_database=False,
        requires_native_build=False,
        deterministic_tests=True,
        estimated_runtime=runtime,
        fixture_files=fixture_files,
        fixture_size=fixture_size,
        edit_rounds=2,
        cross_module_reasoning=cross_module,
        requires_test_feedback=True,
        misleading_symptom=True,
        visible_tests="Adapt the official FAIL_TO_PASS regression into one visible behavior test.",
        hidden_tests=hidden_tests,
        anti_hardcoding=anti_hardcoding,
        windows_feasibility=windows_feasibility,
        offline_feasibility="CONDITIONAL: official SWE-bench uses Docker; the proposed crop must prove a local fixed TestProfile.",
        adaptation_actions=adaptation_actions,
        adaptation_risks=adaptation_risks,
        provenance_confidence="HIGH",
        difficulty=difficulty,
        difficulty_rationale=difficulty_rationale,
        audit_status=audit_status,
        exclusion_reasons=[],
        scores=scores,
    )


def _self_candidate(
    *,
    candidate_id: str,
    title: str,
    summary: str,
    failure: str,
    expected: str,
    affected_files: list[str],
    cross_module: str,
    visible_tests: str,
    hidden_tests: str,
    anti_hardcoding: str,
    risks: list[str],
    difficulty: str,
    difficulty_rationale: str,
    scores: Sequence[int],
    runtime: int,
    fixture_files: int,
    fixture_size: int,
    edit_rounds: int,
) -> dict[str, Any]:
    return _candidate(
        candidate_id=candidate_id,
        source_kind="SELF_BUILT",
        source_dataset="AgentForge original candidate concepts",
        source_project="AgentForge M7-B",
        source_bug_id=candidate_id,
        source_issue_title=title,
        source_urls=[],
        issue_url=None,
        buggy_commit=None,
        fixed_commit=None,
        license_name="N/A - original design concept; code not yet authored",
        license_verified=True,
        attribution_requirements="No external attribution; future fixture must remain original and separately reviewed.",
        summary=summary,
        observed_failure=failure,
        expected_behavior=expected,
        affected_files=affected_files,
        patch_stats=(0, 0, 0),
        original_tests=[],
        dependencies=["Python standard library", "pytest supplied by the fixed TestProfile"],
        python_constraints="Proposed fixture targets Python >=3.11.",
        os_constraints=["Must pass on Windows and POSIX without symlink privileges."],
        requires_network=False,
        requires_external_service=False,
        requires_database=False,
        requires_native_build=False,
        deterministic_tests=True,
        estimated_runtime=runtime,
        fixture_files=fixture_files,
        fixture_size=fixture_size,
        edit_rounds=edit_rounds,
        cross_module_reasoning=cross_module,
        requires_test_feedback=True,
        misleading_symptom=True,
        visible_tests=visible_tests,
        hidden_tests=hidden_tests,
        anti_hardcoding=anti_hardcoding,
        windows_feasibility="HIGH by design, but UNVERIFIED until M7-B2 creates and runs the fixture.",
        offline_feasibility="HIGH by design; no network, service, database, or installation is planned.",
        adaptation_actions=[
            "Author a new small multi-module package without copying AgentForge implementation or tests.",
            "Inject exactly one defect and preserve a private reference fix outside the model workspace.",
            "Bind visible and hidden tests to immutable TestProfiles.",
        ],
        adaptation_risks=risks,
        provenance_confidence="MEDIUM",
        difficulty=difficulty,
        difficulty_rationale=difficulty_rationale,
        audit_status="ELIGIBLE",
        exclusion_reasons=[],
        scores=scores,
        verified_fields=SELF_VERIFIED_FIELDS,
        unverified_fields={
            "original_tests",
            "patch_file_count",
            "patch_added_lines",
            "patch_deleted_lines",
        },
    )


def _candidates() -> list[dict[str, Any]]:
    candidates = [
        _quix_candidate(
            name="shortest_path_length",
            title="Dijkstra relaxation uses the queued distance instead of the current path distance",
            summary="The relaxation expression adds an edge weight to the successor's queued distance instead of the popped node distance.",
            failure="A graph with competing routes returns the wrong shortest-path length.",
            expected="Every relaxation uses the current popped distance and returns the minimum reachable path length.",
            tests="python_testcases/test_shortest_path_length.py",
            cross_module="The adapted task should separate graph nodes, heap helpers, and routing logic so the symptom must be traced across data and algorithm modules.",
            hidden_tests="Use disconnected graphs, stale heap entries, alternate route ordering, and equal-cost paths.",
            risks=[
                "The upstream defect is one line and can become too obvious unless the fixture separates graph construction from relaxation.",
                "The upstream helper mutates heap entries without re-heapifying; adaptation must avoid introducing a second defect.",
            ],
            difficulty="BASIC",
            difficulty_rationale="The root fix is local, but graph state and hidden alternate routes require disciplined reasoning.",
            audit_status="ELIGIBLE",
            exclusion_reasons=[],
            scores=(19, 19, 10, 14, 8, 7, 3, 5),
        ),
        _quix_candidate(
            name="topological_ordering",
            title="Topological readiness checks successors instead of prerequisites",
            summary="A node is appended when prior output covers its outgoing nodes rather than its incoming dependencies.",
            failure="Valid DAGs can omit or misorder nodes whose prerequisites differ from their successors.",
            expected="A node becomes ready only after all incoming dependencies have been emitted.",
            tests="python_testcases/test_topological_ordering.py",
            cross_module="The proposed adaptation separates node construction, dependency indexing, and ordering to require contract-level reasoning.",
            hidden_tests="Vary input order, use multiple roots, converging dependencies, isolated nodes, and assert edge-order properties.",
            risks=[
                "Both recommended QuixBugs candidates are graph-oriented, reducing domain diversity.",
                "A visible test that names incoming versus outgoing dependencies would reveal the repair directly.",
            ],
            difficulty="BASIC",
            difficulty_rationale="The correction is small, while hidden DAG shapes prevent an example-specific patch.",
            audit_status="ELIGIBLE",
            exclusion_reasons=[],
            scores=(19, 19, 10, 14, 8, 6, 3, 5),
        ),
        _quix_candidate(
            name="flatten",
            title="Flatten yields recursive generator objects for scalar values",
            summary="The scalar branch yields flatten(x) instead of yielding x.",
            failure="Nested input produces generator objects in place of leaf values.",
            expected="The generator yields every non-list leaf in depth-first order.",
            tests="python_testcases/test_flatten.py",
            cross_module="A useful adaptation would add a traversal API and iterator wrapper, but that may exceed the original semantics.",
            hidden_tests="Use empty lists, mixed depth, repeated values, non-list iterables, and lazy-consumption checks.",
            risks=[
                "The raw defect is too close to a toy exercise.",
                "Adding modules to increase realism may change the original bug rather than faithfully adapt it.",
            ],
            difficulty="BASIC",
            difficulty_rationale="The upstream correction is obvious after one failing example; only careful adaptation could add evaluation value.",
            audit_status="CONDITIONAL",
            exclusion_reasons=[],
            scores=(19, 20, 6, 12, 9, 5, 2, 5),
        ),
        _quix_candidate(
            name="bitcount",
            title="Bit count toggles bits and may not terminate",
            summary="The loop uses XOR with n-1 rather than clearing the lowest set bit with AND.",
            failure="Some positive inputs enter a non-terminating sequence and require an external timeout.",
            expected="The loop terminates and returns the number of set bits for every nonnegative integer.",
            tests="python_testcases/test_bitcount.py",
            cross_module="There is little credible multi-module reasoning to preserve for this single arithmetic loop.",
            hidden_tests="Cover zero, powers of two, dense bit patterns, and large integers under a strict timeout.",
            risks=[
                "The official README explicitly recommends a timeout plugin for this defect.",
                "A non-terminating visible test is a poor fit for a small deterministic repair fixture.",
                "The task remains an algorithmic toy after reasonable cropping.",
            ],
            difficulty="BASIC",
            difficulty_rationale="The known bit-clearing idiom makes the fix trivial once localized.",
            audit_status="REJECTED",
            exclusion_reasons=[
                "Buggy execution may not terminate and depends on timeout containment.",
                "Insufficient engineering realism for the target task set.",
            ],
            scores=(8, 12, 5, 10, 6, 4, 2, 5),
            runtime=5,
        ),
        _bugsinpy_candidate(
            candidate_id="bugsinpy-black-21",
            project_path="black",
            project_repo="psf/black",
            bug_id="21",
            title="Open temporary files with UTF-8 encoding",
            issue_url="https://github.com/psf/black/pull/126",
            buggy_commit="c071af761e1550c6e4ebab8e5af747d2d8fdd48e",
            fixed_commit="8e7848c63efe36f09e4651bece8c0efc34a1c3e1",
            license_name="MIT",
            license_verified=True,
            license_url="https://github.com/psf/black/blob/c071af761e1550c6e4ebab8e5af747d2d8fdd48e/LICENSE",
            attribution="Retain the Black MIT copyright and permission notice.",
            summary="A diagnostic temporary file relies on the platform default text encoding.",
            failure="On Windows locales whose default encoding cannot represent the output, writing or reading the diagnostic file fails.",
            expected="The diagnostic file is consistently written and read as UTF-8.",
            affected_files=["black.py"],
            patch_stats=(1, 1, 1),
            original_tests=["tests/test_black.py::BlackTestCase::test_expression_ff"],
            dependencies=["Python standard library", "pytest supplied by the fixed TestProfile"],
            python_constraints="Upstream BugsInPy metadata pins Python 3.8.3; the proposed crop targets Python >=3.11.",
            os_constraints=["The original regression specifically concerns Windows default encodings."],
            cross_module="A cropped fixture can separate diagnostic serialization, temporary-path ownership, and a formatter caller.",
            hidden_tests="Use non-ASCII source text, mocked non-UTF default encodings, and reopen the result explicitly.",
            anti_hardcoding="Vary Unicode scripts and assert round-trip content rather than a fixed filename.",
            windows_feasibility="HIGH; the bug is Windows-relevant, but the adapted fixture has not been executed.",
            adaptation_actions=[
                "Extract only the diagnostic-file path from the old formatter, not Black's full dependency tree.",
                "Preserve the Windows encoding failure with controlled locale-independent test doubles.",
            ],
            adaptation_risks=[
                "Cropping to a file helper may reduce formatter realism.",
                "A visible test that asserts the literal encoding keyword would leak the fix.",
            ],
            difficulty="BASIC",
            difficulty_rationale="The change is local, but the symptom appears downstream during diagnostic formatting on selected locales.",
            audit_status="ELIGIBLE",
            exclusion_reasons=[],
            scores=(18, 19, 10, 13, 8, 7, 4, 5),
            runtime=3,
            fixture_files=7,
            fixture_size=35000,
        ),
        _bugsinpy_candidate(
            candidate_id="bugsinpy-pysnooper-3",
            project_path="PySnooper",
            project_repo="cool-RR/PySnooper",
            bug_id="3",
            title="File output opens an undefined normalized path",
            issue_url="https://github.com/cool-RR/PySnooper/issues/2",
            buggy_commit="6e3d797be3fa0a746fb5b1b7c7fea78eb926c208",
            fixed_commit="15555ed760000b049aff8fecc79d29339c1224c3",
            license_name="MIT",
            license_verified=True,
            license_url="https://github.com/cool-RR/PySnooper/blob/6e3d797be3fa0a746fb5b1b7c7fea78eb926c208/LICENSE",
            attribution="Retain the PySnooper MIT copyright and permission notice.",
            summary="The path-like output branch opens output_path even though only output is defined in the closure.",
            failure="Tracing to a filesystem path raises a name error instead of appending trace lines.",
            expected="String and path-like destinations append trace output to the requested file.",
            affected_files=["pysnooper/pysnooper.py"],
            patch_stats=(1, 1, 1),
            original_tests=["tests/test_pysnooper.py::test_file_output"],
            dependencies=["Python standard library", "pytest supplied by the fixed TestProfile"],
            python_constraints="Upstream BugsInPy metadata pins Python 3.8.1; the proposed crop targets Python >=3.11.",
            os_constraints=["No upstream OS restriction for the focused path-output behavior."],
            cross_module="The adaptation should route a decorator, writer factory, and path policy through separate modules.",
            hidden_tests="Exercise pathlib paths, strings, repeated appends, nested calls, and Unicode output.",
            anti_hardcoding="Generate multiple temporary destination names and compare semantic trace fragments.",
            windows_feasibility="HIGH for ordinary temporary files; no symlink behavior is required.",
            adaptation_actions=[
                "Retain only a small tracer/writer boundary and its attribution.",
                "Replace python_toolbox test helpers with standard-library temporary directories.",
            ],
            adaptation_risks=[
                "The original fix is a one-token variable change and may be too easy if the writer factory is directly visible.",
                "The crop must avoid copying unrelated tracer complexity solely to inflate difficulty.",
            ],
            difficulty="BASIC",
            difficulty_rationale="The user symptom is clear, while locating the wrong closure variable remains a focused debugging task.",
            audit_status="ELIGIBLE",
            exclusion_reasons=[],
            scores=(18, 19, 9, 12, 9, 7, 3, 5),
            runtime=3,
            fixture_files=6,
            fixture_size=30000,
        ),
        _bugsinpy_candidate(
            candidate_id="bugsinpy-httpie-4",
            project_path="httpie",
            project_repo="httpie/cli",
            bug_id="4",
            title="Preserve a caller-supplied Host header",
            issue_url=None,
            buggy_commit="8c892edd4fe700a7ca5cc733dcb4817831d253e2",
            fixed_commit="040d981f00c3f6830b2d0db3daf3c64c080e96e3",
            license_name="BSD-3-Clause",
            license_verified=True,
            license_url="https://github.com/httpie/cli/blob/8c892edd4fe700a7ca5cc733dcb4817831d253e2/LICENSE",
            attribution="Retain the HTTPie BSD copyright, conditions, disclaimer, and non-endorsement term.",
            summary="Converting case-insensitive request headers to a plain dict before membership testing loses header semantics.",
            failure="A custom Host header can be treated as absent and replaced by a value derived from the URL.",
            expected="Header membership remains case-insensitive and a custom Host value is preserved.",
            affected_files=["httpie/models.py"],
            patch_stats=(1, 1, 1),
            original_tests=["tests/test_regressions.py::test_Host_header_overwrite"],
            dependencies=["requests-compatible case-insensitive header mapping", "pytest supplied by the fixed TestProfile"],
            python_constraints="Upstream BugsInPy metadata pins Python 3.7.3; the proposed crop targets Python >=3.11.",
            os_constraints=["No focused-test OS restriction."],
            cross_module="A useful crop separates request preparation, header storage, and textual rendering.",
            hidden_tests="Vary Host casing, duplicate logical keys, userinfo in URLs, ports, and unrelated headers without network calls.",
            anti_hardcoding="Use several case permutations and assert preservation of the supplied value.",
            windows_feasibility="HIGH because the focused test constructs requests locally and performs no socket I/O.",
            adaptation_actions=[
                "Keep only local request/header rendering code and remove httpbin/network fixtures.",
                "Pre-provision the exact header-mapping dependency in an immutable TestProfile.",
            ],
            adaptation_risks=[
                "The original test environment has a large, old HTTP dependency set.",
                "Replacing the header container during cropping could accidentally remove the original semantic distinction.",
            ],
            difficulty="BASIC",
            difficulty_rationale="The patch is one line, but the failure requires recognizing a semantic change caused by copying a mapping.",
            audit_status="ELIGIBLE",
            exclusion_reasons=[],
            scores=(18, 18, 11, 12, 8, 7, 4, 5),
            runtime=4,
            fixture_files=8,
            fixture_size=50000,
        ),
        _bugsinpy_candidate(
            candidate_id="bugsinpy-cookiecutter-2",
            project_path="cookiecutter",
            project_repo="cookiecutter/cookiecutter",
            bug_id="2",
            title="Generated projects can run multiple hooks of the same type",
            issue_url="https://github.com/cookiecutter/cookiecutter/pull/974",
            buggy_commit="d7e7b28811e474e14d1bed747115e47dcdd15ba3",
            fixed_commit="90434ff4ea4477941444f1e83313beb414838535",
            license_name="BSD-3-Clause",
            license_verified=True,
            license_url="https://github.com/cookiecutter/cookiecutter/blob/d7e7b28811e474e14d1bed747115e47dcdd15ba3/LICENSE",
            attribution="Retain the Cookiecutter BSD copyright, conditions, disclaimer, and non-endorsement term.",
            summary="Hook discovery returns immediately after the first matching script, so projects cannot run both Python and shell hooks for one lifecycle stage.",
            failure="Only one valid pre- or post-generation hook executes when multiple hook implementations are present.",
            expected="Discovery returns every valid matching hook and execution processes each in a deterministic order.",
            affected_files=["cookiecutter/hooks.py"],
            patch_stats=(1, 9, 5),
            original_tests=[
                "tests/test_hooks.py::TestFindHooks::test_find_hook",
                "tests/test_hooks.py::TestExternalHooks::test_run_hook",
            ],
            dependencies=["Jinja2", "pytest supplied by the fixed TestProfile"],
            python_constraints="Upstream BugsInPy metadata pins Python 3.6.9; the proposed crop targets Python >=3.11.",
            os_constraints=["Original hook execution selects batch or shell scripts by platform."],
            cross_module="The behavior spans discovery, validity filtering, ordering, context rendering, and execution dispatch.",
            hidden_tests="Use mixed extensions, invalid hooks, no-hook cases, deterministic ordering, and mocked execution dispatch.",
            anti_hardcoding="Generate hook sets in different directory orders and assert the accepted set plus stable execution ordering.",
            windows_feasibility="MEDIUM because shell/batch semantics must be replaced by a platform-neutral fake dispatcher.",
            adaptation_actions=[
                "Replace real hook subprocess execution with a local recording dispatcher.",
                "Define deterministic ordering explicitly instead of inheriting os.listdir ordering.",
                "Retain only hook discovery/rendering modules and their BSD attribution.",
            ],
            adaptation_risks=[
                "Removing actual script execution may alter the issue's integration semantics.",
                "The upstream patch does not define ordering, which hidden tests must not invent without documentation.",
                "The original tests use tox and platform-specific executable scripts.",
            ],
            difficulty="ENGINEERING",
            difficulty_rationale="The fix changes a return contract and all consumers, but adaptation correctness is unresolved.",
            audit_status="CONDITIONAL",
            exclusion_reasons=[],
            scores=(17, 15, 13, 13, 5, 8, 4, 5),
            runtime=8,
            fixture_files=12,
            fixture_size=90000,
        ),
        _bugsinpy_candidate(
            candidate_id="bugsinpy-fastapi-3",
            project_path="fastapi",
            project_repo="fastapi/fastapi",
            bug_id="3",
            title="Apply exclude_unset and aliases recursively during response validation",
            issue_url="https://github.com/fastapi/fastapi/pull/1074",
            buggy_commit="869c7389e22dc9ad659940fa271da76c4f3ba3b1",
            fixed_commit="aea04ee32ee1942e6e1a904527bb8da6ba76abd9",
            license_name="MIT",
            license_verified=True,
            license_url="https://github.com/fastapi/fastapi/blob/869c7389e22dc9ad659940fa271da76c4f3ba3b1/LICENSE",
            attribution="Retain the FastAPI MIT copyright and permission notice.",
            summary="Response preprocessing only converts a top-level model, so nested models in lists and dictionaries ignore aliases or exclude-unset behavior.",
            failure="Nested response models can expose unset fields or validate against the wrong field names.",
            expected="Response content is recursively prepared before validation for model, list, and dictionary shapes.",
            affected_files=["fastapi/routing.py"],
            patch_stats=(1, 23, 7),
            original_tests=[
                "tests/test_serialize_response_model.py::test_validlist_exclude_unset",
                "tests/test_serialize_response_model.py::test_validdict_exclude_unset",
            ],
            dependencies=["pydantic==1.5.1", "starlette==0.13.2", "pytest supplied by the fixed TestProfile"],
            python_constraints="Upstream BugsInPy metadata pins Python 3.8.3 and Pydantic 1; current AgentForge uses Pydantic 2.",
            os_constraints=["No focused-test OS restriction, but the historical dependency set is not current-Python compatible."],
            cross_module="Reasoning crosses response routing, model serialization, alias handling, nested containers, and validation.",
            hidden_tests="Cover nested lists/dicts, aliases, unset/default fields, mixed scalar leaves, and immutable input behavior.",
            anti_hardcoding="Generate multiple nesting shapes and compare normalized semantic output.",
            windows_feasibility="LOW until a Pydantic-1-compatible prebuilt environment or faithful standalone model shim is approved.",
            adaptation_actions=[
                "Decide whether to prebuild an isolated historical dependency environment or design a faithful small model protocol.",
                "Exclude FastAPI networking and server code while preserving recursive response semantics.",
            ],
            adaptation_risks=[
                "Pydantic 1 versus 2 behavior can change the bug materially.",
                "The original dependency snapshot includes packages that may not install on Python 3.14.",
                "A hand-written model shim may turn a real FastAPI defect into an unrelated toy.",
            ],
            difficulty="ENGINEERING",
            difficulty_rationale="Strong cross-module semantics, but environment adaptation dominates the repair itself.",
            audit_status="CONDITIONAL",
            exclusion_reasons=[],
            scores=(16, 10, 15, 14, 3, 9, 5, 5),
            runtime=12,
            fixture_files=15,
            fixture_size=120000,
        ),
        _bugsinpy_candidate(
            candidate_id="bugsinpy-tqdm-8",
            project_path="tqdm",
            project_repo="tqdm/tqdm",
            bug_id="8",
            title="Custom bar_format uses the parsed user sides",
            issue_url=None,
            buggy_commit="08b8ad1ff3bd003ef8309faaa0cc108ffa40317d",
            fixed_commit="cae9d139c6df5614be3bf6e25ccbd600ee3286dc",
            license_name="Historical LICENCE text; SPDX NOASSERTION",
            license_verified=False,
            license_url="https://github.com/tqdm/tqdm/blob/08b8ad1ff3bd003ef8309faaa0cc108ffa40317d/LICENCE",
            attribution="Unresolved: historical dual-license text requires manual review before redistribution.",
            summary="The custom progress-bar branch parses user left/right fragments but formats stale local fragments instead.",
            failure="A custom bar format can render the wrong text or width around the dynamic bar segment.",
            expected="The exact user-provided left and right fragments are formatted before dynamic width calculation.",
            affected_files=["tqdm/_tqdm.py"],
            patch_stats=(1, 1, 1),
            original_tests=["tqdm/tests/tests_tqdm.py::test_format_meter"],
            dependencies=["pytest supplied by the fixed TestProfile"],
            python_constraints="Upstream BugsInPy metadata pins Python 3.6.9; a proposed crop would target Python >=3.11.",
            os_constraints=["Terminal width behavior can vary by platform unless width is fixed."],
            cross_module="The focused behavior spans format parsing, width calculation, and rendering, but remains in one function.",
            hidden_tests="Vary widths, Unicode cells, missing bar placeholders, prefixes, totals, and rates.",
            anti_hardcoding="Use property-style width and formatting assertions over several templates.",
            windows_feasibility="MEDIUM because deterministic terminal width and Unicode cell semantics must be isolated.",
            adaptation_actions=[
                "Complete a manual historical-license determination before any source redistribution.",
                "Fix display width inputs and remove terminal probing from tests.",
            ],
            adaptation_risks=[
                "GitHub license detection reports NOASSERTION for the pinned historical file.",
                "The bug is a one-line stale-variable defect with limited engineering depth.",
            ],
            difficulty="BASIC",
            difficulty_rationale="Formatting edge cases are useful, but the source repair is a direct variable substitution.",
            audit_status="REJECTED",
            exclusion_reasons=["License status is not sufficiently verified for fixture redistribution."],
            scores=(15, 14, 8, 12, 6, 5, 3, 1),
            runtime=5,
            fixture_files=7,
            fixture_size=45000,
            unverified_fields={"license", "license_verified", "attribution_requirements"},
        ),
        _swe_candidate(
            candidate_id="swebench-flask-5014",
            project_repo="pallets/flask",
            instance_id="pallets__flask-5014",
            title="Require a non-empty Blueprint name",
            issue_url="https://github.com/pallets/flask/issues/5010",
            pull_url="https://github.com/pallets/flask/pull/5014",
            base_commit="7ee9ceb71e868944a46e1ff00b506772a53a4f1d",
            fixed_commit="7ed89d3f9d2207c9a607f5dcdce106c0278e1332",
            license_name="BSD-3-Clause",
            license_url="https://github.com/pallets/flask/blob/7ee9ceb71e868944a46e1ff00b506772a53a4f1d/LICENSE.rst",
            attribution="Retain the Flask BSD copyright, conditions, disclaimer, and non-endorsement term.",
            summary="Blueprint accepts an empty name even though registration and endpoint naming assume a non-empty identifier.",
            failure="An empty Blueprint name creates confusing downstream registration behavior instead of failing at construction.",
            expected="Blueprint construction rejects an empty name with a stable ValueError.",
            affected_files=["src/flask/blueprints.py"],
            patch_stats=(1, 3, 0),
            original_tests=["tests/test_blueprints.py::test_empty_name_not_allowed"],
            dependencies=["Flask 2.3 dependency subset pre-provisioned by a fixed TestProfile"],
            python_constraints="SWE-bench project version is Flask 2.3; proposed crop targets Python >=3.11.",
            os_constraints=["No focused-test OS restriction."],
            cross_module="The constructor invariant protects registration, endpoint prefixing, and nested blueprint behavior.",
            hidden_tests="Cover empty versus whitespace names, non-string values, duplicate registration, nesting, and valid Unicode names.",
            anti_hardcoding="Assert the invariant across constructor and registration paths without requiring a literal error message.",
            windows_feasibility="HIGH for a constructor-only local test; formal fixture execution remains pending.",
            adaptation_actions=[
                "Crop the Blueprint constructor and minimal registration collaborators under BSD attribution.",
                "Pre-provision dependencies and remove documentation/server/network test paths.",
            ],
            adaptation_risks=[
                "The direct validation fix may be too obvious if the visible test calls only the constructor.",
                "Cropping must preserve registration consumers so the invariant remains an engineering behavior.",
            ],
            difficulty="BASIC",
            difficulty_rationale="The change is small and deterministic, with enough lifecycle context for hidden invariant tests.",
            audit_status="ELIGIBLE",
            scores=(20, 19, 11, 14, 9, 7, 4, 5),
            runtime=5,
            fixture_files=10,
            fixture_size=80000,
        ),
        _swe_candidate(
            candidate_id="swebench-pytest-10051",
            project_repo="pytest-dev/pytest",
            instance_id="pytest-dev__pytest-10051",
            title="caplog.get_records remains valid after caplog.clear",
            issue_url="https://github.com/pytest-dev/pytest/issues/9877",
            pull_url="https://github.com/pytest-dev/pytest/pull/10051",
            base_commit="aa55975c7d3f6c9f6d7f68accc41bb7cadf0eb9a",
            fixed_commit="966d4fb3e4640de721f87e4190427975ea020c67",
            license_name="MIT",
            license_url="https://github.com/pytest-dev/pytest/blob/aa55975c7d3f6c9f6d7f68accc41bb7cadf0eb9a/LICENSE",
            attribution="Retain the pytest MIT copyright and permission notice.",
            summary="Clearing captured logs replaces the records list and breaks the reference stored for the current setup/call/teardown stage.",
            failure="After caplog.clear, get_records for the current stage returns stale data and misses newly captured records.",
            expected="Clear removes current records while preserving stage binding so later records remain observable.",
            affected_files=["src/_pytest/logging.py"],
            patch_stats=(1, 5, 2),
            original_tests=["testing/logging/test_fixture.py::test_clear_for_call_stage"],
            dependencies=["Python logging", "pytest test helpers pre-provisioned by a fixed TestProfile"],
            python_constraints="SWE-bench project version is pytest 7.2; proposed crop targets Python >=3.11.",
            os_constraints=["No focused-test OS restriction."],
            cross_module="The defect crosses a handler, fixture facade, per-item stash, and phase lifecycle ownership.",
            hidden_tests="Clear during setup/call/teardown, clear twice, capture after clear, and ensure phases remain isolated.",
            anti_hardcoding="Drive several lifecycle phase sequences and assert record identity plus content invariants.",
            windows_feasibility="HIGH because the focused logging lifecycle is platform-neutral.",
            adaptation_actions=[
                "Extract a small phase-aware capture package rather than pytest's full plugin framework.",
                "Preserve reference identity and stage transitions in visible and hidden tests.",
            ],
            adaptation_risks=[
                "An over-aggressive crop can remove the aliasing relationship that causes the real defect.",
                "Copying pytest's full test harness would make the fixture too large and dependency-heavy.",
            ],
            difficulty="ENGINEERING",
            difficulty_rationale="The repair is compact, but correct diagnosis requires lifecycle and aliasing reasoning across modules.",
            audit_status="ELIGIBLE",
            scores=(20, 18, 14, 15, 7, 8, 5, 5),
            runtime=8,
            fixture_files=12,
            fixture_size=100000,
        ),
        _swe_candidate(
            candidate_id="swebench-requests-5414",
            project_repo="psf/requests",
            instance_id="psf__requests-5414",
            title="Raise InvalidURL for a host beginning with a dot",
            issue_url="https://github.com/psf/requests/issues/5367",
            pull_url="https://github.com/psf/requests/pull/5414",
            base_commit="39d0fdd9096f7dceccbc8f82e1eda7dd64717a8e",
            fixed_commit="d09659997cd1e3eca49a07c59ece5557071c0ab9",
            license_name="Apache-2.0",
            license_url="https://github.com/psf/requests/blob/39d0fdd9096f7dceccbc8f82e1eda7dd64717a8e/LICENSE",
            attribution="Include Apache-2.0 license text, preserve notices, and mark modified files.",
            summary="URL preparation passes a leading-dot hostname to IDNA encoding, which raises a lower-level Unicode error.",
            failure="Preparing a URL such as a leading-dot host raises UnicodeError rather than Requests InvalidURL.",
            expected="Malformed leading-dot hosts fail locally with the public InvalidURL exception.",
            affected_files=["requests/models.py"],
            patch_stats=(1, 1, 1),
            original_tests=["tests/test_requests.py::TestRequests::test_invalid_url[InvalidURL-http://.example.com]"],
            dependencies=["idna/urllib3 compatibility subset pre-provisioned by a fixed TestProfile"],
            python_constraints="SWE-bench project version is Requests 2.26; proposed crop targets Python >=3.11.",
            os_constraints=["No focused-test OS restriction."],
            cross_module="The path spans URL parsing, host validation, IDNA normalization, and public exception translation.",
            hidden_tests="Cover leading/trailing dots, empty labels, Unicode domains, valid subdomains, ports, and no socket calls.",
            anti_hardcoding="Use a table of valid and invalid host forms and assert exception taxonomy.",
            windows_feasibility="HIGH if the test stops before transport and uses fixed IDNA dependencies.",
            adaptation_actions=[
                "Crop URL preparation and exception types without transport adapters.",
                "Lock compatible IDNA behavior in the TestProfile and prove no network calls occur.",
            ],
            adaptation_risks=[
                "Current IDNA behavior may differ from the historical Requests stack.",
                "The issue is largely input validation and may overlap Flask's constructor-invariant task.",
            ],
            difficulty="BASIC",
            difficulty_rationale="The exception boundary is useful but the source fix is a narrow validation guard.",
            audit_status="CONDITIONAL",
            scores=(20, 18, 10, 14, 8, 6, 4, 5),
            runtime=5,
            fixture_files=10,
            fixture_size=80000,
        ),
        _swe_candidate(
            candidate_id="swebench-pylint-6386",
            project_repo="pylint-dev/pylint",
            instance_id="pylint-dev__pylint-6386",
            title="Short verbose option must not require an argument",
            issue_url="https://github.com/pylint-dev/pylint/issues/6385",
            pull_url="https://github.com/pylint-dev/pylint/pull/6386",
            base_commit="754b487f4d892e3d4872b6fc7468a71db4e31c13",
            fixed_commit="b8d3e47a207eb2376b753ce5fd28a17a0a0ebec1",
            license_name="GPL-2.0",
            license_url="https://github.com/pylint-dev/pylint/blob/754b487f4d892e3d4872b6fc7468a71db4e31c13/LICENSE",
            attribution="A redistributed derivative fixture would need GPL-2.0 source and notice compliance; legal review remains advisable.",
            summary="The preprocessing table and help metadata disagree about whether -v/--verbose consumes a value.",
            failure="The short verbose flag expects an argument and help renders a misleading metavar.",
            expected="Both verbose forms act as flags and help shows no argument placeholder.",
            affected_files=[
                "pylint/config/argument.py",
                "pylint/config/arguments_manager.py",
                "pylint/config/utils.py",
                "pylint/lint/base_options.py",
            ],
            patch_stats=(4, 14, 1),
            original_tests=["tests/config/test_config.py::test_short_verbose"],
            dependencies=["argparse", "astroid/Pylint configuration subset in a fixed TestProfile"],
            python_constraints="SWE-bench project version is Pylint 2.14; proposed crop targets Python >=3.11.",
            os_constraints=["No focused-test OS restriction."],
            cross_module="Option definitions, early preprocessing, callback metadata, and help rendering must agree.",
            hidden_tests="Exercise short/long forms, combined arguments, help output, missing values, and neighboring options.",
            anti_hardcoding="Build several option definitions and verify parse/help consistency rather than one -v string.",
            windows_feasibility="MEDIUM until the Pylint dependency subset and GPL redistribution approach are approved.",
            adaptation_actions=[
                "Design a small GPL-compliant configuration subset or reject redistribution after review.",
                "Remove unrelated lint checkers while preserving option preprocessing and help generation.",
            ],
            adaptation_risks=[
                "GPL obligations are stronger than the permissive-license candidates.",
                "The four-file upstream patch may lose semantics if reduced to a custom parser toy.",
                "Historical Pylint dependencies require a compatibility trial.",
            ],
            difficulty="ENGINEERING",
            difficulty_rationale="Good multi-module contract bug, but license and environment costs prevent primary recommendation.",
            audit_status="CONDITIONAL",
            scores=(18, 12, 14, 13, 4, 9, 4, 4),
            runtime=12,
            fixture_files=18,
            fixture_size=160000,
        ),
        _swe_candidate(
            candidate_id="swebench-pytest-5840",
            project_repo="pytest-dev/pytest",
            instance_id="pytest-dev__pytest-5840",
            title="Resolve conftest paths consistently across Windows path casing",
            issue_url="https://github.com/pytest-dev/pytest/issues/5819",
            pull_url="https://github.com/pytest-dev/pytest/pull/5840",
            base_commit="73c5b7f4b11a81e971f7d1bb18072e06a87060f4",
            fixed_commit="9422e10322562b11cf626e2adde015f715fc429a",
            license_name="MIT",
            license_url="https://github.com/pytest-dev/pytest/blob/73c5b7f4b11a81e971f7d1bb18072e06a87060f4/LICENSE",
            attribution="Retain the pytest MIT copyright and permission notice.",
            summary="Mixed path representations on case-insensitive filesystems allow the same conftest to be considered under different keys.",
            failure="On Windows, loading conftest from differently cased paths can raise import errors or duplicate module handling.",
            expected="Resolved path identity is stable across drive-letter and directory casing differences.",
            affected_files=["src/_pytest/config/__init__.py", "src/_pytest/pathlib.py"],
            patch_stats=(2, 8, 19),
            original_tests=[
                "testing/test_conftest.py::test_setinitial_conftest_subdirs[test]",
                "testing/test_conftest.py::test_setinitial_conftest_subdirs[tests]",
            ],
            dependencies=["pytest 5.1 path/configuration subset in a fixed TestProfile"],
            python_constraints="SWE-bench project version is pytest 5.1; the historical suite predates current Python.",
            os_constraints=["The regression is Windows and case-insensitive-filesystem specific."],
            cross_module="Path canonicalization, conftest discovery, module identity, and cache keys interact.",
            hidden_tests="Vary drive-letter case, directory case, relative segments, duplicate discovery roots, and cache identity.",
            anti_hardcoding="Create temporary roots with generated casing permutations and assert one logical import.",
            windows_feasibility="MEDIUM: directly relevant, but old pytest internals and filesystem behavior need a dedicated trial.",
            adaptation_actions=[
                "Recreate the path/cache contract in a small package without depending on pytest 5.1 running on Python 3.14.",
                "Prove the adaptation on a real case-insensitive Windows filesystem.",
            ],
            adaptation_risks=[
                "A synthetic path normalizer may not preserve the original conftest import semantics.",
                "The upstream PR also reverted an earlier approach, complicating reference-fix extraction.",
                "The behavior is difficult to validate on case-sensitive filesystems.",
            ],
            difficulty="ENGINEERING",
            difficulty_rationale="Strong Windows engineering value, offset by substantial historical-runtime adaptation risk.",
            audit_status="CONDITIONAL",
            scores=(18, 12, 14, 13, 4, 9, 5, 5),
            runtime=15,
            fixture_files=16,
            fixture_size=140000,
        ),
        _self_candidate(
            candidate_id="self-durable-double-consumption",
            title="Restarted worker consumes an already applied command twice",
            summary="A recovery cursor is persisted before the side-effect receipt, so restart replays a command whose external effect already completed.",
            failure="A durable queue consumer increments an account or emits a notification twice after a crash window.",
            expected="The receipt and cursor protocol make each command consumption idempotent across restart.",
            affected_files=["durable_queue/models.py", "durable_queue/repository.py", "durable_queue/worker.py", "durable_queue/service.py"],
            cross_module="The model state, SQLite transaction boundary, worker recovery cursor, and service-visible result must be reasoned about together.",
            visible_tests="Show one ordinary restart path and one command receipt without exposing the crash-window interleaving.",
            hidden_tests="Crash after effect/before cursor, restart twice, duplicate command IDs, stale receipts, and concurrent consumers.",
            anti_hardcoding="Generate command IDs and crash points; verify ledger invariants and exact effect counts.",
            risks=[
                "It may resemble AgentForge approval consumption if names, states, or tests are copied.",
                "The fixture must use a different domain and independently authored persistence protocol.",
            ],
            difficulty="ENGINEERING",
            difficulty_rationale="The defect is only visible across a durable crash boundary and requires a coherent atomicity fix.",
            scores=(15, 18, 15, 14, 7, 10, 5, 4),
            runtime=10,
            fixture_files=11,
            fixture_size=90000,
            edit_rounds=3,
        ),
        _self_candidate(
            candidate_id="self-policy-priority-shadow",
            title="Broad allow rule shadows a later deny rule",
            summary="A policy evaluator returns on the first broad match instead of selecting the most specific or highest-priority decision.",
            failure="A protected resource is allowed because a generic prefix rule is evaluated before its deny exception.",
            expected="Deterministic priority and specificity rules ensure deny or approval policies cannot be shadowed.",
            affected_files=["access_policy/models.py", "access_policy/matcher.py", "access_policy/evaluator.py", "access_policy/config.py"],
            cross_module="Rule parsing, normalization, specificity calculation, priority ordering, and final decision provenance must remain consistent.",
            visible_tests="Expose a generic allow and one unrelated deny while keeping the decisive overlap hidden.",
            hidden_tests="Overlapping prefixes, equal priority, explicit deny, approval requirements, order permutations, and unknown resources.",
            anti_hardcoding="Randomize declaration order and assert decision plus winning-rule provenance.",
            risks=[
                "The domain overlaps AgentForge Policy Engine concepts.",
                "Future implementation must use distinct rule semantics and must not copy existing AgentForge tests.",
            ],
            difficulty="ENGINEERING",
            difficulty_rationale="The repair changes a cross-module ordering contract and must preserve deterministic explanations.",
            scores=(16, 19, 14, 14, 8, 9, 5, 4),
            runtime=8,
            fixture_files=10,
            fixture_size=75000,
            edit_rounds=2,
        ),
        _self_candidate(
            candidate_id="self-async-cancel-cleanup",
            title="Cancellation leaves child tasks and leased resources alive",
            summary="An async coordinator cancels only its parent task and exits before awaiting child shutdown and releasing leases.",
            failure="After cancellation, background work continues and a resource pool remains exhausted for later requests.",
            expected="Cancellation propagates to every child, awaits termination, and releases each lease exactly once.",
            affected_files=["async_jobs/coordinator.py", "async_jobs/leases.py", "async_jobs/workers.py", "async_jobs/service.py"],
            cross_module="Structured concurrency, exception groups, lease ownership, cleanup ordering, and service state interact under races.",
            visible_tests="Show cancellation during one worker await and verify the top-level task ends.",
            hidden_tests="Cancel during lease acquisition, partial fan-out, child failure, repeated cancel, timeout, and post-cancel reuse.",
            anti_hardcoding="Vary synchronization barriers and assert no live task plus exact lease accounting.",
            risks=[
                "Race-based tests can become flaky without explicit barriers.",
                "The task must avoid OS process semantics already covered by AgentForge M6.",
            ],
            difficulty="CHALLENGE",
            difficulty_rationale="Correctness depends on several cancellation interleavings and resource ownership invariants.",
            scores=(14, 17, 15, 15, 6, 10, 5, 4),
            runtime=12,
            fixture_files=12,
            fixture_size=95000,
            edit_rounds=3,
        ),
        _self_candidate(
            candidate_id="self-terminal-state-cas",
            title="Late worker update overwrites a terminal cancelled state",
            summary="A worker writes RUNNING or COMPLETED without a compare-and-swap guard after another actor has committed CANCELLED.",
            failure="A cancelled operation later appears running or completed, contradicting the durable audit trail.",
            expected="Terminal states are immutable and late updates fail without erasing the real execution fact.",
            affected_files=["job_state/models.py", "job_state/repository.py", "job_state/worker.py", "job_state/api.py"],
            cross_module="State transition rules, repository CAS predicates, worker retries, and API projections must agree under concurrency.",
            visible_tests="Show normal cancel and normal complete paths separately.",
            hidden_tests="Race cancel versus start/complete, stale versions, repeated updates, and audit-event ordering.",
            anti_hardcoding="Schedule deterministic interleavings with barriers and assert version plus transition invariants.",
            risks=[
                "The state-machine theme overlaps AgentForge durable Run behavior.",
                "The domain and tests must be independently authored to avoid benchmark leakage from this repository.",
            ],
            difficulty="ENGINEERING",
            difficulty_rationale="The bug needs transactional state-machine reasoning but overlaps the durable-consumption primary candidate.",
            scores=(15, 18, 15, 15, 7, 9, 4, 4),
            runtime=10,
            fixture_files=11,
            fixture_size=85000,
            edit_rounds=3,
        ),
        _self_candidate(
            candidate_id="self-adapter-default-propagation",
            title="Adapter drops explicit false and zero values while applying defaults",
            summary="A transport adapter uses truthiness fallback, replacing valid false, zero, and empty values with defaults across nested requests.",
            failure="A user disables retries or sets a zero limit, but the downstream client receives the default value.",
            expected="Only missing or null fields use defaults; explicit false, zero, and valid empty collections survive every mapping layer.",
            affected_files=["client_adapter/request.py", "client_adapter/mapper.py", "client_adapter/provider.py", "client_adapter/config.py"],
            cross_module="Input parsing, default resolution, nested DTO mapping, and provider payload serialization each influence the final value.",
            visible_tests="Expose one explicit false case and ordinary missing-field defaults.",
            hidden_tests="Zero, empty list, empty string where valid, nested override, null, absent key, and provider round-trip.",
            anti_hardcoding="Generate falsy values by field type and compare a semantic payload matrix.",
            risks=[
                "The root cause may be found too quickly if all adapters use obvious `or default` expressions.",
                "The fixture needs realistic nested mapping without becoming a model-provider clone of AgentForge.",
            ],
            difficulty="BASIC",
            difficulty_rationale="The bug is common and realistic, but a well-scoped mapping trace should resolve in one or two rounds.",
            scores=(16, 19, 14, 14, 8, 8, 4, 4),
            runtime=6,
            fixture_files=9,
            fixture_size=65000,
            edit_rounds=2,
        ),
    ]
    ids = [candidate["candidate_id"] for candidate in candidates]
    if len(ids) != len(set(ids)):
        raise ValueError("candidate IDs must be unique")
    if set(ids) != set(PRIMARY_IDS) | set(ALTERNATE_IDS) | set(REJECTED_IDS):
        raise ValueError("selection lists must partition the candidate pool")
    return candidates


SELF_BRIEF_DETAILS = {
    "self-durable-double-consumption": {
        "root_cause_category": "durability ordering and idempotency",
        "common_bad_patches": [
            "Move the cursor write later without making receipt and cursor atomic.",
            "Ignore duplicate errors while the side effect still executes twice.",
        ],
        "why_not_agentforge_copy": "Use a fictional billing ledger and queue protocol with no Approval, Run, Snapshot, or AgentForge repository APIs.",
    },
    "self-policy-priority-shadow": {
        "root_cause_category": "policy ordering and specificity",
        "common_bad_patches": [
            "Always evaluate deny first, breaking explicit higher-priority exceptions.",
            "Sort rules in place and mutate administrator configuration.",
        ],
        "why_not_agentforge_copy": "Define a standalone resource-routing policy language with different capabilities and decision rules.",
    },
    "self-async-cancel-cleanup": {
        "root_cause_category": "structured cancellation and resource ownership",
        "common_bad_patches": [
            "Catch CancelledError and return before child tasks finish.",
            "Release leases in both child and parent cleanup, causing double release.",
        ],
        "why_not_agentforge_copy": "Use asyncio tasks and in-memory leases, not AgentForge process supervision or test execution records.",
    },
    "self-terminal-state-cas": {
        "root_cause_category": "optimistic concurrency and terminal-state invariants",
        "common_bad_patches": [
            "Check terminal state in Python before an unconditional database update.",
            "Let COMPLETED always win, even when cancellation committed first.",
        ],
        "why_not_agentforge_copy": "Use a document-conversion job domain and a distinct state/version model with independently written tests.",
    },
    "self-adapter-default-propagation": {
        "root_cause_category": "field-presence semantics and nested parameter propagation",
        "common_bad_patches": [
            "Special-case only false while zero and empty collections still disappear.",
            "Stop defaulting entirely and break absent-field behavior.",
        ],
        "why_not_agentforge_copy": "Use a storage-client adapter unrelated to LLM providers or AgentForge model request types.",
    },
}


def _recommendation(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "candidate_id": candidate["candidate_id"],
        "recommendation_reason": (
            f"Score {candidate['total_score']}/100 with {candidate['difficulty_recommendation']} "
            f"scope. {candidate['difficulty_rationale']}"
        ),
        "maximum_risk": candidate["adaptation_risks"][0],
        "required_adaptation": candidate["adaptation_actions"],
        "visible_hidden_test_feasibility": {
            "visible": candidate["visible_test_feasibility"],
            "hidden": candidate["hidden_test_feasibility"],
        },
        "license_and_provenance": {
            "license": candidate["license"],
            "license_verified": candidate["license_verified"],
            "provenance_confidence": candidate["provenance_confidence"],
        },
        "difficulty_basis": candidate["difficulty_rationale"],
        "budget_profile": candidate["difficulty_recommendation"],
        "main_demo_recommendation": candidate["candidate_id"]
        == "self-durable-double-consumption",
        "b2_manual_confirmation": [
            "Approve the source and license obligations.",
            "Approve the proposed crop without changing the bug's semantics.",
            "Approve visible versus hidden test boundaries and the fixed TestProfile.",
        ],
    }


def _selection_documents(candidates: list[dict[str, Any]]) -> None:
    by_id = {candidate["candidate_id"]: candidate for candidate in candidates}
    _write_json(
        "recommended_primary.json",
        {
            "candidate_schema_version": SCHEMA_VERSION,
            "selection_status": "HUMAN_APPROVED_FOR_B2_PREFLIGHT",
            "human_decision": HUMAN_DECISION,
            "candidate_ids": list(PRIMARY_IDS),
            "recommendations": [_recommendation(by_id[candidate_id]) for candidate_id in PRIMARY_IDS],
        },
    )
    _write_json(
        "recommended_alternates.json",
        {
            "candidate_schema_version": SCHEMA_VERSION,
            "selection_status": "REGISTERED_ALTERNATES",
            "candidate_ids": list(ALTERNATE_IDS),
            "recommendations": [_recommendation(by_id[candidate_id]) for candidate_id in ALTERNATE_IDS],
        },
    )
    _write_json(
        "rejected_candidates.json",
        {
            "candidate_schema_version": SCHEMA_VERSION,
            "candidate_ids": list(REJECTED_IDS),
            "rejections": [
                {
                    "candidate_id": candidate_id,
                    "reasons": by_id[candidate_id]["exclusion_reasons"],
                }
                for candidate_id in REJECTED_IDS
            ],
        },
    )


def _self_briefs(candidates: list[dict[str, Any]]) -> None:
    briefs = []
    for candidate in candidates:
        if candidate["source_kind"] != "SELF_BUILT":
            continue
        detail = SELF_BRIEF_DETAILS[candidate["candidate_id"]]
        briefs.append(
            {
                "candidate_id": candidate["candidate_id"],
                "user_visible_symptom": candidate["observed_failure"],
                "expected_module_structure": candidate["affected_files"],
                "root_cause_category": detail["root_cause_category"],
                "cross_module_reasoning": candidate["cross_module_reasoning"],
                "visible_tests": candidate["visible_test_feasibility"],
                "hidden_tests": candidate["hidden_test_feasibility"],
                "common_bad_patches": detail["common_bad_patches"],
                "anti_hardcoding": candidate["anti_hardcoding_feasibility"],
                "difficulty": candidate["difficulty_recommendation"],
                "expected_edit_rounds": candidate["expected_edit_rounds"],
                "why_not_agentforge_copy": detail["why_not_agentforge_copy"],
            }
        )
    _write_json(
        "self_built_candidate_briefs.json",
        {"candidate_schema_version": SCHEMA_VERSION, "briefs": briefs},
    )


def _candidate_matrix(candidates: list[dict[str, Any]]) -> None:
    with (ROOT / "candidate_matrix.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for candidate in candidates:
            row: dict[str, str] = {}
            for field in CSV_FIELDS:
                value = candidate[field]
                row[field] = str(value).lower() if isinstance(value, bool) else str(value)
            writer.writerow(row)


def _artifact_registry() -> None:
    project_root = ROOT.parents[1]
    artifacts = []
    for path in sorted(ROOT.iterdir(), key=lambda item: item.name):
        if not path.is_file() or path.name == "artifact_registry.json":
            continue
        content = path.read_bytes()
        artifacts.append(
            {
                "path": path.relative_to(project_root).as_posix(),
                "sha256": hashlib.sha256(content).hexdigest(),
                "size_bytes": len(content),
            }
        )
    _write_json(
        "artifact_registry.json",
        {
            "registry_version": "1.0.0",
            "generated_at": AUDITED_AT,
            "artifacts": artifacts,
        },
    )


def main() -> None:
    candidates = _candidates()
    _write_json("candidate_schema.json", _schema())
    _write_json(
        "candidates.json",
        {
            "candidate_schema_version": SCHEMA_VERSION,
            "audit_status": "M7_B1_COMPLETE",
            "candidates": candidates,
        },
    )
    _candidate_matrix(candidates)
    _selection_documents(candidates)
    _self_briefs(candidates)
    _artifact_registry()


if __name__ == "__main__":
    main()
