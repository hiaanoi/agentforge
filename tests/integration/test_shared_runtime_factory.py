from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import fields, replace
from pathlib import Path
from uuid import uuid4

import pytest

from agentforge.application.contracts import ProfilePurpose
from agentforge.application.runtime_factory import (
    RuntimeAssemblyRequest,
    RuntimeComponentFactory,
    SupervisorIdentity,
)
from agentforge.context.models import ContextPolicy
from agentforge.domain.repair import (
    BudgetProfile,
    RepairDifficulty,
    RepairTaskPolicy,
)
from agentforge.evaluation.pilot_factory import PilotRuntimeFactory
from agentforge.evaluation.provider_factory import MockEvaluationProviderFactory
from agentforge.models.base import ModelRequest
from agentforge.models.domain import ModelBudget, ModelResponse
from agentforge.models.mock import MockModelProvider
from agentforge.persistence.database import Database
from agentforge.persistence.legacy_evaluator import LegacyEvaluatorEventRepository
from agentforge.persistence.profile_trust import ProfileKernel
from agentforge.persistence.repair_terminal import RepairStateProvenance
from agentforge.persistence.repair_workflow import RepairWorkflow
from agentforge.runtime.repair import RepairCoordinator
from agentforge.tools.paths import WorkspacePathResolver
from agentforge.tools.testing.profiles import (
    TestProfileDefinition as ProfileDefinition,
)
from agentforge.tools.testing.profiles import (
    TestProfileRegistry as ProfileRegistry,
)


def _git_init(workspace: Path) -> None:
    subprocess.run(
        ["git", "init", "--quiet"],
        cwd=workspace,
        check=True,
        capture_output=True,
        shell=False,
    )


def _policy(*, max_file_bytes: int = 1_048_576) -> RepairTaskPolicy:
    return RepairTaskPolicy(
        task_id="shared-runtime",
        policy_version=1,
        difficulty=RepairDifficulty.ENGINEERING,
        budget_profile=BudgetProfile.ENGINEERING,
        allowed_write_paths=("src/**",),
        forbidden_write_paths=(".git/**",),
        protected_paths=("tests/**",),
        allowed_development_test_profiles=("visible",),
        final_verification_profile_id="hidden",
        allow_file_creation=True,
        allowed_create_paths=("src/**",),
        max_created_files=2,
        max_changed_files=4,
        max_total_changed_bytes=max_file_bytes,
        max_single_file_changed_bytes=max_file_bytes,
        max_model_calls=10,
        max_read_calls=35,
        max_edit_attempts=4,
        max_test_runs=5,
        max_completion_corrections=1,
        max_policy_violations=2,
        max_wall_time_seconds=600,
        path_case_sensitive=os.path.normcase("A") != os.path.normcase("a"),
    )


def _request(tmp_path: Path) -> RuntimeAssemblyRequest:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "src").mkdir()
    (workspace / "tests").mkdir()
    verifier = tmp_path / "verifier"
    verifier.mkdir()
    _git_init(workspace)
    database = Database.from_path(tmp_path / "runtime.sqlite3")
    database.create_schema()
    profiles = ProfileRegistry(WorkspacePathResolver(workspace))
    profiles.register(
        ProfileDefinition(
            profile_id="visible",
            name="Visible",
            description="Visible development tests",
            executable=sys.executable,
            argv=("-m", "pytest", "tests"),
            cwd=".",
            timeout_seconds=30,
            max_output_bytes=100_000,
            profile_version=1,
            purpose=ProfilePurpose.DEVELOPMENT,
        )
    )
    profiles.register(
        ProfileDefinition(
            profile_id="hidden",
            name="Hidden",
            description="Sealed final verification",
            executable=sys.executable,
            argv=("-m", "pytest", "{VERIFIER}"),
            cwd=".",
            timeout_seconds=30,
            max_output_bytes=100_000,
            profile_version=1,
            purpose=ProfilePurpose.VERIFICATION,
            verifier_root=str(verifier),
        )
    )
    kernel = ProfileKernel(database, profiles)
    for profile in profiles.list_enabled():
        challenge = kernel.challenge(profile.profile_id, purpose=profile.purpose)
        kernel.trust(challenge, command_id=uuid4())
    repair_workflow = RepairWorkflow(database)
    return RuntimeAssemblyRequest(
        database=database,
        workspace=workspace,
        provider=MockModelProvider([], model_id="shared"),
        policy=_policy(),
        context_policy=ContextPolicy(system_instructions="shared system prompt"),
        model_budget=ModelBudget(max_model_requests=7, max_retries=1),
        profiles=profiles,
        events=LegacyEvaluatorEventRepository(database),
        repair_workflow=repair_workflow,
        repair_coordinator=RepairCoordinator(repair_workflow),
        max_output_chars=20_000,
    )


