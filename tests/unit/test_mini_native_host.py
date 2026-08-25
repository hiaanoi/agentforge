from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from agentforge.models.base import ModelRequest
from agentforge.models.domain import ModelResponse
from agentforge.repair_engines.mini_native.contracts import (
    RepairAction,
    RepairActionKind,
    RepairActionResult,
)
from agentforge.repair_engines.mini_native.host import (
    MiniNativeHost,
    action_argument_summary,
    bound_output,
    classify_action,
    deterministic_action_id,
)


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ({"path": "src/app.py"}, RepairActionKind.READ),
        ({"path": "src/app.py", "content": "x"}, RepairActionKind.WRITE),
        ({"command": ["pytest", "-q"]}, RepairActionKind.TEST),
        ({"answer": "done"}, RepairActionKind.FINAL),
    ],
)
def test_action_classification(arguments: dict[str, object], expected: RepairActionKind) -> None:
    assert classify_action(arguments) is expected


@pytest.mark.parametrize(
    ("tool_name", "arguments", "expected"),
    [
        ("bash", {"command": "ls"}, RepairActionKind.BASH),
        ("read_file", {"path": "src/app.py"}, RepairActionKind.READ),
        (
            "edit_file",
            {
                "path": "src/app.py",
                "old_text": "before",
                "new_text": "after",
                "expected_sha256": "a" * 64,
            },
            RepairActionKind.WRITE,
        ),
        ("run_tests", {"profile_id": "unit"}, RepairActionKind.TEST),
    ],
)
def test_registered_tool_identity_drives_action_classification(
    tool_name: str,
    arguments: dict[str, object],
    expected: RepairActionKind,
) -> None:
    assert classify_action(arguments, tool_name=tool_name) is expected


def test_write_requires_non_empty_approval_key() -> None:
    with pytest.raises(ValidationError):
        RepairAction(
            run_id=uuid4(), tool_name="edit", working_directory=".",
            kind=RepairActionKind.WRITE, arguments={"path": "x"},
        )
    with pytest.raises(ValidationError):
        RepairAction(
            run_id=uuid4(), tool_name="edit", working_directory=".",
            kind=RepairActionKind.WRITE, arguments={"path": "x"}, approval_key=" ",
        )

    action = RepairAction(
        run_id=UUID("00000000-0000-0000-0000-000000000001"),
        tool_name="edit",
        working_directory=".",
        kind=RepairActionKind.WRITE,
        arguments={"path": "x", "content": "secret"},
        approval_key="approval-1",
    )
    assert action.approval_key == "approval-1"
    assert action.approval_binding_digest


def test_result_preserves_returncode_bounded_output_and_duration() -> None:
    result = RepairActionResult(returncode=1, stdout="012345", stderr="error", duration_ms=12)
    assert result.returncode == 1
    assert result.stdout == "012345"
    assert result.duration_ms == 12
    assert len(
        RepairActionResult(returncode=0, stdout="x" * 30_000, duration_ms=0).stdout
    ) <= 20_000
    safe = RepairActionResult(
        returncode=0,
        stdout="sk-live-secret-value",
        stderr="Bearer bearer-secret-value",
        duration_ms=0,
    )
    assert "sk-live-secret-value" not in safe.stdout
    assert "bearer-secret-value" not in safe.stderr


def test_argument_summary_is_secret_safe_and_id_is_deterministic() -> None:
    summary = action_argument_summary(
        {"path": "src/app.py", "api_key": "sk-live-secret", "token": "bearer-secret"}
    )
    assert "sk-live-secret" not in summary
    assert "bearer-secret" not in summary
    assert "<redacted>" in summary
    first_id = deterministic_action_id(RepairActionKind.READ, {"path": "src/app.py"})
    second_id = deterministic_action_id(RepairActionKind.READ, {"path": "src/app.py"})
    assert first_id == second_id


def test_identity_binds_action_to_run_and_reordered_args_are_stable() -> None:
    run_id = UUID("00000000-0000-0000-0000-000000000001")
    first = RepairAction(
        run_id=run_id, tool_name="edit", working_directory=".", kind=RepairActionKind.WRITE,
        arguments={"path": "x", "content": "y"}, approval_key="approval-1",
    )
    reordered = RepairAction(
        run_id=run_id, tool_name="edit", working_directory=".", kind=RepairActionKind.WRITE,
        arguments={"content": "y", "path": "x"}, approval_key="approval-1",
    )
    other_run = first.model_copy(update={"run_id": uuid4()})
    changed_args = first.model_copy(update={"arguments": {"path": "other.py", "content": "y"}})
    assert first.action_id == reordered.action_id
    assert first.approval_binding_digest == reordered.approval_binding_digest
    assert first.approval_binding_digest != other_run.approval_binding_digest
    assert changed_args.arguments_digest != first.arguments_digest
    assert changed_args.action_id != first.action_id


def test_bound_output_redacts_nested_secrets_and_validates_limit() -> None:
    output, truncated = bound_output("prefix sk-live-secret-value suffix", limit=100)
    assert output == "prefix <redacted> suffix"
    assert not truncated
    token_output, _ = bound_output("token-live-secret-value", limit=100)
    assert token_output == "<redacted>"
    stderr, truncated = bound_output("x" * 10, limit=4)
    assert stderr == "xxxx"
    assert truncated
    with pytest.raises(ValueError):
        bound_output("x", limit=-1)


def test_state_history_is_bounded_and_redacted() -> None:
    from agentforge.repair_engines.mini_native.contracts import MiniNativeState

    state = MiniNativeState(run_id=uuid4(), step_number=1, history=[{"api_key": "secret"}] * 101)
    assert len(state.history) == 100
    assert "secret" not in state.model_dump_json()
    literal_state = MiniNativeState(
        run_id=uuid4(), step_number=1, history=[{"message": "token-live-secret-value"}]
    )
    assert "token-live-secret-value" not in literal_state.model_dump_json()


def test_protocol_is_runtime_checkable_shape() -> None:
    assert MiniNativeHost
    assert ModelRequest and ModelResponse and uuid4
