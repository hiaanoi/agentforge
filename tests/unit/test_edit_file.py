import hashlib
from pathlib import Path

import pytest
from pydantic import ValidationError

from agentforge.domain.enums import ToolErrorCode, ToolRisk
from agentforge.domain.errors import ToolExecutionError
from agentforge.policy.sensitive import SensitiveFilePolicy
from agentforge.tools.mutation.base import MutationLimits
from agentforge.tools.mutation.edit_file import EditFileArguments, EditFileTool
from agentforge.tools.mutation.security import MutationSecurityPolicy
from agentforge.tools.paths import WorkspacePathResolver


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def make_tool(workspace: Path) -> EditFileTool:
    return EditFileTool(
        MutationSecurityPolicy(
            WorkspacePathResolver(workspace),
            SensitiveFilePolicy(),
            MutationLimits(
                max_file_bytes=128,
                max_old_text_bytes=32,
                max_new_text_bytes=32,
            ),
        )
    )


def test_edit_file_arguments_require_hash_and_nonempty_old_text() -> None:
    arguments = EditFileArguments(
        path="app.py",
        old_text="old",
        new_text="new",
        expected_sha256="a" * 64,
    )

    assert arguments.old_text == "old"
    with pytest.raises(ValidationError):
        EditFileArguments(
            path="app.py",
            old_text="",
            new_text="new",
            expected_sha256="a" * 64,
        )


def test_edit_file_prepares_unique_exact_replacement_without_writing(
    tmp_path: Path,
) -> None:
    target = tmp_path / "app.py"
    target.write_text("before old after\n", encoding="utf-8")
    before = target.read_bytes()
    tool = make_tool(tmp_path)

    plan = tool.prepare(
        EditFileArguments(
            path="app.py",
            old_text="old",
            new_text="new",
            expected_sha256=sha256(before),
        )
    )

    assert target.read_bytes() == before
    assert plan.before_sha256 == sha256(before)
    assert plan.expected_after_sha256 == sha256(before.replace(b"old", b"new"))


@pytest.mark.parametrize(
    ("content", "old_text"),
    [("nothing here", "missing"), ("old then old", "old")],
)
def test_edit_file_requires_exactly_one_match(
    tmp_path: Path, content: str, old_text: str
) -> None:
    target = tmp_path / "app.py"
    target.write_text(content, encoding="utf-8")
    tool = make_tool(tmp_path)

    with pytest.raises(ToolExecutionError) as error:
        tool.prepare(
            EditFileArguments(
                path="app.py",
                old_text=old_text,
                new_text="new",
                expected_sha256=sha256(target.read_bytes()),
            )
        )

    assert error.value.code is ToolErrorCode.MUTATION_TEXT_MISMATCH


def test_edit_file_rejects_empty_result_and_executes_safe_edit(tmp_path: Path) -> None:
    target = tmp_path / "app.py"
    target.write_text("old", encoding="utf-8")
    tool = make_tool(tmp_path)
    with pytest.raises(ToolExecutionError):
        tool.prepare(
            EditFileArguments(
                path="app.py",
                old_text="old",
                new_text="",
                expected_sha256=sha256(b"old"),
            )
        )

    target.write_text("prefix old suffix", encoding="utf-8")
    result = tool.execute(
        EditFileArguments(
            path="app.py",
            old_text="old",
            new_text="new",
            expected_sha256=sha256(b"prefix old suffix"),
        )
    )

    assert result.success is True
    assert target.read_text(encoding="utf-8") == "prefix new suffix"
    assert "prefix" not in result.model_dump_json()
    assert "suffix" not in result.model_dump_json()
    assert result.metadata["after_sha256"] == sha256(b"prefix new suffix")


def test_edit_file_spec_is_local_write_and_requires_approval(tmp_path: Path) -> None:
    spec = make_tool(tmp_path).spec

    assert spec.risk_level is ToolRisk.WRITE
    assert spec.requires_approval is True


@pytest.mark.parametrize(
    ("old_text", "new_text"),
    [("x" * 33, "new"), ("old", "x" * 33)],
)
def test_edit_file_enforces_old_and_new_text_limits(
    tmp_path: Path, old_text: str, new_text: str
) -> None:
    target = tmp_path / "app.py"
    target.write_text("old", encoding="utf-8")

    with pytest.raises(ToolExecutionError) as error:
        make_tool(tmp_path).prepare(
            EditFileArguments(
                path="app.py",
                old_text=old_text,
                new_text=new_text,
                expected_sha256=sha256(target.read_bytes()),
            )
        )

    assert error.value.code is ToolErrorCode.FILE_TOO_LARGE
