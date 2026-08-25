from __future__ import annotations

import re
from enum import StrEnum


class CommandKind(StrEnum):
    READ = "READ"
    TEST = "TEST"
    WRITE = "WRITE"
    OTHER = "OTHER"


_READ_PREFIXES = (
    "ls",
    "find",
    "grep",
    "rg",
    "cat",
    "sed -n",
    "head",
    "tail",
    "git status",
    "git diff",
)
_TEST_PREFIXES = ("pytest", "tox", "nox", "python -m pytest")
_WRITE_COMMAND = re.compile(r"(?:^|[;&|]\s*|\s)(?:cp|mv|rm)\b")
_IN_PLACE_EDIT = re.compile(r"(?:^|[;&|]\s*|\s)(?:sed|perl)\s+-[^\n;|]*i\b")


def classify_command(command: str) -> CommandKind:
    """Classify only obvious shell commands; unknown commands remain unrestricted."""
    normalized = command.strip().casefold()
    if _IN_PLACE_EDIT.search(normalized) or ">" in normalized or _WRITE_COMMAND.search(normalized):
        return CommandKind.WRITE
    if normalized.startswith(_TEST_PREFIXES):
        return CommandKind.TEST
    if normalized.startswith(_READ_PREFIXES):
        return CommandKind.READ
    return CommandKind.OTHER


__all__ = ["CommandKind", "classify_command"]
