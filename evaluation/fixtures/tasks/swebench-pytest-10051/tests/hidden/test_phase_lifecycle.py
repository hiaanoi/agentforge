import pytest
from phase_capture import PhaseCapture


@pytest.mark.parametrize("phase", ["setup", "call", "teardown"])
def test_clear_preserves_binding_for_every_phase(phase: str) -> None:
    capture = PhaseCapture()
    capture.begin_phase(phase)
    bound = capture.get_records(phase)
    capture.log("before")
    capture.clear()
    assert capture.get_records(phase) is bound
    assert bound == []


def test_logging_after_clear_uses_the_existing_phase_binding() -> None:
    capture = PhaseCapture()
    capture.begin_phase("call")
    bound = capture.get_records("call")
    capture.log("discarded")
    capture.clear()
    capture.log("after")
    assert bound == ["after"]
    assert capture.get_records("call") is bound


def test_repeated_clear_is_stable_and_phases_remain_isolated() -> None:
    capture = PhaseCapture()
    capture.begin_phase("setup")
    capture.log("setup message")
    setup_records = capture.get_records("setup")
    capture.begin_phase("call")
    capture.log("call message")
    call_records = capture.get_records("call")
    capture.clear()
    capture.clear()
    assert setup_records == ["setup message"]
    assert call_records == []
    assert setup_records is not call_records