def test_product_and_pilot_use_same_runtime_components(tmp_path: Path) -> None:
    request = _request(tmp_path)
    product = RuntimeComponentFactory().build(request)
    pilot_factory = PilotRuntimeFactory(
        request.database,
        MockEvaluationProviderFactory([]),
        executable=Path(sys.executable),
        allowed_env={},
    )
    pilot = pilot_factory.build_components(request)

    assert type(product.runtime) is type(pilot.runtime)
    assert type(product.test_coordinator) is type(pilot.test_coordinator)
    assert type(product.mutation_coordinator) is type(pilot.mutation_coordinator)
    assert product.tool_names == pilot.tool_names
    assert product.common_binding_digest == pilot.common_binding_digest
    assert "git_status" in product.tool_names
    assert "git_log" in product.tool_names


def test_public_runtime_request_rejects_workflow_injection(tmp_path: Path) -> None:
    request = _request(tmp_path)

    with pytest.raises(TypeError):
        replace(request, workflows=object())
    with pytest.raises(TypeError):
        RuntimeAssemblyRequest(
            **{field.name: getattr(request, field.name) for field in fields(request)},
            workflows=object(),
        )


def test_public_factory_always_constructs_product_fixed_workflows(tmp_path: Path) -> None:
    components = RuntimeComponentFactory().build(_request(tmp_path))

    assert (
        components.runtime._approval_workflow._repair_state_provenance
        is RepairStateProvenance.PRODUCT_BUNDLE
    )
    assert (
        components.mutation_coordinator._workflow._repair_state_provenance
        is RepairStateProvenance.PRODUCT_BUNDLE
    )
    assert (
        components.test_coordinator._workflow._repair_state_provenance
        is RepairStateProvenance.PRODUCT_BUNDLE
    )


def test_common_binding_changes_for_every_material_surface(tmp_path: Path) -> None:
    request = _request(tmp_path)
    factory = RuntimeComponentFactory()
    baseline = factory.build(request).common_binding_digest

    changed_context = replace(
        request,
        context_policy=request.context_policy.model_copy(
            update={"max_items": request.context_policy.max_items + 1}
        ),
    )
    changed_budget = replace(
        request,
        model_budget=request.model_budget.model_copy(
            update={"max_model_requests": request.model_budget.max_model_requests + 1}
        ),
    )
    changed_provider = replace(
        request,
        provider=MockModelProvider([], model_id="different"),
    )
    changed_output_limit = replace(
        request,
        max_output_chars=request.max_output_chars + 1,
    )

    assert factory.build(changed_context).common_binding_digest != baseline
    assert factory.build(changed_budget).common_binding_digest != baseline
    assert factory.build(changed_provider).common_binding_digest != baseline
    assert factory.build(changed_output_limit).common_binding_digest != baseline


