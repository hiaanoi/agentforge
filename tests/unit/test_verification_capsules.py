import os
import stat
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


def test_capture_preserves_safe_relative_symlink_and_detects_target_tampering(
    tmp_path: Path,
) -> None:
    source, verifier, store = _roots(tmp_path)
    source_link = source / "pkg" / "linked.py"
    non_nfc_source_link = source / "pkg" / "cafe\u0301-link.py"
    verifier_link = verifier / "linked_test.py"
    try:
        source_link.symlink_to("module.py")
        non_nfc_source_link.symlink_to("module.py")
        verifier_link.symlink_to("test_hidden.py")
    except OSError as exc:
        pytest.skip(f"Symlinks are unavailable on this platform: {exc}")

    builder = VerificationCapsuleBuilder(store)
    capsule = builder.capture(
        execution_id=uuid4(), source_root=source, verifier_root=verifier
    )

    copied_source_link = capsule.source_root / "pkg" / "linked.py"
    copied_normalized_source_link = capsule.source_root / "pkg" / "caf\u00e9-link.py"
    copied_verifier_link = capsule.verifier_root / "linked_test.py"
    assert os.path.islink(copied_source_link)
    assert os.path.islink(copied_normalized_source_link)
    assert os.path.islink(copied_verifier_link)
    assert os.readlink(copied_source_link) == "module.py"
    assert os.readlink(copied_normalized_source_link) == "module.py"
    assert os.readlink(copied_verifier_link) == "test_hidden.py"
    builder.verify(capsule)

    copied_source_link.unlink()
    copied_source_link.symlink_to("different.py")
    with pytest.raises(WorkspaceDigestError):
        builder.verify(capsule)


@pytest.mark.parametrize(
    ("raw_name", "normalized_name"),
    [
        ("cafe\u0301.py", "caf\u00e9.py"),
        ("plain.py", "plain.py"),
    ],
)
def test_capsule_normalizes_entry_names_like_workspace_digester(
    raw_name: str, normalized_name: str
) -> None:
    assert VerificationCapsuleBuilder._normalized_entry_name(raw_name) == normalized_name


@pytest.mark.parametrize("name", ["", "bad/name.py", "bad\x00name.py"])
def test_capsule_rejects_invalid_normalized_entry_names(name: str) -> None:
    with pytest.raises(WorkspaceDigestError):
        VerificationCapsuleBuilder._normalized_entry_name(name)


def test_capture_preserves_workspace_digester_nfc_name_semantics(tmp_path: Path) -> None:
    source, verifier, store = _roots(tmp_path)
    (source / "pkg" / "cafe\u0301.py").write_text("VALUE = 2\n", encoding="utf-8")

    capsule = VerificationCapsuleBuilder(store).capture(
        execution_id=uuid4(), source_root=source, verifier_root=verifier
    )

    assert (
        capsule.source_root / "pkg" / "caf\u00e9.py"
    ).read_text(encoding="utf-8") == "VALUE = 2\n"
    assert capsule.source_digest == WorkspaceDigester().digest(source)


@pytest.mark.parametrize("target", ["/outside.py", "../outside.py"])
def test_capture_rejects_absolute_or_escaping_symlink_targets(
    tmp_path: Path, target: str
) -> None:
    source, verifier, store = _roots(tmp_path)
    link = source / "linked.py"
    try:
        link.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"Symlinks are unavailable on this platform: {exc}")

    execution_id = uuid4()
    with pytest.raises(WorkspaceDigestError):
        VerificationCapsuleBuilder(store).capture(
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


def test_capture_discards_published_capsule_when_sealing_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, verifier, store = _roots(tmp_path)
    capsule_id = uuid4()
    builder = VerificationCapsuleBuilder(store)
    original_seal = builder._seal_tree
    seal_calls = 0

    def fail_seal(root: Path) -> None:
        nonlocal seal_calls
        seal_calls += 1
        if seal_calls == 2:
            raise WorkspaceDigestError()
        original_seal(root)

    monkeypatch.setattr(builder, "_seal_tree", fail_seal)

    with pytest.raises(WorkspaceDigestError):
        builder.capture(
            execution_id=uuid4(),
            capsule_id=capsule_id,
            source_root=source,
            verifier_root=verifier,
        )

    assert not (store / str(capsule_id)).exists()
    assert not any(path.name.startswith(".staging-") for path in store.iterdir())


def test_cleanup_restores_regular_file_permission_before_unlinking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    staging = tmp_path / "sealed-staging"
    staging.mkdir()
    sealed_file = staging / "sealed.py"
    sealed_file.write_text("value = 1\n", encoding="utf-8")
    permission_restored = False
    original_unlink = Path.unlink

    def chmod(path: Path, mode: int, *, follow_symlinks: bool = True) -> None:
        nonlocal permission_restored
        if Path(path) == sealed_file and mode == 0o600:
            permission_restored = True

    def unlink(path: Path, *args: object, **kwargs: object) -> None:
        if Path(path) == sealed_file and not permission_restored:
            raise PermissionError("sealed regular file")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "chmod", chmod)
    monkeypatch.setattr(os, "supports_follow_symlinks", set())
    monkeypatch.setattr(Path, "unlink", unlink)

    VerificationCapsuleBuilder._discard_staging(staging)

    assert permission_restored
    assert not staging.exists()


@pytest.mark.parametrize(
    "item",
    [
        type("LinkStat", (), {"st_mode": stat.S_IFLNK, "st_file_attributes": 0})(),
        type(
            "ReparseStat",
            (),
            {
                "st_mode": stat.S_IFREG,
                "st_file_attributes": getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400),
            },
        )(),
    ],
)
def test_cleanup_never_chmods_symlink_or_reparse_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, item: object
) -> None:
    def forbidden_chmod(*args: object, **kwargs: object) -> None:
        pytest.fail("cleanup must not chmod a link or reparse entry")

    monkeypatch.setattr(os, "chmod", forbidden_chmod)
    VerificationCapsuleBuilder._restore_owned_deletion_mode(
        tmp_path / "entry", item  # type: ignore[arg-type]
    )


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


def test_seal_requests_no_follow_chmod_and_staging_cleanup_never_chmods(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "sealed"
    root.mkdir()
    (root / "child.py").write_text("value = 1\n", encoding="utf-8")
    chmod_calls: list[tuple[Path, int, bool]] = []

    def chmod_no_follow(path: Path, mode: int, *, follow_symlinks: bool) -> None:
        chmod_calls.append((Path(path), mode, follow_symlinks))

    monkeypatch.setattr(os, "chmod", chmod_no_follow)
    monkeypatch.setattr(os, "supports_follow_symlinks", {chmod_no_follow})
    VerificationCapsuleBuilder._seal_tree(root)

    assert chmod_calls
    assert all(not follow_symlinks for _, _, follow_symlinks in chmod_calls)

    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "partial.py").write_text("partial\n", encoding="utf-8")
    VerificationCapsuleBuilder._discard_staging(staging)
    assert not staging.exists()


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
