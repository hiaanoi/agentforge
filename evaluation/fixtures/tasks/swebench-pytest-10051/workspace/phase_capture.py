from __future__ import annotations


class CaptureHandler:
    def __init__(self) -> None:
        self.records: list[str] = []

    def emit(self, message: str) -> None:
        self.records.append(message)

    def reset(self) -> None:
        self.records = []


class PhaseCapture:
    def __init__(self) -> None:
        self.handler = CaptureHandler()
        self._phase_records: dict[str, list[str]] = {}
        self._current_phase: str | None = None

    def begin_phase(self, phase: str) -> None:
        self.handler.reset()
        self._current_phase = phase
        self._phase_records[phase] = self.handler.records

    def log(self, message: str) -> None:
        if self._current_phase is None:
            raise RuntimeError("No active phase")
        self.handler.emit(message)

    def clear(self) -> None:
        self.handler.reset()

    def get_records(self, phase: str) -> list[str]:
        return self._phase_records.get(phase, [])
