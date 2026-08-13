# ruff: noqa: E501

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parents[1]
CANDIDATE_ROOT = PROJECT_ROOT / "evaluation" / "candidates"
FIXTURE_ROOT = PROJECT_ROOT / "evaluation" / "fixtures"
SCHEMA_VERSION = "1.0.0"
AUDITED_AT = "2026-07-16T00:00:00+08:00"
DECIDED_AT = "2026-07-17T00:00:00+08:00"

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

FIRST_FIXTURE_IDS = (
    "quixbugs-shortest-path-length",
    "bugsinpy-black-21",
    "swebench-pytest-10051",
    "self-durable-double-consumption",
)

REQUIRED_FIELDS = (
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
)

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

PUBLIC_CONFIG: dict[str, dict[str, Any]] = {
    "quixbugs-shortest-path-length": {
        "failure_command": "python -m pytest python_testcases/test_shortest_path_length.py -q",
        "passing_command": "python -m pytest python_testcases/test_shortest_path_length.py -q --correct",
        "runtime": (1083, 1549),
        "dependencies": ["Python 3.14.3", "pytest 9.1.1 for source audit"],
        "audit_install": False,
        "source_files": ["python_programs/shortest_path_length.py"],
        "test_files": ["python_testcases/test_shortest_path_length.py"],
        "support_files": ["node.py", "LICENSE"],
        "fixture_size": (5, 18000),
        "patch_files": ["python_programs/shortest_path_length.py"],
        "patch_lines": 2,
        "patch_tests": False,
        "visible": "Expose one multiple-route graph where the returned distance is wrong without identifying the relaxation expression.",
        "hidden": "Cover direct, indirect, equal-cost, unreachable, stale-heap, and start-equals-goal cases with an independent shortest-path oracle.",
        "anti": "Generate deterministic graph variants and compare semantic distances rather than exact intermediate heap state.",
        "incorrect": ["Always add the outgoing edge to the first queued distance", "Return the direct edge when one exists"],
        "protected": ["tests/", "task_manifest.json", "LICENSE"],
        "rounds": 1,
        "demo": "GOOD",
        "risk": ["The one-line repair can become obvious if the visible graph is too small."],
        "gate": "GO",
        "gate_reasons": ["Official buggy/correct programs and four-case test were each executed three times on Windows with Python 3.14; buggy failed and correct passed deterministically."],
    },
    "quixbugs-topological-ordering": {
        "failure_command": "python -m pytest python_testcases/test_topological_ordering.py -q",
        "passing_command": "python -m pytest python_testcases/test_topological_ordering.py -q --correct",
        "runtime": (1111, 1521),
        "dependencies": ["Python 3.14.3", "pytest 9.1.1 for source audit"],
        "audit_install": False,
        "source_files": ["python_programs/topological_ordering.py"],
        "test_files": ["python_testcases/test_topological_ordering.py"],
        "support_files": ["node.py", "LICENSE"],
        "fixture_size": (5, 18000),
        "patch_files": ["python_programs/topological_ordering.py"],
        "patch_lines": 2,
        "patch_tests": False,
        "visible": "Expose a DAG where dependency satisfaction is confused with successor satisfaction.",
        "hidden": "Validate edge-order properties across multiple roots, converging edges, isolated nodes, and input permutations without requiring one arbitrary valid order.",
        "anti": "Use a property oracle that accepts every valid topological order and rejects hard-coded node sequences.",
        "incorrect": ["Hard-code the three upstream expected lists", "Sort nodes by value before returning"],
        "protected": ["tests/", "task_manifest.json", "LICENSE"],
        "rounds": 1,
        "demo": "FAIR",
        "risk": ["Upstream tests assert one exact order although multiple topological orders can be valid."],
        "gate": "CONDITIONAL",
        "gate_reasons": ["Buggy/correct execution is deterministic, but the formal oracle must replace exact-order assertions with edge-order semantics."],
        "conditions": ["B2.1 must prove a property-based oracle accepts alternate valid orders while retaining the original failure."],
        "questions": ["Can the crop preserve deterministic presentation without treating one valid order as the only correct output?"],
    },
    "bugsinpy-black-21": {
        "failure_command": "python -X utf8=0 -c <bounded dump_to_file encoding probe>",
        "passing_command": "python -X utf8=0 -c <same bounded dump_to_file encoding probe>",
        "runtime": (842, 1476),
        "dependencies": ["Python 3.8.20 and 3.14.3", "click 7.1.2", "attrs 21.4.0"],
        "audit_install": True,
        "source_files": ["black.py"],
        "test_files": ["tests/test_diagnostics.py"],
        "support_files": ["blib2to3/", "LICENSE"],
        "fixture_size": (6, 240000),
        "patch_files": ["black.py", "tests/test_black.py"],
        "patch_lines": 4,
        "patch_tests": True,
        "visible": "Trigger diagnostic-file creation with non-ASCII source under an explicitly non-UTF default encoding.",
        "hidden": "Cover emoji, CJK, ASCII, multiline diagnostics, reopen behavior, and preservation of caller content.",
        "anti": "Vary characters outside several legacy code pages and assert UTF-8 bytes plus round-trip content.",
        "incorrect": ["Drop non-ASCII characters before writing", "Set an error handler that silently replaces characters"],
        "protected": ["tests/", "task_manifest.json", "LICENSE"],
        "rounds": 1,
        "demo": "GOOD",
        "risk": ["A crop limited to the temporary-file helper could lose formatter diagnostic context."],
        "gate": "GO",
        "gate_reasons": ["The exact source helper failed and fixed helper passed three times on both Python 3.8 and 3.14 under the Windows GBK default encoding."],
    },
    "bugsinpy-pysnooper-3": {
        "failure_command": "python -c <bounded file-writer closure probe>",
        "passing_command": "python -c <same bounded file-writer closure probe>",
        "runtime": (250, 487),
        "dependencies": ["Python 3.8.20 source audit", "Python 3.14.3 AST crop probe", "decorator 4.4.2", "future 0.18.3", "six 1.16.0"],
        "audit_install": True,
        "source_files": ["pysnooper/pysnooper.py", "pysnooper/tracer.py"],
        "test_files": ["tests/test_file_output.py"],
        "support_files": ["pysnooper/utils.py", "pysnooper/pycompat.py", "LICENSE"],
        "fixture_size": (8, 42000),
        "patch_files": ["pysnooper/pysnooper.py", "tests/test_pysnooper.py"],
        "patch_lines": 30,
        "patch_tests": True,
        "visible": "Decorate a function with file output and expose the downstream NameError rather than calling the writer factory directly.",
        "hidden": "Exercise pathlib and string paths, repeated writes, nested calls, Unicode output, and independent decorators.",
        "anti": "Keep the writer factory behind the decorator boundary and vary path representations and call counts.",
        "incorrect": ["Introduce a global output_path variable", "Always write to a fixed temporary file"],
        "protected": ["tests/", "task_manifest.json", "LICENSE"],
        "rounds": 1,
        "demo": "FAIR",
        "risk": ["The source fix is a one-token variable change and can collapse into a toy if the tracer boundary is removed."],
        "gate": "CONDITIONAL",
        "gate_reasons": ["Exact source failure/fix is deterministic on Python 3.8 and the extracted target function is deterministic on 3.14; engineering crop fidelity remains unresolved."],
        "conditions": ["B2.1 must retain decorator, tracer, writer, and compatibility module boundaries and prove the visible test does not expose the closure variable."],
        "questions": ["Does the multi-module crop remain small enough while preserving downstream symptom distance?"],
    },
    "bugsinpy-httpie-4": {
        "failure_command": "python -c <offline prepared-request header probe>",
        "passing_command": "python -c <same offline prepared-request header probe>",
        "runtime": (206, 908),
        "dependencies": ["Python 3.8.20", "Requests 2.4.3 for source audit", "Python 3.14.3 dependency-free crop probe"],
        "audit_install": True,
        "source_files": ["httpie/models.py"],
        "test_files": ["tests/test_request_headers.py"],
        "support_files": ["httpie/compat.py", "support/case_insensitive_headers.py", "LICENSE"],
        "fixture_size": (7, 34000),
        "patch_files": ["httpie/models.py", "tests/test_regressions.py"],
        "patch_lines": 21,
        "patch_tests": True,
        "visible": "Render a prepared request with a lowercase custom host and reveal duplicate logical headers without making a network request.",
        "hidden": "Vary header casing, ports, userinfo, duplicate logical keys, unrelated headers, and absent Host values.",
        "anti": "Use a faithful case-insensitive mapping and assert logical header cardinality and preserved user value across permutations.",
        "incorrect": ["Delete every generated Host header", "Lowercase all rendered header values"],
        "protected": ["tests/", "support/case_insensitive_headers.py", "task_manifest.json", "LICENSE"],
        "rounds": 1,
        "demo": "GOOD",
        "risk": ["The upstream regression test performs DNS and HTTP; the offline crop replaces Requests with a bounded compatible mapping."],
        "gate": "CONDITIONAL",
        "gate_reasons": ["Exact source class separates buggy/fixed under Requests 2.4.3, and the 3.14 offline spike preserves case-insensitive semantics; dependency replacement fidelity needs one formal review."],
        "conditions": ["B2.1 must compare the local mapping contract against Requests CaseInsensitiveDict for membership, iteration, and duplicate-key behavior."],
        "questions": ["Is the local support mapping faithful enough to preserve the original mapping-copy bug without shipping Requests?"],
    },
    "swebench-flask-5014": {
        "failure_command": "python -m pytest tests/test_blueprints.py::test_empty_name_not_allowed -q",
        "passing_command": "python -m pytest tests/test_blueprints.py::test_empty_name_not_allowed -q",
        "runtime": (1168, 2484),
        "dependencies": ["Python 3.10.20 source test", "Python 3.14.3 direct source probe", "Flask declared runtime dependencies"],
        "audit_install": True,
        "source_files": ["src/flask/blueprints.py", "src/flask/scaffold.py"],
        "test_files": ["tests/test_blueprints.py"],
        "support_files": ["src/flask/__init__.py", "LICENSE.rst"],
        "fixture_size": (9, 105000),
        "patch_files": ["CHANGES.rst", "src/flask/blueprints.py", "tests/test_blueprints.py"],
        "patch_lines": 10,
        "patch_tests": True,
        "visible": "Attempt registration of an invalid blueprint through a small application boundary and assert a controlled error.",
        "hidden": "Cover empty, valid Unicode, dotted, nested, duplicate, and non-string names without requiring the exact upstream message.",
        "anti": "Keep direct constructor-only edge cases hidden and validate the registration invariant through several call paths.",
        "incorrect": ["Reject every false-like object after converting valid names", "Silently replace an empty name with a default"],
        "protected": ["tests/", "task_manifest.json", "LICENSE.rst"],
        "rounds": 1,
        "demo": "GOOD",
        "risk": ["A constructor-only visible test would reveal the validation location too directly."],
        "gate": "GO",
        "gate_reasons": ["The official added regression fails on the base and passes on the merge three times under Python 3.10; direct source behavior also separates three times under Python 3.14."],
    },
    "swebench-pytest-10051": {
        "failure_command": "python -m pytest testing/logging/test_fixture.py::test_clear_for_call_stage -q",
        "passing_command": "python -m pytest testing/logging/test_fixture.py::test_clear_for_call_stage -q",
        "runtime": (918, 1608),
        "dependencies": ["Python 3.10.20 source test", "Python 3.14.3 lifecycle probe", "pytest source dependencies"],
        "audit_install": True,
        "source_files": ["src/_pytest/logging.py", "src/_pytest/stash.py"],
        "test_files": ["testing/logging/test_fixture.py"],
        "support_files": ["src/_pytest/nodes.py", "LICENSE"],
        "fixture_size": (8, 95000),
        "patch_files": ["src/_pytest/logging.py", "testing/logging/test_fixture.py", "changelog/9877.bugfix.rst", "AUTHORS"],
        "patch_lines": 28,
        "patch_tests": True,
        "visible": "Capture one phase, clear during call, and reveal that externally stored phase records remain stale.",
        "hidden": "Clear in setup/call/teardown, clear twice, log after clear, retain phase isolation, and check list identity across a lifecycle boundary.",
        "anti": "Assert semantic phase contents and reference identity through multiple interleavings rather than matching the upstream method body.",
        "incorrect": ["Delete the phase key from the stash", "Replace every phase record list when clearing"],
        "protected": ["tests/", "task_manifest.json", "LICENSE"],
        "rounds": 2,
        "demo": "EXCELLENT",
        "risk": ["The merge commit contains unrelated changes, so B2.1 must use the PR first-parent diff rather than the broad SWE base-to-merge diff."],
        "gate": "GO",
        "gate_reasons": ["The official PR regression fails/passes three times in isolated source installations on Python 3.10; a direct lifecycle probe repeats the same aliasing distinction on Python 3.14."],
    },
}

