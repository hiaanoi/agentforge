from uuid import uuid4

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


def test_write_requires_non_empty_approval_key() -> None:
    with pytest.raises(ValidationError):
        RepairAction(kind=RepairActionKind.WRITE, arguments={"path": "x"})
    with pytest.raises(ValidationError):
        RepairAction(kind=RepairActionKind.WRITE, arguments={"path": "x"}, approval_key=" ")

    action = RepairAction(
        kind=RepairActionKind.WRITE,
        arguments={"path": "x", "content": "secret"},
        approval_key="approval-1",
    )
    assert action.approval_key == "approval-1"


def test_result_preserves_returncode_bounded_output_and_duration() -> None:
    result = RepairActionResult(returncode=1, stdout="012345", stderr="error", duration_ms=12)
    assert result.returncode == 1
    assert result.stdout == "012345"
    assert result.duration_ms == 12
    assert len(
        RepairActionResult(returncode=0, stdout="x" * 30_000, duration_ms=0).stdout
    ) <= 20_000


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


def test_protocol_is_runtime_checkable_shape() -> None:
    assert MiniNativeHost
    assert ModelRequest and ModelResponse and uuid4
