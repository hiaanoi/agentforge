from pydantic import BaseModel, ConfigDict, Field, JsonValue

from agentforge.domain.enums import PathKind, ToolRisk
from agentforge.domain.models import ToolResult, ToolSpec
from agentforge.policy.sensitive import SensitiveFilePolicy
from agentforge.tools.paths import WorkspacePathResolver
from agentforge.tools.repository.common import iter_safe_files


class ListFilesArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = "."
    recursive: bool = False
    max_results: int = Field(default=200, ge=1, le=10_000)


class ListFilesTool:
    def __init__(
        self,
        resolver: WorkspacePathResolver,
        sensitive_files: SensitiveFilePolicy,
    ) -> None:
        self._resolver = resolver
        self._sensitive_files = sensitive_files

    @property
    def input_model(self) -> type[BaseModel]:
        return ListFilesArguments

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="list_files",
            description="List non-sensitive files within a workspace directory.",
            input_schema=self.input_model.model_json_schema(),
            risk_level=ToolRisk.READ,
            path_argument="path",
            path_kind=PathKind.DIRECTORY,
        )

    def execute(self, arguments: BaseModel) -> ToolResult:
        parsed = ListFilesArguments.model_validate(arguments)
        root = self._resolver.resolve(parsed.path, PathKind.DIRECTORY)
        files: list[JsonValue] = []
        truncated = False
        for _, relative in iter_safe_files(
            self._resolver,
            self._sensitive_files,
            root,
            recursive=parsed.recursive,
        ):
            if len(files) >= parsed.max_results:
                truncated = True
                break
            files.append(relative)
        return ToolResult(
            success=True,
            output={
                "path": self._resolver.relative(root),
                "files": files,
                "count": len(files),
                "truncated": truncated,
            },
            truncated=truncated,
        )
