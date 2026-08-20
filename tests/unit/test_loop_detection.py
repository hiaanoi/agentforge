from agentforge.context.loop import LoopDetector
from agentforge.context.models import LoopPolicy, LoopState


def _observe(
    detector: LoopDetector,
    state: LoopState,
    *,
    action: str,
    result: str,
    tool: str,
    success: bool,
    error: str | None = None,
):
    return detector.observe(
        state,
        action_digest=action,
        result_digest=result,
        error_code=error,
        tool_name=tool,
        success=success,
    )


def test_repeated_successful_edit_warns_then_terminates() -> None:
    detector = LoopDetector(LoopPolicy(warning_threshold=2, terminal_threshold=3))
    first = _observe(
        detector, LoopState(), action="a", result="r", tool="edit_file", success=True
    )
    second = _observe(
        detector, first.state, action="a", result="r", tool="edit_file", success=True
    )
    third = _observe(
        detector, second.state, action="a", result="r", tool="edit_file", success=True
    )

    assert not first.warning and not first.terminal
    assert second.warning and not second.terminal
    assert third.terminal


def test_successful_repeated_read_is_not_terminal_at_three() -> None:
    detector = LoopDetector(LoopPolicy(warning_threshold=2, terminal_threshold=3))
    state = LoopState()
    for _ in range(3):
        observation = _observe(
            detector, state, action="read", result="result", tool="read_file", success=True
        )
        state = observation.state

    assert observation.warning
    assert not observation.terminal


def test_successful_repeated_read_becomes_terminal_at_dedicated_cap() -> None:
    detector = LoopDetector(LoopPolicy(warning_threshold=2, terminal_threshold=3))
    state = LoopState()
    for _ in range(9):
        observation = _observe(
            detector, state, action="read", result="result", tool="read_file", success=True
        )
        state = observation.state

    assert observation.terminal


def test_repeated_failed_read_remains_terminal_at_three() -> None:
    detector = LoopDetector(LoopPolicy(warning_threshold=2, terminal_threshold=3))
    state = LoopState()
    for _ in range(3):
        observation = _observe(
            detector,
            state,
            action="read",
            result="error",
            tool="read_file",
            success=False,
            error="TOOL_FAILURE",
        )
        state = observation.state

    assert observation.terminal


def test_new_result_and_different_action_do_not_immediately_loop() -> None:
    detector = LoopDetector(LoopPolicy(warning_threshold=2, terminal_threshold=3))
    state = _observe(
        detector, LoopState(), action="a", result="r1", tool="read_file", success=True
    ).state

    changed_result = _observe(
        detector, state, action="a", result="r2", tool="read_file", success=True
    )
    changed_action = _observe(
        detector, changed_result.state, action="b", result="r2", tool="read_file", success=True
    )

    assert not changed_result.terminal
    assert not changed_action.warning


def test_repeated_stable_error_is_detected() -> None:
    detector = LoopDetector(LoopPolicy(warning_threshold=2, terminal_threshold=3))
    first = _observe(
        detector,
        LoopState(),
        action="a",
        result="r",
        tool="read_file",
        success=False,
        error="TIMEOUT",
    )
    second = _observe(
        detector,
        first.state,
        action="a",
        result="r2",
        tool="read_file",
        success=False,
        error="TIMEOUT",
    )

    assert second.warning