def test_common_binding_uses_stable_explicit_supervisor_identity(tmp_path: Path) -> None:
    request = _request(tmp_path)
    factory = RuntimeComponentFactory()

    first_factory = lambda: request.supervisor_factory()  # noqa: E731
    second_factory = lambda: request.supervisor_factory()  # noqa: E731
    identity = SupervisorIdentity(
        implementation="tests.supervisor.StableSupervisor",
        implementation_version="2",
        config_digest="1" * 64,
    )
    first = factory.build(
        replace(
            request,
            supervisor_factory=first_factory,
            supervisor_identity=identity,
        )
    ).common_binding_digest
    second = factory.build(
        replace(
            request,
            supervisor_factory=second_factory,
            supervisor_identity=identity,
        )
    ).common_binding_digest

    assert first == second
    assert factory.build(
        replace(
            request,
            supervisor_factory=second_factory,
            supervisor_identity=identity.model_copy(
                update={"implementation_version": "3"}
            ),
        )
    ).common_binding_digest != first
    assert factory.build(
        replace(
            request,
            supervisor_factory=second_factory,
            supervisor_identity=identity.model_copy(
                update={"config_digest": "2" * 64}
            ),
        )
    ).common_binding_digest != first


def test_runtime_assembly_rejects_unidentified_custom_supervisor(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)

    with pytest.raises(ValueError, match="supervisor identity"):
        replace(request, supervisor_factory=lambda: request.supervisor_factory())


@pytest.mark.parametrize("component", ["events", "workflow", "coordinator"])
def test_runtime_assembly_rejects_split_brain_database_components(
    tmp_path: Path,
    component: str,
) -> None:
    request = _request(tmp_path)
    secondary = Database.from_path(tmp_path / "secondary.sqlite3")
    secondary.create_schema()
    changes: dict[str, object]
    if component == "events":
        changes = {"events": LegacyEvaluatorEventRepository(secondary)}
    elif component == "workflow":
        changes = {"repair_workflow": RepairWorkflow(secondary)}
    else:
        secondary_workflow = RepairWorkflow(secondary)
        changes = {"repair_coordinator": RepairCoordinator(secondary_workflow)}

    with pytest.raises(ValueError, match="same Database"):
        replace(request, **changes)


def test_runtime_assembly_requires_coordinator_to_wrap_injected_workflow(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)

    with pytest.raises(ValueError, match="repair workflow"):
        replace(
            request,
            repair_coordinator=RepairCoordinator(RepairWorkflow(request.database)),
        )


def test_runtime_assembly_accepts_all_components_bound_to_same_database(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)

    components = RuntimeComponentFactory().build(request)

    assert components.common_binding_digest


class _EquivalentProviderSurface:
    @property
    def name(self) -> str:
        return "mock"

    @property
    def journal_identity(self) -> str:
        return "mock/shared"

    async def generate(self, request: ModelRequest) -> ModelResponse:
        raise AssertionError(f"provider should not execute during assembly: {request.task}")


def test_common_binding_uses_provider_protocol_surface_not_wrapper_class(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    factory = RuntimeComponentFactory()

    baseline = factory.build(request).common_binding_digest
    equivalent = factory.build(
        replace(request, provider=_EquivalentProviderSurface())
    ).common_binding_digest

    assert equivalent == baseline


def test_component_serialization_exposes_workspace_but_no_secret(tmp_path: Path) -> None:
    request = _request(tmp_path)
    components = RuntimeComponentFactory().build(request)

    serialized = components.model_dump_json()
    payload = json.loads(serialized)
    assert Path(payload["workspace"]).samefile(components.workspace)
    assert "shared system prompt" not in serialized
    assert "OPENAI_API_KEY" not in serialized


def test_shared_factory_rejects_copied_policy_that_bypassed_validation(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    corrupted = request.policy.model_copy(
        update={"final_verification_profile_id": "missing"}
    )

    with pytest.raises(ValueError, match="policy"):
        RuntimeComponentFactory().build(replace(request, policy=corrupted))
