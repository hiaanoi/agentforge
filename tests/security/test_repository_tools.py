import hashlib
import subprocess
from datetime import timedelta
from pathlib import Path

import pytest

from agentforge.application.run_driver import RunOwnership
from agentforge.domain.enums import EventType, ToolErrorCode
from agentforge.domain.models import Run
from agentforge.persistence.database import Database
from agentforge.persistence.repositories import EventRepository, RunRepository
from agentforge.persistence.run_leases import RunLeaseStore
from agentforge.policy.engine import PolicyEngine
from agentforge.policy.sensitive import SensitiveFilePolicy
from agentforge.tools.executor import ToolExecutor
from agentforge.tools.paths import WorkspacePathResolver
from agentforge.tools.registry import ToolRegistry
from agentforge.tools.repository import (
    GetGitDiffTool,
    ListFilesTool,
    ReadFileTool,
    SearchTextTool,
)


def make_repository_harness(
    tmp_path: Path,
) -> tuple[ToolExecutor, Run, EventRepository, Database, Path, RunOwnership]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    resolver = WorkspacePathResolver(workspace)
    sensitive = SensitiveFilePolicy()
    database = Database.from_path(tmp_path / "repository-tools.sqlite3")
    database.create_schema()
    runs = RunRepository(database)
    events = EventRepository(database)
    run = runs.create(Run(task="repository tools", max_tool_calls=100))
    registry = ToolRegistry(
        [
            ListFilesTool(resolver, sensitive),
            ReadFileTool(resolver, sensitive),
            SearchTextTool(resolver, sensitive),
            GetGitDiffTool(resolver),
        ]
    )
    executor = ToolExecutor(
        registry,
        PolicyEngine(resolver, sensitive),
        events,
        runs,
        max_output_chars=200_000,
    )
    lease = RunLeaseStore(database).acquire(
        run.run_id, owner_id="test:repository-tools", ttl=timedelta(seconds=30)
    )
    return (
        executor,
        run,
        events,
        database,
        workspace,
        RunOwnership(lambda: lease.authority),
    )


def output_dict(result_output: object) -> dict[str, object]:
    assert isinstance(result_output, dict)
    return result_output


@pytest.mark.asyncio
async def test_list_files_is_sorted_bounded_and_ignores_internal_directories(
    tmp_path: Path,
) -> None:
    executor, run, _, database, workspace, ownership = make_repository_harness(tmp_path)
    (workspace / "b.txt").write_text("b", encoding="utf-8")
    (workspace / "a.txt").write_text("a", encoding="utf-8")
    (workspace / "nested").mkdir()
    (workspace / "nested" / "c.txt").write_text("c", encoding="utf-8")
    (workspace / ".git").mkdir()
    (workspace / ".git" / "config").write_text("hidden", encoding="utf-8")

    bounded = await executor.execute(
        run,
        "list_files",
        {"path": ".", "recursive": True, "max_results": 1},
        ownership=ownership,
    )
    non_recursive = await executor.execute(
        run,
        "list_files",
        {"path": ".", "recursive": False, "max_results": 10},
        ownership=ownership,
    )
    complete = await executor.execute(
        run,
        "list_files",
        {"path": ".", "recursive": True, "max_results": 10},
        ownership=ownership,
    )

    assert output_dict(bounded.output)["files"] == ["a.txt"]
    assert bounded.truncated
    assert output_dict(non_recursive.output)["files"] == ["a.txt", "b.txt"]
    assert output_dict(complete.output)["files"] == ["a.txt", "b.txt", "nested/c.txt"]
    database.close()


