import json
import os
from pathlib import Path
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agentforge.evaluation.costs import PricingSnapshot
from agentforge.evaluation.protocol import canonical_digest
from agentforge.evaluation.study_builder import EvaluationStudyBuild
from agentforge.models.identity import is_exact_or_dated_openai_snapshot


class StudyManifestError(RuntimeError):
    pass


class PrivateStudyManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    study_id: UUID
    definition_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    protocol_digests: tuple[str, str, str, str]
    model_id: str = Field(min_length=1, max_length=200)
    response_model_id: str = Field(default="", max_length=200)
    pricing_snapshot: PricingSnapshot
    manifest_digest: str = Field(default="", pattern=r"^$|^[0-9a-f]{64}$")

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
            raise ValueError(
                "Private Study manifest response model binding is invalid"
            )
        if (
            self.pricing_snapshot.model_id != self.model_id
            or len(set(self.protocol_digests)) != 4
        ):
            raise ValueError("Private Study manifest bindings do not match")
        expected = canonical_digest(
            self.model_dump(mode="json", exclude={"manifest_digest"})
        )
        if self.manifest_digest and self.manifest_digest != expected:
            raise ValueError("Private Study manifest digest does not match")
        object.__setattr__(self, "manifest_digest", expected)
        return self

    @classmethod
    def from_build(
        cls,
        study_id: UUID,
        build: EvaluationStudyBuild,
    ) -> "PrivateStudyManifest":
        return cls(
            study_id=study_id,
            definition_digest=build.definition.definition_digest,
            protocol_digests=build.definition.protocol_digests,
            model_id=build.definition.model_id,
            response_model_id=build.definition.response_model_id,
            pricing_snapshot=build.pricing_snapshot,
        )


def load_private_study_manifest(path: Path) -> PrivateStudyManifest:
    try:
        raw = path.resolve(strict=True).read_text(encoding="utf-8")
        return PrivateStudyManifest.model_validate_json(raw)
    except (OSError, UnicodeError, ValueError) as exc:
        raise StudyManifestError(
            "Private Study manifest is missing or invalid"
        ) from exc


def save_private_study_manifest(
    path: Path,
    manifest: PrivateStudyManifest,
) -> None:
    target = path.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        persisted = load_private_study_manifest(target)
        if persisted != manifest:
            raise StudyManifestError(
                "Private Study manifest identity conflict"
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
        raise StudyManifestError(
            "Private Study manifest could not be saved"
        ) from exc
