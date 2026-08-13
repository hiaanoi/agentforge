"""Validation for schema-v2 public evaluation evidence bundles."""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agentforge.evaluation.public_artifacts import (
    ForbiddenPublicArtifactError,
    PublicArtifactScanner,
)
from agentforge.evaluation.study_reports import (
    PublicEvaluationStudyReport,
    render_evaluation_study_json,
    render_evaluation_study_markdown,
)

_SHA256_PATTERN = r"^[0-9a-f]{64}$"


class LegacyEvidenceError(RuntimeError):
    """Raised when an artifact is not current schema-v2 evidence."""


class EvidenceMismatchError(RuntimeError):
    """Raised when current evidence is malformed or internally inconsistent."""


@dataclass(frozen=True)
class EvidenceBundlePaths:
    report_json: Path
    report_markdown: Path
    manifest_json: Path


class EvidenceBundleManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    report_digest: str = Field(pattern=_SHA256_PATTERN)
    report_json_sha256: str = Field(pattern=_SHA256_PATTERN)
    report_markdown_sha256: str = Field(pattern=_SHA256_PATTERN)


def load_current_report(path: Path) -> PublicEvaluationStudyReport:
    """Load a current public report without interpreting legacy artifacts."""
    try:
        raw = path.read_bytes().decode("utf-8")
        payload: Any = _parse_standard_json(raw)
    except (OSError, UnicodeDecodeError, ValueError, RecursionError) as error:
        raise EvidenceMismatchError("invalid current report") from error

    if not isinstance(payload, dict):
        raise EvidenceMismatchError("invalid current report")
    version = payload.get("report_schema_version")
    if type(version) is int and version == 1:
        raise LegacyEvidenceError("legacy evidence is not a schema-v2 report")
    if type(version) is not int or version != 2:
        raise EvidenceMismatchError("invalid current report")
    if not isinstance(payload.get("report_digest"), str) or not payload[
        "report_digest"
    ]:
        raise EvidenceMismatchError("invalid current report")

    try:
        PublicArtifactScanner().validate(raw)
    except ForbiddenPublicArtifactError as error:
        raise EvidenceMismatchError("unsafe public content") from error
    try:
        return PublicEvaluationStudyReport.model_validate_json(raw, strict=True)
    except ValidationError as error:
        raise EvidenceMismatchError("invalid current report") from error


def validate_evidence_bundle(
    paths: EvidenceBundlePaths,
) -> PublicEvaluationStudyReport:
    """Return a report only when every public artifact matches its manifest."""
    report = load_current_report(paths.report_json)
    manifest = _load_manifest(paths.manifest_json)
    if manifest.report_digest != report.report_digest:
        raise EvidenceMismatchError("manifest digest does not match report")

    report_json = _read_bytes(paths.report_json, "report JSON")
    report_markdown = _read_bytes(paths.report_markdown, "report Markdown")
    if _sha256(report_json) != manifest.report_json_sha256:
        raise EvidenceMismatchError("JSON checksum does not match manifest")
    if _sha256(report_markdown) != manifest.report_markdown_sha256:
        raise EvidenceMismatchError("Markdown checksum does not match manifest")

    try:
        markdown_text = report_markdown.decode("utf-8")
        scanner = PublicArtifactScanner()
        scanner.validate(markdown_text)
        expected_json = render_evaluation_study_json(report, scanner=scanner)
        expected_markdown = render_evaluation_study_markdown(
            report,
            scanner=scanner,
        )
    except (UnicodeDecodeError, ForbiddenPublicArtifactError, ValueError) as error:
        raise EvidenceMismatchError("unsafe or invalid public content") from error

    if report_json != (expected_json + "\n").encode("utf-8"):
        raise EvidenceMismatchError("report JSON is not canonical")
    if report_markdown != expected_markdown.encode("utf-8"):
        raise EvidenceMismatchError("report Markdown is not canonical Markdown")
    return report


def _load_manifest(path: Path) -> EvidenceBundleManifest:
    try:
        raw = path.read_bytes().decode("utf-8")
        payload: Any = _parse_standard_json(raw)
        if not isinstance(payload, dict):
            raise ValueError("manifest must be an object")
        if type(payload.get("schema_version")) is not int or (
            payload["schema_version"] != 1
        ):
            raise ValueError("manifest schema version is invalid")
        return EvidenceBundleManifest.model_validate_json(raw, strict=True)
    except (
        OSError,
        UnicodeDecodeError,
        ValidationError,
        ValueError,
        RecursionError,
    ) as error:
        raise EvidenceMismatchError("invalid evidence manifest") from error


def _read_bytes(path: Path, label: str) -> bytes:
    try:
        return path.read_bytes()
    except OSError as error:
        raise EvidenceMismatchError(f"unable to read {label}") from error


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _parse_standard_json(raw: str) -> Any:
    return json.loads(raw, parse_constant=_reject_nonstandard_json_constant)


def _reject_nonstandard_json_constant(_constant: str) -> None:
    raise ValueError("nonstandard JSON constant")
