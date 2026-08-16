from pathlib import Path

import pytest

from agentforge.domain.enums import ToolErrorCode
from agentforge.domain.errors import (
    SensitivePathError,
    ToolExecutionError,
    WorkspacePathError,
)
from agentforge.policy.sensitive import SensitiveFilePolicy
from agentforge.tools.mutation.base import MutationLimits
from agentforge.tools.mutation.security import MutationSecurityPolicy
from agentforge.tools.paths import WorkspacePathResolver


def make_policy(workspace: Path, *, max_file_bytes: int = 64) -> MutationSecurityPolicy:
    return MutationSecurityPolicy(
        WorkspacePathResolver(workspace),
        SensitiveFilePolicy(),
        MutationLimits(max_file_bytes=max_file_bytes),
    )


@pytest.mark.parametrize(
    "requested",
    ["../outside.py", "C:\\outside.py", "/outside.py", "\\\\server\\share\\x.py"],
)
def test_mutation_target_rejects_escape_absolute_and_unc(
    tmp_path: Path, requested: str
) -> None:
    policy = make_policy(tmp_path)

    with pytest.raises(WorkspacePathError) as error:
        policy.resolve_target(requested)

    assert error.value.code is ToolErrorCode.PATH_OUTSIDE_WORKSPACE


def test_mutation_target_allows_missing_file_but_requires_existing_parent(
    tmp_path: Path,
) -> None:
    (tmp_path / "src").mkdir()
    policy = make_policy(tmp_path)

    assert policy.resolve_target("src/new.py") == tmp_path / "src" / "new.py"
    with pytest.raises(WorkspacePathError) as error:
        policy.resolve_target("missing/new.py")
    assert error.value.code is ToolErrorCode.PATH_NOT_FOUND


@pytest.mark.parametrize("requested", [".env", "nested/api_token.txt", ".git/config"])
def test_mutation_target_rejects_sensitive_paths(tmp_path: Path, requested: str) -> None:
    (tmp_path / "nested").mkdir()
    (tmp_path / ".git").mkdir()
    policy = make_policy(tmp_path)

    with pytest.raises(SensitivePathError) as error:
        policy.resolve_target(requested)

    assert error.value.code is ToolErrorCode.SENSITIVE_PATH


def test_mutation_target_rejects_symlink_components(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    real = workspace / "real"
    real.mkdir()
    link = workspace / "linked"
    try:
        link.symlink_to(real, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"Symlinks are unavailable on this platform: {exc}")

    with pytest.raises(WorkspacePathError) as error:
        make_policy(workspace).resolve_target("linked/new.py")

    assert error.value.code is ToolErrorCode.PATH_OUTSIDE_WORKSPACE


def test_mutation_target_rejects_detected_reparse_points(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "junction"
    directory.mkdir()
    resolver = WorkspacePathResolver(tmp_path)
    monkeypatch.setattr(
        resolver,
        "_is_reparse_point",
        lambda path: path.name == "junction",
    )
    policy = MutationSecurityPolicy(resolver, SensitiveFilePolicy(), MutationLimits())

    with pytest.raises(WorkspacePathError):
        policy.resolve_target("junction/new.py")


@pytest.mark.parametrize(
    "content",
    [
        "hello\x00world",
        "hello\x01world",
        "-----BEGIN PRIVATE KEY-----\nnot-a-real-key",
        "token = 'sk-" + "x" * 24 + "'",
        "OPENAI_API_KEY=definitely-not-a-placeholder",
        "DEEPSEEK_API_KEY=definitely-not-a-placeholder",
        'deepseek_api_key = "definitely-not-a-placeholder"',
    ],
)
def test_mutation_content_rejects_binary_controls_and_secret_markers(
    tmp_path: Path, content: str
) -> None:
    policy = make_policy(tmp_path)

    with pytest.raises(ToolExecutionError):
        policy.encode_text(content, max_bytes=64)


def test_mutation_content_enforces_utf8_byte_limit(tmp_path: Path) -> None:
    policy = make_policy(tmp_path, max_file_bytes=4)

    assert policy.encode_text("test", max_bytes=4) == b"test"
    with pytest.raises(ToolExecutionError) as error:
        policy.encode_text("你好", max_bytes=4)
    assert error.value.code is ToolErrorCode.FILE_TOO_LARGE


def test_mutation_content_allows_documented_deepseek_environment_name(
    tmp_path: Path,
) -> None:
    policy = make_policy(tmp_path)

    content = "Set the DEEPSEEK_API_KEY environment variable securely."

    assert policy.encode_text(content, max_bytes=128) == content.encode("utf-8")
