import hashlib
import re
from dataclasses import dataclass
from typing import BinaryIO

_PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?"
    r"-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.DOTALL,
)
_NAMED_SECRET = re.compile(
    r"(?im)\b(?:api[_-]?key|access[_-]?token|client[_-]?secret|password|credential)"
    r"\s*[:=]\s*[^\s]+"
)
_PROVIDER_TOKEN = re.compile(r"\b(?:sk|ghp|github_pat)-?[A-Za-z0-9_-]{20,}\b")


@dataclass(frozen=True)
class CapturedStream:
    retained_bytes: bytes
    summary: str
    sha256_digest: str
    size: int
    truncated: bool


def capture_stream(
    stream: BinaryIO,
    *,
    max_output_bytes: int,
    chunk_size: int = 65_536,
) -> CapturedStream:
    if max_output_bytes <= 0 or chunk_size <= 0:
        raise ValueError("Output and chunk limits must be positive")
    digest = hashlib.sha256()
    retained = bytearray()
    total = 0
    while chunk := stream.read(chunk_size):
        digest.update(chunk)
        total += len(chunk)
        remaining = max_output_bytes - len(retained)
        if remaining > 0:
            retained.extend(chunk[:remaining])
    captured = bytes(retained)
    return CapturedStream(
        retained_bytes=captured,
        summary=_sanitize_summary(captured.decode("utf-8", errors="replace")),
        sha256_digest=digest.hexdigest(),
        size=total,
        truncated=total > len(captured),
    )


def _sanitize_summary(value: str) -> str:
    normalized = "".join(
        character
        for character in value
        if character in {"\n", "\r", "\t"} or ord(character) >= 32
    )
    normalized = _PRIVATE_KEY.sub("<redacted>", normalized)
    normalized = _NAMED_SECRET.sub("<redacted>", normalized)
    return _PROVIDER_TOKEN.sub("<redacted>", normalized)
