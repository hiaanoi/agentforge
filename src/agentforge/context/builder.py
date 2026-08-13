import json
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from agentforge.context.models import (
    ContextItem,
    ContextItemKind,
    ContextPolicy,
    ResumeContextState,
)
from agentforge.models.base import ModelRequest


class ContextBuildResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request: ModelRequest
    items: list[ContextItem]
    state: ResumeContextState
    removed_summaries: list[str] = Field(default_factory=list)


class ContextBuilder:
    _PROTECTED: ClassVar[set[ContextItemKind]] = {
        ContextItemKind.APPROVAL_RESULT,
        ContextItemKind.RUNTIME_ERROR,
        ContextItemKind.LOOP_WARNING,
        ContextItemKind.MULTI_TOOL_NORMALIZATION,
        ContextItemKind.REPAIR_CONTRACT_FEEDBACK,
        ContextItemKind.REPAIR_RUNTIME_STATE,
        ContextItemKind.EVALUATION_BASELINE_FAILURE,
    }

    def __init__(self, policy: ContextPolicy | None = None) -> None:
        self._policy = policy or ContextPolicy()

    def build(
        self,
        *,
        task: str,
        items: list[ContextItem],
        step_number: int = 1,
    ) -> ContextBuildResult:
        kept = [item.model_copy(deep=True) for item in items]
        removed: list[str] = []
        while not self._fits(kept):
            pair = self._oldest_removable_pair(kept)
            if pair is None:
                break
            first, second = pair
            removed.append(f"{kept[first].kind.value}:{kept[first].call_id or ''}")
            del kept[second]
            del kept[first]
        serialized = self._serialize_items(kept)
        state = ResumeContextState(
            item_count=len(kept),
            character_count=len(serialized),
            utf8_bytes=len(serialized.encode("utf-8")),
            compacted_pairs=len(removed),
        )
        return ContextBuildResult(
            request=ModelRequest(
                task=task,
                step_number=step_number,
                instructions=self._policy.system_instructions,
                history=[item.model_dump(mode="json") for item in kept],
            ),
            items=kept,
            state=state,
            removed_summaries=removed,
        )

    def _fits(self, items: list[ContextItem]) -> bool:
        serialized = self._serialize_items(items)
        return (
            len(items) <= self._policy.max_items
            and len(serialized) <= self._policy.max_characters
            and len(serialized.encode("utf-8")) <= self._policy.max_utf8_bytes
        )

    def _oldest_removable_pair(
        self, items: list[ContextItem]
    ) -> tuple[int, int] | None:
        for index, item in enumerate(items[:-1]):
            following = items[index + 1]
            if (
                item.kind is ContextItemKind.TOOL_CALL
                and following.kind is ContextItemKind.TOOL_RESULT
                and item.call_id == following.call_id
                and item.kind not in self._PROTECTED
                and following.kind not in self._PROTECTED
            ):
                return index, index + 1
        return None

    @staticmethod
    def _serialize_items(items: list[ContextItem]) -> str:
        return json.dumps(
            [item.model_dump(mode="json") for item in items],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
