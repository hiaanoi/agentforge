import hashlib
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from agentforge.application.contracts import ProfilePurpose, VerificationRuntimeMode
from agentforge.application.runtime_factory import (
    RuntimeAssemblyRequest,
    RuntimeComponentFactory,
    RuntimeComponents,
)
from agentforge.context.models import ContextPolicy
from agentforge.domain.enums import ConfigSourceKind, EvaluationAttemptStatus
from agentforge.domain.models import Run, RunBudget
from agentforge.domain.repair import RepairState
from agentforge.domain.test_execution import TestExecutionPlan
from agentforge.evaluation.auto_approval import EvaluationWorkspaceHandle
from agentforge.evaluation.baseline import BaselineExecutionCoordinator
from agentforge.evaluation.baseline_models import BaselineExecutionRecord
from agentforge.evaluation.baseline_persistence import (
    BaselineExecutionRepository,
    BaselineExecutionWorkflow,
)
from agentforge.evaluation.campaign_models import EvaluationPilotAttempt
from agentforge.evaluation.formal_fixtures import (
    FormalFixtureManifest,
    compute_fixture_registry_digest,
)
from agentforge.evaluation.harness import (
    EvaluationHarness,
    EvaluationRunMetadata,
)
from agentforge.evaluation.persistence import (
    EvaluationRunRepository,
    EvaluationWorkspaceRepository,
)
from agentforge.evaluation.pilot_workspace import PilotWorkspaceLease
from agentforge.evaluation.prompts import (
    BASELINE_PROMPT_VARIANT,
    PromptVariant,
    build_formal_prompt_bundle,
)
from agentforge.evaluation.protocol import EvaluationProtocol
from agentforge.evaluation.provider_factory import (
    EvaluationProviderFactory,
    ProtocolBoundModelProvider,
)
from agentforge.evaluation.telemetry import EvaluationTelemetryCollector
from agentforge.evaluation.telemetry_persistence import (
    EvaluationTelemetryRepository,
)
from agentforge.evaluation.validators import WorkspaceDiffValidator
from agentforge.evaluation.workspace import (
    WorkspaceBaseline,
    WorkspaceBaselineBuilder,
)
from agentforge.persistence.application_uow import ApplicationUnitOfWork
from agentforge.persistence.approval_workflow import ApprovalWorkflow
from agentforge.persistence.database import Database
from agentforge.persistence.legacy_evaluator import LegacyEvaluatorEventRepository
from agentforge.persistence.mutation_workflow import MutationWorkflow
from agentforge.persistence.profile_trust import ProfileKernel, ProfileTrustChallenge
from agentforge.persistence.repair_workflow import RepairWorkflow
from agentforge.persistence.repositories import (
    EventRepository,
)
from agentforge.persistence.source_revisions import (
    SourceRevisionStore,
    WorkspaceDigester,
)
from agentforge.persistence.test_execution_workflow import TestExecutionWorkflow
from agentforge.policy.sensitive import SensitiveFilePolicy
from agentforge.process.base import ProcessTreeSupervisor
from agentforge.process.managed import ManagedTestExecutionCore
from agentforge.process.runner import create_process_tree_supervisor
from agentforge.repair_engines.mini_native.agentforge_host import (
    BashEnvironmentCallback,
    MiniNativeBashEnvironment,
)
from agentforge.repair_engines.models import RepairEngineKind
from agentforge.runtime.engine import AgentRuntime
from agentforge.runtime.repair import RepairCoordinator
from agentforge.tools.paths import WorkspacePathResolver
from agentforge.tools.registry import ToolRegistry
from agentforge.tools.testing.profiles import (
    TestProfileDefinition,
    TestProfileRegistry,
    is_reserved_verification_environment,
)

_FORMAL_PILOT_TRUST_NAMESPACE = UUID("35ec89a2-a47b-5e60-9376-ecaa17d668ca")


def _bootstrap_formal_pilot_profile_trust(
    database: Database,
    profiles: TestProfileRegistry,
    profile_id: str,
    *,
    purpose: ProfilePurpose,
) -> None:
    """Issue the evaluator operator's explicit, replayable trust command.

    This seam is deliberately confined to the formal evaluator assembly.  It
    trusts only a profile that the factory constructed from an already validated
    frozen fixture manifest; product/project configuration is never discovered
    or trusted implicitly.
    """

    kernel = ProfileKernel(database, profiles)
    challenge = kernel.challenge(
        profile_id,
        purpose=purpose,
    )
    command_id = _formal_pilot_trust_command_id(challenge)
    kernel.trust(challenge, command_id=command_id)


