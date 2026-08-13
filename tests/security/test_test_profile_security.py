import hashlib
import os
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from agentforge.application.contracts import VerificationRuntimeMode
from agentforge.domain.errors import (
    DuplicateTestProfileError,
    ToolExecutionError,
)
from agentforge.domain.errors import (
    TestProfileBindingMismatchError as BindingMismatchError,
)
from agentforge.domain.test_execution import (
    TestApprovalBinding as ApprovalBinding,
)
from agentforge.domain.test_execution import (
    TestProfile as Profile,
)
from agentforge.persistence.profile_trust import ProfilePurpose
from agentforge.tools.paths import WorkspacePathResolver
from agentforge.tools.testing.profiles import TestProfileDefinition as ProfileDefinition
from agentforge.tools.testing.profiles import (
    TestProfileRegistry as ProfileRegistry,
)


def definition(**updates: object) -> ProfileDefinition:
    values: dict[str, object] = {
        "profile_id": "unit_tests",
        "name": "Unit tests",
        "description": "Run unit tests",
        "executable": sys.executable,
        "argv": ("-c", "print('ok')"),
        "cwd": ".",
        "allowed_env": {"PYTHONUTF8": "1"},
        "timeout_seconds": 30,
        "max_output_bytes": 4096,
        "enabled": True,
        "profile_version": 1,
    }
    values.update(updates)
    return ProfileDefinition(**values)


def make_registry(workspace: Path) -> ProfileRegistry:
    return ProfileRegistry(WorkspacePathResolver(workspace))


def binding_for(profile: Profile) -> ApprovalBinding:
    data = profile.model_dump()
    return ApprovalBinding(
        approval_id=uuid4(),
        run_id=uuid4(),
        checkpoint_id=uuid4(),
        tool_call_digest="f" * 64,
        profile_id=data["profile_id"],
        profile_version=data["profile_version"],
        profile_digest=data["profile_digest"],
        executable_path=data["executable_path"],
        argv_digest=data["argv_digest"],
        cwd=data["cwd"],
        environment_digest=data["environment_digest"],
    )


def test_registration_resolves_executable_once_and_never_inherits_host_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = make_registry(workspace)
    executable_name = Path(sys.executable).name
    profile = registry.register(
        definition(executable=executable_name),
        trusted_search_path=str(Path(sys.executable).parent),
    )
    monkeypatch.setenv("PATH", str(tmp_path / "untrusted"))
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-be-inherited")

    loaded = registry.get("unit_tests")

    assert Path(loaded.executable_path).is_absolute()
    assert loaded.executable_path == profile.executable_path
    assert loaded.argv[0] == loaded.executable_path
    assert loaded.allowed_env == {"PYTHONUTF8": "1"}
    assert "PATH" not in loaded.allowed_env
    assert "OPENAI_API_KEY" not in loaded.allowed_env


@pytest.mark.parametrize(
    "name",
    [
        "OPENAI_API_KEY",
        "ACCESS_TOKEN",
        "CLIENT_SECRET",
        "SSH_AUTH_SOCK",
        "AWS_SECRET_ACCESS_KEY",
        "PASSWORD",
    ],
)
def test_registration_rejects_sensitive_environment_names(
    tmp_path: Path,
    name: str,
) -> None:
    registry = make_registry(tmp_path)

    with pytest.raises(ToolExecutionError):
        registry.register(definition(allowed_env={name: "value"}))


