from agentforge.context.loop import LoopDetector
from agentforge.context.models import LoopPolicy, LoopState


def test_repeated_action_and_result_warns_then_terminates() -> None:
    detector = LoopDetector(LoopPolicy(warning_threshold=2, terminal_threshold=3))
    state = LoopState()

    first = detector.observe(state, action_digest="a", result_digest="r", error_code=None)
    second = detector.observe(first.state, action_digest="a", result_digest="r", error_code=None)
    third = detector.observe(second.state, action_digest="a", result_digest="r", error_code=None)

    assert not first.warning and not first.terminal
    assert second.warning and not second.terminal
    assert third.terminal


def test_new_result_and_different_action_do_not_immediately_loop() -> None:
    detector = LoopDetector(LoopPolicy(warning_threshold=2, terminal_threshold=3))
    state = detector.observe(
        LoopState(), action_digest="a", result_digest="r1", error_code=None
    ).state

    changed_result = detector.observe(
        state, action_digest="a", result_digest="r2", error_code=None
    )
    changed_action = detector.observe(
        changed_result.state,
        action_digest="b",
        result_digest="r2",
        error_code=None,
    )

    assert not changed_result.terminal
    assert not changed_action.warning


def test_repeated_stable_error_is_detected() -> None:
    detector = LoopDetector(LoopPolicy(warning_threshold=2, terminal_threshold=3))
    first = detector.observe(
        LoopState(), action_digest="a", result_digest="r", error_code="TIMEOUT"
    )
    second = detector.observe(
        first.state, action_digest="a", result_digest="r2", error_code="TIMEOUT"
    )

    assert second.warning
