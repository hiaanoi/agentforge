from phase_capture import PhaseCapture


def test_clear_updates_records_already_bound_to_call_phase() -> None:
    capture = PhaseCapture()
    capture.begin_phase("call")
    capture.log("before clear")

    capture.clear()

    assert capture.get_records("call") == []
