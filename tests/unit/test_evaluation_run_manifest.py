from pathlib import Path
from uuid import uuid4

import pytest

from agentforge.evaluation.run_manifest import (
    EvaluationRunManifest,
    EvaluationRunManifestError,
    load_evaluation_run_manifest,
    save_evaluation_run_manifest,
)


def manifest() -> EvaluationRunManifest:
    return EvaluationRunManifest(
        study_id=uuid4(),
        definition_digest="a" * 64,
        protocol_digests=("b" * 64, "c" * 64, "d" * 64, "e" * 64),
        model_id="gpt-test-exact",
        response_model_id="gpt-test-exact",
        pricing_digest="f" * 64,
        fixture_registry_digest="1" * 64,
        platform_binding_digest="2" * 64,
        git_commit_sha="3" * 40,
        runtime_source_digest="4" * 64,
        pyproject_sha256="5" * 64,
        uv_lock_sha256="6" * 64,
    )


def test_manifest_digest_is_deterministic_and_secret_free() -> None:
    first = manifest()
    second = EvaluationRunManifest.model_validate(first.model_dump(mode="json"))

    assert first == second
    assert len(first.manifest_digest) == 64
    assert "api_key" not in first.model_dump_json().casefold()


def test_manifest_save_is_idempotent_and_loads(tmp_path: Path) -> None:
    path = tmp_path / "run_manifest.json"
    value = manifest()

    save_evaluation_run_manifest(path, value)
    save_evaluation_run_manifest(path, value)

    assert load_evaluation_run_manifest(path) == value


def test_manifest_save_rejects_identity_conflict(tmp_path: Path) -> None:
    path = tmp_path / "run_manifest.json"
    save_evaluation_run_manifest(path, manifest())
    changed = manifest().model_copy(update={"study_id": uuid4()})

    with pytest.raises(EvaluationRunManifestError, match="identity conflict"):
        save_evaluation_run_manifest(path, changed)

