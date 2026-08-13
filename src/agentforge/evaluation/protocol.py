import hashlib
import json
from enum import StrEnum
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agentforge.context.models import ContextPolicy
from agentforge.domain.enums import MultiToolResponsePolicy
from agentforge.domain.repair import CompletionCorrectionMode
from agentforge.models.domain import ModelBudget
from agentforge.models.identity import is_exact_or_dated_openai_snapshot

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_REPLACEABLE_INFRASTRUCTURE_FAILURES = {
    "BASELINE_LAUNCH_FAILURE",
    "BASELINE_TIMEOUT",
    "MODEL_PROVIDER_ERROR",
    "MODEL_RATE_LIMITED",
    "MODEL_TIMEOUT",
    "MODEL_TRANSPORT_ERROR",
    "PILOT_PREPARATION_ERROR",
    "PILOT_RECOVERY_INTERRUPTED",
    "WORKSPACE_PREPARATION_ERROR",
}


def canonical_digest(value: object) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def text_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class EvaluationExecutionMode(StrEnum):
    OFFLINE_TEST = "OFFLINE_TEST"
    REAL_MODEL = "REAL_MODEL"


class ReplacementMode(StrEnum):
    INFRASTRUCTURE_ONLY = "INFRASTRUCTURE_ONLY"


class ProviderBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: Literal["mock", "openai"]
    model_id: str = Field(min_length=1, max_length=200)
    response_model_id: str = Field(default="", max_length=200)
    timeout_seconds: float = Field(gt=0, le=600)
    max_retries: int = Field(ge=0, le=10)
    store: Literal[False] = False
    max_output_tokens: int | None = Field(default=None, gt=0)
    multi_tool_response_policy: MultiToolResponsePolicy
    max_function_calls_per_response: int = Field(ge=1, le=32)
    configuration_digest: str = ""

    @model_validator(mode="after")
    def validate_configuration_digest(self) -> Self:
        response_model_id = self.response_model_id or self.model_id
        if self.provider == "mock":
            valid_response_model = response_model_id == self.model_id
        else:
            valid_response_model = is_exact_or_dated_openai_snapshot(
                self.model_id,
                response_model_id,
            )
        if not valid_response_model:
            raise ValueError(
                "Provider response model must be the exact requested model or its dated snapshot"
            )
        object.__setattr__(self, "response_model_id", response_model_id)
        expected = canonical_digest(
            self.model_dump(mode="json", exclude={"configuration_digest"})
        )
        if self.configuration_digest and self.configuration_digest != expected:
            raise ValueError("configuration_digest does not match provider settings")
        object.__setattr__(self, "configuration_digest", expected)
        return self


class ModelBudgetBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_model_requests: int = Field(default=20, gt=0)
    max_retries: int = Field(default=2, ge=0, le=10)
    max_output_tokens_per_request: int | None = Field(default=None, gt=0)
    max_total_input_tokens: int | None = Field(default=None, gt=0)
    max_total_output_tokens: int | None = Field(default=None, gt=0)
    max_total_tokens: int | None = Field(default=100_000, gt=0)

    @model_validator(mode="after")
    def validate_token_envelope(self) -> Self:
        if (
            self.max_output_tokens_per_request is not None
            and self.max_total_output_tokens is not None
            and self.max_output_tokens_per_request
            > self.max_total_output_tokens
        ):
            raise ValueError(
                "Per-request output limit exceeds total output budget"
            )
        if (
            self.max_total_input_tokens is not None
            and self.max_total_output_tokens is not None
            and self.max_total_tokens is not None
            and self.max_total_input_tokens + self.max_total_output_tokens
            > self.max_total_tokens
        ):
            raise ValueError(
                "Input and output budgets exceed the total token budget"
            )
        return self

    def to_domain(self) -> ModelBudget:
        return ModelBudget.model_validate(self.model_dump(mode="json"))


class ContextPolicyBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_items: int = Field(default=100, gt=0)
    max_characters: int = Field(default=20_000, gt=0)
    max_utf8_bytes: int = Field(default=40_000, gt=0)
    version: str = Field(min_length=1, max_length=40)
    system_prompt_version: str = Field(min_length=1, max_length=40)
    system_instructions: str = Field(min_length=1, max_length=20_000)

    def to_domain(self) -> ContextPolicy:
        return ContextPolicy.model_validate(self.model_dump(mode="json"))


class ReplacementPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Literal[ReplacementMode.INFRASTRUCTURE_ONLY] = (
        ReplacementMode.INFRASTRUCTURE_ONLY
    )
    max_replacements_per_slot: int = Field(default=1, ge=0, le=3)
    replaceable_failure_categories: tuple[str, ...] = ()

    @field_validator("replaceable_failure_categories", mode="before")
    @classmethod
    def normalize_categories(cls, value: object) -> tuple[str, ...]:
        if not isinstance(value, (list, tuple)):
            raise ValueError("Replacement categories must be a list or tuple")
        normalized = tuple(
            sorted({str(item).strip().upper() for item in value if str(item).strip()})
        )
        if any(item not in _REPLACEABLE_INFRASTRUCTURE_FAILURES for item in normalized):
            raise ValueError("Only allowlisted infrastructure failures can be replaced")
        return normalized

    def allows(self, failure_category: str) -> bool:
        return failure_category.upper() in self.replaceable_failure_categories


class PlatformBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    os_family: Literal["WINDOWS", "POSIX"]
    python_implementation: str = Field(min_length=1, max_length=100)
    python_version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    executable_path: str = Field(min_length=1, max_length=4096)
    executable_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def validate_executable(self) -> Self:
        path = Path(self.executable_path)
        if not path.is_absolute():
            raise ValueError("Python executable path must be absolute")
        try:
            resolved = path.resolve(strict=True)
            digest = hashlib.sha256(resolved.read_bytes()).hexdigest()
        except OSError as exc:
            raise ValueError("Python executable could not be verified") from exc
        if not resolved.is_file():
            raise ValueError("Python executable must be a file")
        if digest != self.executable_sha256:
            raise ValueError("Python executable digest does not match")
        object.__setattr__(self, "executable_path", str(resolved))
        return self


class EvaluationProtocol(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    protocol_name: str = Field(min_length=1, max_length=200)
    execution_mode: EvaluationExecutionMode
    task_id: str = Field(pattern=r"^[a-z0-9-]+$", max_length=200)
    fixture_registry_digest: str = Field(pattern=_SHA256_PATTERN)
    fixture_asset_digest: str = Field(pattern=_SHA256_PATTERN)
    expected_baseline_fingerprint_digest: str = Field(pattern=_SHA256_PATTERN)
    task_policy_digest: str = Field(pattern=_SHA256_PATTERN)
    test_profile_template_digest: str = Field(pattern=_SHA256_PATTERN)
    provider_binding: ProviderBinding
    model_budget: ModelBudgetBinding
    system_prompt_version: int = Field(gt=0)
    system_prompt: str = Field(min_length=1, max_length=20_000)
    system_prompt_digest: str = ""
    task_prompt: str = Field(min_length=1, max_length=20_000)
    task_prompt_digest: str = ""
    tool_schema_digest: str = Field(pattern=_SHA256_PATTERN)
    context_policy: ContextPolicyBinding
    completion_correction_mode: CompletionCorrectionMode
    repetition_count: int = Field(gt=0, le=100)
    replacement_policy: ReplacementPolicy
    platform_binding: PlatformBinding
    real_model_authorized: bool
    protocol_digest: str = ""

    @model_validator(mode="after")
    def validate_and_digest(self) -> Self:
        expected_system = text_digest(self.system_prompt)
        if self.system_prompt_digest and self.system_prompt_digest != expected_system:
            raise ValueError("system_prompt_digest does not match system_prompt")
        expected_task = text_digest(self.task_prompt)
        if self.task_prompt_digest and self.task_prompt_digest != expected_task:
            raise ValueError("task_prompt_digest does not match task_prompt")
        if self.context_policy.system_instructions != self.system_prompt:
            raise ValueError("Context policy must use the frozen system prompt")
        if self.context_policy.system_prompt_version != str(
            self.system_prompt_version
        ):
            raise ValueError("Context policy system prompt version does not match")
        if self.model_budget.max_retries != self.provider_binding.max_retries:
            raise ValueError("Model and provider retry limits must match")
        if self.execution_mode is EvaluationExecutionMode.REAL_MODEL:
            if not self.real_model_authorized:
                raise ValueError("REAL_MODEL execution must be explicitly authorized")
            if self.provider_binding.provider != "openai":
                raise ValueError("REAL_MODEL execution requires the OpenAI provider")
        else:
            if self.real_model_authorized:
                raise ValueError("OFFLINE_TEST cannot carry real-model authorization")
            if self.provider_binding.provider != "mock":
                raise ValueError("OFFLINE_TEST requires the mock provider")
        object.__setattr__(self, "system_prompt_digest", expected_system)
        object.__setattr__(self, "task_prompt_digest", expected_task)
        expected_protocol = canonical_digest(
            self.model_dump(
                mode="json",
                exclude={
                    "protocol_digest",
                },
            )
        )
        if self.protocol_digest and self.protocol_digest != expected_protocol:
            raise ValueError("protocol_digest does not match normalized protocol")
        object.__setattr__(self, "protocol_digest", expected_protocol)
        return self