@pytest.mark.asyncio
async def test_read_file_reads_utf8_and_truncates_large_content(tmp_path: Path) -> None:
    executor, run, _, database, workspace, ownership = make_repository_harness(tmp_path)
    (workspace / "note.txt").write_text("你好 AgentForge", encoding="utf-8")

    complete = await executor.execute(run, "read_file", {"path": "note.txt"}, ownership=ownership)
    truncated = await executor.execute(
        run,
        "read_file",
        {"path": "note.txt", "max_bytes": 6},
        ownership=ownership,
    )

    complete_output = output_dict(complete.output)
    truncated_output = output_dict(truncated.output)
    expected_digest = hashlib.sha256((workspace / "note.txt").read_bytes()).hexdigest()
    assert complete_output["sha256"] == expected_digest
    assert truncated_output["sha256"] == expected_digest
    assert complete_output["content"] == "你好 AgentForge"
    assert complete_output["path"] == "note.txt"
    assert truncated_output["content"] == "你好"
    assert truncated.truncated
    assert truncated_output["byte_size"] > truncated_output["bytes_read"]
    database.close()


@pytest.mark.asyncio
async def test_read_file_rejects_binary_and_sensitive_files(tmp_path: Path) -> None:
    executor, run, events, database, workspace, ownership = make_repository_harness(tmp_path)
    (workspace / "binary.bin").write_bytes(b"abc\x00def")
    (workspace / ".env").write_text("TOKEN=value", encoding="utf-8")
    (workspace / ".ENV").write_text("TOKEN=value", encoding="utf-8")
    (workspace / ".env.local").write_text("TOKEN=value", encoding="utf-8")
    (workspace / "id_rsa").write_text("key", encoding="utf-8")
    (workspace / "service.key").write_text("key", encoding="utf-8")
    (workspace / ".git").mkdir()
    (workspace / ".git" / "config").write_text("config", encoding="utf-8")

    binary = await executor.execute(run, "read_file", {"path": "binary.bin"}, ownership=ownership)
    sensitive_results = [
        await executor.execute(run, "read_file", {"path": path}, ownership=ownership)
        for path in (".env", ".ENV", ".env.local", "id_rsa", "service.key", ".git/config")
    ]

    assert binary.error_type is ToolErrorCode.BINARY_FILE
    assert all(result.error_type is ToolErrorCode.SENSITIVE_PATH for result in sensitive_results)
    event_types = [event.event_type for event in events.list_for_run(run.run_id)]
    assert event_types.count(EventType.TOOL_STARTED) == 1
    database.close()


@pytest.mark.asyncio
async def test_read_file_standardizes_invalid_utf8(tmp_path: Path) -> None:
    executor, run, _, database, workspace, ownership = make_repository_harness(tmp_path)
    (workspace / "invalid.txt").write_bytes(b"valid-prefix\xffinvalid")

    result = await executor.execute(run, "read_file", {"path": "invalid.txt"}, ownership=ownership)

    assert result.error_type is ToolErrorCode.ENCODING_ERROR
    database.close()


@pytest.mark.asyncio
async def test_search_text_reports_lines_skips_sensitive_and_limits_results(
    tmp_path: Path,
) -> None:
    executor, run, _, database, workspace, ownership = make_repository_harness(tmp_path)
    (workspace / "a.py").write_text("first\nNeedle one\nneedle two\n", encoding="utf-8")
    (workspace / "b.py").write_text("needle three\n", encoding="utf-8")
    (workspace / ".env").write_text("needle secret\n", encoding="utf-8")
    (workspace / "binary.bin").write_bytes(b"needle\x00hidden")

    result = await executor.execute(
        run,
        "search_text",
        {
            "query": "needle",
            "path": ".",
            "glob": "*.py",
            "case_sensitive": False,
            "max_results": 2,
        },
        ownership=ownership,
    )

    output = output_dict(result.output)
    matches = output["results"]
    assert isinstance(matches, list)
    assert matches == [
        {"path": "a.py", "line_number": 2, "snippet": "Needle one"},
        {"path": "a.py", "line_number": 3, "snippet": "needle two"},
    ]
    assert result.truncated
    assert all(match["path"] != ".env" for match in matches)
    database.close()


