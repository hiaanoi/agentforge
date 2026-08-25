from __future__ import annotations

import subprocess

import pytest

from agentforge.repair_engines.mini_native.classifier import CommandKind, classify_command
from agentforge.repair_engines.mini_native.environment import DockerBashEnvironment


@pytest.mark.asyncio
async def test_environment_executes_bash_in_workspace() -> None:
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def runner(arguments: tuple[str, ...], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((arguments, kwargs))
        return subprocess.CompletedProcess(arguments, 0, stdout="ok", stderr="")

    environment = DockerBashEnvironment("task-container", runner=runner)

    result = await environment.execute("printf 'ok'", cwd="/workspace", timeout_seconds=5)

    assert result.returncode == 0
    assert result.output == "ok"
    assert result.exception_info is None
    assert result.timed_out is False
    assert result.duration_ms >= 0
    assert calls == [
        (
            (
                "docker",
                "exec",
                "-w",
                "/workspace",
                "task-container",
                "/bin/bash",
                "-lc",
                "printf 'ok'",
            ),
            {"capture_output": True, "text": True, "timeout": 5, "check": False},
        )
    ]


@pytest.mark.asyncio
async def test_environment_returns_timeout_output_and_exception() -> None:
    def runner(arguments: tuple[str, ...], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        raise subprocess.TimeoutExpired(arguments, timeout=1, output=b"before", stderr=b"after")

    environment = DockerBashEnvironment("task-container", runner=runner)

    result = await environment.execute("sleep 10", cwd="/workspace", timeout_seconds=1)

    assert result.returncode is None
    assert result.output == "beforeafter"
    assert result.timed_out is True
    assert result.exception_info is not None
    assert "TimeoutExpired" in result.exception_info


@pytest.mark.asyncio
async def test_environment_returns_launch_exception() -> None:
    def runner(arguments: tuple[str, ...], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del arguments, kwargs
        raise OSError("docker unavailable")

    environment = DockerBashEnvironment("task-container", runner=runner)

    result = await environment.execute("pwd", cwd="/workspace", timeout_seconds=1)

    assert result.returncode is None
    assert result.output == ""
    assert result.timed_out is False
    assert result.exception_info == "OSError: docker unavailable"


def test_classifier_splits_read_test_write_and_other() -> None:
    assert classify_command("rg -n bug src") is CommandKind.READ
    assert classify_command("python -m pytest tests/test_bug.py") is CommandKind.TEST
    assert classify_command("sed -i 's/old/new/' src/a.py") is CommandKind.WRITE
    assert classify_command("custom_tool --repair") is CommandKind.OTHER


@pytest.mark.parametrize(
    ("command", "kind"),
    [
        ("git diff -- src/a.py", CommandKind.READ),
        ("nox -s tests", CommandKind.TEST),
        ("cp source.py target.py", CommandKind.WRITE),
        ("printf x>result.txt", CommandKind.WRITE),
    ],
)
def test_classifier_recognizes_other_obvious_prefixes(command: str, kind: CommandKind) -> None:
    assert classify_command(command) is kind
