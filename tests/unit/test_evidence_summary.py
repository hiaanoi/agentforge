import hashlib
import json
import re
import sys
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

import pytest

from agentforge.domain.enums import (
    EvaluationCampaignStatus,
    EvaluationSlotStatus,
    EvaluationStudyStatus,
)
from agentforge.domain.repair import RepairCompletionStatus
from agentforge.evaluation.evidence_summary import (
    EvidenceBundlePaths,
    EvidenceMismatchError,
    LegacyEvidenceError,
    load_current_report,
    validate_evidence_bundle,
)
from agentforge.evaluation.public_artifacts import PublicArtifactScanner
from agentforge.evaluation.study_models import B2_4_TASK_ORDER
from agentforge.evaluation.study_reports import (
    PublicEvaluationStudyReport,
    PublicPricingFacts,
    PublicProviderSettings,
    PublicScoredRun,
    PublicSlotStudyRecord,
    PublicTaskStudyReport,
    aggregate_study_summary,
    render_evaluation_study_json,
    render_evaluation_study_markdown,
)


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _task(index: int) -> PublicTaskStudyReport:
    run = PublicScoredRun(
        repetition_index=0,
        attempt_number=1,
        final_status=RepairCompletionStatus.VERIFIED_SUCCESS,
        verified_success=True,
        model_calls=1,
        read_calls=1,
        edit_attempts=1,
        test_runs=1,
        wall_time_ms=1,
        total_tokens=1,
    )
    return PublicTaskStudyReport(
        task_id=B2_4_TASK_ORDER[index],
        task_order=index,
        source_category="SELF_BUILT",
        protocol_digest=f"{index + 1:x}" * 64,
        campaign_status=EvaluationCampaignStatus.COMPLETED,
        fixture_asset_digest="a" * 64,
        task_policy_digest="b" * 64,
        system_prompt_digest="c" * 64,
        task_prompt_digest="d" * 64,
        tool_schema_digest="e" * 64,
        scored_slots=1,
        successful_slots=1,
        infrastructure_invalid_slots=0,
        indeterminate_slots=0,
        unexecuted_slots=2,
        scoring_coverage=1 / 3,
        scored_success_rate=1,
        planned_slot_success_rate=1 / 3,
        pass_at_1=1,
        pass_at_3=None,
        first_attempt_success=True,
        majority_success=False,
        stable_success=False,
        any_success_in_3=True,
        logical_model_calls=1,
        physical_model_requests=1,
        retry_count=0,
        read_call_count=1,
        mutation_committed_count=1,
        managed_test_completed_count=1,
        wall_time_ms=1,
        input_tokens=1,
        output_tokens=0,
        total_tokens=1,
        usage_complete=True,
        estimated_cost_complete=True,
        estimated_cost=Decimal("0.1"),
        provider_deviation_count=0,
        normalized_multi_tool_response_count=0,
        mean_logical_model_calls_per_scored_run=1,
        mean_physical_model_requests_per_scored_run=1,
        mean_retries_per_scored_run=0,
        mean_reads_per_scored_run=1,
        mean_edits_per_scored_run=1,
        mean_development_tests_per_scored_run=1,
        median_wall_time_ms=1,
        mean_total_tokens_per_scored_run=1,
        provider_deviation_rate=0,
        multi_tool_normalization_rate=0,
        raw_infrastructure_attempt_count=0,
        replacement_count=0,
        policy_block_rate=0,
        budget_exhaustion_rate=0,
        protocol_error_rate=0,
        visible_test_failure_rate=0,
        hidden_test_failure_rate=0,
        model_quality_failure_distribution={},
        infrastructure_failure_distribution={},
        slots=(
            PublicSlotStudyRecord(
                repetition_index=0,
                slot_status=EvaluationSlotStatus.ACCEPTED,
                attempt_count=1,
                replacement_count=0,
                selected_run=run,
            ),
            PublicSlotStudyRecord(
                repetition_index=1,
                slot_status=EvaluationSlotStatus.PENDING,
                attempt_count=0,
                replacement_count=0,
            ),
            PublicSlotStudyRecord(
                repetition_index=2,
                slot_status=EvaluationSlotStatus.PENDING,
                attempt_count=0,
                replacement_count=0,
            ),
        ),
        scored_runs=(run,),
    )


