from __future__ import annotations

from pathlib import Path
from typing import TextIO


def _legacy_open(path: Path, mode: str, *, encoding: str | None = None) -> TextIO:
    return path.open(mode, encoding=encoding or "cp1252", newline="")


def dump_diagnostic(path: Path, *output: str) -> Path:
    with _legacy_open(path, "w", encoding="utf-8") as handle:
        for text in output:
            handle.write(text)
    return path
