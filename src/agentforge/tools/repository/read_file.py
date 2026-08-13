import hashlib

from pydantic import BaseModel, ConfigDict, Field

from agentforge.domain.enums import PathKind, ToolRisk
from agentforge.domain.models import ToolResult, ToolSpec
from agentforge.policy.sensitive import SensitiveFilePolicy
from agentforge.tools.paths import WorkspacePathResolver
from agentforge.tools.repository.common import decode_utf8


class ReadFileArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    max_bytes: int = Field(default=65_536, ge=1, le=1_048_576)


class ReadFileTool:
    def __init__(
        self,
        resolver: WorkspacePathResolver,
        sensitive_files: SensitiveFilePolicy,
    ) -> None:
        self._resolver = resolver
        self._sensitive_files = sensitive_files

    @property
    def input_model(self) -> type[BaseModel]:
        return ReadFileArguments

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="read_file",
            description=(
                "Read bounded UTF-8 text and the complete SHA-256 digest "
                "from a non-sensitive workspace file."
            ),
            input_schema=self.input_model.model_json_schema(),
            risk_level=ToolRisk.READ,
            path_argument="path",
            path_kind=PathKind.FILE,
            protect_sensitive_path=True,
        )

    def execute(self, arguments: BaseModel) -> ToolResult:
        parsed = ReadFileArguments.model_validate(arguments)
        path = self._resolver.resolve(parsed.path, PathKind.FILE)
        relative = self._resolver.relative(path)
        self._sensitive_files.require_allowed(relative)
        preview = bytearray()
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as handle:
            while chunk := handle.read(65_536):
                size += len(chunk)
                digest.update(chunk)
                remaining = parsed.max_bytes + 1 - len(preview)
                if remaining > 0:
                    preview.extend(chunk[:remaining])
        truncated = size > parsed.max_bytes
        bounded = bytes(preview[: parsed.max_bytes])
        content = decode_utf8(bounded, allow_truncated_tail=truncated)
        sha256 = digest.hexdigest()
        return ToolResult(
            success=True,
            output={
                "path": relative,
                "content": content,
                "byte_size": size,
                "bytes_read": len(bounded),
                "truncated": truncated,
                "sha256": sha256,
            },
            truncated=truncated,
            metadata={
                "path": relative,
                "byte_size": size,
                "sha256": sha256,
            },
        )
