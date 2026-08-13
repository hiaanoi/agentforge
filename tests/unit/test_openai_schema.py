from agentforge.models.openai_schema import convert_tool_spec
from agentforge.policy.sensitive import SensitiveFilePolicy
from agentforge.tools.paths import WorkspacePathResolver
from agentforge.tools.repository.read_file import ReadFileTool


def test_tool_schema_uses_responses_function_format_and_strict_objects(tmp_path) -> None:
    tool = ReadFileTool(
        WorkspacePathResolver(tmp_path),
        SensitiveFilePolicy(),
    )

    converted = convert_tool_spec(tool.spec)

    assert converted["type"] == "function"
    assert converted["name"] == "read_file"
    assert converted["strict"] is True
    parameters = converted["parameters"]
    assert parameters["type"] == "object"
    assert parameters["additionalProperties"] is False
    assert set(parameters["required"]) == set(parameters["properties"])


def test_nested_objects_are_normalized_recursively() -> None:
    from agentforge.domain.enums import ToolRisk
    from agentforge.domain.models import ToolSpec

    spec = ToolSpec(
        name="nested_tool",
        description="Test nested strict schema.",
        input_schema={
            "type": "object",
            "properties": {
                "options": {
                    "anyOf": [
                        {
                            "type": "object",
                            "properties": {"flag": {"type": "boolean"}},
                            "required": ["flag"],
                        },
                        {"type": "null"},
                    ]
                }
            },
            "required": ["options"],
        },
        risk_level=ToolRisk.READ,
    )

    converted = convert_tool_spec(spec)
    nested = converted["parameters"]["properties"]["options"]["anyOf"][0]

    assert nested["additionalProperties"] is False
