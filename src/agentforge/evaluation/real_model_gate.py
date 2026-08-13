import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Protocol
from uuid import UUID

from pydantic import SecretStr

from agentforge.domain.enums import EvaluationStudyStatus
from agentforge.evaluation.protocol import (
    EvaluationExecutionMode,
    EvaluationProtocol,
    canonical_digest,
)
from agentforge.evaluation.source_provenance import (
    SourceProvenance,
    SourceProvenanceCollector,
    SourceProvenanceError,
)
from agentforge.evaluation.study_models import (
    B2_4_TASK_ORDER,
    EvaluationStudy,
    EvaluationStudyDefinition,
    RealModelAuthorization,
    validate_real_model_authorization,
)


class RealModelAuthorizationError(RuntimeError):
    pass


class SourceProvenanceReader(Protocol):
    def collect(self, repository_root: Path) -> SourceProvenance: ...


class RealModelExecutionGate:
    def __init__(
        self,
        definition: EvaluationStudyDefinition,
        authorization: RealModelAuthorization,
        repository_root: Path,
        *,
        source_collector: SourceProvenanceReader | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        try:
            root = repository_root.resolve(strict=True)
        except OSError as exc:
            raise RealModelAuthorizationError(
                "Bound source repository is unavailable"
            ) from exc
        if not root.is_dir():
            raise RealModelAuthorizationError(
                "Bound source repository is invalid"
            )
        self._definition = definition
        self._authorization = authorization
        self._repository_root = root
        self._source_collector = (
            source_collector or SourceProvenanceCollector()
        )
        self._environment = environment if environment is not None else os.environ
        self._validated_study_id: UUID | None = None
        self._validated_protocol_digests: frozenset[str] = frozenset()

    @property
    def validated_study_id(self) -> UUID | None:
        return self._validated_study_id

    def validate(
        self,
        study: EvaluationStudy,
        protocols: Sequence[EvaluationProtocol],
        *,
        operator_confirmation_digest: str,
        api_key: SecretStr,
    ) -> None:
        self._require_operator_inputs(
            operator_confirmation_digest=operator_confirmation_digest,
            api_key=api_key,
        )
        self._validate_study(study)
        validated_protocols = self._validate_protocols(protocols)
        self._validate_current_source()

        protocol_digests = frozenset(
            protocol.protocol_digest for protocol in validated_protocols
        )
        if self._validated_study_id is not None and (
            self._validated_study_id != study.study_id
            or self._validated_protocol_digests != protocol_digests
        ):
            raise RealModelAuthorizationError(
                "Execution gate is already bound to another Study"
            )
        self._validated_study_id = study.study_id
        self._validated_protocol_digests = protocol_digests

    def require_protocol(self, protocol: EvaluationProtocol) -> None:
        if self._validated_study_id is None:
            raise RealModelAuthorizationError(
                "Real-model execution gate has not been validated"
            )
        if self._environment.get("RUN_REAL_MODEL_PILOT") != "1":
            raise RealModelAuthorizationError(
                "Real-model execution opt-in is not active"
            )
        try:
            validated = EvaluationProtocol.model_validate(
                protocol.model_dump(mode="json")
            )
        except ValueError as exc:
            raise RealModelAuthorizationError(
                "Provider Protocol failed integrity validation"
            ) from exc
        if (
            validated.protocol_digest
            not in self._validated_protocol_digests
            or validated.protocol_digest
            not in self._authorization.protocol_digests
            or validated.provider_binding.configuration_digest
            != self._definition.provider_configuration_digest
            or validated.provider_binding.model_id != self._definition.model_id
            or validated.provider_binding.response_model_id
            != self._definition.response_model_id
        ):
            raise RealModelAuthorizationError(
                "Provider Protocol is outside the authorized Study"
            )
        self._validate_current_source()

    def _require_operator_inputs(
        self,
        *,
        operator_confirmation_digest: str,
        api_key: SecretStr,
    ) -> None:
        if self._environment.get("RUN_REAL_MODEL_PILOT") != "1":
            raise RealModelAuthorizationError(
                "Real-model execution opt-in must equal 1"
            )
        if (
            operator_confirmation_digest
            != self._definition.definition_digest
        ):
            raise RealModelAuthorizationError(
                "Operator confirmation digest does not match the Study"
            )
        if not api_key.get_secret_value().strip():
            raise RealModelAuthorizationError(
                "Real-model runtime secret is missing"
            )

    def _validate_study(self, study: EvaluationStudy) -> None:
        if study.status is EvaluationStudyStatus.DRAFT:
            raise RealModelAuthorizationError(
                "Study is not authorized for execution"
            )
        try:
            validate_real_model_authorization(
                self._definition,
                self._authorization,
                study=study,
            )
        except ValueError as exc:
            raise RealModelAuthorizationError(
                "Study authorization binding is invalid"
            ) from exc

    def _validate_protocols(
        self,
        protocols: Sequence[EvaluationProtocol],
    ) -> tuple[
        EvaluationProtocol,
        EvaluationProtocol,
        EvaluationProtocol,
        EvaluationProtocol,
    ]:
        if len(protocols) != 4:
            raise RealModelAuthorizationError(
                "Protocol set must contain exactly four entries"
            )
        try:
            validated = tuple(
                EvaluationProtocol.model_validate(
                    protocol.model_dump(mode="json")
                )
                for protocol in protocols
            )
        except ValueError as exc:
            raise RealModelAuthorizationError(
                "Protocol set failed integrity validation"
            ) from exc
        typed = (
            validated[0],
            validated[1],
            validated[2],
            validated[3],
        )
        if (
            tuple(protocol.task_id for protocol in typed) != B2_4_TASK_ORDER
            or tuple(protocol.protocol_digest for protocol in typed)
            != self._definition.protocol_digests
            or tuple(protocol.protocol_digest for protocol in typed)
            != self._authorization.protocol_digests
        ):
            raise RealModelAuthorizationError(
                "Protocol order or digest binding does not match the Study"
            )
        for protocol in typed:
            if (
                protocol.execution_mode
                is not EvaluationExecutionMode.REAL_MODEL
                or not protocol.real_model_authorized
                or protocol.provider_binding.provider != "openai"
                or protocol.provider_binding.model_id
                != self._definition.model_id
                or protocol.provider_binding.response_model_id
                != self._definition.response_model_id
                or protocol.provider_binding.configuration_digest
                != self._definition.provider_configuration_digest
                or protocol.fixture_registry_digest
                != self._definition.fixture_registry_digest
                or canonical_digest(protocol.platform_binding)
                != self._definition.platform_binding_digest
            ):
                raise RealModelAuthorizationError(
                    "Protocol facts do not match the authorized Study"
                )
        return typed

    def _validate_current_source(self) -> None:
        try:
            current = self._source_collector.collect(
                self._repository_root
            )
        except (OSError, SourceProvenanceError, ValueError) as exc:
            raise RealModelAuthorizationError(
                "Bound source provenance could not be verified"
            ) from exc
        expected = self._definition
        if (
            not current.git_worktree_clean
            or current.git_commit_sha != expected.git_commit_sha
            or current.runtime_source_digest
            != expected.runtime_source_digest
            or current.pyproject_sha256 != expected.pyproject_sha256
            or current.uv_lock_sha256 != expected.uv_lock_sha256
        ):
            raise RealModelAuthorizationError(
                "Bound source provenance has drifted"
            )
