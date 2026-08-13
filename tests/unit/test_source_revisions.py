from __future__ import annotations

import os
import subprocess
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from agentforge.application.kernel_errors import WorkspaceDigestError
from agentforge.persistence.source_revisions import (
    DIGEST_ALGORITHM_VERSION,
    SourceRevision,
    WorkspaceDigester,
    WorkspaceDigestLimits,
    source_revision_audit,
)


def _write_tree(root: Path, files: dict[str, bytes]) -> None:
    for relative, data in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


@pytest.mark.parametrize(
    ("source_bound", "actual_verified", "semantics", "verified"),
    [
        (True, False, "BOUND_REVISION_V1", False),
        (True, True, "BOUND_REVISION_V1", True),
        (False, False, "UNBOUND_EVALUATOR_ONLY", False),
        (False, True, "UNBOUND_EVALUATOR_ONLY", False),
    ],
)
def test_source_revision_audit_separates_binding_from_actual_verification(
    source_bound: bool,
    actual_verified: bool,
    semantics: str,
    verified: bool,
) -> None:
    assert source_revision_audit(
        source_bound, actual_digest_verified=actual_verified
    ) == (semantics, verified)


def test_digest_is_sorted_byte_exact_and_excludes_only_approved_metadata(
    tmp_path: Path,
) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    logical = {"b.py": b"b\r\n", "nested/a.py": b"a\n"}
    _write_tree(first_root, logical)
    _write_tree(second_root, dict(reversed(tuple(logical.items()))))
    _write_tree(
        first_root,
        {
            ".git/HEAD": b"ignored",
            "nested/.agentforge/state.db": b"ignored",
            "nested/build/generated.py": b"ignored",
            "real_build.py": b"included",
        },
    )
    _write_tree(second_root, {"real_build.py": b"included"})

    digester = WorkspaceDigester()
    assert digester.digest(first_root) == digester.digest(second_root)
    assert digester.digest(first_root) != digester.project_digest(
        digester.snapshot(first_root),
        relative_path="b.py",
        size_bytes=2,
        content_sha256=digester.content_digest(b"b\n"),
    )


def test_digest_distinguishes_unambiguous_path_length_and_raw_content_frames(
    tmp_path: Path,
) -> None:
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()
    _write_tree(left, {"a": b"bc", "d": b""})
    _write_tree(right, {"a": b"b", "cd": b""})
    assert WorkspaceDigester().digest(left) != WorkspaceDigester().digest(right)


def test_digest_rejects_symlink_root_and_entry_without_disclosing_path(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    target.mkdir()
    (target / "source.py").write_text("safe", encoding="utf-8")
    link = tmp_path / "link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable on this platform")

    with pytest.raises(WorkspaceDigestError) as root_error:
        WorkspaceDigester().digest(link)
    with pytest.raises(WorkspaceDigestError) as entry_error:
        WorkspaceDigester().digest(tmp_path)
    assert str(root_error.value) == "workspace digest rejected"
    assert str(entry_error.value) == "workspace digest rejected"
    assert str(target) not in str(root_error.value)


@pytest.mark.parametrize(
    "limits",
    [
        WorkspaceDigestLimits(max_files=1, max_file_bytes=10, max_total_bytes=10),
        WorkspaceDigestLimits(
            max_entries=10, max_files=10, max_file_bytes=1, max_total_bytes=10
        ),
        WorkspaceDigestLimits(
            max_entries=10, max_files=10, max_file_bytes=10, max_total_bytes=1
        ),
    ],
)
def test_digest_fails_closed_at_every_resource_limit(
    tmp_path: Path, limits: WorkspaceDigestLimits
) -> None:
    _write_tree(tmp_path, {"a.py": b"aa", "b.py": b"bb"})
    with pytest.raises(WorkspaceDigestError, match=r"^workspace digest rejected$"):
        WorkspaceDigester(limits=limits).digest(tmp_path)


def test_digest_rejects_case_collisions(tmp_path: Path) -> None:
    _write_tree(tmp_path, {"A.py": b"one", "a.py": b"two"})
    if len(list(tmp_path.iterdir())) == 1:
        pytest.skip("case-insensitive filesystem cannot construct the collision")
    with pytest.raises(WorkspaceDigestError):
        WorkspaceDigester().digest(tmp_path)


def test_digest_rejects_unicode_normalization_collisions(tmp_path: Path) -> None:
    composed = "caf\u00e9.py"
    decomposed = "cafe\u0301.py"
    _write_tree(tmp_path, {composed: b"one", decomposed: b"two"})
    if len(list(tmp_path.iterdir())) == 1:
        pytest.skip("normalizing filesystem cannot construct the collision")
    with pytest.raises(WorkspaceDigestError):
        WorkspaceDigester().digest(tmp_path)


