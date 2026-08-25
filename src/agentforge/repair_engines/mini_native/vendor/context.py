# SPDX-License-Identifier: MIT
# Copyright (c) 2025 Kilian A. Lieret and Carlos E. Jimenez
#
# Ported from SWE-agent/mini-swe-agent at commit
# 25941c89cfbc91eb40b3f8756348c91d9977d57e. See ../NOTICE.md.

"""Context bounding adapted from mini-SWE-agent's observation formatting."""

import json
import re
from typing import Any, cast

from pydantic import JsonValue

MAX_CONTEXT_ITEMS = 12
MAX_OBSERVATION_CHARS = 10_000
_SECRET_KEY = re.compile(r"(?:api[_-]?key|token|secret|password|authorization|credential)", re.I)
_SECRET_VALUE = re.compile(
    r"(?:sk-[A-Za-z0-9_-]{8,}|Bearer\s+[A-Za-z0-9._-]{8,}|token-[A-Za-z0-9._-]{8,})",
    re.I,
)


def compact_history(history: list[JsonValue]) -> list[JsonValue]:
    """Keep the latest complete action/observation turns in a bounded context."""
    compacted = [_bound_item(item) for item in history[-MAX_CONTEXT_ITEMS:]]
    if len(compacted) % 2 == 1 and len(compacted) > 1:
        compacted = compacted[1:]
    return compacted


def _bound_item(item: JsonValue) -> JsonValue:
    if not isinstance(item, dict) or item.get("kind") not in {"TOOL_CALL", "TOOL_RESULT"}:
        return item
    payload = _redact(item.get("payload"))
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    result = dict(cast(dict[str, Any], item))
    if len(encoded) <= MAX_OBSERVATION_CHARS:
        result["payload"] = payload
        return cast(JsonValue, result)
    half = (MAX_OBSERVATION_CHARS - 500) // 2
    bounded = {
        "output_head": encoded[:half],
        "output_tail": encoded[-half:],
        "elided_chars": len(encoded) - MAX_OBSERVATION_CHARS,
        "warning": "Output too long.",
    }
    result["payload"] = bounded
    return cast(JsonValue, result)


def _redact(value: Any, key: str | None = None) -> Any:
    if key is not None and _SECRET_KEY.search(key):
        return "<redacted>"
    if isinstance(value, dict):
        return {str(name): _redact(item, str(name)) for name, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        return _SECRET_VALUE.sub("<redacted>", value)
    return value


__all__ = ["MAX_CONTEXT_ITEMS", "MAX_OBSERVATION_CHARS", "compact_history"]
