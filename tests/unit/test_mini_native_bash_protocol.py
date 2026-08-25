from agentforge.repair_engines.mini_native.vendor.bash_protocol import (
    bash_tool_schema,
    format_observation,
    parse_submit_output,
)


def test_bash_tool_schema_matches_upstream_shape():
    assert bash_tool_schema() == {
        "type": "function",
        "name": "bash",
        "description": "Execute a bash command",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The bash command to execute"}
            },
            "required": ["command"],
        },
    }


def test_observation_formatter_preserves_short_and_long_output():
    assert format_observation({"output": "ok", "returncode": 0}) == (
        '{"returncode": 0, "output": "ok"}'
    )
    long = format_observation({"output": "x" * 12_000, "returncode": 1})
    assert '"output_head"' in long and '"output_tail"' in long


def test_submit_marker_is_detected_only_as_first_output_line():
    assert parse_submit_output("COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\npatch") == "patch"
    assert parse_submit_output("prefix\nCOMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT") is None
