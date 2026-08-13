import os
import subprocess

from pydantic import BaseModel, ConfigDict, Field

from agentforge.domain.enums import ToolErrorCode, ToolRisk
from agentforge.domain.errors import ToolExecutionError
from agentforge.domain.models import ToolResult, ToolSpec
from agentforge.tools.paths import WorkspacePathResolver
from agentforge.tools.repository.common import decode_utf8


class GetGitDiffArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_chars: int = Field(default=100_000, ge=1, le=1_000_000)


class GetGitDiffTool:
    def __init__(self, resolver: WorkspacePathResolver, *, timeout_seconds: float = 10.0) -> None:
        self._resolver = resolver
        self._timeout_seconds = timeout_seconds

    @property
    def input_model(self) -> type[BaseModel]:
        return GetGitDiffArguments

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="get_git_diff",
            description="Return the bounded working-tree diff for the workspace Git repository.",
            input_schema=self.input_model.model_json_schema(),
            risk_level=ToolRisk.READ,
            timeout_seconds=self._timeout_seconds,
        )

    def execute(self, arguments: BaseModel) -> ToolResult:
        parsed = GetGitDiffArguments.model_validate(arguments)
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.upper().startswith("GIT_")
        }
        environment.update(
            {"GIT_EXTERNAL_DIFF": "", "GIT_OPTIONAL_LOCKS": "0", "GIT_PAGER": "cat"}
        )
        probe = self._run_git(
            ["git", "-c", "core.pager=cat", "rev-parse", "--is-inside-work-tree"],
            environment,
        )
        if probe.returncode != 0 or probe.stdout.strip() != b"true":
            raise ToolExecutionError(
                ToolErrorCode.NOT_GIT_REPOSITORY,
                "Workspace is not a Git repository",
            )
        diff = self._run_git(
            [
                "git",
                "-c",
                "core.pager=cat",
                "diff",
                "--no-ext-diff",
                "--no-textconv",
                "--no-color",
                "--",
            ],
            environment,
        )
        if diff.returncode != 0:
            raise ToolExecutionError(
                ToolErrorCode.GIT_COMMAND_FAILED,
                "Git could not produce a working-tree diff",
            )
        text = decode_utf8(diff.stdout)
        truncated = len(text) > parsed.max_chars
        bounded = text[: parsed.max_chars]
        return ToolResult(
            success=True,
            output={
                "diff": bounded,
                "characters": len(bounded),
                "truncated": truncated,
            },
            truncated=truncated,
            metadata={"original_characters": len(text)},
        )

    def _run_git(
        self,
        command: list[str],
        environment: dict[str, str],
    ) -> subprocess.CompletedProcess[bytes]:
        try:
            return subprocess.run(
                command,
                cwd=self._resolver.workspace,
                env=environment,
                capture_output=True,
                check=False,
                shell=False,
                timeout=self._timeout_seconds,
            )
        except FileNotFoundError as exc:
            raise ToolExecutionError(
                ToolErrorCode.GIT_COMMAND_FAILED, "Git executable is unavailable"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise ToolExecutionError(
                ToolErrorCode.TOOL_TIMEOUT, "Git diff operation timed out"
            ) from exc
