from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError
from test_evaluation_provider_factory import protocol
from test_evaluation_study_models import SHA, definition

from agentforge.evaluation.protocol import canonical_digest
from agentforge.evaluation.real_model_gate import (
    RealModelAuthorizationError,
    RealModelExecutionGate,
)
from agentforge.evaluation.source_provenance import SourceProvenance
from agentforge.evaluation.study_models import (
    B2_4_TASK_ORDER,
    EvaluationStudy,
    EvaluationStudyDefinition,
    RealModelAuthorization,
    validate_real_model_authorization,
)


def authorization() -> RealModelAuthorization:
    value = definition()
    return RealModelAuthorization(
        study_definition_digest=value.definition_digest,
        protocol_digests=value.protocol_digests,
        model_id=value.model_id,
        network_access_acknowledged=True,
    )


def test_authorization_digest_is_deterministic_and_secret_free() -> None:
    first = authorization()
    repeated = authorization()

    assert first == repeated
    assert first.authorization_digest == repeated.authorization_digest
    serialized = first.model_dump_json().casefold()
    assert "api_key" not in serialized
    assert "token" not in serialized


@pytest.mark.parametrize(
    "updates",
    [
        {"study_definition_digest": "f" * 64},
        {
            "protocol_digests": (
                "1" * 64,
                "2" * 64,
                "3" * 64,
                "f" * 64,
            )
        },
        {
            "model_id": "other-model",
            "response_model_id": "other-model",
        },
    ],
)
def test_authorization_rejects_cross_binding(
    updates: dict[str, object],
) -> None:
    value = definition()
    auth_data = authorization().model_dump(mode="json")
    auth_data.update(updates)
    auth_data["authorization_digest"] = ""
    changed = RealModelAuthorization.model_validate(auth_data)

    with pytest.raises(ValueError, match="authorization"):
        validate_real_model_authorization(value, changed)


def test_authorization_requires_fixed_limits_and_network_acknowledgement() -> None:
    data = authorization().model_dump(mode="json")
    data.update(
        {
            "maximum_planned_slots": 13,
            "network_access_acknowledged": False,
            "authorization_digest": "",
        }
    )

    with pytest.raises(ValidationError):
        RealModelAuthorization.model_validate(data)


def test_authorized_study_must_bind_exact_authorization_digest() -> None:
    value = definition()
    auth = authorization()
    study = EvaluationStudy(
        definition_digest=value.definition_digest,
        authorization_digest=auth.authorization_digest,
        status="AUTHORIZED",
    )

    validate_real_model_authorization(value, auth, study=study)
    with pytest.raises(ValueError, match="authorization"):
        validate_real_model_authorization(
            value,
            auth,
            study=study.model_copy(update={"authorization_digest": SHA}),
        )


class _SourceCollector:
    def __init__(self, value: SourceProvenance) -> None:
        self.value = value
        self.calls = 0

    def collect(self, repository_root: Path) -> SourceProvenance:
        assert repository_root.is_dir()
        self.calls += 1
        return self.value


def gate_facts(
    tmp_path: Path,
    *,
    environment: dict[str, str] | None = None,
    source: SourceProvenance | None = None,
) -> tuple[
    RealModelExecutionGate,
    EvaluationStudy,
    tuple,
    EvaluationStudyDefinition,
]:
    protocols = tuple(
        protocol(real=True, model_id="gpt-test", task_id=task_id)
        for task_id in B2_4_TASK_ORDER
    )
    provenance = source or SourceProvenance(
        git_commit_sha="b" * 40,
        git_worktree_clean=True,
        runtime_source_digest="c" * 64,
        pyproject_sha256="d" * 64,
        uv_lock_sha256="e" * 64,
    )
    value = EvaluationStudyDefinition(
        study_name="gate-test",
        task_ids=B2_4_TASK_ORDER,
        protocol_digests=tuple(
            item.protocol_digest for item in protocols
        ),
        provider_configuration_digest=(
            protocols[0].provider_binding.configuration_digest
        ),
        model_id="gpt-test",
        runtime_source_digest=provenance.runtime_source_digest,
        git_commit_sha=provenance.git_commit_sha,
        git_worktree_clean=True,
        pyproject_sha256=provenance.pyproject_sha256,
        uv_lock_sha256=provenance.uv_lock_sha256,
        fixture_registry_digest=protocols[0].fixture_registry_digest,
        platform_binding_digest=canonical_digest(
            protocols[0].platform_binding
        ),
        pricing_snapshot_digest="f" * 64,
    )
    auth = RealModelAuthorization(
        study_definition_digest=value.definition_digest,
        protocol_digests=value.protocol_digests,
        model_id=value.model_id,
        network_access_acknowledged=True,
    )
    study = EvaluationStudy(
        definition_digest=value.definition_digest,
        authorization_digest=auth.authorization_digest,
        status="AUTHORIZED",
    )
    collector = _SourceCollector(provenance)
    gate = RealModelExecutionGate(
        value,
        auth,
        tmp_path,
        source_collector=collector,
        environment=(
            environment
            if environment is not None
            else {"RUN_REAL_MODEL_PILOT": "1"}
        ),
    )
    return gate, study, protocols, value


