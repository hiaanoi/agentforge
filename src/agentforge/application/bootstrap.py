"""Trusted project-runtime assembly for the public product boundary.

The four-value :class:`ProductConfig` deliberately does not carry executable
commands, verifier assets, or repair policy.  Those inputs live in the
separately reviewed project file ``.agentforge/runtime.toml`` and are bound
into every registered profile identity.  This module is the only product
bootstrap path; it never reuses evaluator assembly or invents test profiles.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, SecretStr, ValidationError

from agentforge.application.app import AgentApplication
from agentforge.application.config import (
    ProductConfig,
    UnsafeConfigurationError,
    _load_toml,
    _safe_workspace,
)
from agentforge.application.contracts import ProfilePurpose
from agentforge.application.doctor import Doctor
from agentforge.application.run_creation import ProductStartRunAssembler
from agentforge.application.runtime_factory import (
    RuntimeAssemblyRequest,
    RuntimeComponentFactory,
    RuntimeComponents,
)
from agentforge.context.models import ContextPolicy
from agentforge.domain.enums import ConfigSourceKind
from agentforge.domain.repair import RepairTaskPolicy
from agentforge.models.deepseek_provider import DeepSeekModelProvider
from agentforge.models.domain import ModelBudget, ModelProviderConfig
from agentforge.models.mock import MockModelProvider
from agentforge.models.openai_provider import OpenAIModelProvider
from agentforge.persistence.database import Database
from agentforge.persistence.repair_workflow import RepairWorkflow
from agentforge.persistence.repositories import EventRepository
from agentforge.tools.paths import WorkspacePathResolver
from agentforge.tools.testing.profiles import TestProfileDefinition, TestProfileRegistry

_RUNTIME_FILENAME = "runtime.toml"
_SECRET_COMPONENTS = frozenset(
    {"api", "apikey", "credential", "key", "password", "secret", "token"}
)
_PRODUCT_REPAIR_SYSTEM_INSTRUCTIONS = (
    "You are AgentForge, a constrained code repair agent. "
    "Treat tool results as the only source of execution truth. "
    "Inspect the repository efficiently, make the smallest policy-compliant source change, "
    "and use registered test profiles to verify the latest source state. "
    "Respect workspace permissions, approval requirements, and repair budgets. "
    "Never fabricate file contents, test results, or side effects, and never claim completion "
    "until the latest source state has been tested."
)


def _default_product_context_policy() -> ContextPolicy:
    return ContextPolicy(
        system_prompt_version="2",
        system_instructions=_PRODUCT_REPAIR_SYSTEM_INSTRUCTIONS,
    )


class ProductProviderDefinition(BaseModel):
    """Closed, non-secret provider selection from the runtime definition."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    kind: Literal["openai", "deepseek", "mock"]
    mock_responses: tuple[JsonValue, ...] = ()
    timeout_seconds: float = Field(default=30.0, gt=0, le=600.0)
    temperature: float | None = Field(default=None, ge=0.0, le=0.0)