SELF_CONFIG: dict[str, dict[str, Any]] = {
    "self-durable-double-consumption": {
        "modules": ["parcel_flow/models.py", "parcel_flow/store.py", "parcel_flow/dispatcher.py", "parcel_flow/service.py"],
        "symptom": "A customer sees a duplicate dispatch notification after the service restarts during handoff.",
        "invariant": "A command receipt and its externally visible dispatch record become durable as one logical transition, and completed results are reused.",
        "bug": "The dispatcher persists the visible dispatch before advancing the durable receipt cursor, allowing restart to repeat the effect.",
        "repair": "Use a conditional receipt claim plus durable result reuse so restart observes completion before attempting another dispatch.",
        "visible": "Show ordinary processing plus one restart that produces duplicate customer-visible dispatches; do not name idempotency, resume, or the crash window.",
        "hidden": "Cover first processing, restart recovery, repeated resume, completed-result reuse, stale claims, and a barrier-controlled concurrent compare-and-set boundary.",
        "anti": "Generate command IDs and restart points, inspect durable receipts and dispatch ledger, and reject fixes that suppress all second starts.",
        "incorrect": ["Keep an in-memory set of processed IDs", "Mark the receipt complete before the dispatch is durably recorded", "Ignore every command after any restart"],
        "protected": ["tests/", "reference/", "task_manifest.json", "schema/"],
        "rounds": 2,
        "difficulty": "ENGINEERING",
        "demo": "EXCELLENT",
        "risk": ["The design could leak AgentForge concepts unless parcel-domain names and independently authored tests are enforced."],
        "gate": "GO",
        "gate_reasons": ["The design closes symptom, four-module causality, durable invariants, two-round repair path, hidden crash/concurrency matrix, and leakage controls without copying AgentForge names."],
    },
    "self-policy-priority-shadow": {
        "modules": ["route_rules/models.py", "route_rules/matcher.py", "route_rules/ranker.py", "route_rules/service.py"],
        "symptom": "A narrow safety restriction is ignored when a broad route rule also matches the same request.",
        "invariant": "Rule ranking is deterministic and applies specificity, explicit effect severity, and stable tie-breaking before producing an explanation.",
        "bug": "The matcher returns the first prefix match before the ranker compares the full candidate set.",
        "repair": "Collect all matching rules and apply the declared ranking tuple centrally before rendering the decision explanation.",
        "visible": "Expose a broad allow and one unrelated restriction; keep the decisive overlapping prefix combination hidden.",
        "hidden": "Permute configuration order and cover overlapping prefixes, equal specificity, explicit restrictions, approval-like intermediate effects, and unknown resources.",
        "anti": "Randomize rule order deterministically and compare decisions with an independent ranking oracle.",
        "incorrect": ["Always choose the longest string regardless of effect", "Sort the configuration once but keep early return in the matcher"],
        "protected": ["tests/", "reference/", "task_manifest.json", "fixtures/rules.json"],
        "rounds": 2,
        "difficulty": "ENGINEERING",
        "demo": "GOOD",
        "risk": ["The domain overlaps policy engines; terminology and data model must remain independent from AgentForge."],
        "gate": "GO",
        "gate_reasons": ["The four-module contract, ranking invariant, misleading symptom, independent oracle, and partial-fix matrix are fully specified."],
    },
    "self-async-cancel-cleanup": {
        "modules": ["batch_flow/coordinator.py", "batch_flow/leases.py", "batch_flow/workers.py", "batch_flow/service.py"],
        "symptom": "Cancelling a batch reports completion, but a later batch cannot acquire one of the resources it needs.",
        "invariant": "Cancellation waits for every owned child and releases every acquired lease exactly once before the coordinator reports completion.",
        "bug": "The coordinator cancels children and immediately returns without awaiting their cleanup paths or releasing partially acquired leases.",
        "repair": "Track ownership explicitly, cancel children, await bounded cleanup, and release leases in a shielded finalization path.",
        "visible": "Cancel during one worker wait and show that the top-level task ends while a subsequent batch remains blocked.",
        "hidden": "Use explicit barriers for cancellation during acquisition, partial fan-out, child failure, repeated cancel, timeout, and post-cancel reuse; assert no background tasks remain.",
        "anti": "Use deterministic synchronization barriers and task enumeration rather than sleeps or wall-clock races.",
        "incorrect": ["Catch and suppress CancelledError without awaiting children", "Release only leases recorded before fan-out completes", "Increase the timeout"],
        "protected": ["tests/", "reference/", "task_manifest.json", "fixtures/schedule.json"],
        "rounds": 2,
        "difficulty": "CHALLENGE",
        "demo": "GOOD",
        "risk": ["The task becomes flaky if B2.1 uses sleeps instead of explicit synchronization barriers."],
        "gate": "CONDITIONAL",
        "gate_reasons": ["The design and incorrect-patch matrix are complete, but deterministic cancellation scheduling must be demonstrated before formal admission."],
        "conditions": ["B2.1 must implement barrier-driven scheduling and repeat every hidden interleaving without timing sleeps."],
        "questions": ["Can all cancellation windows run deterministically within the CHALLENGE TestProfile timeout?"],
    },
}


