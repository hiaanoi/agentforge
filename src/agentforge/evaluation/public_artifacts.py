"""Validation for JSON that crosses a public product boundary."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from typing import Any


class ForbiddenPublicArtifactError(RuntimeError):
    """Intentionally generic: callers must never echo unsafe artifact content."""


_FORBIDDEN_JSON_KEYS = frozenset(
    {
        "api_key",
        "openai_api_key",
        "password",
        "provider_request_id",
        "raw_arguments",
        "raw_output",
        "raw_response",
        "tool_arguments",
        "tool_output",
        "hidden",
    }
)
_FORBIDDEN_MARKERS = (
    "tests/hidden",
    r"tests\hidden",
    "reference/fixed_files",
    r"reference\fixed_files",
)
_WINDOWS_ABSOLUTE_PATH = re.compile(
    r"(?i)(?:^|[\"'\s=])(?:[a-z]:[\\/]|"
    r"(?:\\\\|//)[^\\/\s]+[\\/][^\\/\s]+[\\/])"
)
_POSIX_ABSOLUTE_PATH = re.compile(r"(?:^|[\"'\s=])/(?!/)")
_MAX_ARTIFACT_BYTES = 1_000_000
_MAX_NODES = 10_000
_MAX_STRING_BYTES = 64_000
_MAX_DEPTH = 64


class PublicArtifactScanner:
    def __init__(
        self,
        *,
        forbidden_values: Iterable[str] = (),
        forbidden_prompt_texts: Iterable[str] = (),
    ) -> None:
        self._forbidden_values = tuple(value for value in forbidden_values if value)
        self._forbidden_prompt_texts = tuple(
            value for value in forbidden_prompt_texts if value
        )

    def validate(self, artifact: str) -> None:
        try:
            if type(artifact) is not str or len(artifact.encode("utf-8")) > _MAX_ARTIFACT_BYTES:
                self._reject()
            stripped = artifact.lstrip()
            if not stripped.startswith(("{", "[", '"')):
                # Evaluation reports are deliberately public Markdown.  Their
                # JSON manifests take the strict decoded branch below.
                self._scan_text(artifact)
                return
            decoded = json.loads(artifact, object_pairs_hook=self._no_duplicate_pairs)
            self._scan_value(decoded)
            # Re-encode decoded values: escaped spellings cannot evade the same checks.
            canonical = json.dumps(
                decoded, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            self._scan_text(canonical)
        except ForbiddenPublicArtifactError:
            raise
        except (TypeError, ValueError, UnicodeError, json.JSONDecodeError):
            self._reject()

    @staticmethod
    def _no_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ForbiddenPublicArtifactError("unsafe public artifact")
            result[key] = value
        return result

    def _scan_value(self, value: object) -> None:
        nodes = 0
        stack: list[tuple[object, int]] = [(value, 0)]
        while stack:
            item, depth = stack.pop()
            nodes += 1
            if nodes > _MAX_NODES or depth > _MAX_DEPTH:
                self._reject()
            if type(item) is str:
                if len(item.encode("utf-8")) > _MAX_STRING_BYTES:
                    self._reject()
                self._scan_text(item)
            elif item is None or type(item) in {bool, int, float}:
                continue
            elif isinstance(item, list):
                stack.extend((child, depth + 1) for child in item)
            elif isinstance(item, Mapping):
                for key, child in item.items():
                    if type(key) is not str or len(key.encode("utf-8")) > _MAX_STRING_BYTES:
                        self._reject()
                    if key.casefold() in _FORBIDDEN_JSON_KEYS:
                        self._reject()
                    self._scan_text(key)
                    stack.append((child, depth + 1))
            else:
                self._reject()

    def _scan_text(self, text: str) -> None:
        folded = text.casefold()
        if folded in _FORBIDDEN_JSON_KEYS:
            self._reject()
        if any(marker.casefold() in folded for marker in _FORBIDDEN_MARKERS):
            self._reject()
        if _WINDOWS_ABSOLUTE_PATH.search(text) or _POSIX_ABSOLUTE_PATH.search(text):
            self._reject()
        if any(value in text for value in self._forbidden_values):
            self._reject()
        if any(prompt in text for prompt in self._forbidden_prompt_texts):
            self._reject()

    @staticmethod
    def _reject() -> None:
        raise ForbiddenPublicArtifactError("unsafe public artifact")
