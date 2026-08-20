from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_serializer

from agentforge.application.contracts import ProfilePurpose
from agentforge.application.product_workspace import (
    ProductWorkspaceBaselineRepository,
    ProductWorkspaceScanner,
)
from agentforge.context.builder import ContextBuilder
from agentforge.context.models import ContextPolicy
from agentforge.domain.repair import RepairTaskPolicy
from agentforge.evaluation.validators import WorkspaceDiffValidator
from agentforge.models.base import ModelProvider
from agentforge.models.domain import ModelBudget
from agentforge.models.executor import ModelExecutor
from agentforge.persistence.approval_workflow import ApprovalWorkflow
from agentforge.persistence.database import Database
from agentforge.persistence.model_workflow import ModelWorkflow
from agentforge.persistence.mutation_workflow import MutationWorkflow
from agentforge.persistence.mutations import (
    MutationApprovalBindingRepository,
    MutationExecutionRepository,
)
from agentforge.persistence.profile_trust import ProfileKernel, TrustedProfile
from agentforge.persistence.repair_workflow import RepairWorkflow
from agentforge.persistence.repositories import (
    ApprovalRepository,
    CheckpointRepository,
    EventRepository,
    RunRepository,
)
from agentforge.persistence.source_revisions import WorkspaceDigester
from agentforge.persistence.test_execution_workflow import TestExecutionWorkflow
from agentforge.persistence.test_executions import (
    ProcessExecutionRepository,
    TestApprovalBindingRepository,
)
from agentforge.policy.engine import PolicyEngine
from agentforge.policy.repair import RepairPolicyEnforcer
from agentforge.policy.sensitive import SensitiveFilePolicy
from agentforge.process.base import ProcessTreeSupervisor
from agentforge.process.runner import create_process_tree_supervisor
from agentforge.runtime.engine import AgentRuntime
from agentforge.runtime.mutations import MutationCoordinator
from agentforge.runtime.repair import RepairCoordinator
from agentforge.runtime.test_execution import TestExecutionCoordinator
from agentforge.tools.base import Tool
from agentforge.tools.executor import ToolExecutor
from agentforge.tools.mutation.base import MutationLimits
from agentforge.tools.mutation.edit_file import EditFileTool
from agentforge.tools.mutation.security import MutationSecurityPolicy
from agentforge.tools.mutation.write_file import WriteFileTool
from agentforge.tools.paths import WorkspacePathResolver
from agentforge.tools.registry import ToolRegistry
from agentforge.tools.repository.git_log import GitLogTool
from agentforge.tools.repository.git_status import GitStatusTool
from agentforge.tools.repository.list_files import ListFilesTool
from agentforge.tools.repository.read_file import ReadFileTool
from agentforge.tools.repository.search_text import SearchTextTool
from agentforge.tools.testing.profiles import TestProfileRegistry
from agentforge.tools.testing.run_tests import RunTestsTool

_DIGEST_PATTERN = r"^[0-9a-f]{64}$"
_MODEL_PROVIDER_PROTOCOL_VERSION = "agentforge.models.base.ModelProvider/v1"
_SUPERVISOR_FACTORY_PROTOCOL_VERSION = "agentforge.process.ProcessTreeSupervisorFactory/v1"


class SupervisorIdentity(BaseModel):
    """Stable, non-secret identity supplied for a supervisor factory."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol_version: str = _SUPERVISOR_FACTORY_PROTOCOL_VERSION
    implementation: str = Field(min_length=1, max_length=200)
    implementation_version: str = Field(min_length=1, max_length=100)
    config_digest: str = Field(pattern=_DIGEST_PATTERN)


class ProviderBindingIdentity(BaseModel):
    """Immutable, non-secret provider identity captured at runtime assembly."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol: Literal["agentforge.models.base.ModelProvider/v1"] = (
        "agentforge.models.base.ModelProvider/v1"
    )
    name: str = Field(min_length=1, max_length=200)
    journal_identity: str = Field(min_length=3, max_length=256)


_DEFAULT_SUPERVISOR_IDENTITY = SupervisorIdentity(
    implementation="agentforge.process.runner.create_process_tree_supervisor",
    implementation_version="1",
    config_digest=hashlib.sha256(b"agentforge-default-process-supervisor-config-v1").hexdigest(),
)