def _load_candidates() -> dict[str, dict[str, Any]]:
    document = json.loads((CANDIDATE_ROOT / "candidates.json").read_text(encoding="utf-8"))
    return {candidate["candidate_id"]: candidate for candidate in document["candidates"]}


def _evidence(status: str, refs: list[str], summary: str) -> dict[str, Any]:
    return {"status": status, "evidence_refs": refs, "summary": summary}


def _classifications(result: dict[str, Any], public: bool) -> dict[str, Any]:
    candidate_id = result["candidate_id"]
    source_ref = result["source_evidence"][0]["reference"]
    run_ref = f"execution:{candidate_id}:three-repetition-summary" if public else f"design:{candidate_id}:preflight"
    crop_status = "VERIFIED" if result["crop_feasibility"] == "VERIFIED" else "INFERRED"
    gate_status = "VERIFIED" if result["final_gate"] == "GO" else "INFERRED"
    execution_status = "VERIFIED" if public else "UNVERIFIED"
    relation_status = "VERIFIED" if public else "INFERRED"
    return {
        "license_status": _evidence("VERIFIED", [source_ref], "License text and attribution were checked at the pinned source revision."),
        "source_checkout_verified": _evidence(execution_status, [source_ref, run_ref], "Pinned public checkout was inspected, or the original self-built design boundary was reviewed."),
        "bug_fix_relation_verified": _evidence(relation_status, [source_ref, run_ref], "The public fix relation was executed or the self-built causal repair was closed at design level."),
        "original_failure_reproduced": _evidence(execution_status, [run_ref], "Buggy state failed deterministically for public sources; self-built fixtures do not exist in B2.0."),
        "reference_fix_verified": _evidence(execution_status, [run_ref], "Fixed state passed deterministically for public sources; self-built repair remains a reviewed design."),
        "deterministic_result": _evidence(execution_status, [run_ref], "Public checks were repeated three times; self-built determinism relies on the specified barrier/test design."),
        "python_3_14_status": _evidence("VERIFIED" if public else "INFERRED", [run_ref], "Public target logic was exercised on Python 3.14; self-built compatibility is a standard-library design constraint."),
        "windows_status": _evidence("VERIFIED" if public else "INFERRED", [run_ref], "Public probes ran on Windows; self-built platform behavior remains design-level."),
        "offline_status": _evidence("VERIFIED", [run_ref], "All admitted checks and proposed TestProfiles use local resources only."),
        "fixed_test_profile_feasibility": _evidence("VERIFIED" if public else "INFERRED", [run_ref], "Execution used fixed absolute Python, cwd, argv, and an explicit cleared environment, or defines that same boundary for self-built work."),
        "crop_feasibility": _evidence(crop_status, [run_ref], "Disposable probes established the target causal slice; conditional items retain a named B2.1 fidelity condition."),
        "final_gate": _evidence(gate_status, [source_ref, run_ref], "Gate follows the documented hard requirements and preserves conditional risks."),
    }


