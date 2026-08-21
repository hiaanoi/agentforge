from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID


@dataclass(frozen=True, slots=True)
class CandidateWorkspace:
    canonical_root: Path
    root: Path

    @classmethod
    def create(cls, canonical_root: Path, *, run_id: UUID) -> CandidateWorkspace:
        canonical = canonical_root.resolve(strict=True)
        if not canonical.is_dir():
            raise ValueError("Canonical workspace must be a directory")
        root = canonical / ".agentforge" / "candidates" / str(run_id) / "workspace"
        shutil.copytree(
            canonical,
            root,
            symlinks=True,
            ignore=shutil.ignore_patterns(".agentforge"),
        )
        return cls(canonical_root=canonical, root=root)
