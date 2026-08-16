from copy import deepcopy

from pydantic import JsonValue

from agentforge.domain.models import ToolSpec


def convert_tool_spec(spec: ToolSpec) -> dict[str, JsonValue]:
    return {
        "type": "function",
        "function": {
            "name": spec.name,
            "description": spec.description,
            "parameters": deepcopy(spec.input_schema),
        },
    }