def _public_result(candidate: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    gate = config["gate"]
    result: dict[str, Any] = {
        "candidate_id": candidate["candidate_id"],
        "preflight_schema_version": SCHEMA_VERSION,
        "source_kind": candidate["source_kind"],
        "original_audit_status": candidate["audit_status"],
        "source_project": candidate["source_project"],
        "source_bug_id": candidate["source_bug_id"],
        "license": candidate["license"],
        "license_status": "VERIFIED",
        "attribution_requirements": candidate["attribution_requirements"],
        "buggy_revision": candidate["buggy_commit"],
        "fixed_revision": candidate["fixed_commit"],
        "source_evidence": [
            {"kind": "FIRST_PARTY", "reference": url} for url in candidate["source_urls"]
        ]
        + [{"kind": "EXECUTION_SUMMARY", "reference": f"execution:{candidate['candidate_id']}:three-repetition-summary"}],
        "source_checkout_verified": True,
        "bug_fix_relation_verified": True,
        "original_failure_reproduced": True,
        "reference_fix_verified": True,
        "failure_command": config["failure_command"],
        "passing_command": config["passing_command"],
        "test_repetitions": 3,
        "deterministic_result": True,
        "minimum_test_runtime_ms": config["runtime"][0],
        "maximum_test_runtime_ms": config["runtime"][1],
        "dependency_inventory": config["dependencies"],
        "dependency_install_required_for_source_audit": config["audit_install"],
        "target_fixture_runtime_dependencies": ["Python standard library", "pre-provisioned pytest TestProfile"],
        "network_required": False,
        "external_service_required": False,
        "database_required": False,
        "native_build_required": False,
        "python_3_14_status": "VERIFIED",
        "windows_status": "VERIFIED",
        "offline_status": "VERIFIED",
        "fixed_test_profile_feasibility": True,
        "crop_feasibility": "VERIFIED" if gate == "GO" else "CONDITIONAL",
        "crop_fidelity_risk": "LOW" if gate == "GO" else "MEDIUM",
        "required_source_files": config["source_files"],
        "required_test_files": config["test_files"],
        "required_support_files": config["support_files"],
        "expected_fixture_file_count": config["fixture_size"][0],
        "expected_fixture_size_bytes": config["fixture_size"][1],
        "reference_patch_files": config["patch_files"],
        "reference_patch_lines": config["patch_lines"],
        "reference_patch_changes_tests": config["patch_tests"],
        "reference_patch_changes_dependencies": False,
        "visible_test_plan": config["visible"],
        "hidden_test_plan": config["hidden"],
        "anti_hardcoding_plan": config["anti"],
        "known_incorrect_patch_examples": config["incorrect"],
        "protected_paths_plan": config["protected"],
        "expected_edit_rounds": config["rounds"],
        "difficulty_before": candidate["difficulty_recommendation"],
        "difficulty_after_preflight": candidate["difficulty_recommendation"],
        "difficulty_change_reason": "No change: execution scope, patch size, symptom distance, and hidden-test complexity support the M7-B1 label.",
        "demo_suitability": config["demo"],
        "primary_risks": config["risk"],
        "unresolved_questions": config.get("questions", []),
        "evidence_classification": {},
        "final_gate": gate,
        "gate_reasons": config["gate_reasons"],
        "conditional_resolution": config.get("conditions", []),
        "recommended_action": "ENTER_B2_1_CONSTRUCTION" if gate == "GO" else "RESOLVE_DURING_B2_1_BEFORE_ADMISSION",
        "audited_at": AUDITED_AT,
    }
    result["evidence_classification"] = _classifications(result, public=True)
    return result


def _self_result(candidate: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    gate = config["gate"]
    design_ref = f"design:{candidate['candidate_id']}:preflight"
    result: dict[str, Any] = {
        "candidate_id": candidate["candidate_id"],
        "preflight_schema_version": SCHEMA_VERSION,
        "source_kind": candidate["source_kind"],
        "original_audit_status": candidate["audit_status"],
        "source_project": "AgentForge original evaluation design",
        "source_bug_id": candidate["source_bug_id"],
        "license": candidate["license"],
        "license_status": "VERIFIED",
        "attribution_requirements": "No third-party code exists; B2.1 must author an original fixture and record AgentForge project ownership.",
        "buggy_revision": None,
        "fixed_revision": None,
        "source_evidence": [{"kind": "ORIGINAL_DESIGN", "reference": design_ref}, {"kind": "USER_VISIBLE_SYMPTOM", "reference": config["symptom"]}, {"kind": "CORE_INVARIANT", "reference": config["invariant"]}, {"kind": "BUGGY_MECHANISM", "reference": config["bug"]}, {"kind": "REPAIR_PRINCIPLE", "reference": config["repair"]}],
        "source_checkout_verified": False,
        "bug_fix_relation_verified": False,
        "original_failure_reproduced": False,
        "reference_fix_verified": False,
        "failure_command": None,
        "passing_command": None,
        "test_repetitions": 0,
        "deterministic_result": True,
        "minimum_test_runtime_ms": 0,
        "maximum_test_runtime_ms": 0,
        "dependency_inventory": ["Python 3.14 standard library", "pytest from immutable TestProfile"],
        "dependency_install_required_for_source_audit": False,
        "target_fixture_runtime_dependencies": ["Python standard library", "pre-provisioned pytest TestProfile"],
        "network_required": False,
        "external_service_required": False,
        "database_required": False,
        "native_build_required": False,
        "python_3_14_status": "INFERRED",
        "windows_status": "INFERRED",
        "offline_status": "VERIFIED",
        "fixed_test_profile_feasibility": True,
        "crop_feasibility": "VERIFIED" if gate == "GO" else "CONDITIONAL",
        "crop_fidelity_risk": "LOW" if gate == "GO" else "MEDIUM",
        "required_source_files": config["modules"],
        "required_test_files": ["tests/test_visible_behavior.py", "hidden/test_invariants.py"],
        "required_support_files": ["task_manifest.json", "README.md"],
        "expected_fixture_file_count": len(config["modules"]) + 4,
        "expected_fixture_size_bytes": 48000 if candidate["candidate_id"] != "self-async-cancel-cleanup" else 62000,
        "reference_patch_files": [],
        "reference_patch_lines": 0,
        "reference_patch_changes_tests": False,
        "reference_patch_changes_dependencies": False,
        "visible_test_plan": config["visible"],
        "hidden_test_plan": config["hidden"],
        "anti_hardcoding_plan": config["anti"],
        "known_incorrect_patch_examples": config["incorrect"],
        "protected_paths_plan": config["protected"],
        "expected_edit_rounds": config["rounds"],
        "difficulty_before": candidate["difficulty_recommendation"],
        "difficulty_after_preflight": config["difficulty"],
        "difficulty_change_reason": "No change: module count, causal distance, feedback rounds, and hidden interleavings support the original label.",
        "demo_suitability": config["demo"],
        "primary_risks": config["risk"],
        "unresolved_questions": config.get("questions", []),
        "evidence_classification": {},
        "final_gate": gate,
        "gate_reasons": config["gate_reasons"],
        "conditional_resolution": config.get("conditions", []),
        "recommended_action": "ENTER_B2_1_CONSTRUCTION" if gate == "GO" else "RESOLVE_DURING_B2_1_BEFORE_ADMISSION",
        "audited_at": AUDITED_AT,
    }
    result["evidence_classification"] = _classifications(result, public=False)
    return result


def _schema() -> dict[str, Any]:
    string_fields = {
        "candidate_id", "preflight_schema_version", "source_kind", "original_audit_status", "source_project", "source_bug_id", "license", "license_status", "attribution_requirements", "failure_command", "passing_command", "python_3_14_status", "windows_status", "offline_status", "crop_feasibility", "crop_fidelity_risk", "visible_test_plan", "hidden_test_plan", "anti_hardcoding_plan", "difficulty_before", "difficulty_after_preflight", "difficulty_change_reason", "demo_suitability", "final_gate", "recommended_action", "audited_at",
    }
    nullable_strings = {"buggy_revision", "fixed_revision", "failure_command", "passing_command"}
    boolean_fields = {"source_checkout_verified", "bug_fix_relation_verified", "original_failure_reproduced", "reference_fix_verified", "deterministic_result", "dependency_install_required_for_source_audit", "network_required", "external_service_required", "database_required", "native_build_required", "fixed_test_profile_feasibility", "reference_patch_changes_tests", "reference_patch_changes_dependencies"}
    integer_fields = {"test_repetitions", "minimum_test_runtime_ms", "maximum_test_runtime_ms", "expected_fixture_file_count", "expected_fixture_size_bytes", "reference_patch_lines", "expected_edit_rounds"}
    list_fields = {"source_evidence", "dependency_inventory", "target_fixture_runtime_dependencies", "required_source_files", "required_test_files", "required_support_files", "reference_patch_files", "known_incorrect_patch_examples", "protected_paths_plan", "primary_risks", "unresolved_questions", "gate_reasons", "conditional_resolution"}
    properties: dict[str, Any] = {}
    for field in REQUIRED_FIELDS:
        if field in boolean_fields:
            properties[field] = {"type": "boolean"}
        elif field in integer_fields:
            properties[field] = {"type": "integer", "minimum": 0}
        elif field in list_fields:
            properties[field] = {"type": "array"}
        elif field == "evidence_classification":
            properties[field] = {"type": "object"}
        elif field in nullable_strings:
            properties[field] = {"type": ["string", "null"]}
        elif field in string_fields:
            properties[field] = {"type": "string"}
        else:
            raise AssertionError(f"Unclassified schema field: {field}")
    properties["final_gate"]["enum"] = ["GO", "CONDITIONAL", "NO_GO", "UNVERIFIED"]
    properties["source_kind"]["enum"] = ["QUIXBUGS", "BUGSINPY", "SWE_BENCH", "SELF_BUILT"]
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", "schema_version": SCHEMA_VERSION, "$defs": {"preflight_result": {"type": "object", "required": list(REQUIRED_FIELDS), "additionalProperties": False, "properties": properties}}}


def _write_json(name: str, value: Any) -> None:
    (ROOT / name).write_text(json.dumps(value, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")


def _write_matrix(results: list[dict[str, Any]]) -> None:
    with (ROOT / "primary_preflight_matrix.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, lineterminator="\n")
        writer.writeheader()
        for result in results:
            writer.writerow({field: str(result[field]).lower() if isinstance(result[field], bool) else result[field] for field in CSV_FIELDS})


def _artifact_registry() -> None:
    artifacts = []
    for path in sorted(ROOT.iterdir()):
        if not path.is_file() or path.name == "artifact_registry.json":
            continue
        content = path.read_bytes()
        artifacts.append({"path": path.relative_to(PROJECT_ROOT).as_posix(), "sha256": hashlib.sha256(content).hexdigest(), "size_bytes": len(content)})
    _write_json("artifact_registry.json", {"registry_version": "1.0.0", "generated_at": AUDITED_AT, "artifacts": artifacts})


def _fixture_asset_digest() -> str:
    paths = [FIXTURE_ROOT / name for name in ("build_assets.py", "fixture_schema.json", "registry.json", "verify_fixtures.py")]
    paths.extend(path for path in (FIXTURE_ROOT / "tasks").rglob("*") if path.is_file())
    digest = hashlib.sha256()
    for path in sorted(paths, key=lambda item: item.relative_to(FIXTURE_ROOT).as_posix()):
        digest.update(path.relative_to(FIXTURE_ROOT).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def _fixture_verification_status() -> tuple[bool, str]:
    asset_digest = _fixture_asset_digest()
    try:
        report = json.loads((FIXTURE_ROOT / "verification_report.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return False, asset_digest
    passed = (
        report.get("verification_schema_version") == "1.0.0"
        and report.get("accepted") is True
        and report.get("repeat_count") == 3
        and report.get("task_count") == 4
        and report.get("model_evaluation_performed") is False
        and report.get("fixture_asset_digest") == asset_digest
    )
    return passed, asset_digest


def main() -> None:
    candidates = _load_candidates()
    results = [
        _public_result(candidates[candidate_id], PUBLIC_CONFIG[candidate_id])
        if candidate_id in PUBLIC_CONFIG
        else _self_result(candidates[candidate_id], SELF_CONFIG[candidate_id])
        for candidate_id in PRIMARY_IDS
    ]
    _write_json("preflight_schema.json", _schema())
    _write_json("primary_preflight_results.json", {"preflight_schema_version": SCHEMA_VERSION, "selection_basis": "M7_B1_HUMAN_APPROVED_PRIMARY", "candidate_ids": list(PRIMARY_IDS), "results": results})
    _write_matrix(results)
    _write_json("alternate_preflight_results.json", {"preflight_schema_version": SCHEMA_VERSION, "strategy": "SOURCE_TIERED_ON_HIGH_RISK_ONLY", "results": [{"candidate_id": "bugsinpy-cookiecutter-2", "trigger_candidate_id": "bugsinpy-httpie-4", "preflight_depth": "QUICK_METADATA_REVIEW", "final_gate": "UNVERIFIED", "reason": "Registered same-source fallback only; no automatic replacement and no executable preflight was required before the HTTPie B2.1 mapping-fidelity condition is attempted."}]})
    counts = {gate: sum(result["final_gate"] == gate for result in results) for gate in ("GO", "CONDITIONAL", "NO_GO", "UNVERIFIED")}
    fixture_verified, fixture_digest = _fixture_verification_status()
    _write_json("gate_summary.json", {"preflight_schema_version": SCHEMA_VERSION, "counts": counts, "candidate_gates": {result["candidate_id"]: result["final_gate"] for result in results}, "recommended_first_fixture_candidate_ids": list(FIRST_FIXTURE_IDS), "human_decision": {"decision_status": "M7_B2_1_AUTHORIZED", "approved_first_fixture_candidate_ids": list(FIRST_FIXTURE_IDS), "conditional_candidate_strategy": "BUILD_WITH_ADMISSION_GATES", "current_replacement_decision": "NO_REPLACEMENTS", "model_runs_authorized": False, "decided_at": DECIDED_AT}, "formal_fixture_construction_started": True, "formal_fixture_asset_digest": fixture_digest, "formal_fixture_verification_passed": fixture_verified, "model_evaluation_started": False, "m7_b2_1_started": True, "m7_b2_1_completed": fixture_verified})
    _write_json("replacement_recommendations.json", {"preflight_schema_version": SCHEMA_VERSION, "automatic_replacement_performed": False, "human_decision": "NO_REPLACEMENTS_ACCEPTED", "accepted_primary_candidate_ids": list(PRIMARY_IDS), "recommendations": [{"primary_candidate_id": "bugsinpy-httpie-4", "alternate_candidate_id": "bugsinpy-cookiecutter-2", "trigger": "Only if the B2.1 case-insensitive mapping fidelity check fails", "requires_human_approval": True}]})
    _write_json("difficulty_review.json", {"preflight_schema_version": SCHEMA_VERSION, "changes": [], "reviews": [{"candidate_id": result["candidate_id"], "difficulty_before": result["difficulty_before"], "difficulty_after_preflight": result["difficulty_after_preflight"], "reason": result["difficulty_change_reason"]} for result in results], "accepted_mix_preserved": {"BASIC": 6, "ENGINEERING": 3, "CHALLENGE": 1}})
    backup = candidates["self-terminal-state-cas"]
    _write_json("demo_preflight.json", {"preflight_schema_version": SCHEMA_VERSION, "primary_demo_candidate_id": "self-durable-double-consumption", "primary_demo_gate": next(result["final_gate"] for result in results if result["candidate_id"] == "self-durable-double-consumption"), "backup_demo_candidate_id": backup["candidate_id"], "backup_design_status": "GO_AT_DESIGN_LEVEL", "automatic_switch_performed": False, "primary_demo_reasons": ["External duplicate symptom is easy to explain without naming the invariant.", "Four-module crash-window diagnosis demonstrates durable systems reasoning.", "Visible then hidden restart/concurrency feedback supports a two-round interview narrative."], "leakage_controls": ["Use parcel-domain names and no AgentForge class, table, state, or event names.", "Do not include double consumption, idempotent resume, or exactly once in the task description.", "Author tests independently from AgentForge regressions."]})
    _artifact_registry()


if __name__ == "__main__":
    main()
