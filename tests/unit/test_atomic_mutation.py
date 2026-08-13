import hashlib
import os
import stat
from pathlib import Path

import pytest

from agentforge.domain.enums import ToolErrorCode
from agentforge.domain.errors import ToolExecutionError
from agentforge.domain.mutations import MutationPlan
from agentforge.tools.mutation.atomic import AtomicMutationWriter


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def plan(path: str, before: bytes | None, after: bytes) -> MutationPlan:
    return MutationPlan(
        tool_name="write_file",
        target_path=path,
        target_existed=before is not None,
        before_sha256=digest(before) if before is not None else None,
        expected_after_sha256=digest(after),
        bytes_written=len(after),
    )


def test_atomic_create_publishes_without_clobber(tmp_path: Path) -> None:
    target = tmp_path / "new.py"
    data = b"print('new')\n"

    result = AtomicMutationWriter().apply(target, data, plan("new.py", None, data))

    assert target.read_bytes() == data
    assert result.actual_after_sha256 == digest(data)
    assert result.bytes_written == len(data)
    assert list(tmp_path.glob(".new.py.agentforge-*.tmp")) == []


def test_atomic_create_refuses_existing_target(tmp_path: Path) -> None:
    target = tmp_path / "existing.py"
    target.write_bytes(b"original")
    data = b"replacement"

    with pytest.raises(ToolExecutionError) as error:
        AtomicMutationWriter().apply(target, data, plan("existing.py", None, data))

    assert error.value.code is ToolErrorCode.MUTATION_TARGET_EXISTS
    assert target.read_bytes() == b"original"


def test_atomic_replace_checks_hash_and_preserves_mode(tmp_path: Path) -> None:
    target = tmp_path / "app.py"
    before = b"before\n"
    after = b"after\n"
    target.write_bytes(before)
    target.chmod(0o640)
    original_mode = stat.S_IMODE(target.stat().st_mode)

    result = AtomicMutationWriter().apply(target, after, plan("app.py", before, after))

    assert target.read_bytes() == after
    assert result.actual_after_sha256 == digest(after)
    assert stat.S_IMODE(target.stat().st_mode) == original_mode


def test_atomic_replace_rejects_stale_hash_without_writing(tmp_path: Path) -> None:
    target = tmp_path / "app.py"
    target.write_bytes(b"changed")
    expected_before = b"before"
    after = b"after"

    with pytest.raises(ToolExecutionError) as error:
        AtomicMutationWriter().apply(
            target,
            after,
            plan("app.py", expected_before, after),
        )

    assert error.value.code is ToolErrorCode.MUTATION_HASH_MISMATCH
    assert target.read_bytes() == b"changed"


def test_atomic_replace_rechecks_hash_after_temp_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "app.py"
    before = b"before"
    after = b"after"
    target.write_bytes(before)
    writer = AtomicMutationWriter()
    original = writer._write_temp

    def write_then_race(path: Path, data: bytes, mode: int | None) -> Path:
        temporary = original(path, data, mode)
        target.write_bytes(b"external")
        return temporary

    monkeypatch.setattr(writer, "_write_temp", write_then_race)

    with pytest.raises(ToolExecutionError) as error:
        writer.apply(target, after, plan("app.py", before, after))

    assert error.value.code is ToolErrorCode.MUTATION_HASH_MISMATCH
    assert target.read_bytes() == b"external"
    assert list(tmp_path.glob(".app.py.agentforge-*.tmp")) == []


def test_atomic_replace_cleans_temp_when_replace_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "app.py"
    before = b"before"
    after = b"after"
    target.write_bytes(before)

    def fail_replace(source: Path, destination: Path) -> None:
        raise OSError("simulated")

    monkeypatch.setattr(os, "replace", fail_replace)

    with pytest.raises(ToolExecutionError) as error:
        AtomicMutationWriter().apply(target, after, plan("app.py", before, after))

    assert error.value.code is ToolErrorCode.ATOMIC_WRITE_FAILED
    assert target.read_bytes() == before
    assert list(tmp_path.glob(".app.py.agentforge-*.tmp")) == []
