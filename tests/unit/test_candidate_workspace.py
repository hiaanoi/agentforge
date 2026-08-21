from pathlib import Path
from uuid import uuid4


def test_candidate_workspace_write_does_not_change_canonical_workspace(tmp_path: Path) -> None:
    from agentforge.runtime.candidate_workspace import CandidateWorkspace

    canonical = tmp_path / "workspace"
    canonical.mkdir()
    (canonical / "src").mkdir()
    (canonical / "src" / "module.py").write_text("VALUE = 1\n", encoding="utf-8")

    candidate = CandidateWorkspace.create(canonical, run_id=uuid4())
    (candidate.root / "src" / "module.py").write_text("VALUE = 2\n", encoding="utf-8")

    assert (canonical / "src" / "module.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert (candidate.root / "src" / "module.py").read_text(encoding="utf-8") == "VALUE = 2\n"
    assert candidate.root.parent.parent == canonical / ".agentforge" / "candidates"