class ProductRuntimeDefinition(BaseModel):
    """All operator-authored inputs omitted from layered product config."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    provider: ProductProviderDefinition
    policy: RepairTaskPolicy
    profiles: tuple[TestProfileDefinition, ...] = Field(min_length=1)
    model_budget: ModelBudget = Field(default_factory=ModelBudget)
    context_policy: ContextPolicy = Field(default_factory=_default_product_context_policy)
    max_output_chars: int = Field(default=20_000, gt=0, le=1_000_000)
    config_source_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @property
    def profile_ids(self) -> tuple[str, ...]:
        return tuple(sorted(profile.profile_id for profile in self.profiles if profile.enabled))


class ProductRuntimeDefinitionLoader:
    """Load a safe project-local runtime definition without secret surfaces."""

    def load(self, workspace: Path, *, config: ProductConfig) -> ProductRuntimeDefinition:
        try:
            root = _safe_workspace(workspace)
            path = root / ".agentforge" / _RUNTIME_FILENAME
            if not path.exists():
                raise UnsafeConfigurationError()
            raw = _load_toml(path, root=root)
            _reject_secret_keys(raw)
            if not isinstance(raw, Mapping):
                raise UnsafeConfigurationError()
            payload = dict(raw)
            provider = payload.get("provider")
            if not isinstance(provider, Mapping):
                raise UnsafeConfigurationError()
            provider_data = dict(provider)
            if isinstance(provider_data.get("mock_responses"), list):
                provider_data["mock_responses"] = tuple(provider_data["mock_responses"])
            payload["provider"] = provider_data
            profiles = payload.get("profiles")
            if not isinstance(profiles, list):
                raise UnsafeConfigurationError()
            source_digest = _canonical_digest(payload)
            definitions: list[dict[str, object]] = []
            for item in profiles:
                if not isinstance(item, Mapping):
                    raise UnsafeConfigurationError()
                profile = dict(item)
                if {
                    "config_source_identity",
                    "config_source_kind",
                    "config_source_digest",
                } & set(profile):
                    raise UnsafeConfigurationError()
                # The operator never chooses profile provenance: it is bound to
                # this exact project runtime document.
                profile["config_source_identity"] = "project-runtime/v1"
                profile["config_source_kind"] = ConfigSourceKind.PROJECT
                profile["config_source_digest"] = source_digest
                definitions.append(profile)
            payload["profiles"] = tuple(definitions)
            payload["config_source_digest"] = source_digest
            definition = ProductRuntimeDefinition.model_validate(payload)
            if definition.profile_ids != config.profile_ids:
                raise UnsafeConfigurationError()
            _validate_policy_profiles(definition)
            return definition
        except UnsafeConfigurationError:
            raise
        except (OSError, TypeError, ValueError, ValidationError):
            raise UnsafeConfigurationError() from None


class ProductApplicationFactory:
    """Construct an AgentApplication from two explicit product trust sources."""

    def __init__(self, *, runtime_loader: ProductRuntimeDefinitionLoader | None = None) -> None:
        self._runtime_loader = runtime_loader or ProductRuntimeDefinitionLoader()

    def build(self, workspace: Path, *, config: ProductConfig) -> AgentApplication:
        root = _safe_workspace(workspace)
        self._validate_database_location(root, config)
        definition = self._runtime_loader.load(root, config=config)
        artifact_root = _product_artifact_root(root)
        database = Database.from_path(config.database_path, artifact_root=artifact_root)
        database.create_schema()
        profiles = TestProfileRegistry(WorkspacePathResolver(root))
        for profile in definition.profiles:
            profiles.register(profile)

        def build_runtime() -> tuple[RuntimeComponents, ProductStartRunAssembler]:
            provider = _build_provider(
                definition.provider,
                config,
                max_output_tokens=definition.model_budget.max_output_tokens_per_request,
            )
            workflow = RepairWorkflow(database)
            components = RuntimeComponentFactory().build(
                RuntimeAssemblyRequest(
                    database=database,
                    workspace=root,
                    provider=provider,
                    policy=definition.policy,
                    context_policy=definition.context_policy,
                    model_budget=definition.model_budget,
                    profiles=profiles,
                    events=EventRepository(database),
                    repair_workflow=workflow,
                    max_output_chars=definition.max_output_chars,
                )
            )
            assembler = ProductStartRunAssembler(
                config=config,
                workspace=root,
                components=components,
                profiles=profiles,
                repair_policy=definition.policy,
                model_budget=definition.model_budget,
            )
            return components, assembler

        return AgentApplication(
            database,
            None,
            profiles=profiles,
            runtime_builder=build_runtime,
            database_path=config.database_path,
        )

    def build_doctor(self, workspace: Path, *, config: ProductConfig) -> Doctor:
        """Build diagnostics without creating a database, schema, or runtime."""

        root = _safe_workspace(workspace)
        self._validate_database_location(root, config)
        definition = self._runtime_loader.load(root, config=config)
        profiles = TestProfileRegistry(WorkspacePathResolver(root))
        for profile in definition.profiles:
            profiles.register(profile)
        return Doctor(
            Database.read_only_from_path(config.database_path),
            profiles,
            provider_kind=definition.provider.kind,
        )

    @staticmethod
    def _validate_database_location(root: Path, config: ProductConfig) -> None:
        if config.database_path.resolve(strict=False).parent != (root / ".agentforge").resolve(
            strict=False
        ):
            raise UnsafeConfigurationError()


def _build_provider(
    definition: ProductProviderDefinition,
    config: ProductConfig,
    *,
    max_output_tokens: int | None = None,
) -> object:
    if definition.kind == "mock":
        # Product apps are reconstructed between durable approval/resume phases.
        # Choose mock answers by persisted step rather than process-local call
        # order so the deterministic demo has the same behavior after restart.
        return MockModelProvider(
            list(definition.mock_responses), model_id=config.model, response_by_step=True
        )
    provider_type: type[OpenAIModelProvider] | type[DeepSeekModelProvider]
    if definition.kind == "openai":
        environment_name = "OPENAI_API_KEY"
        provider_type = OpenAIModelProvider
    else:
        environment_name = "DEEPSEEK_API_KEY"
        provider_type = DeepSeekModelProvider
    value = os.environ.get(environment_name)
    if not value:
        raise UnsafeConfigurationError()
    return provider_type(
        ModelProviderConfig(
            api_key=SecretStr(value),
            model=config.model,
            timeout_seconds=definition.timeout_seconds,
            temperature=definition.temperature,
            max_output_tokens=max_output_tokens,
        )
    )


def _validate_policy_profiles(definition: ProductRuntimeDefinition) -> None:
    by_id = {profile.profile_id: profile for profile in definition.profiles if profile.enabled}
    if len(by_id) != len(definition.profile_ids):
        raise UnsafeConfigurationError()
    if not set(definition.policy.allowed_development_test_profiles).issubset(by_id):
        raise UnsafeConfigurationError()
    if any(
        by_id[profile_id].purpose is not ProfilePurpose.DEVELOPMENT
        for profile_id in definition.policy.allowed_development_test_profiles
    ):
        raise UnsafeConfigurationError()
    final = by_id.get(definition.policy.final_verification_profile_id)
    if final is None or final.purpose is not ProfilePurpose.VERIFICATION:
        raise UnsafeConfigurationError()
    # The project runtime definition is the explicit operator trust source for
    # executable profiles.  A repair policy must never authorize the model to
    # rewrite it and solicit a different trust decision.
    protected_runtime = ".agentforge/runtime.toml"
    if (
        definition.policy.allows_write(protected_runtime, creating=False)
        or definition.policy.allows_write(protected_runtime, creating=True)
    ):
        raise UnsafeConfigurationError()


def _reject_secret_keys(value: object) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if type(key) is not str:
                raise UnsafeConfigurationError()
            parts = {part for part in key.lower().replace("-", "_").split("_") if part}
            if parts & _SECRET_COMPONENTS:
                raise UnsafeConfigurationError()
            _reject_secret_keys(nested)
    elif isinstance(value, list):
        for item in value:
            _reject_secret_keys(item)


def _canonical_digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _product_artifact_root(workspace: Path) -> Path:
    # Verification artifacts must not live under the model-writable workspace.
    identity = hashlib.sha256(str(workspace).encode("utf-8")).hexdigest()
    return workspace.parent / ".agentforge-artifacts" / identity