def test_real_model_gate_validates_every_bound_fact(tmp_path: Path) -> None:
    gate, study, protocols, value = gate_facts(tmp_path)

    gate.validate(
        study,
        protocols,
        operator_confirmation_digest=value.definition_digest,
        api_key=SecretStr("runtime-only-secret"),
    )
    for item in protocols:
        gate.require_protocol(item)

    assert gate.validated_study_id == study.study_id


@pytest.mark.parametrize(
    ("environment", "confirmation", "secret", "message"),
    [
        ({}, "definition", "secret", "opt-in"),
        (
            {"RUN_REAL_MODEL_PILOT": "true"},
            "definition",
            "secret",
            "opt-in",
        ),
        (
            {"RUN_REAL_MODEL_PILOT": "1"},
            "wrong",
            "secret",
            "confirmation",
        ),
        (
            {"RUN_REAL_MODEL_PILOT": "1"},
            "definition",
            "",
            "secret",
        ),
    ],
)
def test_real_model_gate_rejects_missing_operator_inputs(
    tmp_path: Path,
    environment: dict[str, str],
    confirmation: str,
    secret: str,
    message: str,
) -> None:
    gate, study, protocols, value = gate_facts(
        tmp_path,
        environment=environment,
    )
    digest = (
        value.definition_digest
        if confirmation == "definition"
        else confirmation
    )

    with pytest.raises(RealModelAuthorizationError, match=message):
        gate.validate(
            study,
            protocols,
            operator_confirmation_digest=digest,
            api_key=SecretStr(secret),
        )


def test_real_model_gate_rejects_protocol_or_study_drift(
    tmp_path: Path,
) -> None:
    gate, study, protocols, value = gate_facts(tmp_path)

    with pytest.raises(RealModelAuthorizationError, match="Protocol"):
        gate.validate(
            study,
            tuple(reversed(protocols)),
            operator_confirmation_digest=value.definition_digest,
            api_key=SecretStr("secret"),
        )
    with pytest.raises(RealModelAuthorizationError, match="Study"):
        gate.validate(
            study.model_copy(update={"definition_digest": "0" * 64}),
            protocols,
            operator_confirmation_digest=value.definition_digest,
            api_key=SecretStr("secret"),
        )


def test_real_model_gate_rejects_source_drift_and_rechecks_before_provider(
    tmp_path: Path,
) -> None:
    gate, study, protocols, value = gate_facts(tmp_path)
    gate.validate(
        study,
        protocols,
        operator_confirmation_digest=value.definition_digest,
        api_key=SecretStr("secret"),
    )
    gate._source_collector.value = gate._source_collector.value.model_copy(
        update={"git_worktree_clean": False}
    )

    with pytest.raises(RealModelAuthorizationError, match="source"):
        gate.require_protocol(protocols[0])


def test_gate_error_never_contains_secret(tmp_path: Path) -> None:
    gate, study, protocols, _ = gate_facts(tmp_path)
    secret = "do-not-leak-this-value"

    with pytest.raises(RealModelAuthorizationError) as raised:
        gate.validate(
            study,
            protocols,
            operator_confirmation_digest="wrong",
            api_key=SecretStr(secret),
        )

    assert secret not in str(raised.value)
