from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import cast

import pytest
from pydantic import JsonValue

import agentforge.tools.repository.git_status as git_status_module
from agentforge.domain.enums import ToolErrorCode
from agentforge.domain.errors import ToolExecutionError
from agentforge.tools.paths import WorkspacePathResolver
from agentforge.tools.repository.git_log import GitLogArguments, GitLogTool
from agentforge.tools.repository.git_status import GitStatusArguments, GitStatusTool


def _git(workspace: Path, *arguments: str) -> None:
    environment = dict(os.environ)
    environment.update(
        {
            "GIT_AUTHOR_EMAIL": "agentforge@example.invalid",
            "GIT_AUTHOR_NAME": "AgentForge",
            "GIT_COMMITTER_EMAIL": "agentforge@example.invalid",
            "GIT_COMMITTER_NAME": "AgentForge",
        }
    )
    subprocess.run(
        ["git", *arguments],
        cwd=workspace,
        env=environment,
        check=True,
        capture_output=True,
        shell=False,
    )


def _git_output(workspace: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=workspace,
        check=True,
        capture_output=True,
        shell=False,
        text=True,
    )
    return completed.stdout.strip()


def _redirect_directory(link: Path, target: Path) -> None:
    backup = link.with_name(f"{link.name}-original")
    link.rename(backup)
    _link_directory(link, target)


def _link_directory(link: Path, target: Path) -> None:
    if os.name == "nt":
        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            check=False,
            capture_output=True,
            shell=False,
        )
        if completed.returncode != 0:
            pytest.skip("Windows junction creation is unavailable")
    else:
        link.symlink_to(target, target_is_directory=True)


def _loose_object_fanout(git_workspace: Path) -> Path:
    candidates = sorted(
        candidate
        for candidate in (git_workspace / ".git" / "objects").iterdir()
        if candidate.is_dir()
        and len(candidate.name) == 2
        and all(character in "0123456789abcdef" for character in candidate.name)
    )
    assert candidates
    return candidates[0]


@pytest.fixture
def git_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "repository"
    workspace.mkdir()
    _git(workspace, "init", "--quiet")
    _git(workspace, "config", "core.autocrlf", "false")
    (workspace / "tracked.txt").write_text("initial\n", encoding="utf-8")
    _git(workspace, "add", "tracked.txt")
    _git(workspace, "commit", "--quiet", "-m", "initial commit")
    return workspace


def test_git_status_reports_bounded_workspace_state(git_workspace: Path) -> None:
    (git_workspace / "untracked.txt").write_text("new\n", encoding="utf-8")

    result = GitStatusTool(WorkspacePathResolver(git_workspace)).execute(
        GitStatusArguments()
    )

    assert result.success is True
    assert result.output == {
        "status": "?? untracked.txt\n",
        "bytes": 17,
        "truncated": False,
    }
    assert result.truncated is False


def test_git_status_caps_bytes_without_exposing_an_argv(git_workspace: Path) -> None:
    for index in range(20):
        (git_workspace / f"untracked-{index:02}.txt").write_text(
            "new\n", encoding="utf-8"
        )

    tool = GitStatusTool(WorkspacePathResolver(git_workspace), max_output_bytes=32)
    result = tool.execute(GitStatusArguments())
    output = cast(dict[str, JsonValue], result.output)

    assert set(GitStatusArguments.model_fields) == set()
    assert result.truncated is True
    assert output["truncated"] is True
    assert len(str(output["status"]).encode("utf-8")) <= 32


def test_git_log_uses_only_a_bounded_entry_count(git_workspace: Path) -> None:
    for index in range(3):
        (git_workspace / "tracked.txt").write_text(
            f"revision {index}\n", encoding="utf-8"
        )
        _git(git_workspace, "add", "tracked.txt")
        _git(git_workspace, "commit", "--quiet", "-m", f"revision {index}")

    result = GitLogTool(WorkspacePathResolver(git_workspace)).execute(
        GitLogArguments(max_entries=2)
    )
    output = cast(dict[str, JsonValue], result.output)
    entries = cast(list[dict[str, str]], output["entries"])

    assert result.success is True
    assert output["returned_entries"] == 2
    assert [entry["subject"] for entry in entries] == [
        "revision 2",
        "revision 1",
    ]
    assert all(len(entry["commit"]) == 40 for entry in entries)


def test_git_log_treats_repository_without_commits_as_empty(tmp_path: Path) -> None:
    workspace = tmp_path / "empty-repository"
    workspace.mkdir()
    _git(workspace, "init", "--quiet")

    result = GitLogTool(WorkspacePathResolver(workspace)).execute(GitLogArguments())

    assert result.success is True
    assert result.output == {
        "entries": [],
        "returned_entries": 0,
        "truncated": False,
    }


