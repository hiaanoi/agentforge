# ruff: noqa: E501 - frozen upstream prompt text is intentionally preserved.
"""Frozen mini-SWE-agent bash protocol (commit 25941c89cfbc91eb40b3f8756348c91d9977d57e)."""

import json
from typing import Any

SUBMIT_MARKER = "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"

BASH_SYSTEM_TEMPLATE = "You are a helpful assistant that can interact with a computer."
BASH_INSTANCE_TEMPLATE = '''Please solve this issue: {{task}}

You can execute bash commands and edit files to implement the necessary changes.

## Recommended Workflow

This workflow should be done step-by-step so that you can iterate on your changes and any possible problems.

1. Analyze the codebase by finding and reading relevant files
2. Create a script to reproduce the issue
3. Edit the source code to resolve the issue
4. Verify your fix works by running your script again
5. Test edge cases to ensure your fix is robust
6. Submit your changes and finish your work by issuing the following command: `echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT`.
   Do not combine it with any other command. <important>After this command, you cannot continue working on this task.</important>

## Command Execution Rules

You are operating in an environment where

1. You issue at least one command
2. The system executes the command(s) in a subshell
3. You see the result(s)
4. You write your next command(s)

Each response should include:

1. **Reasoning text** where you explain your analysis and plan
2. At least one tool call with your command

**CRITICAL REQUIREMENTS:**

- Your response SHOULD include reasoning text explaining what you're doing
- Your response MUST include AT LEAST ONE bash tool call
- Directory or environment variable changes are not persistent. Every action is executed in a new subshell.
- However, you can prefix any action with `MY_ENV_VAR=MY_VALUE cd /path/to/working/dir && ...` or write/load environment variables from files
- Submit your changes and finish your work by issuing the following command: `echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT`.
  Do not combine it with any other command. <important>After this command, you cannot continue working on this task.</important>

Example of a CORRECT response:
<example_response>
I need to understand the structure of the repository first. Let me check what files are in the current directory to get a better understanding of the codebase.

[Makes bash tool call with {"command": "ls -la"} as arguments]
</example_response>

<system_information>
{{system}} {{release}} {{version}} {{machine}}
</system_information>

## Useful command examples

### Create a new file:

```bash
cat <<'EOF' > newfile.py
import numpy as np
hello = "world"
print(hello)
EOF
```

### Edit files with sed:

{%- if system == "Darwin" -%}
<important>
You are on MacOS. For all the below examples, you need to use `sed -i ''` instead of `sed -i`.
</important>
{%- endif -%}

```bash
# Replace all occurrences
sed -i 's/old_string/new_string/g' filename.py

# Replace only first occurrence
sed -i 's/old_string/new_string/' filename.py

# Replace first occurrence on line 1
sed -i '1s/old_string/new_string/' filename.py

# Replace all occurrences in lines 1-10
sed -i '1,10s/old_string/new_string/g' filename.py
```

### View file content:

```bash
# View specific lines with numbers
nl -ba filename.py | sed -n '10,20p'
```

### Any other command you want to run

```bash
anything
```
'''

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
    lines = output.splitlines()
    if not lines or lines[0] != SUBMIT_MARKER:
        return None
    return "\n".join(lines[1:])
