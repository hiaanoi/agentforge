from pathlib import PurePosixPath

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

from agentforge.domain.enums import PathKind, ToolRisk
from agentforge.domain.errors import ToolRuntimeError
from agentforge.domain.models import ToolResult, ToolSpec
from agentforge.policy.sensitive import SensitiveFilePolicy
from agentforge.tools.paths import WorkspacePathResolver
from agentforge.tools.repository.common import (
    MAX_SEARCH_FILE_BYTES,
    MAX_SNIPPET_CHARS,
    decode_utf8,
    iter_safe_files,
)


class SearchTextArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=500)
    path: str = "."
    glob: str | None = Field(default=None, max_length=200)
    case_sensitive: bool = False
    max_results: int = Field(default=100, ge=1, le=1_000)

    @field_validator("query")
    @classmethod
    def query_must_contain_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Search query must not be blank")
        return stripped


class SearchTextTool:
    def __init__(
        self,
        resolver: WorkspacePathResolver,
        sensitive_files: SensitiveFilePolicy,
    ) -> None:
        self._resolver = resolver
        self._sensitive_files = sensitive_files

    @property
    def input_model(self) -> type[BaseModel]:
        return SearchTextArguments

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="search_text",
            description="Search bounded UTF-8 workspace files without invoking a shell.",
            input_schema=self.input_model.model_json_schema(),
            risk_level=ToolRisk.READ,
            path_argument="path",
            path_kind=PathKind.DIRECTORY,
        )

    def execute(self, arguments: BaseModel) -> ToolResult:
        parsed = SearchTextArguments.model_validate(arguments)
        root = self._resolver.resolve(parsed.path, PathKind.DIRECTORY)
        needle = parsed.query if parsed.case_sensitive else parsed.query.casefold()
        results: list[JsonValue] = []
        truncated = False
        skipped_large_files = 0
        for file_path, relative in iter_safe_files(
            self._resolver, self._sensitive_files, root, recursive=True
        ):
            if parsed.glob and not PurePosixPath(relative).match(parsed.glob):
                continue
            if file_path.stat().st_size > MAX_SEARCH_FILE_BYTES:
                skipped_large_files += 1
                continue
            try:
                with file_path.open("rb") as handle:
                    data = handle.read(MAX_SEARCH_FILE_BYTES + 1)
                if len(data) > MAX_SEARCH_FILE_BYTES:
                    skipped_large_files += 1
                    continue
                content = decode_utf8(data)
            except ToolRuntimeError:
                continue
            for line_number, line in enumerate(content.splitlines(), start=1):
                haystack = line if parsed.case_sensitive else line.casefold()
                if needle not in haystack:
                    continue
                if len(results) >= parsed.max_results:
                    truncated = True
                    break
                snippet = line.strip()
                if len(snippet) > MAX_SNIPPET_CHARS:
                    snippet = snippet[:MAX_SNIPPET_CHARS]
                results.append(
                    {"path": relative, "line_number": line_number, "snippet": snippet}
                )
            if truncated:
                break
        return ToolResult(
            success=True,
            output={
                "query": parsed.query,
                "path": self._resolver.relative(root),
                "results": results,
                "count": len(results),
                "truncated": truncated,
            },
            truncated=truncated,
            metadata={"skipped_large_files": skipped_large_files},
        )