def _report(*, model_id: str = "test-model") -> PublicEvaluationStudyReport:
    tasks = tuple(_task(index) for index in range(4))
    return PublicEvaluationStudyReport(
        study_id="123e4567-e89b-12d3-a456-426614174000",
        study_status=EvaluationStudyStatus.RUNNING,
        study_definition_digest="f" * 64,
        protocol_digests=tuple(task.protocol_digest for task in tasks),
        model_id=model_id,
        response_model_id="test-response-model",
        git_commit_sha="a" * 40,
        runtime_source_digest="a" * 64,
        pyproject_sha256="b" * 64,
        uv_lock_sha256="c" * 64,
        fixture_registry_digest="d" * 64,
        platform_binding_digest="e" * 64,
        os_family="WINDOWS",
        python_implementation="cpython",
        python_version="3.11.0",
        provider=PublicProviderSettings(
            timeout_seconds=1,
            max_retries=0,
            max_output_tokens=1,
            multi_tool_response_policy="SEQUENTIAL",
            max_function_calls_per_response=1,
            configuration_digest="f" * 64,
        ),
        pricing=PublicPricingFacts(
            pricing_digest="a" * 64,
            effective_date="2026-01-01",
            source_url="https://example.com/pricing",
            input_per_million=Decimal("1"),
            cached_input_per_million=None,
            output_per_million=Decimal("1"),
        ),
        summary=aggregate_study_summary(tasks),
        tasks=tasks,
    )


def _write_bundle(tmp_path: Path, report: PublicEvaluationStudyReport) -> EvidenceBundlePaths:
    scanner = PublicArtifactScanner()
    report_json = render_evaluation_study_json(report, scanner=scanner)
    report_markdown = render_evaluation_study_markdown(report, scanner=scanner)
    json_path = tmp_path / "study_report.json"
    markdown_path = tmp_path / "study_report.md"
    manifest_path = tmp_path / "evidence_manifest.json"
    json_path.write_bytes((report_json + "\n").encode("utf-8"))
    markdown_path.write_bytes(report_markdown.encode("utf-8"))
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "report_digest": report.report_digest,
                "report_json_sha256": _sha256(json_path.read_bytes()),
                "report_markdown_sha256": _sha256(markdown_path.read_bytes()),
            }
        ),
        encoding="utf-8",
    )
    return EvidenceBundlePaths(json_path, markdown_path, manifest_path)


def test_load_rejects_schema_v1_before_model_errors(tmp_path: Path) -> None:
    path = tmp_path / "legacy.json"
    path.write_text('{"report_schema_version": 1}', encoding="utf-8")

    with pytest.raises(LegacyEvidenceError, match="legacy evidence"):
        load_current_report(path)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"report_schema_version": True},
        {"report_schema_version": "2"},
        {"report_schema_version": 3},
    ],
)
def test_load_treats_only_exact_schema_v1_as_legacy(
    tmp_path: Path, payload: dict[str, object]
) -> None:
    path = tmp_path / "report.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(EvidenceMismatchError, match="invalid current report"):
        load_current_report(path)


@pytest.mark.parametrize("content", ["{", "[]", '{"report_schema_version": 2}'])
def test_load_maps_invalid_current_content_to_safe_error(
    tmp_path: Path, content: str
) -> None:
    path = tmp_path / "report.json"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(EvidenceMismatchError, match="invalid current report"):
        load_current_report(path)


@pytest.mark.parametrize("report_digest", [None, ""])
def test_load_requires_a_nonempty_report_digest(
    tmp_path: Path, report_digest: str | None
) -> None:
    payload = _report().model_dump(mode="json")
    if report_digest is None:
        del payload["report_digest"]
    else:
        payload["report_digest"] = report_digest
    path = tmp_path / "report.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(EvidenceMismatchError, match="invalid current report"):
        load_current_report(path)


@pytest.mark.parametrize(
    "field,value",
    [("max_retries", "0"), ("store", "false")],
)
def test_load_rejects_string_coercions_in_current_report(
    tmp_path: Path, field: str, value: str
) -> None:
    payload = _report().model_dump(mode="json")
    payload["provider"][field] = value
    path = tmp_path / "report.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(EvidenceMismatchError, match="invalid current report"):
        load_current_report(path)


def test_bundle_validates_canonical_public_artifacts(tmp_path: Path) -> None:
    report = _report()
    paths = _write_bundle(tmp_path, report)

    assert validate_evidence_bundle(paths) == report


