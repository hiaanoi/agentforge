from copy import deepcopy
from typing import cast

from pydantic import JsonValue

from agentforge.domain.models import ToolSpec


def convert_tool_spec(spec: ToolSpec) -> dict[str, JsonValue]:
    parameters = _normalize_schema(deepcopy(spec.input_schema))
    return {
        "type": "function",
        "name": spec.name,
        "description": spec.description,
        "parameters": parameters,
        "strict": True,
    }


def _normalize_schema(value: JsonValue) -> JsonValue:
    if isinstance(value, list):
        return [_normalize_schema(item) for item in value]
    if not isinstance(value, dict):
        return value

    normalized: dict[str, JsonValue] = {
        key: _normalize_schema(item)
        for key, item in value.items()
    }
    if normalized.get("type") == "object" or "properties" in normalized:
        properties = normalized.get("properties", {})
        if not isinstance(properties, dict):
            raise ValueError("Object schema properties must be an object")
        normalized["type"] = "object"
        normalized["additionalProperties"] = False
        normalized["required"] = cast(JsonValue, sorted(properties))
    return normalized
