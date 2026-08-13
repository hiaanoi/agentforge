import hashlib
import json
import re

from pydantic import JsonValue

from agentforge.context.models import RenderedToolResult
from agentforge.domain.models import ToolResult


class ToolResultRenderer:
    def __init__(self, *, max_content_chars: int = 4_000) -> None:
        self._max_content_chars = max_content_chars

    def render(self, tool_name: str, result: ToolResult) -> RenderedToolResult:
        raw = result.model_dump(mode="json")
        original = self._serialize(raw)
        output = self._render_output(tool_name, result.output)
        rendered = self._serialize(output)
        returned_count = self._returned_count(output)
        return RenderedToolResult(
            output=output,
            original_size=len(original),
            rendered_size=len(rendered),
            truncated=result.truncated or len(rendered) < len(original),
            sha256_digest=hashlib.sha256(original.encode("utf-8")).hexdigest(),
            returned_count=returned_count,
        )

    def _render_output(
        self,
        tool_name: str,
        output: JsonValue | None,
    ) -> dict[str, JsonValue]:
        if not isinstance(output, dict):
            return {"value": output}
        if tool_name == "read_file":
            content = output.get("content")
            if isinstance(content, str) and len(content) > self._max_content_chars:
                half = self._max_content_chars // 2
                return {
                    "path": output.get("path"),
                    "byte_size": output.get("byte_size"),
                    "bytes_read": output.get("bytes_read"),
                    "head": content[:half],
                    "tail": content[-half:],
                    "omitted": len(content) - (half * 2),
                    "truncated": True,
                }
        if tool_name == "run_tests":
            rendered = dict(output)
            if rendered.get("success") is False:
                rendered["repair_feedback"] = self._repair_feedback(rendered)
                rendered["agent_guidance"] = {
                    "phase": "REPAIR_AFTER_TEST_FAILURE",
                    "instruction": (
                        "Development tests failed. Inspect the failure summary, "
                        "re-read the affected files, make the smallest necessary "
                        "approved edit, then rerun the same registered test profile. "
                        "Do not assume the previous edit is correct."
                    ),
                }
            return rendered
        if tool_name in {"list_files", "search_text", "get_git_diff", "read_file"}:
            return dict(output)
        return dict(output)

    @staticmethod
    def _repair_feedback(output: dict[str, JsonValue]) -> dict[str, JsonValue]:
        stdout = output.get("stdout_summary")
        stderr = output.get("stderr_summary")
        combined = "\n".join(
            value for value in (stdout, stderr) if isinstance(value, str)
        )
        failed_node_ids: list[JsonValue] = []
        assertion_summaries: list[JsonValue] = []
        test_summary = ""
        for line in combined.splitlines():
            stripped = line.strip()
            assertion_line = (
                stripped.rsplit(" - ", 1)[-1]
                if " - " in stripped
                else stripped
            )
            if stripped.startswith("FAILED "):
                node = stripped.removeprefix("FAILED ").split(" - ", 1)[0]
                if node and node not in failed_node_ids:
                    failed_node_ids.append(node[:500])
            if re.match(r"^\d+ failed(?:,| )", stripped):
                test_summary = stripped[:500]
            if re.match(
                r"^(?:AssertionError|[A-Za-z_]+Error): ", assertion_line
            ):
                if assertion_line not in assertion_summaries:
                    assertion_summaries.append(assertion_line[:500])
        return {
            "schema_version": 1,
            "phase": "DEVELOPMENT_TEST_FAILURE",
            "failure_kind": output.get("failure_kind"),
            "exit_code": output.get("exit_code"),
            "failed_node_ids": failed_node_ids[:8],
            "assertion_summaries": assertion_summaries[:8],
            "test_summary": test_summary,
            "next_actions": [
                "First re-read the affected files and the failed test contract before editing.",
                "Check each stated success condition, including state and retry behavior.",
                "Make one minimal approved edit and rerun the same registered test profile.",
            ],
        }

    @staticmethod
    def _returned_count(output: dict[str, JsonValue]) -> int | None:
        count = output.get("count")
        if isinstance(count, int):
            return count
        results = output.get("results")
        if isinstance(results, list):
            return len(results)
        files = output.get("files")
        if isinstance(files, list):
            return len(files)
        return None

    @staticmethod
    def _serialize(value: JsonValue) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
