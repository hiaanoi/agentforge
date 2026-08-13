from pathlib import Path
from typing import Self, cast

from pydantic import BaseModel, ConfigDict, model_validator

from agentforge.domain.enums import MultiToolResponsePolicy
from agentforge.domain.repair import CompletionCorrectionMode
from agentforge.evaluation.costs import PricingSnapshot
from agentforge.evaluation.formal_fixtures import (
    FormalFixtureLoader,
    FormalFixtureManifest,
)
from agentforge.evaluation.pilot_factory import (
    PilotManifestFacts,
    PilotRuntimeFactory,
)
from agentforge.evaluation.protocol import (
    ContextPolicyBinding,
    EvaluationExecutionMode,
    EvaluationProtocol,
    ModelBudgetBinding,
    PlatformBinding,
    ProviderBinding,
    ReplacementPolicy,
    canonical_digest,
)
from agentforge.evaluation.source_provenance import SourceProvenance
from agentforge.evaluation.study_models import (
    B2_4_TASK_ORDER,
    EvaluationStudyDefinition,
)

B2_4_STUDY_NAME = "AgentForge M7-B2.4 Real Model Portfolio Pilot"
B2_4_REPLACEABLE_FAILURES: tuple[str, ...] = (
    "BASELINE_LAUNCH_FAILURE",
    "BASELINE_TIMEOUT",
    "MODEL_PROVIDER_ERROR",
    "MODEL_RATE_LIMITED",
    "MODEL_TIMEOUT",
    "MODEL_TRANSPORT_ERROR",
    "PILOT_PREPARATION_ERROR",
    "PILOT_RECOVERY_INTERRUPTED",
    "WORKSPACE_PREPARATION_ERROR",
)

_BASIC_MODEL_BUDGET = {
    "max_model_requests": 12,
    "max_total_input_tokens": 50_000,
    "max_total_output_tokens": 10_000,
    "max_total_tokens": 60_000,
}
_ENGINEERING_MODEL_BUDGET = {
    "max_model_requests": 20,
    "max_total_input_tokens": 85_000,
    "max_total_output_tokens": 15_000,
    "max_total_tokens": 100_000,
}


class StudyBuildError(RuntimeError):
    pass


