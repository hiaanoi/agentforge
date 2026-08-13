import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest

from agentforge.application.contracts import ProfilePurpose, VerificationRuntimeMode
from agentforge.application.kernel_errors import WorkspaceDigestError
from agentforge.persistence.source_revisions import WorkspaceDigester
from agentforge.persistence.verification_capsules import VerificationCapsuleBuilder
from agentforge.tools.paths import WorkspacePathResolver
from agentforge.tools.testing.profiles import TestProfileDefinition as ProfileDefinition
from agentforge.tools.testing.profiles import TestProfileRegistry as ProfileRegistry


def _roots(tmp_path: Path) -> tuple[Path, Path, Path]:
    source = tmp_path / "workspace"
    verifier = tmp_path / "hidden"
    store = tmp_path / "owned-store"
    (source / "pkg").mkdir(parents=True)
    verifier.mkdir()
    (source / "pkg" / "module.py").write_bytes(b"VALUE = 1\n")
    (verifier / "test_hidden.py").write_bytes(b"def test_value(): pass\n")
    return source.resolve(), verifier.resolve(), store.resolve()


def test_capture_writes_exact_source_and_verifier_bytes_then_seals(
    tmp_path: Path,
) -> None:
    source, verifier, store = _roots(tmp_path)
    builder = VerificationCapsuleBuilder(store)

    capsule = builder.capture(
        execution_id=uuid4(), source_root=source, verifier_root=verifier
    )

    assert capsule.source_digest == WorkspaceDigester().digest(source)
    assert capsule.verifier_digest == WorkspaceDigester().digest(verifier)
    assert (capsule.source_root / "pkg" / "module.py").read_bytes() == b"VALUE = 1\n"
    assert (capsule.verifier_root / "test_hidden.py").read_bytes() == (
        b"def test_value(): pass\n"
    )
    assert capsule.scratch_root.is_dir()
    assert capsule.root.parent == store
    assert capsule.root.name == str(capsule.capsule_id)
    assert not any(path.name.startswith(".staging-") for path in store.iterdir())
    builder.verify(capsule)


def test_store_must_be_disjoint_from_workspace(tmp_path: Path) -> None:
    source, verifier, _ = _roots(tmp_path)

    for unsafe_store in (source / ".agentforge", source.parent):
        with pytest.raises(WorkspaceDigestError):
            VerificationCapsuleBuilder(unsafe_store).capture(
                execution_id=uuid4(), source_root=source, verifier_root=verifier
            )


@pytest.mark.parametrize("relationship", ["equal", "verifier_inside", "source_inside"])
def test_capture_requires_disjoint_source_and_verifier_roots(
    tmp_path: Path, relationship: str
) -> None:
    outer = tmp_path / "outer"
    outer.mkdir()
    if relationship == "equal":
        source = verifier = outer
    elif relationship == "verifier_inside":
        source = outer
        verifier = outer / "verifier"
        verifier.mkdir()
    else:
        verifier = outer
        source = outer / "source"
        source.mkdir()

    with pytest.raises(WorkspaceDigestError):
        VerificationCapsuleBuilder((tmp_path / "store").resolve()).capture(
            execution_id=uuid4(),
            source_root=source.resolve(),
            verifier_root=verifier.resolve(),
        )


def test_store_rejects_linked_ancestor(tmp_path: Path) -> None:
    source, verifier, _ = _roots(tmp_path)
    real_parent = tmp_path / "real-store-parent"
    real_parent.mkdir()
    linked_parent = tmp_path / "linked-store-parent"
    try:
        linked_parent.symlink_to(real_parent, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"Directory symlinks are unavailable on this platform: {exc}")

    with pytest.raises(WorkspaceDigestError):
        VerificationCapsuleBuilder(linked_parent / "capsules").capture(
            execution_id=uuid4(), source_root=source, verifier_root=verifier
        )


def test_capture_rejects_links_and_partial_staging_is_never_executable(
    tmp_path: Path,
) -> None:
    source, verifier, store = _roots(tmp_path)
    link = source / "linked.py"
    try:
        link.symlink_to(source / "pkg" / "module.py")
    except OSError as exc:
        pytest.skip(f"Symlinks are unavailable on this platform: {exc}")

    builder = VerificationCapsuleBuilder(store)
    execution_id = uuid4()
    with pytest.raises(WorkspaceDigestError):
        builder.capture(
            execution_id=execution_id, source_root=source, verifier_root=verifier
        )

    assert not (store / str(execution_id)).exists()


def test_capture_detects_source_mutation_during_single_pass_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, verifier, store = _roots(tmp_path)
    target = source / "pkg" / "module.py"
    original_read = os.read
    changed = False

    def mutate_after_read(descriptor: int, size: int) -> bytes:
        nonlocal changed
        data = original_read(descriptor, size)
        if data and not changed:
            changed = True
            target.write_bytes(b"VALUE = 2\n")
        return data

    monkeypatch.setattr(os, "read", mutate_after_read)

    with pytest.raises(WorkspaceDigestError):
        VerificationCapsuleBuilder(store).capture(
            execution_id=uuid4(), source_root=source, verifier_root=verifier
        )
    assert changed


