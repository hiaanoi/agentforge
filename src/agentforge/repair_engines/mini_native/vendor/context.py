# SPDX-License-Identifier: MIT
# Copyright (c) 2025 Kilian A. Lieret and Carlos E. Jimenez
#
# Ported from SWE-agent/mini-swe-agent at commit
# 25941c89cfbc91eb40b3f8756348c91d9977d57e. See ../NOTICE.md.

"""Context bounding adapted from mini-SWE-agent's observation formatting."""

import json
from typing import Any, cast

from pydantic import JsonValue

MAX_CONTEXT_ITEMS = 12
MAX_OBSERVATION_CHARS = 10_000


def compact_history(history: list[JsonValue]) -> list[JsonValue]:
    """Keep the latest complete action/observation turns in a bounded context."""
    compacted = [_bound_item(item) for item in history[-MAX_CONTEXT_ITEMS:]]
    if len(compacted) % 2 == 1 and len(compacted) > 1:
        compacted = compacted[1:]
    return compacted


def _bound_item(item: JsonValue) -> JsonValue:
    if not isinstance(item, dict) or item.get("kind") != "TOOL_RESULT":
        return item
    payload = item.get("payload")
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    if len(encoded) <= MAX_OBSERVATION_CHARS:
        return item
    half = MAX_OBSERVATION_CHARS // 2
    bounded = {
        "output_head": encoded[:half],
        "output_tail": encoded[-half:],
        "elided_chars": len(encoded) - MAX_OBSERVATION_CHARS,
        "warning": "Output too long.",
    }
    result = dict(cast(dict[str, Any], item))
    result["payload"] = bounded
    return cast(JsonValue, result)


__all__ = ["MAX_CONTEXT_ITEMS", "MAX_OBSERVATION_CHARS", "compact_history"]
