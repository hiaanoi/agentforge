from uuid import uuid4

import pytest
from pydantic import ValidationError

from agentforge.domain.enums import EventType, RunStatus, ToolRisk
from agentforge.domain.errors import InvalidStateTransitionError
from agentforge.domain.models import (
    Checkpoint,
    Event,
    Run,
    RunBudget,
    ToolResult,
    ToolSpec,
)


def test_run_has_safe_defaults_and_can_follow_valid_transitions() -> None:
    run = Run(task="exercise deterministic runtime", max_steps=3)

    assert run.status is RunStatus.CREATED
    assert run.current_step == 0

    run.transition_to(RunStatus.RUNNING)
    run.transition_to(RunStatus.COMPLETED)

    assert run.status is RunStatus.COMPLETED


def test_illegal_run_transition_is_rejected() -> None:
    run = Run(task="cannot skip execution")

    with pytest.raises(InvalidStateTransitionError):
        run.transition_to(RunStatus.COMPLETED)


def test_terminal_run_cannot_restart() -> None:
    run = Run(task="terminal state")
    run.transition_to(RunStatus.CANCELLED)

    with pytest.raises(InvalidStateTransitionError):
        run.transition_to(RunStatus.RUNNING)


def test_domain_models_validate_required_constraints() -> None:
    run_id = uuid4()
    event = Event(run_id=run_id, event_type=EventType.RUN_CREATED, sequence_number=1)
    checkpoint = Checkpoint(run_id=run_id, step_number=0, runtime_state={"history": []})
    spec = ToolSpec(
        name="echo",
        description="Return text",
        input_schema={"type": "object"},
        risk_level=ToolRisk.READ,
    )
    result = ToolResult(success=True, output="hello")
    budget = RunBudget(max_steps=2, max_model_calls=2, max_tool_calls=1)

    assert event.run_id == checkpoint.run_id
    assert spec.timeout_seconds > 0
    assert result.error_message is None
    assert budget.max_steps == 2

    with pytest.raises(ValidationError):
        RunBudget(max_steps=0)