@pytest.mark.parametrize(("file_count", "accepted"), [(2, True), (3, False)])
def test_digest_max_files_exact_boundary(
    tmp_path: Path, file_count: int, accepted: bool
) -> None:
    _write_tree(tmp_path, {f"{index}.py": b"x" for index in range(file_count)})
    digester = WorkspaceDigester(
        limits=WorkspaceDigestLimits(
            max_entries=10, max_files=2, max_file_bytes=10, max_total_bytes=10
        )
    )
    if accepted:
        digester.digest(tmp_path)
    else:
        with pytest.raises(WorkspaceDigestError):
            digester.digest(tmp_path)


@pytest.mark.parametrize(("file_size", "accepted"), [(2, True), (3, False)])
def test_digest_max_file_bytes_exact_boundary(
    tmp_path: Path, file_size: int, accepted: bool
) -> None:
    (tmp_path / "source.py").write_bytes(b"x" * file_size)
    digester = WorkspaceDigester(
        limits=WorkspaceDigestLimits(
            max_entries=10, max_files=10, max_file_bytes=2, max_total_bytes=10
        )
    )
    if accepted:
        digester.digest(tmp_path)
    else:
        with pytest.raises(WorkspaceDigestError):
            digester.digest(tmp_path)


@pytest.mark.parametrize(("total_bytes", "accepted"), [(4, True), (5, False)])
def test_digest_max_total_bytes_exact_boundary(
    tmp_path: Path, total_bytes: int, accepted: bool
) -> None:
    _write_tree(tmp_path, {"a.py": b"xx", "b.py": b"x" * (total_bytes - 2)})
    digester = WorkspaceDigester(
        limits=WorkspaceDigestLimits(
            max_entries=10, max_files=10, max_file_bytes=10, max_total_bytes=4
        )
    )
    if accepted:
        digester.digest(tmp_path)
    else:
        with pytest.raises(WorkspaceDigestError):
            digester.digest(tmp_path)


def test_digest_rejects_special_files_when_constructible(tmp_path: Path) -> None:
    if os.name == "nt" or not hasattr(os, "mkfifo"):
        pytest.skip("FIFO construction is unavailable on this platform")
    os.mkfifo(tmp_path / "pipe")
    with pytest.raises(WorkspaceDigestError):
        WorkspaceDigester().digest(tmp_path)


def test_source_revision_is_strict_frozen_and_requires_fixed_digest_shape() -> None:
    revision = SourceRevision(
        run_id=UUID(int=1),
        initial_source_digest="a" * 64,
        expected_source_digest="b" * 64,
        source_revision_number=0,
        digest_algorithm_version=DIGEST_ALGORITHM_VERSION,
    )
    with pytest.raises(ValidationError):
        SourceRevision.model_validate(
            {
                **revision.model_dump(),
                "source_revision_number": True,
            }
        )
    with pytest.raises(ValidationError):
        SourceRevision.model_validate(
            {
                **revision.model_dump(),
                "expected_source_digest": "short",
            }
        )
    with pytest.raises(ValidationError):
        SourceRevision.model_validate(
            {
                **revision.model_dump(),
                "digest_algorithm_version": DIGEST_ALGORITHM_VERSION + 1,
            }
        )
    with pytest.raises(ValidationError):
        revision.source_revision_number = 1