class EvaluationStudyBuild(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    definition: EvaluationStudyDefinition
    protocols: tuple[
        EvaluationProtocol,
        EvaluationProtocol,
        EvaluationProtocol,
        EvaluationProtocol,
    ]
    pricing_snapshot: PricingSnapshot

    @model_validator(mode="after")
    def validate_bindings(self) -> Self:
        if (
            tuple(protocol.task_id for protocol in self.protocols)
            != self.definition.task_ids
            or tuple(
                protocol.protocol_digest for protocol in self.protocols
            )
            != self.definition.protocol_digests
        ):
            raise ValueError("Study build Protocol bindings do not match")
        if (
            self.pricing_snapshot.model_id != self.definition.model_id
            or self.pricing_snapshot.pricing_digest
            != self.definition.pricing_snapshot_digest
        ):
            raise ValueError("Study build pricing binding does not match")
        return self


class EvaluationStudyBuilder:
    def __init__(
        self,
        runtime_factory: PilotRuntimeFactory,
        fixture_root: Path,
    ) -> None:
        try:
            root = fixture_root.resolve(strict=True)
        except OSError as exc:
            raise StudyBuildError("Fixture registry is unavailable") from exc
        task_root = root / "tasks"
        if not root.is_dir() or not task_root.is_dir():
            raise StudyBuildError("Fixture registry layout is invalid")
        self._runtime_factory = runtime_factory
        self._fixture_root = root
        self._task_root = task_root
        self._loader = FormalFixtureLoader()

    def build(
        self,
        *,
        model_id: str,
        response_model_id: str | None = None,
        source_provenance: SourceProvenance,
        pricing_snapshot: PricingSnapshot,
        study_name: str = B2_4_STUDY_NAME,
        execution_mode: EvaluationExecutionMode = (
            EvaluationExecutionMode.REAL_MODEL
        ),
    ) -> EvaluationStudyBuild:
        normalized_model_id = model_id.strip()
        if not normalized_model_id or normalized_model_id != model_id:
            raise StudyBuildError("Model ID must be an exact non-empty value")
        normalized_response_model_id = (
            response_model_id.strip()
            if response_model_id is not None
            else normalized_model_id
        )
        if (
            not normalized_response_model_id
            or (
                response_model_id is not None
                and normalized_response_model_id != response_model_id
            )
        ):
            raise StudyBuildError(
                "Response model ID must be an exact non-empty value"
            )
        if not source_provenance.git_worktree_clean:
            raise StudyBuildError(
                "Real-model Study requires a clean source worktree"
            )
        if pricing_snapshot.model_id != normalized_model_id:
            raise StudyBuildError(
                "Frozen pricing model does not match the Study model"
            )

        manifests = tuple(
            self._load_manifest(task_id) for task_id in B2_4_TASK_ORDER
        )
        facts = tuple(
            self._runtime_factory.inspect_manifest(manifest)
            for manifest in manifests
        )
        typed_facts = cast(
            tuple[
                PilotManifestFacts,
                PilotManifestFacts,
                PilotManifestFacts,
                PilotManifestFacts,
            ],
            facts,
        )
        self._validate_common_facts(typed_facts)

        provider_binding = ProviderBinding(
            provider=(
                "openai"
                if execution_mode is EvaluationExecutionMode.REAL_MODEL
                else "mock"
            ),
            model_id=normalized_model_id,
            response_model_id=normalized_response_model_id,
            timeout_seconds=90,
            max_retries=1,
            store=False,
            max_output_tokens=4_000,
            multi_tool_response_policy=(
                MultiToolResponsePolicy.SEQUENTIAL_READ_ONLY
            ),
            max_function_calls_per_response=8,
        )
        protocols = tuple(
            self._build_protocol(
                manifest,
                manifest_facts,
                provider_binding,
                execution_mode,
            )
            for manifest, manifest_facts in zip(
                manifests,
                typed_facts,
                strict=True,
            )
        )
        typed_protocols = (
            protocols[0],
            protocols[1],
            protocols[2],
            protocols[3],
        )
        platform_binding_digest = canonical_digest(
            typed_protocols[0].platform_binding
        )
        definition = EvaluationStudyDefinition(
            study_name=study_name,
            task_ids=B2_4_TASK_ORDER,
            protocol_digests=(
                typed_protocols[0].protocol_digest,
                typed_protocols[1].protocol_digest,
                typed_protocols[2].protocol_digest,
                typed_protocols[3].protocol_digest,
            ),
            provider_configuration_digest=(
                provider_binding.configuration_digest
            ),
            model_id=normalized_model_id,
            response_model_id=normalized_response_model_id,
            runtime_source_digest=source_provenance.runtime_source_digest,
            git_commit_sha=source_provenance.git_commit_sha,
            git_worktree_clean=True,
            pyproject_sha256=source_provenance.pyproject_sha256,
            uv_lock_sha256=source_provenance.uv_lock_sha256,
            fixture_registry_digest=typed_facts[0].fixture_registry_digest,
            platform_binding_digest=platform_binding_digest,
            pricing_snapshot_digest=pricing_snapshot.pricing_digest,
        )
        return EvaluationStudyBuild(
            definition=definition,
            protocols=typed_protocols,
            pricing_snapshot=pricing_snapshot,
        )

    def _load_manifest(self, task_id: str) -> FormalFixtureManifest:
        try:
            manifest = self._loader.load(self._task_root / task_id)
        except (OSError, ValueError) as exc:
            raise StudyBuildError(
                f"Formal Fixture failed validation for {task_id}"
            ) from exc
        if manifest.task_id != task_id:
            raise StudyBuildError("Formal Fixture task identity does not match")
        return manifest

    @staticmethod
    def _validate_common_facts(
        facts: tuple[
            PilotManifestFacts,
            PilotManifestFacts,
            PilotManifestFacts,
            PilotManifestFacts,
        ],
    ) -> None:
        first = facts[0]
        shared_fields = (
            "fixture_registry_digest",
            "system_prompt_version",
            "system_prompt",
            "path_case_sensitive",
            "os_family",
            "python_implementation",
            "python_version",
            "executable_path",
            "executable_sha256",
        )
        for candidate in facts[1:]:
            if any(
                getattr(candidate, field) != getattr(first, field)
                for field in shared_fields
            ):
                raise StudyBuildError(
                    "Formal Fixtures do not share one runtime platform"
                )

    @staticmethod
    def _build_protocol(
        manifest: FormalFixtureManifest,
        facts: PilotManifestFacts,
        provider_binding: ProviderBinding,
        execution_mode: EvaluationExecutionMode,
    ) -> EvaluationProtocol:
        EvaluationStudyBuilder._validate_prompt_boundary(manifest, facts)
        budget_values = EvaluationStudyBuilder._budget_values(
            manifest.difficulty
        )
        return EvaluationProtocol(
            protocol_name=f"agentforge-m7-b2.4-{manifest.task_id}",
            execution_mode=execution_mode,
            task_id=manifest.task_id,
            fixture_registry_digest=facts.fixture_registry_digest,
            fixture_asset_digest=facts.fixture_asset_digest,
            expected_baseline_fingerprint_digest=(
                facts.expected_baseline_fingerprint_digest
            ),
            task_policy_digest=facts.task_policy_digest,
            test_profile_template_digest=facts.test_profile_template_digest,
            provider_binding=provider_binding,
            model_budget=ModelBudgetBinding(
                **budget_values,
                max_retries=1,
                max_output_tokens_per_request=4_000,
            ),
            system_prompt_version=facts.system_prompt_version,
            system_prompt=facts.system_prompt,
            task_prompt=facts.task_prompt,
            tool_schema_digest=facts.tool_schema_digest,
            context_policy=ContextPolicyBinding(
                max_items=100,
                max_characters=20_000,
                max_utf8_bytes=40_000,
                version="1",
                system_prompt_version=str(facts.system_prompt_version),
                system_instructions=facts.system_prompt,
            ),
            completion_correction_mode=CompletionCorrectionMode.DEFAULT,
            repetition_count=3,
            replacement_policy=ReplacementPolicy(
                max_replacements_per_slot=1,
                replaceable_failure_categories=(
                    B2_4_REPLACEABLE_FAILURES
                ),
            ),
            platform_binding=PlatformBinding(
                os_family=facts.os_family,
                python_implementation=facts.python_implementation,
                python_version=facts.python_version,
                executable_path=facts.executable_path,
                executable_sha256=facts.executable_sha256,
            ),
            real_model_authorized=(
                execution_mode is EvaluationExecutionMode.REAL_MODEL
            ),
        )

    @staticmethod
    def _budget_values(difficulty: str) -> dict[str, int]:
        if difficulty == "BASIC":
            return dict(_BASIC_MODEL_BUDGET)
        if difficulty == "ENGINEERING":
            return dict(_ENGINEERING_MODEL_BUDGET)
        raise StudyBuildError(
            f"Unsupported real-model task difficulty: {difficulty}"
        )

    @staticmethod
    def _validate_prompt_boundary(
        manifest: FormalFixtureManifest,
        facts: PilotManifestFacts,
    ) -> None:
        prompt = f"{facts.system_prompt}\n{facts.task_prompt}".casefold()
        forbidden_paths = (
            *manifest.reference_files,
            *(
                path
                for path in manifest.immutable_file_sha256
                if path.replace("\\", "/").startswith("tests/hidden/")
            ),
        )
        if any(
            path.replace("\\", "/").casefold() in prompt
            for path in forbidden_paths
        ):
            raise StudyBuildError(
                "Formal prompt exposes a hidden or reference asset"
            )