def test_sealed_mutation_is_detected_and_concurrent_captures_never_share_staging(
    tmp_path: Path,
) -> None:
    source, verifier, store = _roots(tmp_path)
    builder = VerificationCapsuleBuilder(store)

    with ThreadPoolExecutor(max_workers=2) as pool:
        capsules = list(
            pool.map(
                lambda _: builder.capture(
                    execution_id=uuid4(), source_root=source, verifier_root=verifier
                ),
                range(2),
            )
        )

    assert capsules[0].root != capsules[1].root
    assert all(item.root.is_dir() for item in capsules)
    assert not any(path.name.startswith(".staging-") for path in store.iterdir())
    captured = capsules[0].source_root / "pkg" / "module.py"
    captured.chmod(0o600)
    captured.write_bytes(b"tampered\n")
    with pytest.raises(WorkspaceDigestError):
        builder.verify(capsules[0])


def test_launch_profile_expands_only_capsule_mount_references(tmp_path: Path) -> None:
    source, verifier, store = _roots(tmp_path)
    executable = tmp_path / "runtime.exe"
    executable.write_bytes(b"operator trusted runtime")
    registry = ProfileRegistry(WorkspacePathResolver(source))
    profile = registry.register(
        ProfileDefinition(
            profile_id="hidden",
            name="Hidden",
            description="Hidden verifier",
            executable=str(executable),
            argv=("-m", "pytest", "{VERIFIER}/test_hidden.py", "{SOURCE}/pkg"),
            cwd=".",
            allowed_env={},
            timeout_seconds=30,
            max_output_bytes=4096,
            profile_version=1,
            purpose=ProfilePurpose.VERIFICATION,
            runtime_mode=VerificationRuntimeMode.SYSTEM_RUNTIME,
            verifier_root=str(verifier),
        )
    )
    builder = VerificationCapsuleBuilder(store)
    capsule = builder.capture(
        execution_id=uuid4(), source_root=source, verifier_root=verifier
    )

    launched = builder.launch_profile(profile, capsule)

    assert launched.executable_path == profile.executable_path
    assert launched.argv == (
        profile.executable_path,
        "-m",
        "pytest",
        str(capsule.verifier_root / "test_hidden.py"),
        str(capsule.source_root / "pkg"),
    )
    assert launched.cwd == str(capsule.source_root)
    assert launched.allowed_env["PYTHONPATH"] == str(capsule.source_root)
    assert launched.allowed_env["PYTHONNOUSERSITE"] == "1"
    assert launched.allowed_env["PYTHONDONTWRITEBYTECODE"] == "1"
    assert launched.allowed_env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"

    bypassed = profile.model_copy(
        update={"argv": (profile.executable_path, "{SOURCE}/../outside")}
    )
    with pytest.raises(WorkspaceDigestError):
        builder.launch_profile(bypassed, capsule)

    bypassed_environment = profile.model_copy(
        update={"allowed_env": {"CONFIG_PATH": "../outside"}}
    )
    with pytest.raises(WorkspaceDigestError):
        builder.launch_profile(bypassed_environment, capsule)


def test_capsule_launch_profile_runs_captured_verifier_against_captured_source(
    tmp_path: Path,
) -> None:
    source, verifier, store = _roots(tmp_path)
    (verifier / "hidden_test.py").write_text(
        "from pkg.module import VALUE\nprint(VALUE)\n", encoding="utf-8"
    )
    registry = ProfileRegistry(WorkspacePathResolver(source))
    profile = registry.register(
        ProfileDefinition(
            profile_id="hidden_smoke",
            name="Hidden smoke",
            description="Run captured verifier",
            executable=sys.executable,
            argv=("{VERIFIER}/hidden_test.py",),
            cwd=".",
            allowed_env={},
            timeout_seconds=30,
            max_output_bytes=4096,
            profile_version=1,
            purpose=ProfilePurpose.VERIFICATION,
            runtime_mode=VerificationRuntimeMode.SYSTEM_RUNTIME,
            verifier_root=str(verifier),
        )
    )
    builder = VerificationCapsuleBuilder(store)
    capsule = builder.capture(
        execution_id=uuid4(), source_root=source, verifier_root=verifier
    )
    launched = builder.launch_profile(profile, capsule)

    completed = subprocess.run(
        launched.argv,
        cwd=launched.cwd,
        env=dict(launched.allowed_env),
        capture_output=True,
        check=False,
        timeout=10,
    )

    assert completed.returncode == 0
    assert completed.stdout.strip() == b"1"