@pytest.mark.parametrize("tool", [GitStatusTool, GitLogTool])
def test_git_tools_reject_non_repository(
    tmp_path: Path,
    tool: type[GitStatusTool] | type[GitLogTool],
) -> None:
    with pytest.raises(ToolExecutionError) as raised:
        tool(WorkspacePathResolver(tmp_path)).execute(tool.arguments_model())

    assert raised.value.code is ToolErrorCode.NOT_GIT_REPOSITORY
    assert str(tmp_path) not in str(raised.value)


def test_git_tools_clear_inherited_git_environment(
    git_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GIT_DIR", str(git_workspace.parent / "attacker"))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.pager")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "attacker")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-reach-git")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "must-not-reach-git")

    status = GitStatusTool(WorkspacePathResolver(git_workspace)).execute(
        GitStatusArguments()
    )
    log = GitLogTool(WorkspacePathResolver(git_workspace)).execute(
        GitLogArguments(max_entries=1)
    )

    assert status.success is True
    assert log.success is True


def test_git_tools_ignore_path_and_outside_global_config_drift(
    git_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    external = git_workspace.parent / "external-worktree"
    external.mkdir()
    secret_name = "global-config-secret.txt"
    (external / secret_name).write_text("secret\n", encoding="utf-8")
    global_config = git_workspace.parent / "outside-global-config"
    global_config.write_text(
        f"[core]\nworktree = {external}\n",
        encoding="utf-8",
    )
    status_tool = GitStatusTool(WorkspacePathResolver(git_workspace))
    log_tool = GitLogTool(WorkspacePathResolver(git_workspace))
    monkeypatch.setenv("PATH", str(git_workspace.parent / "attacker-bin"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    monkeypatch.setenv("HOME", str(git_workspace.parent))

    status = status_tool.execute(GitStatusArguments())
    log = log_tool.execute(GitLogArguments(max_entries=1))

    assert status.success is True
    assert log.success is True
    assert secret_name not in str(status.output)
    assert secret_name not in str(log.output)


def test_git_tools_launch_only_fixed_protected_commands(
    git_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process_launches: list[tuple[tuple[str, ...], dict[str, object]]] = []
    git_launches: list[tuple[tuple[str, ...], dict[str, str]]] = []
    real_popen = subprocess.Popen
    real_start = git_status_module._start_managed_process
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-reach-git")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "must-not-reach-git")

    def recording_popen(
        command: tuple[str, ...], **options: object
    ) -> subprocess.Popen[bytes]:
        process_launches.append((command, dict(options)))
        return real_popen(command, **options)  # type: ignore[arg-type,return-value]

    def recording_start(
        command: tuple[str, ...],
        *,
        cwd: Path,
        environment: dict[str, str],
    ) -> object:
        git_launches.append((command, dict(environment)))
        return real_start(command, cwd=cwd, environment=environment)

    monkeypatch.setattr(git_status_module.subprocess, "Popen", recording_popen)
    monkeypatch.setattr(git_status_module, "_start_managed_process", recording_start)

    GitStatusTool(WorkspacePathResolver(git_workspace)).execute(GitStatusArguments())
    GitLogTool(WorkspacePathResolver(git_workspace)).execute(
        GitLogArguments(max_entries=7)
    )

    status_command, _ = git_launches[0]
    log_command, _ = git_launches[1]
    expected_git_dir = f"--git-dir={git_workspace / '.git'}"
    expected_work_tree = f"--work-tree={git_workspace}"
    assert status_command[1:3] == (expected_git_dir, expected_work_tree)
    assert log_command[1:3] == (expected_git_dir, expected_work_tree)
    assert status_command[-3:] == (
        "status",
        "--short",
        "--untracked-files=normal",
    )
    assert log_command[-5:] == (
        "log",
        "-n",
        "7",
        "--pretty=format:%H%x09%s",
        "--no-decorate",
    )
    for command, environment in git_launches:
        assert Path(command[0]).is_absolute()
        assert "--no-pager" in command
        assert "--no-replace-objects" in command
        assert "--no-optional-locks" in command
        assert "core.fsmonitor=false" in command
        assert any(argument.startswith("core.hooksPath=") for argument in command)
        assert "OPENAI_API_KEY" not in environment
        assert "AWS_SECRET_ACCESS_KEY" not in environment
        assert set(environment).issubset(
            {
                "COMSPEC",
                "GIT_CONFIG_GLOBAL",
                "GIT_CONFIG_NOSYSTEM",
                "HOME",
                "LANG",
                "LC_ALL",
                "PATHEXT",
                "PAGER",
                "SYSTEMROOT",
                "TEMP",
                "TMP",
                "TMPDIR",
                "WINDIR",
                "XDG_CONFIG_HOME",
            }
        )
    for command, options in process_launches:
        assert Path(command[0]).is_absolute()
        assert options["shell"] is False
        process_environment = cast(dict[str, str], options["env"])
        assert "OPENAI_API_KEY" not in process_environment
        assert "AWS_SECRET_ACCESS_KEY" not in process_environment


def test_local_core_worktree_is_rejected_without_exposing_external_secret(
    git_workspace: Path,
) -> None:
    external = git_workspace.parent / "external-worktree"
    external.mkdir()
    secret_name = "external-secret-do-not-disclose.txt"
    (external / secret_name).write_text("secret\n", encoding="utf-8")
    _git(git_workspace, "config", "core.worktree", str(external))
    (git_workspace / "workspace-only.txt").write_text("safe\n", encoding="utf-8")

    with pytest.raises(ToolExecutionError) as raised:
        GitStatusTool(WorkspacePathResolver(git_workspace)).execute(
            GitStatusArguments()
        )

    assert raised.value.code is ToolErrorCode.UNSAFE_REPOSITORY_LAYOUT
    assert secret_name not in str(raised.value)


def test_git_tools_reject_local_config_include(git_workspace: Path) -> None:
    external = git_workspace.parent / "external-git-config"
    external.write_text("[core]\nworktree = ../outside\n", encoding="utf-8")
    with (git_workspace / ".git" / "config").open("a", encoding="utf-8") as stream:
        stream.write(f"\n[include]\npath = {external}\n")

    with pytest.raises(ToolExecutionError) as raised:
        GitLogTool(WorkspacePathResolver(git_workspace)).execute(GitLogArguments())

    assert raised.value.code is ToolErrorCode.UNSAFE_REPOSITORY_LAYOUT
    assert str(external) not in str(raised.value)


@pytest.mark.parametrize(
    "config_key",
    [
        "core.attributesFile",
        "core.excludesFile",
        "core.fsmonitor",
        "core.hooksPath",
    ],
)
def test_git_tools_reject_external_local_config_capabilities(
    git_workspace: Path,
    config_key: str,
) -> None:
    external = git_workspace.parent / "external-git-capability"
    external.write_text("outside\n", encoding="utf-8")
    _git(git_workspace, "config", config_key, str(external))

    with pytest.raises(ToolExecutionError) as raised:
        GitStatusTool(WorkspacePathResolver(git_workspace)).execute(
            GitStatusArguments()
        )

    assert raised.value.code is ToolErrorCode.UNSAFE_REPOSITORY_LAYOUT
    assert str(external) not in str(raised.value)


def test_git_executable_is_pinned_and_revalidated(
    git_workspace: Path,
    tmp_path: Path,
) -> None:
    discovered = shutil.which("git")
    assert discovered is not None
    trusted_copy = tmp_path / Path(discovered).name
    shutil.copy2(discovered, trusted_copy)
    tool = GitStatusTool(
        WorkspacePathResolver(git_workspace), git_executable=trusted_copy
    )
    with trusted_copy.open("ab") as stream:
        stream.write(b"drift")

    with pytest.raises(ToolExecutionError) as raised:
        tool.execute(GitStatusArguments())

    assert raised.value.code is ToolErrorCode.GIT_COMMAND_FAILED


def test_git_log_parses_repository_object_format(tmp_path: Path) -> None:
    workspace = tmp_path / "sha256-repository"
    workspace.mkdir()
    completed = subprocess.run(
        ["git", "init", "--quiet", "--object-format=sha256"],
        cwd=workspace,
        capture_output=True,
        check=False,
        shell=False,
    )
    if completed.returncode != 0:
        pytest.skip("Installed Git does not support SHA-256 repositories")
    (workspace / "tracked.txt").write_text("sha256\n", encoding="utf-8")
    _git(workspace, "add", "tracked.txt")
    _git(workspace, "commit", "--quiet", "-m", "sha256 commit")

    result = GitLogTool(WorkspacePathResolver(workspace)).execute(
        GitLogArguments(max_entries=1)
    )
    output = cast(dict[str, JsonValue], result.output)
    entries = cast(list[dict[str, str]], output["entries"])

    assert len(entries[0]["commit"]) == 64


@pytest.mark.parametrize("tool", [GitStatusTool, GitLogTool])
def test_git_tools_reject_initialized_local_submodule(
    git_workspace: Path,
    tmp_path: Path,
    tool: type[GitStatusTool] | type[GitLogTool],
) -> None:
    source = tmp_path / "submodule-source"
    source.mkdir()
    _git(source, "init", "--quiet")
    (source / "tracked.txt").write_text("initial\n", encoding="utf-8")
    _git(source, "add", "tracked.txt")
    _git(source, "commit", "--quiet", "-m", "submodule initial")
    _git(
        git_workspace,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        "--quiet",
        str(source),
        "vendor/example",
    )
    _git(git_workspace, "commit", "--quiet", "-am", "add submodule")
    (git_workspace / "vendor" / "example" / "tracked.txt").write_text(
        "dirty\n", encoding="utf-8"
    )
    assert "vendor/example" in _git_output(git_workspace, "status", "--short")

    with pytest.raises(ToolExecutionError) as raised:
        tool(WorkspacePathResolver(git_workspace)).execute(tool.arguments_model())

    assert raised.value.code is ToolErrorCode.UNSAFE_REPOSITORY_LAYOUT
    assert str(source) not in str(raised.value)
    assert ".gitmodules" not in str(raised.value)


def test_git_tools_allow_nested_file_named_gitmodules(git_workspace: Path) -> None:
    nested = git_workspace / "ordinary"
    nested.mkdir()
    (nested / ".gitmodules").write_text("ordinary\n", encoding="utf-8")
    _git(git_workspace, "add", "ordinary/.gitmodules")
    _git(git_workspace, "commit", "--quiet", "-m", "ordinary similarly named file")

    assert GitStatusTool(WorkspacePathResolver(git_workspace)).execute(
        GitStatusArguments()
    ).success
    assert GitLogTool(WorkspacePathResolver(git_workspace)).execute(
        GitLogArguments(max_entries=1)
    ).success


def test_git_tool_rejects_submodule_indicator_created_during_launch(
    git_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_start = git_status_module._start_managed_process

    def create_indicator_after_launch(
        command: tuple[str, ...],
        *,
        cwd: Path,
        environment: dict[str, str],
    ) -> object:
        managed = real_start(command, cwd=cwd, environment=environment)
        (git_workspace / ".gitmodules").write_text("[submodule]\n", encoding="utf-8")
        return managed

    monkeypatch.setattr(
        git_status_module,
        "_start_managed_process",
        create_indicator_after_launch,
    )

    with pytest.raises(ToolExecutionError) as raised:
        GitStatusTool(WorkspacePathResolver(git_workspace)).execute(
            GitStatusArguments()
        )

    assert raised.value.code is ToolErrorCode.UNSAFE_REPOSITORY_LAYOUT


@pytest.mark.parametrize("tool", [GitStatusTool, GitLogTool])
def test_git_tools_reject_nested_gitlink_without_conventional_indicators(
    git_workspace: Path,
    tool: type[GitStatusTool] | type[GitLogTool],
) -> None:
    child = git_workspace / "nested-child"
    child.mkdir()
    _git(child, "init", "--quiet")
    (child / "tracked.txt").write_text("initial\n", encoding="utf-8")
    _git(child, "add", "tracked.txt")
    _git(child, "commit", "--quiet", "-m", "nested initial")
    child_commit = _git_output(child, "rev-parse", "HEAD")
    _git(
        git_workspace,
        "update-index",
        "--add",
        "--cacheinfo",
        f"160000,{child_commit},nested-child",
    )
    _git(git_workspace, "commit", "--quiet", "-m", "manual gitlink")
    assert not (git_workspace / ".gitmodules").exists()
    assert not (git_workspace / ".git" / "modules").exists()
    (child / "tracked.txt").write_text("dirty\n", encoding="utf-8")
    assert "nested-child" in _git_output(git_workspace, "status", "--short")

    with pytest.raises(ToolExecutionError) as raised:
        tool(WorkspacePathResolver(git_workspace)).execute(tool.arguments_model())

    assert raised.value.code is ToolErrorCode.UNSAFE_REPOSITORY_LAYOUT
    assert "nested-child" not in str(raised.value)


@pytest.mark.parametrize("tool", [GitStatusTool, GitLogTool])
def test_git_tools_ignore_agentforge_control_workspace_git(
    git_workspace: Path,
    tool: type[GitStatusTool] | type[GitLogTool],
) -> None:
    control_workspace = (
        git_workspace / ".agentforge" / "candidates" / "run" / "workspace"
    )
    control_workspace.mkdir(parents=True)
    _git(control_workspace, "init", "--quiet")

    result = tool(WorkspacePathResolver(git_workspace)).execute(tool.arguments_model())

    assert result.success is True


@pytest.mark.parametrize("indicator_kind", ["directory", "file"])
def test_git_tool_rejects_nested_git_created_during_launch(
    git_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    indicator_kind: str,
) -> None:
    nested = git_workspace / "created-during-launch"
    nested.mkdir()
    real_start = git_status_module._start_managed_process

    def create_nested_git_after_launch(
        command: tuple[str, ...],
        *,
        cwd: Path,
        environment: dict[str, str],
    ) -> object:
        managed = real_start(command, cwd=cwd, environment=environment)
        indicator = nested / ".git"
        if indicator_kind == "directory":
            indicator.mkdir()
        else:
            indicator.write_text("gitdir: outside\n", encoding="utf-8")
        return managed

    monkeypatch.setattr(
        git_status_module,
        "_start_managed_process",
        create_nested_git_after_launch,
    )

    with pytest.raises(ToolExecutionError) as raised:
        GitStatusTool(WorkspacePathResolver(git_workspace)).execute(
            GitStatusArguments()
        )

    assert raised.value.code is ToolErrorCode.UNSAFE_REPOSITORY_LAYOUT
    assert nested.name not in str(raised.value)


def test_git_tools_allow_ordinary_github_and_git_names(git_workspace: Path) -> None:
    github = git_workspace / ".github"
    github.mkdir()
    (github / "workflow.txt").write_text("ordinary\n", encoding="utf-8")
    (git_workspace / "git").write_text("ordinary\n", encoding="utf-8")

    assert GitStatusTool(WorkspacePathResolver(git_workspace)).execute(
        GitStatusArguments()
    ).success
    assert GitLogTool(WorkspacePathResolver(git_workspace)).execute(
        GitLogArguments(max_entries=1)
    ).success


def test_git_status_records_but_does_not_follow_internal_relative_symlink(
    git_workspace: Path,
) -> None:
    target = git_workspace / "tracked.txt"
    link = git_workspace / "tracked-link.txt"
    try:
        link.symlink_to("tracked.txt")
    except OSError as exc:
        pytest.skip(f"File symlinks are unavailable on this platform: {exc}")
    _git(git_workspace, "add", "tracked-link.txt")
    _git(git_workspace, "commit", "--quiet", "-m", "safe internal link")

    result = GitStatusTool(WorkspacePathResolver(git_workspace)).execute(
        GitStatusArguments()
    )

    assert result.success
    assert target.read_text(encoding="utf-8") == "initial\n"


def test_git_status_rejects_relative_symlink_escaping_workspace(
    git_workspace: Path,
) -> None:
    external = git_workspace.parent / "external-link-target.txt"
    external.write_text("external\n", encoding="utf-8")
    link = git_workspace / "escaping-link.txt"
    try:
        link.symlink_to("../external-link-target.txt")
    except OSError as exc:
        pytest.skip(f"File symlinks are unavailable on this platform: {exc}")

    with pytest.raises(ToolExecutionError) as raised:
        GitStatusTool(WorkspacePathResolver(git_workspace)).execute(
            GitStatusArguments()
        )

    assert raised.value.code is ToolErrorCode.UNSAFE_REPOSITORY_LAYOUT


def test_git_timeout_includes_workspace_traversal(
    git_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_scandir = os.scandir
    real_monotonic = time.monotonic
    workspace_scan_started = [False]

    def tracked_workspace_scandir(path: os.PathLike[str] | str):
        if Path(path) == git_workspace:
            workspace_scan_started[0] = True
        return real_scandir(path)

    def shifted_monotonic() -> float:
        return real_monotonic() + (100.0 if workspace_scan_started[0] else 0.0)

    monkeypatch.setattr(git_status_module.os, "scandir", tracked_workspace_scandir)
    monkeypatch.setattr(git_status_module.time, "monotonic", shifted_monotonic)

    with pytest.raises(ToolExecutionError) as raised:
        GitStatusTool(
            WorkspacePathResolver(git_workspace), timeout_seconds=10.0
        ).execute(GitStatusArguments())

    assert raised.value.code is ToolErrorCode.TOOL_TIMEOUT


def test_git_workspace_traversal_is_entry_bounded(
    git_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(git_status_module, "_MAX_WORKSPACE_PATHS", 1)

    with pytest.raises(ToolExecutionError) as raised:
        GitStatusTool(WorkspacePathResolver(git_workspace)).execute(
            GitStatusArguments()
        )

    assert raised.value.code is ToolErrorCode.UNSAFE_REPOSITORY_LAYOUT
    assert str(git_workspace) not in str(raised.value)


def test_git_timeout_includes_prelaunch_manifest_scan(
    git_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture = git_status_module._capture_control_manifest

    def slow_capture(
        git_directory: Path,
        deadline: object,
    ) -> tuple[object, ...]:
        time.sleep(0.05)
        return cast(tuple[object, ...], capture(git_directory, deadline))

    monkeypatch.setattr(
        git_status_module,
        "_capture_control_manifest",
        slow_capture,
    )

    with pytest.raises(ToolExecutionError) as raised:
        GitStatusTool(
            WorkspacePathResolver(git_workspace), timeout_seconds=0.01
        ).execute(GitStatusArguments())

    assert raised.value.code is ToolErrorCode.TOOL_TIMEOUT


def test_git_timeout_closes_pipes_held_by_descendant(
    git_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed = threading.Event()
    terminated = [False]

    class HoldingPipe:
        def read(self, _size: int) -> bytes:
            closed.wait(0.25)
            return b""

        def close(self) -> None:
            closed.set()

    class FakeProcess:
        stdout = HoldingPipe()
        stderr = HoldingPipe()

        def wait(self, timeout: float | None = None) -> int:
            if closed.is_set():
                return -9
            raise subprocess.TimeoutExpired("trusted-git", timeout)

    class FakeTree:
        process = FakeProcess()

        def terminate(self) -> None:
            terminated[0] = True
            self.process.stdout.close()
            self.process.stderr.close()

        def close(self) -> None:
            self.process.stdout.close()
            self.process.stderr.close()

    monkeypatch.setattr(
        git_status_module,
        "_start_managed_process",
        lambda *args, **kwargs: FakeTree(),
        raising=False,
    )
    started = time.monotonic()

    with pytest.raises(ToolExecutionError) as raised:
        GitStatusTool(
            WorkspacePathResolver(git_workspace), timeout_seconds=0.1
        ).execute(GitStatusArguments())

    assert raised.value.code is ToolErrorCode.TOOL_TIMEOUT
    assert time.monotonic() - started < 0.15
    assert terminated[0] is True


def test_git_cleanup_is_bounded_when_termination_fails_and_reader_owns_pipe_lock(
) -> None:
    reader_started = threading.Event()
    release_reader = threading.Event()
    pipe_closed = threading.Event()
    managed_closed = [False]
    terminate_called = [False]

    class DescendantHeldPipe:
        def __init__(self) -> None:
            self._lock = threading.Lock()

        def read(self, _size: int) -> bytes:
            with self._lock:
                reader_started.set()
                release_reader.wait(2.0)
            return b""

        def close(self) -> None:
            with self._lock:
                pipe_closed.set()

    class FakeProcess:
        stdout = DescendantHeldPipe()
        stderr = None

    class FailedTerminationTree:
        process = FakeProcess()

        def terminate(self) -> bool:
            terminate_called[0] = True
            return False

        def close(self) -> None:
            managed_closed[0] = True

    reader = threading.Thread(
        target=git_status_module._bounded_read,
        args=(FakeProcess.stdout, bytearray(), 100, [False]),
        daemon=True,
    )
    reader.start()
    assert reader_started.wait(0.5)
    started = time.monotonic()

    git_status_module._stop_managed_process(FailedTerminationTree(), (reader,))

    elapsed = time.monotonic() - started
    assert elapsed < 1.5
    assert terminate_called[0] is True
    assert managed_closed[0] is True
    release_reader.set()
    reader.join(timeout=0.5)
    assert reader.is_alive() is False
    assert pipe_closed.is_set()


@pytest.mark.parametrize("raises", [False, True])
def test_git_windows_job_handle_closes_immediately_when_termination_unconfirmed(
    raises: bool,
) -> None:
    closed_handles: list[int] = []

    class UnconfirmedJobApi:
        def terminate_and_wait(self, job_handle: int, timeout: float) -> bool:
            assert job_handle == 17
            assert timeout > 0
            if raises:
                raise OSError("unconfirmed")
            return False

        def close(self, job_handle: int) -> None:
            closed_handles.append(job_handle)

    class FakeProcess:
        pid = 123

    managed = git_status_module._ManagedGitProcess(
        FakeProcess(),
        windows_api=UnconfirmedJobApi(),
        windows_job_handle=17,
    )

    assert managed.terminate() is False
    assert closed_handles == [17]
    managed.close()
    assert closed_handles == [17]


@pytest.mark.parametrize("tool", [GitStatusTool, GitLogTool])
def test_git_tools_reject_external_object_alternates(
    git_workspace: Path,
    tool: type[GitStatusTool] | type[GitLogTool],
) -> None:
    external_objects = git_workspace.parent / "external-objects"
    external_objects.mkdir()
    alternates = git_workspace / ".git" / "objects" / "info" / "alternates"
    alternates.parent.mkdir(parents=True, exist_ok=True)
    alternates.write_text(str(external_objects), encoding="utf-8")

    with pytest.raises(ToolExecutionError) as raised:
        tool(WorkspacePathResolver(git_workspace)).execute(tool.arguments_model())

    assert raised.value.code is ToolErrorCode.UNSAFE_REPOSITORY_LAYOUT
    assert str(external_objects) not in str(raised.value)


@pytest.mark.parametrize("redirection_file", ["commondir", "gitdir", "config.worktree"])
def test_git_tools_reject_repository_redirection_files(
    git_workspace: Path,
    redirection_file: str,
) -> None:
    external = git_workspace.parent / "external-control"
    external.mkdir()
    (git_workspace / ".git" / redirection_file).write_text(
        str(external), encoding="utf-8"
    )

    with pytest.raises(ToolExecutionError) as raised:
        GitStatusTool(WorkspacePathResolver(git_workspace)).execute(
            GitStatusArguments()
        )

    assert raised.value.code is ToolErrorCode.UNSAFE_REPOSITORY_LAYOUT
    assert str(external) not in str(raised.value)


def test_git_tool_rejects_repository_config_changed_during_launch(
    git_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_popen = subprocess.Popen
    config = git_workspace / ".git" / "config"

    def mutating_popen(
        command: tuple[str, ...], **options: object
    ) -> subprocess.Popen[bytes]:
        process = real_popen(
            command, **options  # type: ignore[arg-type,call-overload]
        )
        with config.open("a", encoding="utf-8") as stream:
            stream.write("\n[core]\n\tworktree = ../external\n")
        return process  # type: ignore[return-value]

    monkeypatch.setattr(git_status_module.subprocess, "Popen", mutating_popen)

    with pytest.raises(ToolExecutionError) as raised:
        GitStatusTool(WorkspacePathResolver(git_workspace)).execute(
            GitStatusArguments()
        )

    assert raised.value.code is ToolErrorCode.UNSAFE_REPOSITORY_LAYOUT


def test_git_tools_fail_closed_on_worktree_gitfile_indirection(
    git_workspace: Path,
) -> None:
    external_git_directory = git_workspace.parent / "external-git-directory"
    (git_workspace / ".git").rename(external_git_directory)
    (git_workspace / ".git").write_text(
        f"gitdir: {external_git_directory}\n", encoding="utf-8"
    )

    with pytest.raises(ToolExecutionError) as raised:
        GitLogTool(WorkspacePathResolver(git_workspace)).execute(GitLogArguments())

    assert raised.value.code is ToolErrorCode.UNSAFE_REPOSITORY_LAYOUT
    assert str(external_git_directory) not in str(raised.value)


@pytest.mark.parametrize("tool", [GitStatusTool, GitLogTool])
def test_git_tools_reject_external_object_database_with_secret_commit(
    git_workspace: Path,
    tool: type[GitStatusTool] | type[GitLogTool],
) -> None:
    external = git_workspace.parent / "external-object-repository"
    external.mkdir()
    _git(external, "init", "--quiet")
    (external / "external.txt").write_text("external\n", encoding="utf-8")
    _git(external, "add", "external.txt")
    secret_subject = "external-secret-commit-subject"
    _git(external, "commit", "--quiet", "-m", secret_subject)
    external_commit = _git_output(external, "rev-parse", "HEAD")

    _redirect_directory(
        git_workspace / ".git" / "objects",
        external / ".git" / "objects",
    )
    (git_workspace / ".git" / "HEAD").write_text(
        f"{external_commit}\n", encoding="ascii"
    )
    raw = _git_output(git_workspace, "log", "-1", "--pretty=format:%s")
    assert raw == secret_subject

    with pytest.raises(ToolExecutionError) as raised:
        tool(WorkspacePathResolver(git_workspace)).execute(tool.arguments_model())

    assert raised.value.code is ToolErrorCode.UNSAFE_REPOSITORY_LAYOUT
    assert secret_subject not in str(raised.value)


def test_git_tools_reject_external_refs_root(git_workspace: Path) -> None:
    external_refs = git_workspace.parent / "external-refs"
    external_refs.mkdir()
    _redirect_directory(git_workspace / ".git" / "refs", external_refs)

    with pytest.raises(ToolExecutionError) as raised:
        GitStatusTool(WorkspacePathResolver(git_workspace)).execute(
            GitStatusArguments()
        )

    assert raised.value.code is ToolErrorCode.UNSAFE_REPOSITORY_LAYOUT


@pytest.mark.parametrize("relative", ["HEAD", "index"])
def test_git_tools_reject_hardlinked_critical_files(
    git_workspace: Path,
    relative: str,
) -> None:
    critical = git_workspace / ".git" / relative
    external = git_workspace.parent / f"external-{relative.lower()}"
    critical.rename(external)
    os.link(external, critical)

    with pytest.raises(ToolExecutionError) as raised:
        GitLogTool(WorkspacePathResolver(git_workspace)).execute(GitLogArguments())

    assert raised.value.code is ToolErrorCode.UNSAFE_REPOSITORY_LAYOUT


def test_git_tool_rejects_objects_identity_changed_during_launch(
    git_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_popen = subprocess.Popen
    objects = git_workspace / ".git" / "objects"

    def mutating_popen(
        command: tuple[str, ...], **options: object
    ) -> subprocess.Popen[bytes]:
        process = real_popen(
            command, **options  # type: ignore[arg-type,call-overload]
        )
        objects.rename(objects.with_name("objects-replaced"))
        objects.mkdir()
        return process  # type: ignore[return-value]

    monkeypatch.setattr(git_status_module.subprocess, "Popen", mutating_popen)

    with pytest.raises(ToolExecutionError) as raised:
        GitStatusTool(WorkspacePathResolver(git_workspace)).execute(
            GitStatusArguments()
        )

    assert raised.value.code is ToolErrorCode.UNSAFE_REPOSITORY_LAYOUT


@pytest.mark.parametrize("tool", [GitStatusTool, GitLogTool])
def test_git_tools_reject_external_loose_object_fanout(
    git_workspace: Path,
    tool: type[GitStatusTool] | type[GitLogTool],
) -> None:
    fanout = _loose_object_fanout(git_workspace)
    external_fanout = git_workspace.parent / f"external-fanout-{fanout.name}"
    fanout.rename(external_fanout)
    _link_directory(fanout, external_fanout)
    assert _git_output(git_workspace, "log", "-1", "--pretty=format:%s") == (
        "initial commit"
    )

    with pytest.raises(ToolExecutionError) as raised:
        tool(WorkspacePathResolver(git_workspace)).execute(tool.arguments_model())

    assert raised.value.code is ToolErrorCode.UNSAFE_REPOSITORY_LAYOUT
    assert str(external_fanout) not in str(raised.value)


def test_git_tools_reject_hardlinked_loose_object(git_workspace: Path) -> None:
    fanout = _loose_object_fanout(git_workspace)
    loose_object = next(candidate for candidate in fanout.iterdir() if candidate.is_file())
    external = git_workspace.parent / "external-loose-object"
    loose_object.rename(external)
    os.link(external, loose_object)

    with pytest.raises(ToolExecutionError) as raised:
        GitLogTool(WorkspacePathResolver(git_workspace)).execute(GitLogArguments())

    assert raised.value.code is ToolErrorCode.UNSAFE_REPOSITORY_LAYOUT


def test_git_tools_reject_symlinked_loose_object(git_workspace: Path) -> None:
    fanout = _loose_object_fanout(git_workspace)
    loose_object = next(candidate for candidate in fanout.iterdir() if candidate.is_file())
    external = git_workspace.parent / "external-symlinked-loose-object"
    loose_object.rename(external)
    try:
        loose_object.symlink_to(external)
    except OSError as exc:
        pytest.skip(f"File symlinks are unavailable on this platform: {exc}")

    with pytest.raises(ToolExecutionError) as raised:
        GitStatusTool(WorkspacePathResolver(git_workspace)).execute(
            GitStatusArguments()
        )

    assert raised.value.code is ToolErrorCode.UNSAFE_REPOSITORY_LAYOUT


@pytest.mark.parametrize("entry_name", ["not-hex", "abc", "0g"])
def test_git_tools_reject_invalid_object_database_entries(
    git_workspace: Path,
    entry_name: str,
) -> None:
    (git_workspace / ".git" / "objects" / entry_name).mkdir()

    with pytest.raises(ToolExecutionError) as raised:
        GitLogTool(WorkspacePathResolver(git_workspace)).execute(GitLogArguments())

    assert raised.value.code is ToolErrorCode.UNSAFE_REPOSITORY_LAYOUT


def test_git_tool_rejects_fanout_identity_changed_during_launch(
    git_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_popen = subprocess.Popen
    fanout = _loose_object_fanout(git_workspace)

    def mutating_popen(
        command: tuple[str, ...], **options: object
    ) -> subprocess.Popen[bytes]:
        process = real_popen(
            command, **options  # type: ignore[arg-type,call-overload]
        )
        fanout.rename(fanout.with_name(f"{fanout.name}-replaced"))
        fanout.mkdir()
        return process  # type: ignore[return-value]

    monkeypatch.setattr(git_status_module.subprocess, "Popen", mutating_popen)

    with pytest.raises(ToolExecutionError) as raised:
        GitStatusTool(WorkspacePathResolver(git_workspace)).execute(
            GitStatusArguments()
        )

    assert raised.value.code is ToolErrorCode.UNSAFE_REPOSITORY_LAYOUT


@pytest.mark.parametrize("kind", ["invalid_name", "nested_directory"])
def test_git_tools_reject_invalid_loose_object_entries(
    git_workspace: Path,
    kind: str,
) -> None:
    fanout = _loose_object_fanout(git_workspace)
    if kind == "invalid_name":
        (fanout / "not-a-loose-object").write_text("invalid", encoding="utf-8")
    else:
        (fanout / ("a" * 38)).mkdir()

    with pytest.raises(ToolExecutionError) as raised:
        GitStatusTool(WorkspacePathResolver(git_workspace)).execute(
            GitStatusArguments()
        )

    assert raised.value.code is ToolErrorCode.UNSAFE_REPOSITORY_LAYOUT