@pytest.mark.parametrize(
    "replacement",
    [lambda canonical: canonical.rstrip(b"\n"), lambda canonical: canonical + b"\n"],
    ids=["missing-final-newline", "multiple-final-newlines"],
)
def test_bundle_rejects_json_without_exactly_one_final_newline(
    tmp_path: Path, replacement: Callable[[bytes], bytes]
) -> None:
    paths = _write_bundle(tmp_path, _report())
    canonical = paths.report_json.read_bytes()
    paths.report_json.write_bytes(replacement(canonical))
    manifest = json.loads(paths.manifest_json.read_text(encoding="utf-8"))
    manifest["report_json_sha256"] = _sha256(paths.report_json.read_bytes())
    paths.manifest_json.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(EvidenceMismatchError, match="report JSON is not canonical"):
        validate_evidence_bundle(paths)


def test_bundle_rejects_json_with_noncanonical_line_ending(tmp_path: Path) -> None:
    paths = _write_bundle(tmp_path, _report())
    paths.report_json.write_bytes(paths.report_json.read_bytes().replace(b"\n", b"\r\n"))
    manifest = json.loads(paths.manifest_json.read_text(encoding="utf-8"))
    manifest["report_json_sha256"] = _sha256(paths.report_json.read_bytes())
    paths.manifest_json.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(EvidenceMismatchError, match="report JSON is not canonical"):
        validate_evidence_bundle(paths)


def test_bundle_rejects_tampered_json_bytes(tmp_path: Path) -> None:
    paths = _write_bundle(tmp_path, _report())
    paths.report_json.write_bytes(paths.report_json.read_bytes() + b" ")

    with pytest.raises(EvidenceMismatchError, match="JSON checksum"):
        validate_evidence_bundle(paths)


def test_bundle_rejects_tampered_report_digest(tmp_path: Path) -> None:
    paths = _write_bundle(tmp_path, _report())
    payload = json.loads(paths.report_json.read_text(encoding="utf-8"))
    payload["report_digest"] = "0" * 64
    paths.report_json.write_bytes(
        json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    )
    manifest = json.loads(paths.manifest_json.read_text(encoding="utf-8"))
    manifest["report_json_sha256"] = _sha256(paths.report_json.read_bytes())
    paths.manifest_json.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(EvidenceMismatchError, match="invalid current report"):
        validate_evidence_bundle(paths)


def test_bundle_rejects_tampered_markdown_bytes_even_with_matching_checksum(
    tmp_path: Path,
) -> None:
    paths = _write_bundle(tmp_path, _report())
    paths.report_markdown.write_bytes(paths.report_markdown.read_bytes() + b"\n")
    manifest = json.loads(paths.manifest_json.read_text(encoding="utf-8"))
    manifest["report_markdown_sha256"] = _sha256(paths.report_markdown.read_bytes())
    paths.manifest_json.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(EvidenceMismatchError, match="canonical Markdown"):
        validate_evidence_bundle(paths)


@pytest.mark.parametrize(
    "replacement",
    [
        lambda canonical: canonical.rstrip(b"\n"),
        lambda canonical: canonical.replace(b"\n", b"\r\n"),
    ],
    ids=["missing-final-newline", "windows-line-ending"],
)
def test_bundle_rejects_noncanonical_markdown_newlines(
    tmp_path: Path, replacement: Callable[[bytes], bytes]
) -> None:
    paths = _write_bundle(tmp_path, _report())
    paths.report_markdown.write_bytes(replacement(paths.report_markdown.read_bytes()))
    manifest = json.loads(paths.manifest_json.read_text(encoding="utf-8"))
    manifest["report_markdown_sha256"] = _sha256(paths.report_markdown.read_bytes())
    paths.manifest_json.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(EvidenceMismatchError, match="canonical Markdown"):
        validate_evidence_bundle(paths)


def test_bundle_rejects_manifest_digest_mismatch(tmp_path: Path) -> None:
    paths = _write_bundle(tmp_path, _report())
    manifest = json.loads(paths.manifest_json.read_text(encoding="utf-8"))
    manifest["report_digest"] = "0" * 64
    paths.manifest_json.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(EvidenceMismatchError, match="manifest digest"):
        validate_evidence_bundle(paths)


def test_bundle_rejects_manifest_boolean_schema_version(tmp_path: Path) -> None:
    paths = _write_bundle(tmp_path, _report())
    manifest = json.loads(paths.manifest_json.read_text(encoding="utf-8"))
    manifest["schema_version"] = True
    paths.manifest_json.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(EvidenceMismatchError, match="invalid evidence manifest"):
        validate_evidence_bundle(paths)


def test_load_rejects_contradictory_schema_v2_facts(tmp_path: Path) -> None:
    payload = _report().model_dump(mode="json")
    payload["tasks"][0]["pass_at_1"] = 0
    path = tmp_path / "report.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(EvidenceMismatchError, match="invalid current report"):
        load_current_report(path)


