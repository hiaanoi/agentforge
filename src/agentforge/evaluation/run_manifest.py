import json
import os
from pathlib import Path
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agentforge.evaluation.protocol import canonical_digest
from agentforge.evaluation.source_provenance import SourceProvenance
from agentforge.evaluation.study_builder import EvaluationStudyBuild
from agentforge.models.identity import is_exact_or_dated_openai_snapshot

_SHA256_PATTERN = r"^[0-9a-f]{64}$"


class EvaluationRunManifestError(RuntimeError):
    pass


class EvaluationRunManifest(BaseModel):
    """Immutable, machine-readable identity for one evaluation Study."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    study_id: UUID
    definition_digest: str = Field(pattern=_SHA256_PATTERN)
    protocol_digests: tuple[str, str, str, str]
    model_id: str = Field(min_length=1, max_length=200)
    response_model_id: str = Field(default="", max_length=200)
    pricing_digest: str = Field(pattern=_SHA256_PATTERN)
    fixture_registry_digest: str = Field(pattern=_SHA256_PATTERN)
    platform_binding_digest: str = Field(pattern=_SHA256_PATTERN)
    git_commit_sha: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    runtime_source_digest: str = Field(pattern=_SHA256_PATTERN)
    pyproject_sha256: str = Field(pattern=_SHA256_PATTERN)
    uv_lock_sha256: str = Field(pattern=_SHA256_PATTERN)
    manifest_digest: str = Field(default="", pattern=r"^$|" + _SHA256_PATTERN)

    @model_validator(mode="after")
    def validate_manifest(self) -> Self:
        object.__setattr__(
            self,
            "response_model_id",
            self.response_model_id or self.model_id,
        )
        if not is_exact_or_dated_openai_snapshot(
            self.model_id,
            self.response_model_id,
        ):
            raise ValueError("Evaluation manifest model binding is invalid")
        if len(set(self.protocol_digests)) != 4:
            raise ValueError("Evaluation manifest requires four Protocols")
        if any(
            len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
            for digest in self.protocol_digests
        ):
            raise ValueError("Evaluation manifest Protocol digests are invalid")
        expected = canonical_digest(
            self.model_dump(mode="json", exclude={"manifest_digest"})
        )
        if self.manifest_digest and self.manifest_digest != expected:
            raise ValueError("Evaluation manifest digest does not match facts")
        object.__setattr__(self, "manifest_digest", expected)
        return self

    @classmethod
    def from_build(
        cls,
        study_id: UUID,
        build: EvaluationStudyBuild,
        source: SourceProvenance,
    ) -> "EvaluationRunManifest":
        definition = build.definition
        return cls(
            study_id=study_id,
            definition_digest=definition.definition_digest,
            protocol_digests=definition.protocol_digests,
            model_id=definition.model_id,
            response_model_id=definition.response_model_id,
            pricing_digest=build.pricing_snapshot.pricing_digest,
            fixture_registry_digest=definition.fixture_registry_digest,
            platform_binding_digest=definition.platform_binding_digest,
            git_commit_sha=source.git_commit_sha,
            runtime_source_digest=source.runtime_source_digest,
            pyproject_sha256=source.pyproject_sha256,
            uv_lock_sha256=source.uv_lock_sha256,
        )


def load_evaluation_run_manifest(path: Path) -> EvaluationRunManifest:
    try:
        raw = path.resolve(strict=True).read_text(encoding="utf-8")
        return EvaluationRunManifest.model_validate_json(raw)
    except (OSError, UnicodeError, ValueError) as exc:
        raise EvaluationRunManifestError(
            "Evaluation Run Manifest is missing or invalid"
        ) from exc


def save_evaluation_run_manifest(
    path: Path,
    manifest: EvaluationRunManifest,
) -> None:
    target = path.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        persisted = load_evaluation_run_manifest(target)
        if persisted != manifest:
            raise EvaluationRunManifestError(
                "Evaluation Run Manifest identity conflict"
            )
        return
    temporary = target.with_name(f".{target.name}.tmp")
    payload = json.dumps(
        manifest.model_dump(mode="json"),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    try:
        temporary.write_text(payload + "\n", encoding="utf-8", newline="\n")
        os.replace(temporary, target)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise EvaluationRunManifestError(
            "Evaluation Run Manifest could not be saved"
        ) from exc
