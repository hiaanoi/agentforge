from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, JsonValue


class ContextItemKind(StrEnum):
    SYSTEM = "SYSTEM"
    USER_TASK = "USER_TASK"
    TOOL_CALL = "TOOL_CALL"
    TOOL_RESULT = "TOOL_RESULT"
    APPROVAL_RESULT = "APPROVAL_RESULT"
    RUNTIME_ERROR = "RUNTIME_ERROR"
    LOOP_WARNING = "LOOP_WARNING"
    MULTI_TOOL_NORMALIZATION = "MULTI_TOOL_NORMALIZATION"
    REPAIR_CONTRACT_FEEDBACK = "REPAIR_CONTRACT_FEEDBACK"
    REPAIR_RUNTIME_STATE = "REPAIR_RUNTIME_STATE"
    EVALUATION_BASELINE_FAILURE = "EVALUATION_BASELINE_FAILURE"
    LEGACY = "LEGACY"


class ContextItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: ContextItemKind
    payload: JsonValue
    call_id: str | None = None


class ResumeContextState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_count: int = Field(default=0, ge=0)
    character_count: int = Field(default=0, ge=0)
    utf8_bytes: int = Field(default=0, ge=0)
    compacted_pairs: int = Field(default=0, ge=0)


class LoopState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recent_action_digests: list[str] = Field(default_factory=list)
    recent_result_digests: list[str] = Field(default_factory=list)
    recent_error_codes: list[str] = Field(default_factory=list)
    consecutive_same_action_result: int = Field(default=0, ge=0)
    consecutive_same_error: int = Field(default=0, ge=0)
    warning_count: int = Field(default=0, ge=0)


class ContextPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_items: int = Field(default=100, gt=0)
    max_characters: int = Field(default=20_000, gt=0)
    max_utf8_bytes: int = Field(default=40_000, gt=0)
    version: str = "1"
    system_prompt_version: str = "1"
    system_instructions: str = (
        "You are AgentForge, a read-only repository analysis agent. "
        "Use available tools when evidence is needed and cite relative file paths."
    )


class LoopPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    warning_threshold: int = Field(default=2, ge=2)
    terminal_threshold: int = Field(default=3, ge=3)


class LoopObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: LoopState
    warning: bool
    terminal: bool


class RenderedToolResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    output: dict[str, JsonValue]
    original_size: int = Field(ge=0)
    rendered_size: int = Field(ge=0)
    truncated: bool
    sha256_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    returned_count: int | None = Field(default=None, ge=0)