def test_load_scans_public_content(tmp_path: Path) -> None:
    report = _report(model_id="/private/model")
    path = tmp_path / "report.json"
    path.write_text(
        json.dumps(
            report.model_dump(mode="json"),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )

    with pytest.raises(EvidenceMismatchError, match="public content"):
        load_current_report(path)


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_load_and_bundle_reject_nonstandard_json_constants(
    tmp_path: Path, constant: str
) -> None:
    paths = _write_bundle(tmp_path, _report())
    report_json = paths.report_json.read_bytes()
    token = b'"timeout_seconds":1.0'
    assert token in report_json
    paths.report_json.write_bytes(
        report_json.replace(token, b'"timeout_seconds":' + constant.encode())
    )
    manifest = json.loads(paths.manifest_json.read_text(encoding="utf-8"))
    manifest["report_json_sha256"] = _sha256(paths.report_json.read_bytes())
    paths.manifest_json.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(EvidenceMismatchError, match="invalid current report"):
        load_current_report(paths.report_json)
    with pytest.raises(EvidenceMismatchError, match="invalid current report"):
        validate_evidence_bundle(paths)


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_bundle_rejects_nonstandard_json_constants_in_manifest(
    tmp_path: Path, constant: str
) -> None:
    paths = _write_bundle(tmp_path, _report())
    paths.manifest_json.write_bytes(
        (
            "{"
            f'"schema_version":{constant},'
            '"report_digest":"a"'
            "}"
        ).encode()
    )

    with pytest.raises(EvidenceMismatchError, match="invalid evidence manifest"):
        validate_evidence_bundle(paths)


def test_load_hides_overlong_json_integer_errors(tmp_path: Path) -> None:
    path = tmp_path / "report.json"
    path.write_text(
        '{"report_schema_version":2,"oversized":' + "9" * 5_000 + "}",
        encoding="utf-8",
    )

    with pytest.raises(EvidenceMismatchError, match="invalid current report"):
        load_current_report(path)


def _deep_json_array() -> str:
    depth = sys.getrecursionlimit()
    while depth <= 1_000_000:
        candidate = "[" * depth + "0" + "]" * depth
        try:
            json.loads(candidate)
        except RecursionError:
            return candidate
        depth *= 2
    pytest.fail("JSON decoder did not reject a safe nested-array fixture")


def test_load_hides_deeply_nested_json_errors(tmp_path: Path) -> None:
    path = tmp_path / "report.json"
    path.write_text(
        '{"report_schema_version":2,"nested":' + _deep_json_array() + "}",
        encoding="utf-8",
    )

    with pytest.raises(EvidenceMismatchError, match="invalid current report"):
        load_current_report(path)


def test_bundle_hides_deeply_nested_manifest_json_errors(tmp_path: Path) -> None:
    paths = _write_bundle(tmp_path, _report())
    paths.manifest_json.write_text(
        '{"schema_version":1,"nested":' + _deep_json_array() + "}",
        encoding="utf-8",
    )

    with pytest.raises(EvidenceMismatchError, match="invalid evidence manifest"):
        validate_evidence_bundle(paths)


def test_renderer_rejects_model_constructed_nonfinite_public_float() -> None:
    report = _report()
    unsafe_provider = report.provider.model_copy(
        update={"timeout_seconds": float("nan")}
    )
    unsafe_report = report.model_copy(update={"provider": unsafe_provider})

    with pytest.raises(ValueError, match="Out of range float values"):
        render_evaluation_study_json(
            unsafe_report,
            scanner=PublicArtifactScanner(),
        )


@pytest.mark.parametrize("path_name,content", [("missing.json", None), ("bad.json", b"\x80")])
def test_load_hides_missing_and_invalid_utf8_details(
    tmp_path: Path, path_name: str, content: bytes | None
) -> None:
    path = tmp_path / path_name
    if content is not None:
        path.write_bytes(content)

    with pytest.raises(EvidenceMismatchError, match="invalid current report"):
        load_current_report(path)


def test_legacy_directory_list_matches_result_directories() -> None:
    results = Path(__file__).parents[2] / "evaluation" / "results"
    listed = set(
        re.findall(
            r"^- `([^`]+)`$",
            (results / "LEGACY.md").read_text(encoding="utf-8"),
            flags=re.MULTILINE,
        )
    )
    directories = {path.name for path in results.iterdir() if path.is_dir()}

    assert listed
    assert directories <= listed
