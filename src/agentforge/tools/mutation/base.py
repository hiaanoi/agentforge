from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, JsonValue

from agentforge.domain.models import ToolResult
from agentforge.domain.mutations import MutationPlan
from agentforge.tools.mutation.atomic import AtomicMutationResult


@dataclass(frozen=True)
class MutationLimits:
    max_file_bytes: int = 1_048_576
    max_old_text_bytes: int = 65_536
    max_new_text_bytes: int = 262_144

    def __post_init__(self) -> None:
        if min(
            self.max_file_bytes,
            self.max_old_text_bytes,
            self.max_new_text_bytes,
        ) <= 0:
            raise ValueError("Mutation byte limits must be positive")


@runtime_checkable
class MutationTool(Protocol):
    def prepare(self, arguments: BaseModel) -> MutationPlan: ...


def mutation_tool_result(
    plan: MutationPlan,
    result: AtomicMutationResult,
    *,
    operation: str,
) -> ToolResult:
    metadata: dict[str, JsonValue] = {
        "path": plan.target_path,
        "before_sha256": plan.before_sha256,
        "after_sha256": result.actual_after_sha256,
        "bytes_written": result.bytes_written,
        "operation": operation,
    }
    return ToolResult(
        success=True,
        output={"path": plan.target_path, "status": "written"},
        metadata=metadata,
    )
