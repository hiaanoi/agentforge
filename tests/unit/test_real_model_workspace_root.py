import tempfile
from pathlib import Path

from agentforge.evaluation.real_model_pilot import RealModelPilotApplication


def test_real_model_workspace_root_is_under_system_temp_and_isolated() -> None:
    first = RealModelPilotApplication._workspace_root(
        Path("D:/agentforge/.agentforge/pilots/study-a")
    )
    second = RealModelPilotApplication._workspace_root(
        Path("D:/agentforge/.agentforge/pilots/study-b")
    )
    temporary_root = Path(tempfile.gettempdir()).resolve()

    assert first.is_relative_to(temporary_root)
    assert second.is_relative_to(temporary_root)
    assert first != second