def test_registration_rejects_duplicate_missing_disabled_and_escaping_profiles(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = make_registry(workspace)
    registry.register(definition())

    with pytest.raises(DuplicateTestProfileError):
        registry.register(definition())
    with pytest.raises(ToolExecutionError):
        registry.get("missing")

    disabled = make_registry(workspace)
    disabled.register(definition(enabled=False))
    with pytest.raises(ToolExecutionError):
        disabled.prepare("unit_tests")

    escaping = make_registry(workspace)
    with pytest.raises(ToolExecutionError):
        escaping.register(definition(cwd=".."))


def test_registration_rejects_symlink_or_reparse_cwd(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    real = workspace / "real"
    real.mkdir(parents=True)
    linked = workspace / "linked"
    try:
        linked.symlink_to(real, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"Symlinks are unavailable on this platform: {exc}")

    with pytest.raises(ToolExecutionError):
        make_registry(workspace).register(definition(cwd="linked"))


def test_resume_binding_validates_every_registered_profile_identity(tmp_path: Path) -> None:
    registry = make_registry(tmp_path)
    profile = registry.register(definition())
    binding = binding_for(profile)

    assert registry.require_bound(binding) == profile
    mismatches = {
        "profile_version": 2,
        "profile_digest": "1" * 64,
        "executable_path": str(tmp_path / "other.exe"),
        "argv_digest": "2" * 64,
        "cwd": str(tmp_path / "other"),
        "environment_digest": "3" * 64,
    }
    for field, value in mismatches.items():
        changed = binding.model_copy(update={field: value})
        with pytest.raises(BindingMismatchError, match=field):
            registry.require_bound(changed)


def test_profile_digest_changes_with_any_execution_configuration(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    baseline = make_registry(workspace).register(definition())
    variants = [
        definition(argv=("-c", "print('changed')")),
        definition(cwd="sub"),
        definition(allowed_env={"PYTHONUTF8": "0"}),
        definition(profile_version=2),
    ]
    (workspace / "sub").mkdir()

    for variant in variants:
        changed = make_registry(workspace).register(variant)
        assert changed.profile_digest != baseline.profile_digest


def test_definition_rejects_model_shaped_command_and_environment_fields() -> None:
    with pytest.raises(ValueError):
        ProfileDefinition(
            **{
                **definition().model_dump(),
                "command": "pytest",
                "env": {"TOKEN": "value"},
            }
        )


def test_profile_purpose_is_part_of_the_registered_execution_identity(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = make_registry(workspace)
    verifier = tmp_path / "verifier"
    verifier.mkdir()

    profile = registry.register(
        definition(
            purpose=ProfilePurpose.VERIFICATION,
            argv=("-m", "pytest", "{VERIFIER}"),
            verifier_root=str(verifier),
        )
    )

    assert profile.purpose is ProfilePurpose.VERIFICATION


def test_executable_digest_binds_file_bytes_and_replacement_invalidates_binding(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    executable = workspace / "runner.bin"
    executable.write_bytes(b"trusted executable bytes")
    registry = make_registry(workspace)
    profile = registry.register(definition(executable=str(executable)))
    binding = binding_for(profile)

    assert profile.executable_digest == hashlib.sha256(
        b"trusted executable bytes"
    ).hexdigest()

    executable.write_bytes(b"replaced executable bytes")
    with pytest.raises(BindingMismatchError, match="executable_digest"):
        registry.require_bound(binding)


def test_registration_rejects_symlink_executable_when_platform_supports_it(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "runner.bin"
    target.write_bytes(b"trusted executable bytes")
    linked = workspace / "linked-runner.bin"
    try:
        linked.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"Symlinks are unavailable on this platform: {exc}")

    with pytest.raises(ToolExecutionError):
        make_registry(workspace).register(definition(executable=str(linked)))


def test_registration_rejects_executable_mutation_during_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    executable = workspace / "runner.bin"
    executable.write_bytes(b"trusted executable bytes")
    original_read = os.read
    mutated = False

    def mutate_after_read(descriptor: int, size: int) -> bytes:
        nonlocal mutated
        data = original_read(descriptor, size)
        if data and not mutated:
            mutated = True
            executable.write_bytes(b"changed while digesting")
        return data

    monkeypatch.setattr(os, "read", mutate_after_read)

    with pytest.raises(ToolExecutionError):
        make_registry(workspace).register(definition(executable=str(executable)))
    assert mutated


def test_verification_profile_requires_structured_capsule_paths(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    verifier = tmp_path / "hidden"
    workspace.mkdir()
    verifier.mkdir()
    registry = make_registry(workspace)

    profile = registry.register(
        definition(
            purpose=ProfilePurpose.VERIFICATION,
            runtime_mode=VerificationRuntimeMode.SYSTEM_RUNTIME,
            verifier_root=str(verifier),
            argv=("-m", "pytest", "{VERIFIER}/test_hidden.py"),
        )
    )

    assert profile.runtime_mode is VerificationRuntimeMode.SYSTEM_RUNTIME
    assert profile.verifier_root == str(verifier.resolve())

    for unsafe_argv in (
        ("-m", "pytest", str(verifier / "test_hidden.py")),
        ("-m", "pytest", "{SOURCE}/../outside.py"),
        ("-m", "pytest", "{SCRATCH}/../../outside"),
        ("-m", "pytest", "tests/hidden"),
        ("-m", "pytest", "tests"),
        ("-m", "pytest", r"tests\hidden"),
        ("-m", "pytest", r"--rootdir=C:\outside"),
        ("-m", "pytest", "--rootdir=outside"),
        ("-m", "pytest", "--rootdir=/outside"),
        ("-m", "pytest", "C:relative"),
        ("-m", "pytest", r"\\server\share"),
        ("-m", "pytest", "../outside"),
        ("-m", "pytest", "."),
        ("-m", "pytest", "e\u0301"),
        ("-m", "pytest", "value\x00tail"),
    ):
        with pytest.raises(ToolExecutionError):
            make_registry(workspace).register(
                definition(
                    purpose=ProfilePurpose.VERIFICATION,
                    runtime_mode=VerificationRuntimeMode.SYSTEM_RUNTIME,
                    verifier_root=str(verifier),
                    argv=unsafe_argv,
                )
            )

    for reserved_name in (
        "PYTHONHOME",
        "PYTHONPATH",
        "PYTHONUSERBASE",
        "PYTHONNOUSERSITE",
        "PYTHONDONTWRITEBYTECODE",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD",
        "PYTHONSTARTUP",
        "PYTHONBREAKPOINT",
        "PYTEST_ADDOPTS",
        "PYTEST_PLUGINS",
    ):
        with pytest.raises(ToolExecutionError):
            make_registry(workspace).register(
                definition(
                    purpose=ProfilePurpose.VERIFICATION,
                    runtime_mode=VerificationRuntimeMode.SYSTEM_RUNTIME,
                    verifier_root=str(verifier),
                    allowed_env={reserved_name.lower(): "safe-looking-value"},
                )
            )


@pytest.mark.parametrize("relationship", ["equal", "verifier_inside", "source_inside"])
def test_verification_profile_requires_disjoint_source_and_verifier_roots(
    tmp_path: Path, relationship: str
) -> None:
    outer = tmp_path / "outer"
    outer.mkdir()
    if relationship == "equal":
        workspace = verifier = outer
    elif relationship == "verifier_inside":
        workspace = outer
        verifier = outer / "verifier"
        verifier.mkdir()
    else:
        verifier = outer
        workspace = outer / "workspace"
        workspace.mkdir()

    with pytest.raises(ToolExecutionError):
        make_registry(workspace).register(
            definition(
                purpose=ProfilePurpose.VERIFICATION,
                runtime_mode=VerificationRuntimeMode.SYSTEM_RUNTIME,
                verifier_root=str(verifier),
                argv=("-m", "pytest", "{VERIFIER}"),
            )
        )
