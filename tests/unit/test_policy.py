from pathlib import Path

from pydantic import BaseModel

from agentforge.domain.enums import (
    PathKind,
    PolicyOutcome,
    RunStatus,
    ToolErrorCode,
    ToolRisk,
    ToolSource,
)
from agentforge.domain.models import Run, ToolSpec
from agentforge.policy.engine import PolicyEngine
from agentforge.policy.sensitive import SensitiveFilePolicy
from agentforge.tools.paths import WorkspacePathResolver


class PathArguments(BaseModel):
    path: str


def make_policy(workspace: Path) -> PolicyEngine:
    return PolicyEngine(WorkspacePathResolver(workspace), SensitiveFilePolicy())


def make_spec(**changes: object) -> ToolSpec:
    values: dict[str, object] = {
        "name": "read_file",
        "description": "Read a file",
        "input_schema": PathArguments.model_json_schema(),
        "risk_level": ToolRisk.READ,
        "path_argument": "path",
        "path_kind": PathKind.FILE,
        "protect_sensitive_path": True,
    }
    values.update(changes)
    return ToolSpec.model_validate(values)


def test_policy_allows_valid_read_and_denies_sensitive_path(tmp_path: Path) -> None:
    (tmp_path / "note.txt").write_text("ok", encoding="utf-8")
    (tmp_path / ".env").write_text("secret", encoding="utf-8")
    policy = make_policy(tmp_path)
    run = Run(task="policy")

    allowed = policy.evaluate(
        run, "read_file", make_spec(), PathArguments(path="note.txt"), arguments_valid=True
    )
    denied = policy.evaluate(
        run, "read_file", make_spec(), PathArguments(path=".env"), arguments_valid=True
    )

    assert allowed.decision is PolicyOutcome.ALLOW
    assert denied.decision is PolicyOutcome.DENY
    assert denied.metadata["error_type"] == ToolErrorCode.SENSITIVE_PATH.value


def test_policy_denies_unknown_invalid_cancelled_and_exhausted_requests(tmp_path: Path) -> None:
    (tmp_path / "note.txt").write_text("ok", encoding="utf-8")
    policy = make_policy(tmp_path)
    run = Run(task="policy", max_tool_calls=1)
    args = PathArguments(path="note.txt")

    unknown = policy.evaluate(run, "missing", None, None, arguments_valid=False)
    invalid = policy.evaluate(run, "read_file", make_spec(), None, arguments_valid=False)
    run.transition_to(RunStatus.CANCELLED)
    cancelled = policy.evaluate(run, "read_file", make_spec(), args, arguments_valid=True)
    exhausted_run = Run(task="budget", max_tool_calls=0)
    exhausted = policy.evaluate(
        exhausted_run, "read_file", make_spec(), args, arguments_valid=True
    )

    assert unknown.metadata["error_type"] == ToolErrorCode.TOOL_NOT_FOUND.value
    assert invalid.metadata["error_type"] == ToolErrorCode.INVALID_ARGUMENTS.value
    assert cancelled.matched_rule == "run_not_cancelled"
    assert exhausted.metadata["error_type"] == ToolErrorCode.TOOL_BUDGET_EXCEEDED.value


def test_policy_requires_approval_for_local_write_and_allows_approved_write(
    tmp_path: Path,
) -> None:
    (tmp_path / "note.txt").write_text("ok", encoding="utf-8")
    policy = make_policy(tmp_path)
    run = Run(task="policy")
    args = PathArguments(path="note.txt")

    write_spec = make_spec(
        name="write_file",
        risk_level=ToolRisk.WRITE,
        requires_approval=True,
    )
    write = policy.evaluate(
        run,
        "write_file",
        write_spec,
        args,
        arguments_valid=True,
    )
    approved = policy.evaluate(
        run,
        "write_file",
        write_spec,
        args,
        arguments_valid=True,
        approval_granted=True,
    )

    assert write.decision is PolicyOutcome.REQUIRE_APPROVAL
    assert write.metadata["error_type"] == ToolErrorCode.APPROVAL_REQUIRED.value
    assert approved.decision is PolicyOutcome.ALLOW
    assert approved.matched_rule == "approved_write_tool_allowed"


def test_policy_denies_write_tool_that_does_not_require_approval(tmp_path: Path) -> None:
    (tmp_path / "note.txt").write_text("ok", encoding="utf-8")
    decision = make_policy(tmp_path).evaluate(
        Run(task="policy"),
        "write_file",
        make_spec(name="write_file", risk_level=ToolRisk.WRITE),
        PathArguments(path="note.txt"),
        arguments_valid=True,
    )

    assert decision.decision is PolicyOutcome.DENY
    assert decision.matched_rule == "write_requires_approval"


def test_policy_denies_dangerous_and_non_local_tools(tmp_path: Path) -> None:
    (tmp_path / "note.txt").write_text("ok", encoding="utf-8")
    policy = make_policy(tmp_path)
    run = Run(task="policy")
    args = PathArguments(path="note.txt")

    dangerous = policy.evaluate(
        run,
        "dangerous_tool",
        make_spec(name="dangerous_tool", risk_level=ToolRisk.DANGEROUS),
        args,
        arguments_valid=True,
    )
    non_local = policy.evaluate(
        run,
        "mcp_tool",
        make_spec(name="mcp_tool", source=ToolSource.MCP),
        args,
        arguments_valid=True,
    )

    assert dangerous.matched_rule == "dangerous_tools_denied"
    assert non_local.matched_rule == "local_tools_only"