class ToolAssemblyPolicy(Protocol):
    @property
    def max_single_file_changed_bytes(self) -> int: ...

    @property
    def allow_file_creation(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class RuntimeAssemblyRequest:
    """Explicit inputs to the single product/evaluator Runtime assembly chain.

    Evaluator-only setup stays outside this request. In particular, the shared
    factory never accepts the legacy unbound-source policy.
    """

    database: Database
    workspace: Path
    provider: Any
    policy: RepairTaskPolicy
    context_policy: ContextPolicy
    model_budget: ModelBudget
    profiles: TestProfileRegistry
    events: EventRepository
    repair_workflow: RepairWorkflow
    max_output_chars: int
    repair_coordinator: RepairCoordinator | None = None
    supervisor_factory: Callable[[], ProcessTreeSupervisor] = (
        create_process_tree_supervisor
    )
    supervisor_identity: SupervisorIdentity | None = None

    def __post_init__(self) -> None:
        if self.max_output_chars <= 0:
            raise ValueError("Runtime output bound must be positive")
        if self.supervisor_identity is None:
            if self.supervisor_factory is not create_process_tree_supervisor:
                raise ValueError(
                    "Custom supervisor factory requires an explicit supervisor identity"
                )
        if self.events.database is not self.database:
            raise ValueError("Event repository must use the same Database instance")
        if self.repair_workflow.database is not self.database:
            raise ValueError("Repair workflow must use the same Database instance")
        if (
            self.repair_coordinator is not None
            and self.repair_coordinator.database is not self.database
        ):
            raise ValueError("Repair coordinator must use the same Database instance")
        if (
            self.repair_coordinator is not None
            and self.repair_coordinator.workflow is not self.repair_workflow
        ):
            raise ValueError("Repair coordinator must wrap the injected repair workflow")
class RuntimeComponents(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    database: Database
    workspace: Path
    workspace_resolver: WorkspacePathResolver
    provider: Any
    provider_binding: ProviderBindingIdentity
    runtime: AgentRuntime
    profiles: TestProfileRegistry
    test_coordinator: TestExecutionCoordinator
    mutation_coordinator: MutationCoordinator
    model_workflow: ModelWorkflow
    model_provider_name: str = Field(min_length=1, max_length=200)
    configured_model_id: str = Field(min_length=1, max_length=256)
    configured_profile_ids: tuple[str, ...]
    model_budget: ModelBudget
    repair_policy: RepairTaskPolicy
    tool_names: tuple[str, ...]
    common_binding_digest: str = Field(pattern=_DIGEST_PATTERN)

    @field_serializer(
        "database",
        "workspace_resolver",
        "provider",
        "runtime",
        "profiles",
        "test_coordinator",
        "mutation_coordinator",
        "model_workflow",
    )
    def serialize_component_identity(self, value: object) -> str:
        return _qualified_type(value)


class RuntimeComponentFactory:
    """Build the canonical runtime component graph for product and evaluator."""

    def build(self, request: RuntimeAssemblyRequest) -> RuntimeComponents:
        coordinator = request.repair_coordinator or self.product_repair_coordinator(
            request.database, request.workspace, request.repair_workflow
        )
        return self._assemble(
            request,
            repair_coordinator=coordinator,
            approval_workflow=ApprovalWorkflow(request.database),
            mutation_workflow=MutationWorkflow(request.database),
            test_execution_workflow=TestExecutionWorkflow(request.database),
        )

    def _evaluator_only_build(
        self,
        request: RuntimeAssemblyRequest,
        *,
        approval_workflow: ApprovalWorkflow,
        mutation_workflow: MutationWorkflow,
        test_execution_workflow: TestExecutionWorkflow,
    ) -> RuntimeComponents:
        if request.repair_coordinator is None:
            raise ValueError("Evaluator runtime requires an explicit repair coordinator")
        return self._assemble(
            request,
            repair_coordinator=request.repair_coordinator,
            approval_workflow=approval_workflow,
            mutation_workflow=mutation_workflow,
            test_execution_workflow=test_execution_workflow,
        )

    def _assemble(
        self,
        request: RuntimeAssemblyRequest,
        *,
        repair_coordinator: RepairCoordinator,
        approval_workflow: ApprovalWorkflow,
        mutation_workflow: MutationWorkflow,
        test_execution_workflow: TestExecutionWorkflow,
    ) -> RuntimeComponents:
        validated_policy = RepairTaskPolicy.model_validate(
            request.policy.model_dump(mode="python")
        )
        if validated_policy != request.policy:
            raise ValueError("Runtime policy binding is not canonical")
        validated_context = ContextPolicy.model_validate(
            request.context_policy.model_dump(mode="python")
        )
        validated_budget = ModelBudget.model_validate(
            request.model_budget.model_dump(mode="python")
        )
        if (
            validated_context != request.context_policy
            or validated_budget != request.model_budget
        ):
            raise ValueError("Runtime model or context binding is not canonical")
        resolver = WorkspacePathResolver(request.workspace)
        if resolver.workspace != request.profiles.workspace_root:
            raise ValueError("Profile registry does not belong to the Runtime workspace")
        trusted_profiles = self._resolve_trusted_profiles(request)
        self._require_policy_profiles(validated_policy, trusted_profiles)
        sensitive = SensitiveFilePolicy()
        registry, mutation_security = self.build_tool_registry(
            resolver,
            request.profiles,
            request.policy,
            sensitive=sensitive,
        )
        runs = RunRepository(request.database)
        approvals = ApprovalRepository(request.database)
        digester = WorkspaceDigester()
        mutation_coordinator = MutationCoordinator(
            MutationApprovalBindingRepository(request.database),
            MutationExecutionRepository(request.database),
            mutation_workflow,
            mutation_security,
            digester=digester,
        )
        test_coordinator = TestExecutionCoordinator(
            TestApprovalBindingRepository(request.database),
            ProcessExecutionRepository(request.database),
            test_execution_workflow,
            request.profiles,
            supervisor_factory=request.supervisor_factory,
            digester=digester,
        )
        executor = ToolExecutor(
            registry,
            PolicyEngine(resolver, sensitive),
            request.events,
            runs,
            max_output_chars=request.max_output_chars,
            repair_guard=RepairPolicyEnforcer(request.repair_workflow),
        )
        model_workflow = ModelWorkflow(request.database)
        runtime = AgentRuntime(
            runs,
            request.events,
            CheckpointRepository(request.database),
            request.provider,
            executor,
            approval_repository=approvals,
            approval_workflow=approval_workflow,
            model_executor=ModelExecutor(
                request.provider,
                model_workflow,
                provider_identity=self._provider_binding(request.provider).journal_identity,
            ),
            model_workflow=model_workflow,
            model_budget=request.model_budget,
            context_builder=ContextBuilder(request.context_policy),
            mutation_coordinator=mutation_coordinator,
            test_execution_coordinator=test_coordinator,
            repair_coordinator=repair_coordinator,
        )
        binding_digest = self._common_binding_digest(
            request,
            registry,
            trusted_profiles,
            mutation_coordinator,
            test_coordinator,
        )
        return RuntimeComponents(
            database=request.database,
            workspace=resolver.workspace,
            workspace_resolver=resolver,
            provider=request.provider,
            provider_binding=self._provider_binding(request.provider),
            runtime=runtime,
            profiles=request.profiles,
            test_coordinator=test_coordinator,
            mutation_coordinator=mutation_coordinator,
            model_workflow=model_workflow,
            model_provider_name=request.provider.name,
            configured_model_id=self._configured_model_id(request.provider),
            configured_profile_ids=tuple(
                profile.profile_id for profile in request.profiles.list_enabled()
            ),
            model_budget=request.model_budget,
            repair_policy=request.policy,
            tool_names=tuple(tool.spec.name for tool in registry.list_tools()),
            common_binding_digest=binding_digest,
        )

    @staticmethod
    def _configured_model_id(provider: ModelProvider) -> str:
        prefix = f"{provider.name}/"
        identity = provider.journal_identity
        if not identity.startswith(prefix):
            raise ValueError("Provider journal identity does not bind its name")
        model_id = identity[len(prefix) :]
        if not model_id or len(model_id) > 256:
            raise ValueError("Provider model identity is invalid")
        return model_id

    @staticmethod
    def _provider_binding(provider: ModelProvider) -> ProviderBindingIdentity:
        identity = provider.journal_identity
        name = provider.name
        if type(name) is not str or type(identity) is not str:
            raise ValueError("Provider must expose stable string identity")
        binding = ProviderBindingIdentity(name=name, journal_identity=identity)
        if not identity.startswith(f"{binding.name}/"):
            raise ValueError("Provider journal identity does not bind its name")
        return binding

    @staticmethod
    def product_repair_coordinator(
        database: Database, workspace: Path, workflow: RepairWorkflow
    ) -> RepairCoordinator:
        resolver = WorkspacePathResolver(workspace)
        return RepairCoordinator(
            workflow,
            workspace_repository=ProductWorkspaceBaselineRepository(database),
            diff_validator=WorkspaceDiffValidator(
                resolver, scanner=ProductWorkspaceScanner(resolver.workspace)
            ),
        )

    @staticmethod
    def build_tool_registry(
        resolver: WorkspacePathResolver,
        profiles: TestProfileRegistry,
        policy: ToolAssemblyPolicy,
        *,
        sensitive: SensitiveFilePolicy | None = None,
    ) -> tuple[ToolRegistry, MutationSecurityPolicy]:
        sensitive_policy = sensitive or SensitiveFilePolicy()
        mutation_security = MutationSecurityPolicy(
            resolver,
            sensitive_policy,
            RuntimeComponentFactory._mutation_limits(
                policy.max_single_file_changed_bytes
            ),
        )
        tools: list[Tool] = [
            ListFilesTool(resolver, sensitive_policy),
            ReadFileTool(resolver, sensitive_policy),
            SearchTextTool(resolver, sensitive_policy),
            GitStatusTool(resolver),
            GitLogTool(resolver),
            EditFileTool(mutation_security),
            RunTestsTool(profiles),
        ]
        if policy.allow_file_creation:
            tools.append(WriteFileTool(mutation_security))
        return ToolRegistry(tools), mutation_security

    @staticmethod
    def _mutation_limits(max_file_bytes: int) -> MutationLimits:
        return MutationLimits(
            max_file_bytes=max_file_bytes,
            max_old_text_bytes=min(65_536, max_file_bytes),
            max_new_text_bytes=min(262_144, max_file_bytes),
        )

    @staticmethod
    def _resolve_trusted_profiles(
        request: RuntimeAssemblyRequest,
    ) -> tuple[TrustedProfile, ...]:
        kernel = ProfileKernel(request.database, request.profiles)
        trusted: list[TrustedProfile] = []
        for profile in request.profiles.list_enabled():
            trusted.append(
                kernel.resolve_trusted(
                    profile.profile_id,
                    purpose=profile.purpose,
                    argv=profile.argv,
                )
            )
        return tuple(sorted(trusted, key=lambda item: item.profile_id))

    @staticmethod
    def _require_policy_profiles(
        policy: RepairTaskPolicy,
        trusted_profiles: tuple[TrustedProfile, ...],
    ) -> None:
        by_id = {profile.profile_id: profile for profile in trusted_profiles}
        development = set(policy.allowed_development_test_profiles)
        if not development.issubset(by_id):
            raise ValueError("Runtime policy references an untrusted development profile")
        if any(
            by_id[profile_id].purpose is not ProfilePurpose.DEVELOPMENT
            for profile_id in development
        ):
            raise ValueError("Runtime development profile purpose does not match policy")
        final = by_id.get(policy.final_verification_profile_id)
        if final is None or final.purpose is not ProfilePurpose.VERIFICATION:
            raise ValueError("Runtime final verification profile does not match policy")

    @staticmethod
    def _common_binding_digest(
        request: RuntimeAssemblyRequest,
        registry: ToolRegistry,
        trusted_profiles: tuple[TrustedProfile, ...],
        mutation_coordinator: MutationCoordinator,
        test_coordinator: TestExecutionCoordinator,
    ) -> str:
        payload = {
            "schema_version": 1,
            "tools_digest": _digest(registry.export_schemas()),
            "tool_specs_digest": _digest(
                [spec.model_dump(mode="json") for spec in registry.specs()]
            ),
            "mutation_coordinator_type": _qualified_type(mutation_coordinator),
            "test_coordinator_type": _qualified_type(test_coordinator),
            "policy_type": _qualified_type(request.policy),
            "policy_digest": _digest(request.policy.model_dump(mode="json")),
            "trusted_profile_bindings_digest": _digest(
                [profile.model_dump(mode="json") for profile in trusted_profiles]
            ),
            "provider_protocol": _MODEL_PROVIDER_PROTOCOL_VERSION,
            "provider_identity_digest": _digest(
                {
                    "name": request.provider.name,
                    "journal_identity": request.provider.journal_identity,
                }
            ),
            "model_budget_digest": _digest(
                request.model_budget.model_dump(mode="json")
            ),
            "context_policy_digest": _digest(
                request.context_policy.model_dump(mode="json")
            ),
            "max_output_chars": request.max_output_chars,
            "supervisor_identity_digest": _digest(
                (
                    request.supervisor_identity or _DEFAULT_SUPERVISOR_IDENTITY
                ).model_dump(mode="json")
            ),
        }
        return _digest(payload)


def _qualified_type(value: object) -> str:
    kind = type(value)
    return f"{kind.__module__}.{kind.__qualname__}"


def _digest(value: Any) -> str:
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
