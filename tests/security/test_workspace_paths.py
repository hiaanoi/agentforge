from pathlib import Path

import pytest

from agentforge.domain.enums import PathKind, ToolErrorCode
from agentforge.domain.errors import SensitivePathError, WorkspacePathError
from agentforge.policy.sensitive import SensitiveFilePolicy
from agentforge.tools.paths import WorkspacePathResolver


def test_resolver_allows_normal_relative_paths_and_reports_missing(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    expected = tmp_path / "src" / "app.py"
    expected.write_text("print('ok')", encoding="utf-8")
    resolver = WorkspacePathResolver(tmp_path)

    assert resolver.resolve("src/app.py", PathKind.FILE) == expected.resolve()
    assert resolver.relative(expected.resolve()) == "src/app.py"
    with pytest.raises(WorkspacePathError) as error:
        resolver.resolve("missing.txt", PathKind.FILE)
    assert error.value.code is ToolErrorCode.PATH_NOT_FOUND


@pytest.mark.parametrize(
    "requested",
    [
        "../outside.txt",
        "..\\outside.txt",
        "/outside.txt",
        "C:\\outside.txt",
        "\\\\server\\share\\file",
    ],
)
def test_resolver_rejects_escape_and_absolute_paths(tmp_path: Path, requested: str) -> None:
    resolver = WorkspacePathResolver(tmp_path)

    with pytest.raises(WorkspacePathError) as error:
        resolver.resolve(requested)

    assert error.value.code is ToolErrorCode.PATH_OUTSIDE_WORKSPACE


@pytest.mark.parametrize("requested", ["", "   ", "invalid\x00path"])
def test_resolver_rejects_empty_and_invalid_paths(tmp_path: Path, requested: str) -> None:
    resolver = WorkspacePathResolver(tmp_path)

    with pytest.raises(WorkspacePathError) as error:
        resolver.resolve(requested)

    assert error.value.code is ToolErrorCode.INVALID_PATH


def test_resolver_rejects_external_and_internal_symlinks(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    target = workspace / "target.txt"
    target.write_text("inside", encoding="utf-8")
    external_link = workspace / "external-link.txt"
    internal_link = workspace / "internal-link.txt"
    try:
        external_link.symlink_to(outside)
        internal_link.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"Symlinks are unavailable on this platform: {exc}")
    resolver = WorkspacePathResolver(workspace)

    with pytest.raises(WorkspacePathError) as error:
        resolver.resolve("external-link.txt", PathKind.FILE)
    assert error.value.code is ToolErrorCode.PATH_OUTSIDE_WORKSPACE
    with pytest.raises(WorkspacePathError) as error:
        resolver.resolve("internal-link.txt", PathKind.FILE)
    assert error.value.code is ToolErrorCode.PATH_OUTSIDE_WORKSPACE


def test_resolver_rejects_lstat_marked_internal_path_component(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "src"
    source.mkdir()
    (source / "app.py").write_text("print('ok')", encoding="utf-8")
    resolver = WorkspacePathResolver(tmp_path)
    monkeypatch.setattr(
        resolver,
        "_is_reparse_point",
        lambda path: path == source,
    )

    with pytest.raises(WorkspacePathError) as error:
        resolver.resolve("src/app.py", PathKind.FILE)

    assert error.value.code is ToolErrorCode.PATH_OUTSIDE_WORKSPACE


def test_resolver_rejects_invalid_workspace_and_path_types(tmp_path: Path) -> None:
    with pytest.raises(WorkspacePathError):
        WorkspacePathResolver(tmp_path / "missing")

    file_path = tmp_path / "file.txt"
    file_path.write_text("text", encoding="utf-8")
    resolver = WorkspacePathResolver(tmp_path)
    with pytest.raises(WorkspacePathError) as error:
        resolver.resolve("file.txt", PathKind.DIRECTORY)
    assert error.value.code is ToolErrorCode.PATH_TYPE_MISMATCH


@pytest.mark.parametrize(
    "relative",
    [
        ".env",
        ".ENV",
        ".env.local",
        "nested/.env.production",
        "id_rsa",
        "ID_ED25519",
        ".git/config",
        ".git/CREDENTIALS",
        "keys/service.pem",
        "keys/service.key",
        "auth/private_key.txt",
        "config/api_token.json",
    ],
)
def test_sensitive_file_policy_blocks_configured_names(relative: str) -> None:
    policy = SensitiveFilePolicy()

    with pytest.raises(SensitivePathError) as error:
        policy.require_allowed(relative)

    assert error.value.code is ToolErrorCode.SENSITIVE_PATH


def test_sensitive_file_policy_allows_normal_text_file() -> None:
    SensitiveFilePolicy().require_allowed("src/readme.txt")


def test_resolver_rejects_sensitive_internal_symlink_before_policy_checks(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sensitive = workspace / ".env"
    sensitive.write_text("secret", encoding="utf-8")
    alias = workspace / "public.txt"
    try:
        alias.symlink_to(sensitive)
    except OSError as exc:
        pytest.skip(f"Symlinks are unavailable on this platform: {exc}")
    resolver = WorkspacePathResolver(workspace)

    with pytest.raises(WorkspacePathError) as error:
        resolver.resolve("public.txt", PathKind.FILE)

    assert error.value.code is ToolErrorCode.PATH_OUTSIDE_WORKSPACE
