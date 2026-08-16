from agentforge.domain.enums import ToolRisk
from agentforge.domain.models import ToolSpec
from agentforge.models.deepseek_schema import convert_tool_spec


def _spec(input_schema: dict[str, object]) -> ToolSpec:
    return ToolSpec(
        name="read_file",
        description="Read one file.",
        input_schema=input_schema,  # type: ignore[arg-type]
        risk_level=ToolRisk.READ,
    )


def test_deepseek_tool_uses_nested_chat_completions_shape() -> None:
    spec = _spec(
        {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        }
    )

    assert convert_tool_spec(spec) == {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read one file.",
            "parameters": spec.input_schema,
        },
    }


def test_deepseek_tool_converter_does_not_mutate_source_schema() -> None:
    spec = _spec({"type": "object", "properties": {}})

    converted = convert_tool_spec(spec)
    function = converted["function"]
    assert isinstance(function, dict)
    parameters = function["parameters"]
    assert isinstance(parameters, dict)
    parameters["additionalProperties"] = False

    assert "additionalProperties" not in spec.input_schema