def test_digest_wraps_permission_failure_without_path_or_os_details(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = tmp_path / "private-source.py"
    secret.write_text("private", encoding="utf-8")

    def denied(_: object) -> object:
        raise PermissionError("private-source.py access denied")

    monkeypatch.setattr(os, "scandir", denied)
    with pytest.raises(WorkspaceDigestError) as error:
        WorkspaceDigester().digest(tmp_path)
    assert str(error.value) == "workspace digest rejected"
    assert "private-source.py" not in str(error.value)


def test_digest_detects_file_change_during_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "source.py"
    target.write_bytes(b"before")
    original_read = os.read
    changed = False

    def racing_read(descriptor: int, size: int) -> bytes:
        nonlocal changed
        chunk = original_read(descriptor, size)
        if chunk and not changed:
            changed = True
            target.write_bytes(b"changed-size")
        return chunk

    monkeypatch.setattr(os, "read", racing_read)
    with pytest.raises(WorkspaceDigestError, match=r"^workspace digest rejected$"):
        WorkspaceDigester().digest(tmp_path)


@pytest.mark.parametrize("invalid", [0, -1, True, 1.5])
def test_digest_entry_limit_requires_positive_exact_integer(invalid: object) -> None:
    with pytest.raises(ValueError):
        WorkspaceDigestLimits(max_entries=invalid)  # type: ignore[arg-type]


@pytest.mark.parametrize(("count", "accepted"), [(3, True), (4, False)])
def test_digest_counts_empty_directory_entries_at_exact_boundary(
    tmp_path: Path, count: int, accepted: bool
) -> None:
    for index in range(count):
        (tmp_path / f"empty-{index}").mkdir()
    digester = WorkspaceDigester(
        limits=WorkspaceDigestLimits(
            max_entries=3,
            max_files=10,
            max_file_bytes=10,
            max_total_bytes=10,
        )
    )
    if accepted:
        digester.digest(tmp_path)
    else:
        with pytest.raises(WorkspaceDigestError):
            digester.digest(tmp_path)


def test_digest_counts_excluded_directories_before_skipping_contents(tmp_path: Path) -> None:
    for index in range(4):
        parent = tmp_path / f"parent-{index}"
        (parent / "build").mkdir(parents=True)
        (parent / "build" / "ignored.py").write_text("ignored", encoding="utf-8")
    with pytest.raises(WorkspaceDigestError):
        WorkspaceDigester(
            limits=WorkspaceDigestLimits(
                max_entries=7,
                max_files=10,
                max_file_bytes=100,
                max_total_bytes=100,
            )
        ).digest(tmp_path)


def test_digest_rejects_relative_and_parent_traversal_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(tmp_path)
    for root in (Path("workspace"), tmp_path / "workspace" / ".." / "workspace"):
        with pytest.raises(WorkspaceDigestError):
            WorkspaceDigester().digest(root)


def test_digest_rejects_symlink_in_root_ancestor_chain(tmp_path: Path) -> None:
    real_parent = tmp_path / "real-parent"
    workspace = real_parent / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "source.py").write_text("safe", encoding="utf-8")
    linked_parent = tmp_path / "linked-parent"
    try:
        linked_parent.symlink_to(real_parent, target_is_directory=True)
    except OSError:
        pytest.skip("ancestor symlink creation is unavailable on this platform")
    with pytest.raises(WorkspaceDigestError):
        WorkspaceDigester().digest(linked_parent / "workspace")


def test_digest_rejects_junction_in_root_ancestor_chain_on_windows(
    tmp_path: Path,
) -> None:
    if os.name != "nt":
        pytest.skip("Windows junctions are unavailable on this platform")
    real_parent = tmp_path / "real-parent"
    workspace = real_parent / "workspace"
    workspace.mkdir(parents=True)
    junction = tmp_path / "junction-parent"
    created = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(real_parent)],
        capture_output=True,
        check=False,
        text=True,
    )
    if created.returncode != 0:
        pytest.skip("junction creation is unavailable in this environment")
    try:
        with pytest.raises(WorkspaceDigestError):
            WorkspaceDigester().digest(junction / "workspace")
    finally:
        os.rmdir(junction)


def test_digest_rejects_directory_identity_swap_between_enumeration_and_recursion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    child = workspace / "child"
    replacement = tmp_path / "replacement"
    child.mkdir(parents=True)
    replacement.mkdir()
    (child / "source.py").write_text("safe", encoding="utf-8")
    child_stat = os.lstat(child)
    replacement_stat = os.lstat(replacement)
    os.utime(replacement, ns=(child_stat.st_atime_ns, child_stat.st_mtime_ns))
    replacement_stat = os.lstat(replacement)
    original_lstat = os.lstat
    original_scandir = os.scandir
    calls = 0

    def swapped_lstat(path: object, *args: object, **kwargs: object) -> os.stat_result:
        nonlocal calls
        if Path(path) == child:
            calls += 1
            if calls == 2:
                return replacement_stat
        return original_lstat(path, *args, **kwargs)

    def guarded_scandir(path: object) -> object:
        if Path(path) == child and calls >= 2:
            raise AssertionError("changed directory must be rejected before traversal")
        return original_scandir(path)

    monkeypatch.setattr(os, "lstat", swapped_lstat)
    monkeypatch.setattr(os, "scandir", guarded_scandir)
    with pytest.raises(WorkspaceDigestError):
        WorkspaceDigester().digest(workspace)


def test_digest_rejects_directory_to_file_type_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    child = workspace / "child"
    child.mkdir(parents=True)
    replacement_file = tmp_path / "replacement-file"
    replacement_file.write_text("not a directory", encoding="utf-8")
    replacement_stat = os.lstat(replacement_file)
    original_lstat = os.lstat
    original_scandir = os.scandir
    calls = 0

    def swapped_lstat(path: object, *args: object, **kwargs: object) -> os.stat_result:
        nonlocal calls
        if Path(path) == child:
            calls += 1
            if calls == 2:
                return replacement_stat
        return original_lstat(path, *args, **kwargs)

    def guarded_scandir(path: object) -> object:
        if Path(path) == child and calls >= 2:
            raise AssertionError("type-swapped directory must not be traversed")
        return original_scandir(path)

    monkeypatch.setattr(os, "lstat", swapped_lstat)
    monkeypatch.setattr(os, "scandir", guarded_scandir)
    with pytest.raises(WorkspaceDigestError):
        WorkspaceDigester().digest(workspace)