def _formal_pilot_trust_command_id(challenge: ProfileTrustChallenge) -> UUID:
    logical_trust_scope = ":".join(
        (
            "agentforge-formal-pilot-profile-trust-v1",
            challenge.workspace_identity,
            challenge.profile_id,
            str(challenge.profile_version),
            challenge.purpose.value,
        )
    )
    return uuid5(_FORMAL_PILOT_TRUST_NAMESPACE, logical_trust_scope)


class PilotBindingMismatchError(RuntimeError):
    pass


@dataclass(frozen=True)
class _RegistryPolicy:
    max_single_file_changed_bytes: int
    allow_file_creation: bool


class PilotManifestFacts(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    fixture_registry_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    fixture_asset_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_baseline_fingerprint_digest: str = Field(
        pattern=r"^[0-9a-f]{64}$"
    )
    task_policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    test_profile_template_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    system_prompt_version: int = Field(gt=0)
    system_prompt: str
    task_prompt: str
    tool_schema_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    path_case_sensitive: bool
    os_family: Literal["WINDOWS", "POSIX"]
    python_implementation: str
    python_version: str
    executable_path: str
    executable_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class PilotExecution:
    attempt_id: UUID
    lease_id: UUID
    protocol_digest: str
    run: Run
    metadata: EvaluationRunMetadata
    runtime: AgentRuntime
    harness: EvaluationHarness
    provider: ProtocolBoundModelProvider
    events: EventRepository
    context_policy: ContextPolicy
    tool_names: tuple[str, ...]
    profile_ids: tuple[str, ...]
    visible_test_plan: TestExecutionPlan
    hidden_test_plan: TestExecutionPlan
    workspace_baseline: WorkspaceBaseline
    baseline_execution: BaselineExecutionRecord
    repair_state: RepairState
    common_binding_digest: str


class PilotRuntimeFactory:
    def __init__(
        self,
        database: Database,
        provider_factory: EvaluationProviderFactory,
        *,
        executable: Path,
        allowed_env: dict[str, str],
        prompt_variant: PromptVariant = BASELINE_PROMPT_VARIANT,
        supervisor_factory: Callable[[], ProcessTreeSupervisor] = (
            create_process_tree_supervisor
        ),
        repair_engine: RepairEngineKind = RepairEngineKind.NATIVE,
        mini_native_environment: MiniNativeBashEnvironment | BashEnvironmentCallback | None = (
            None
        ),
        mini_native_container: str | None = None,
        mini_native_container_workspace: str | None = None,
    ) -> None:
        self._database = database
        self._provider_factory = provider_factory
        self._executable = executable.resolve(strict=True)
        self._allowed_env = dict(sorted(allowed_env.items()))
        self._prompt_variant = prompt_variant
        self._supervisor_factory = supervisor_factory
        self._repair_engine = RepairEngineKind(repair_engine)
        self._mini_native_environment = mini_native_environment
        self._mini_native_container = mini_native_container
        self._mini_native_container_workspace = mini_native_container_workspace

    @staticmethod
    def build_components(request: RuntimeAssemblyRequest) -> RuntimeComponents:
        """Delegate evaluator assembly to the canonical product component graph."""

        return RuntimeComponentFactory().build(request)

    def inspect_manifest(
        self,
        manifest: FormalFixtureManifest,
    ) -> PilotManifestFacts:
        resolver = WorkspacePathResolver(manifest.root)
        profiles = TestProfileRegistry(resolver)
        profiles.register(self._profile_definitions(manifest, None)[0])
        policy = manifest.to_policy(
            path_case_sensitive=self._path_case_sensitive(),
        )
        registry = self._build_registry(
            resolver,
            SensitiveFilePolicy(),
            profiles,
            policy.max_single_file_changed_bytes,
            allow_file_creation=policy.allow_file_creation,
        )
        tool_schema: dict[str, JsonValue] = {
            "tools": cast(list[JsonValue], registry.export_schemas()),
        }
        prompts = build_formal_prompt_bundle(
            manifest,
            policy,
            tool_schema=tool_schema,
            prompt_variant=self._prompt_variant,
        )
        return PilotManifestFacts(
            fixture_registry_digest=compute_fixture_registry_digest(
                manifest.root.parent.parent
            ),
            fixture_asset_digest=manifest.asset_digest,
            expected_baseline_fingerprint_digest=(
                manifest.expected_baseline_failure.fingerprint_digest
            ),
            task_policy_digest=policy.policy_digest,
            test_profile_template_digest=manifest.profile_template_digest(
                executable=str(self._executable),
                allowed_env=self._allowed_env,
            ),
            system_prompt_version=prompts.system_prompt_version,
            system_prompt=prompts.system_prompt,
            task_prompt=prompts.task_prompt,
            tool_schema_digest=prompts.tool_schema_digest,
            path_case_sensitive=policy.path_case_sensitive,
            os_family="WINDOWS" if sys.platform == "win32" else "POSIX",
            python_implementation=sys.implementation.name,
            python_version=".".join(str(item) for item in sys.version_info[:3]),
            executable_path=str(self._executable),
            executable_sha256=hashlib.sha256(
                self._executable.read_bytes()
            ).hexdigest(),
        )

    def prepare(
        self,
        protocol: EvaluationProtocol,
        manifest: FormalFixtureManifest,
        attempt: EvaluationPilotAttempt,
        lease: PilotWorkspaceLease,
    ) -> PilotExecution:
        validated_protocol = self._validate_protocol(protocol)
        facts = self.inspect_manifest(manifest)
        self._validate_bindings(
            validated_protocol,
            facts,
            manifest,
            attempt,
            lease,
        )
        self._validate_workspace_lease(lease)

        resolver = WorkspacePathResolver(lease.model_workspace)
        sensitive = SensitiveFilePolicy()
        profiles = TestProfileRegistry(resolver)
        visible_definition, hidden_definition = self._profile_definitions(
            manifest,
            lease.hidden_test_root,
            lease.model_workspace,
        )
        profiles.register(visible_definition)
        profiles.register(hidden_definition)
        # Both profiles are evaluator-authored from the frozen manifest. Once
        # this Run has an authoritative source revision, development execution
        # also requires exact trust; hidden keeps VERIFICATION capsule rules.
        _bootstrap_formal_pilot_profile_trust(
            self._database,
            profiles,
            visible_definition.profile_id,
            purpose=ProfilePurpose.DEVELOPMENT,
        )
        _bootstrap_formal_pilot_profile_trust(
            self._database,
            profiles,
            hidden_definition.profile_id,
            purpose=ProfilePurpose.VERIFICATION,
        )
        visible_plan = profiles.prepare(visible_definition.profile_id)
        hidden_plan = profiles.prepare(hidden_definition.profile_id)

        policy = manifest.to_policy(
            path_case_sensitive=facts.path_case_sensitive,
        )
        registry = self._build_registry(
            resolver,
            sensitive,
            profiles,
            policy.max_single_file_changed_bytes,
            allow_file_creation=policy.allow_file_creation,
        )
        tool_schema: dict[str, JsonValue] = {
            "tools": cast(list[JsonValue], registry.export_schemas()),
        }
        prompts = build_formal_prompt_bundle(
            manifest,
            policy,
            tool_schema=tool_schema,
            prompt_variant=self._prompt_variant,
        )
        if prompts.tool_schema_digest != validated_protocol.tool_schema_digest:
            raise PilotBindingMismatchError("Tool schema does not match protocol")

        workspace_baseline = WorkspaceBaselineBuilder(resolver).build(
            task_id=manifest.task_id
        )
        source_baseline_digest = WorkspaceDigester().digest(resolver.workspace)
        workspace_repository = EvaluationWorkspaceRepository(self._database)
        workspace_repository.save_baseline(workspace_baseline)

        events = LegacyEvaluatorEventRepository(self._database)
        repair_workflow = RepairWorkflow(self._database)
        repair_coordinator = RepairCoordinator(
            repair_workflow,
            workspace_repository=workspace_repository,
            diff_validator=WorkspaceDiffValidator(resolver),
        )
        provider = self._provider_factory.create(validated_protocol)
        provider.validate_protocol(validated_protocol)
        model_budget = validated_protocol.model_budget.to_domain()
        context_policy = validated_protocol.context_policy.to_domain()
        components = RuntimeComponentFactory()._evaluator_only_build(
            RuntimeAssemblyRequest(
                database=self._database,
                workspace=resolver.workspace,
                provider=provider,
                policy=policy,
                context_policy=context_policy,
                model_budget=model_budget,
                profiles=profiles,
                events=events,
                repair_workflow=repair_workflow,
                repair_coordinator=repair_coordinator,
                max_output_chars=manifest.limits["max_output_chars"],
                supervisor_factory=self._supervisor_factory,
                repair_engine=self._repair_engine,
                mini_native_environment=self._mini_native_environment,
                mini_native_container=self._mini_native_container,
                mini_native_container_workspace=(
                    self._mini_native_container_workspace
                ),
            ),
            approval_workflow=ApprovalWorkflow._evaluator_only_create(self._database),
            mutation_workflow=MutationWorkflow._evaluator_only_create(self._database),
            test_execution_workflow=TestExecutionWorkflow._evaluator_only_create(
                self._database
            ),
        )
        runtime = components.runtime
        model_workflow = components.model_workflow
        run = runtime.create_run(
            validated_protocol.task_prompt,
            budget=RunBudget(
                max_steps=validated_protocol.model_budget.max_model_requests,
                max_model_calls=validated_protocol.model_budget.max_model_requests,
                max_tool_calls=(
                    policy.max_read_calls
                    + policy.max_edit_attempts
                    + policy.max_test_runs
                ),
                max_total_tokens=(
                    validated_protocol.model_budget.max_total_tokens or 100_000
                ),
                max_wall_time_seconds=policy.max_wall_time_seconds,
            ),
        )
        # The formal pilot still uses the evaluator-only Run constructor during
        # A1. Bind that one Run to the byte-exact baseline before any mutation so
        # the normal mutation CAS can advance it and hidden verification can
        # attest the resulting revision. Other legacy evaluator Runs remain
        # deliberately unbound.
        with ApplicationUnitOfWork(self._database) as uow:
            SourceRevisionStore()._evaluator_formal_bootstrap(
                uow.session,
                run.run_id,
                workspace_root=resolver.workspace,
                expected_initial_digest=source_baseline_digest,
                config_digest=policy.policy_digest,
                profile_digest=hidden_plan.profile_digest,
            )
            uow.commit()
        repair_state = repair_workflow._evaluator_only_start(
            run.run_id,
            policy,
            workspace_baseline.baseline_id,
            workspace_baseline.root_digest,
        )
        baseline_coordinator = BaselineExecutionCoordinator(
            BaselineExecutionRepository(self._database),
            BaselineExecutionWorkflow(self._database),
            profiles,
            ManagedTestExecutionCore(
                supervisor_factory=self._supervisor_factory,
            ),
        )
        baseline_execution = baseline_coordinator.ensure_created(
            run_id=run.run_id,
            task_id=manifest.task_id,
            workspace_baseline=workspace_baseline,
            profile_id=visible_definition.profile_id,
            expected_failure=manifest.expected_baseline_failure,
        )
        metadata = EvaluationRunMetadata(
            protocol_digest=validated_protocol.protocol_digest,
            campaign_id=attempt.campaign_id,
            slot_id=attempt.slot_id,
            attempt_id=attempt.attempt_id,
            attempt_number=attempt.attempt_number,
            replacement_for_evaluation_run_id=(
                attempt.predecessor_evaluation_run_id
            ),
            task_id=manifest.task_id,
            repetition_index=attempt.repetition_index,
            model_id=validated_protocol.provider_binding.model_id,
            model_parameters_digest=(
                validated_protocol.provider_binding.configuration_digest
            ),
            system_prompt_digest=validated_protocol.system_prompt_digest,
            task_prompt_digest=validated_protocol.task_prompt_digest,
            tool_schema_digest=validated_protocol.tool_schema_digest,
            context_policy_version=int(validated_protocol.context_policy.version),
            initial_workspace_digest=workspace_baseline.root_digest,
            task_policy_digest=policy.policy_digest,
            budget_profile=policy.budget_profile,
            completion_correction_mode=(
                validated_protocol.completion_correction_mode
            ),
        )
        workspace_handle = EvaluationWorkspaceHandle.create(
            lease.model_workspace,
            run_id=run.run_id,
            policy_digest=policy.policy_digest,
        )
        harness = EvaluationHarness(
            runtime,
            repair_workflow,
            events,
            workspace_handle,
            EvaluationRunRepository(self._database),
            baseline_coordinator=baseline_coordinator,
            telemetry_collector=EvaluationTelemetryCollector(
                model_workflow,
                events,
            ),
            telemetry_repository=EvaluationTelemetryRepository(
                self._database
            ),
        )
        return PilotExecution(
            attempt_id=attempt.attempt_id,
            lease_id=lease.lease_id,
            protocol_digest=validated_protocol.protocol_digest,
            run=run,
            metadata=metadata,
            runtime=runtime,
            harness=harness,
            provider=provider,
            events=events,
            context_policy=context_policy,
            tool_names=components.tool_names,
            profile_ids=tuple(
                profile.profile_id for profile in profiles.list_enabled()
            ),
            visible_test_plan=visible_plan,
            hidden_test_plan=hidden_plan,
            workspace_baseline=workspace_baseline,
            baseline_execution=baseline_execution,
            repair_state=repair_state,
            common_binding_digest=components.common_binding_digest,
        )

    def _profile_definitions(
        self,
        manifest: FormalFixtureManifest,
        hidden_test_root: Path | None,
        model_workspace: Path | None = None,
    ) -> tuple[TestProfileDefinition, TestProfileDefinition]:
        timeout = manifest.limits["timeout_seconds"]
        output_limit = manifest.limits["max_output_chars"]
        environment = self._profile_environment(model_workspace)
        visible = TestProfileDefinition(
            profile_id=f"visible-{manifest.task_id}",
            name="Visible development tests",
            description="Evaluator-defined visible development profile",
            executable=str(self._executable),
            argv=manifest.visible_command,
            cwd=".",
            allowed_env=environment,
            timeout_seconds=timeout,
            max_output_bytes=output_limit,
            profile_version=1,
            config_source_identity=f"formal-fixture:{manifest.task_id}:manifest",
            config_source_kind=ConfigSourceKind.FORMAL_MANIFEST,
            config_source_digest=manifest.asset_digest,
        )
        hidden_argv = tuple(
            (
                "{VERIFIER}"
                if hidden_test_root is not None and item == "tests/hidden"
                else item
            )
            for item in manifest.hidden_command
        )
        hidden = TestProfileDefinition(
            profile_id=f"hidden-{manifest.task_id}",
            name="Hidden final verification",
            description="Evaluator-defined hidden final profile",
            executable=str(self._executable),
            argv=hidden_argv,
            cwd=".",
            allowed_env={
                key: value
                for key, value in environment.items()
                if not is_reserved_verification_environment(key)
                and not Path(value).is_absolute()
            },
            timeout_seconds=timeout,
            max_output_bytes=output_limit,
            profile_version=1,
            purpose=ProfilePurpose.VERIFICATION,
            runtime_mode=VerificationRuntimeMode.SYSTEM_RUNTIME,
            verifier_root=(
                str(hidden_test_root) if hidden_test_root is not None else None
            ),
            config_source_identity=f"formal-fixture:{manifest.task_id}:manifest",
            config_source_kind=ConfigSourceKind.FORMAL_MANIFEST,
            config_source_digest=manifest.asset_digest,
        )
        return visible, hidden

    def _profile_environment(
        self,
        model_workspace: Path | None,
    ) -> dict[str, str]:
        environment = dict(self._allowed_env)
        if model_workspace is None:
            return environment
        workspace = model_workspace.resolve(strict=True)
        source_root = (workspace / "workspace").resolve(strict=True)
        runtime_temp = (workspace.parent / "runtime_tmp").resolve()
        runtime_temp.mkdir(parents=False, exist_ok=True)
        environment.update(
            {
                "PYTHONPATH": str(source_root),
                "TEMP": str(runtime_temp),
                "TMP": str(runtime_temp),
            }
        )
        return environment

    def _build_registry(
        self,
        resolver: WorkspacePathResolver,
        sensitive: SensitiveFilePolicy,
        profiles: TestProfileRegistry,
        max_file_bytes: int,
        *,
        allow_file_creation: bool,
    ) -> ToolRegistry:
        policy = _RegistryPolicy(
            max_single_file_changed_bytes=max_file_bytes,
            allow_file_creation=allow_file_creation,
        )
        registry, _ = RuntimeComponentFactory.build_tool_registry(
            resolver,
            profiles,
            policy,
            repair_engine=self._repair_engine,
            sensitive=sensitive,
        )
        return registry

    @staticmethod
    def _path_case_sensitive() -> bool:
        return os.path.normcase("AgentForge") != os.path.normcase("agentforge")

    @staticmethod
    def _validate_protocol(protocol: EvaluationProtocol) -> EvaluationProtocol:
        try:
            return EvaluationProtocol.model_validate(
                protocol.model_dump(mode="json")
            )
        except ValueError as exc:
            raise PilotBindingMismatchError(
                "Evaluation protocol failed integrity validation"
            ) from exc

    @staticmethod
    def _validate_bindings(
        protocol: EvaluationProtocol,
        facts: PilotManifestFacts,
        manifest: FormalFixtureManifest,
        attempt: EvaluationPilotAttempt,
        lease: PilotWorkspaceLease,
    ) -> None:
        expected = {
            "fixture_registry_digest": facts.fixture_registry_digest,
            "fixture_asset_digest": facts.fixture_asset_digest,
            "expected_baseline_fingerprint_digest": (
                facts.expected_baseline_fingerprint_digest
            ),
            "task_policy_digest": facts.task_policy_digest,
            "test_profile_template_digest": facts.test_profile_template_digest,
            "system_prompt_version": facts.system_prompt_version,
            "system_prompt": facts.system_prompt,
            "task_prompt": facts.task_prompt,
            "tool_schema_digest": facts.tool_schema_digest,
        }
        for field, value in expected.items():
            if getattr(protocol, field) != value:
                raise PilotBindingMismatchError(
                    f"{field} does not match evaluation protocol"
                )
        if (
            protocol.task_id != manifest.task_id
            or protocol.platform_binding.executable_path != facts.executable_path
            or protocol.platform_binding.executable_sha256
            != facts.executable_sha256
            or protocol.platform_binding.os_family != facts.os_family
            or protocol.platform_binding.python_implementation
            != facts.python_implementation
            or protocol.platform_binding.python_version != facts.python_version
        ):
            raise PilotBindingMismatchError(
                "Platform or task does not match evaluation protocol"
            )
        if (
            attempt.status is not EvaluationAttemptStatus.WORKSPACE_READY
            or attempt.protocol_digest != protocol.protocol_digest
            or attempt.task_id != protocol.task_id
            or attempt.workspace_lease_id != lease.lease_id
            or attempt.workspace_root_digest != lease.workspace_root_digest
            or attempt.workspace_path is None
            or attempt.workspace_path.resolve(strict=True)
            != lease.model_workspace.resolve(strict=True)
        ):
            raise PilotBindingMismatchError(
                "Pilot attempt does not match workspace-ready protocol binding"
            )
        if (
            lease.protocol_digest != protocol.protocol_digest
            or lease.fixture_asset_digest != protocol.fixture_asset_digest
            or lease.campaign_id != attempt.campaign_id
            or lease.slot_id != attempt.slot_id
            or lease.attempt_id != attempt.attempt_id
            or lease.hidden_test_root.is_relative_to(lease.model_workspace)
        ):
            raise PilotBindingMismatchError(
                "Pilot workspace lease does not match attempt binding"
            )

    @staticmethod
    def _validate_workspace_lease(lease: PilotWorkspaceLease) -> None:
        try:
            persisted = PilotWorkspaceLease.model_validate_json(
                lease.lease_file.read_text(encoding="utf-8")
            )
            model_root = lease.model_workspace.resolve(strict=True)
            hidden_root = lease.hidden_test_root.resolve(strict=True)
            model_scan = WorkspaceBaselineBuilder(
                WorkspacePathResolver(model_root)
            ).scan()
            hidden_scan = WorkspaceBaselineBuilder(
                WorkspacePathResolver(hidden_root)
            ).scan()
        except (OSError, RuntimeError, ValueError) as exc:
            raise PilotBindingMismatchError(
                "Pilot workspace lease could not be revalidated"
            ) from exc
        if persisted != lease:
            raise PilotBindingMismatchError(
                "Persisted Pilot workspace lease does not match"
            )
        expected_root_digest = hashlib.sha256(
            str(model_root).encode("utf-8")
        ).hexdigest()
        attempt_root = lease.lease_file.resolve(strict=True).parent
        if (
            model_root != (attempt_root / "model_workspace").resolve(strict=True)
            or hidden_root
            != (
                attempt_root / "evaluator_hidden" / "tests" / "hidden"
            ).resolve(strict=True)
            or lease.workspace_root_digest != expected_root_digest
            or model_scan.root_digest != lease.initial_workspace_digest
        ):
            raise PilotBindingMismatchError(
                "Pilot model workspace changed before Runtime preparation"
            )
        if hidden_scan.root_digest != lease.hidden_test_digest:
            raise PilotBindingMismatchError(
                "Pilot hidden tests changed before Runtime preparation"
            )
