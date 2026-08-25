"""Frozen mini-SWE-agent bash protocol (commit 25941c89cfbc91eb40b3f8756348c91d9977d57e)."""

import json
from typing import Any

SUBMIT_MARKER = "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"

BASH_SYSTEM_TEMPLATE = "You are a helpful assistant that can interact with a computer."
BASH_INSTANCE_TEMPLATE = """Please solve this issue: {{task}}

You can execute bash commands and edit files to implement the necessary changes.

Submit your changes and finish your work by issuing the following command:
`echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT`. Do not combine it with any other
command. <important>After this command, you cannot continue working on this task.</important>"""

# Conventional aliases used by the mini-SWE configuration.
SYSTEM_TEMPLATE = BASH_SYSTEM_TEMPLATE
INSTANCE_TEMPLATE = BASH_INSTANCE_TEMPLATE


def bash_tool_schema() -> dict[str, Any]:
    """Return the OpenAI Responses API schema for the bash function tool."""
    return {
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


def format_observation(output: dict[str, Any]) -> str:
    """Format command output using mini-SWE's 10,000-character elision rule."""
    text = output.get("output", "")
    result: dict[str, Any] = {"returncode": output.get("returncode")}
    if len(text) < 10_000:
        result["output"] = text
    else:
        result.update(
            output_head=text[:5_000],
            elided_chars=len(text) - 10_000,
            output_tail=text[-5_000:],
            warning="Output too long.",
        )
    if output.get("exception_info"):
        result["exception_info"] = output["exception_info"]
    return json.dumps(result, ensure_ascii=False)


def parse_submit_output(output: str) -> str | None:
    """Return text after the submit marker only when it is the first output line."""
    lines = output.lstrip().splitlines()
    if not lines or lines[0].strip() != SUBMIT_MARKER:
        return None
    return "\n".join(lines[1:])