@pytest.mark.asyncio
async def test_search_text_honors_case_glob_and_large_file_limit(tmp_path: Path) -> None:
    executor, run, _, database, workspace, ownership = make_repository_harness(tmp_path)
    (workspace / "a.py").write_text("Needle\n", encoding="utf-8")
    (workspace / "b.py").write_text("needle\n", encoding="utf-8")
    (workspace / "c.txt").write_text("Needle\n", encoding="utf-8")
    (workspace / "large.py").write_bytes(b"Needle\n" + b"x" * 1_048_576)

    result = await executor.execute(
        run,
        "search_text",
        {"query": "Needle", "glob": "*.py", "case_sensitive": True},
        ownership=ownership,
    )

    output = output_dict(result.output)
    assert output["results"] == [{"path": "a.py", "line_number": 1, "snippet": "Needle"}]
    assert result.metadata["skipped_large_files"] == 1
    database.close()


@pytest.mark.asyncio
async def test_search_text_accepts_a_single_workspace_file(tmp_path: Path) -> None:
    executor, run, _, database, workspace, ownership = make_repository_harness(tmp_path)
    (workspace / "target.py").write_text(
        "first line\nNeedle in one file\n",
        encoding="utf-8",
    )
    (workspace / "other.py").write_text("Needle elsewhere\n", encoding="utf-8")
    (workspace / ".env").write_text("Needle secret\n", encoding="utf-8")

    result = await executor.execute(
        run,
        "search_text",
        {"query": "needle", "path": "target.py"},
        ownership=ownership,
    )

    output = output_dict(result.output)
    assert result.success
    assert output["path"] == "target.py"
    assert output["results"] == [
        {"path": "target.py", "line_number": 2, "snippet": "Needle in one file"}
    ]
    sensitive = await executor.execute(
        run,
        "search_text",
        {"query": "needle", "path": ".env"},
        ownership=ownership,
    )
    assert sensitive.error_type is ToolErrorCode.SENSITIVE_PATH
    database.close()


def run_git(workspace: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", *arguments],
        cwd=workspace,
        capture_output=True,
        check=True,
        shell=False,
        timeout=10,
    )


@pytest.mark.asyncio
async def test_get_git_diff_returns_and_truncates_fixed_command_output(tmp_path: Path) -> None:
    executor, run, _, database, workspace, ownership = make_repository_harness(tmp_path)
    run_git(workspace, "init", "-q")
    tracked = workspace / "tracked.txt"
    tracked.write_text("before\n", encoding="utf-8")
    run_git(workspace, "add", "tracked.txt")
    run_git(
        workspace,
        "-c",
        "user.name=AgentForge Test",
        "-c",
        "user.email=agentforge@example.invalid",
        "commit",
        "-qm",
        "baseline",
    )
    tracked.write_text("after\n" + ("changed\n" * 20), encoding="utf-8")

    result = await executor.execute(run, "get_git_diff", {"max_chars": 80}, ownership=ownership)

    output = output_dict(result.output)
    assert result.success
    assert result.truncated
    assert output["truncated"] is True
    assert len(str(output["diff"])) == 80
    database.close()


@pytest.mark.asyncio
async def test_get_git_diff_ignores_inherited_git_control_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executor, run, _, database, workspace, ownership = make_repository_harness(tmp_path)
    run_git(workspace, "init", "-q")
    tracked = workspace / "tracked.txt"
    tracked.write_text("before\n", encoding="utf-8")
    run_git(workspace, "add", "tracked.txt")
    run_git(
        workspace,
        "-c",
        "user.name=AgentForge Test",
        "-c",
        "user.email=agentforge@example.invalid",
        "commit",
        "-qm",
        "baseline",
    )
    tracked.write_text("after\n", encoding="utf-8")
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "attacker-controlled-git-dir"))
    monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path))

    result = await executor.execute(run, "get_git_diff", {}, ownership=ownership)

    assert result.success
    assert "after" in str(output_dict(result.output)["diff"])
    database.close()


@pytest.mark.asyncio
async def test_get_git_diff_returns_standard_error_outside_repository(tmp_path: Path) -> None:
    executor, run, events, database, _, ownership = make_repository_harness(tmp_path)

    result = await executor.execute(run, "get_git_diff", {}, ownership=ownership)

    assert result.error_type is ToolErrorCode.NOT_GIT_REPOSITORY
    assert events.list_for_run(run.run_id)[-1].event_type is EventType.TOOL_FAILED
    database.close()
