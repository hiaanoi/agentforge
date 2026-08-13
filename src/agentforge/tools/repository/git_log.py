from __future__ import annotations

from pathlib import Path
from typing import cast

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from agentforge.domain.enums import ToolRisk
from agentforge.domain.models import ToolResult, ToolSpec
from agentforge.tools.paths import WorkspacePathResolver
from agentforge.tools.repository.git_status import (
    _DEFAULT_MAX_OUTPUT_BYTES,
    _require_success,
    _run_fixed_git,
    _safe_text,
    _TrustedGitExecutable,
)


class GitLogArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    max_entries: int = Field(default=20, ge=1, le=100)


class GitLogTool:
    def __init__(
        self,
        resolver: WorkspacePathResolver,
        *,
        timeout_seconds: float = 10.0,
        max_output_bytes: int = _DEFAULT_MAX_OUTPUT_BYTES,
        git_executable: Path | None = None,
    ) -> None:
        if timeout_seconds <= 0 or max_output_bytes <= 0:
            raise ValueError("Git tool bounds must be positive")
        self._resolver = resolver
        self._timeout_seconds = timeout_seconds
        self._max_output_bytes = max_output_bytes
        self._git_executable = _TrustedGitExecutable.discover(git_executable)

    @classmethod
    def arguments_model(cls) -> BaseModel:
        return GitLogArguments()

    @property
    def input_model(self) -> type[BaseModel]:
        return GitLogArguments

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="git_log",
            description="Return a bounded recent commit summary for the workspace repository.",
            input_schema=self.input_model.model_json_schema(),
            risk_level=ToolRisk.READ,
            timeout_seconds=self._timeout_seconds,
        )

    def execute(self, arguments: BaseModel) -> ToolResult:
        parsed = GitLogArguments.model_validate(arguments)
        result = _run_fixed_git(
            self._resolver,
            (
                "log",
                "-n",
                str(parsed.max_entries),
                "--pretty=format:%H%x09%s",
                "--no-decorate",
            ),
            timeout_seconds=self._timeout_seconds,
            max_output_bytes=self._max_output_bytes,
            executable=self._git_executable,
        )
        empty_repository = (
            result.returncode == 128
            and not result.stdout
            and result.stderr.startswith(b"fatal: your current branch ")
            and result.stderr.rstrip().endswith(b" does not have any commits yet")
        )
        if not empty_repository:
            _require_success(result, operation="log")
        text = _safe_text(result.stdout, self._max_output_bytes)
        entries: list[dict[str, str]] = []
        malformed = False
        for line in text.splitlines():
            commit, separator, subject = line.partition("\t")
            if not separator or len(commit) != result.object_id_length or any(
                character not in "0123456789abcdef" for character in commit
            ):
                malformed = True
                break
            entries.append({"commit": commit, "subject": subject})
        truncated = result.stdout_truncated or malformed
        output: dict[str, JsonValue] = {
            "entries": cast(list[JsonValue], entries),
            "returned_entries": len(entries),
            "truncated": truncated,
        }
        return ToolResult(
            success=True,
            output=output,
            truncated=truncated,
            metadata={"captured_bytes": len(result.stdout)},
        )
