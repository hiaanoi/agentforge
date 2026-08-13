from agentforge.context.renderers import ToolResultRenderer
from agentforge.domain.models import ToolResult


def test_read_file_renderer_keeps_head_tail_and_digest() -> None:
    renderer = ToolResultRenderer(max_content_chars=20)
    result = ToolResult(
        success=True,
        output={
            "path": "README.md",
            "content": "0123456789abcdefghijKLMNOPQRST",
            "byte_size": 30,
            "bytes_read": 30,
            "truncated": False,
        },
    )

    rendered = renderer.render("read_file", result)

    assert rendered.truncated
    assert rendered.output["path"] == "README.md"
    assert "omitted" in rendered.output
    assert len(rendered.sha256_digest) == 64
    assert rendered.original_size > rendered.rendered_size


def test_search_renderer_reports_returned_not_total_count() -> None:
    renderer = ToolResultRenderer()
    result = ToolResult(
        success=True,
        output={
            "results": [{"path": "a.py", "line_number": 1, "snippet": "match"}],
            "count": 1,
            "truncated": True,
        },
    )

    rendered = renderer.render("search_text", result)

    assert rendered.returned_count == 1
    assert "total_count" not in rendered.model_dump()


def test_failed_test_result_contains_structured_repair_guidance() -> None:
    renderer = ToolResultRenderer()
    result = ToolResult(
        success=True,
        output={
            "schema_version": 1,
            "success": False,
            "failure_kind": "TEST_FAILURE",
            "exit_code": 1,
            "stdout_summary": "1 failed",
            "stderr_summary": "",
        },
    )

    rendered = renderer.render("run_tests", result)

    guidance = rendered.output["agent_guidance"]
    assert isinstance(guidance, dict)
    assert guidance["phase"] == "REPAIR_AFTER_TEST_FAILURE"
    assert "rerun" in guidance["instruction"]


def test_failed_test_result_extracts_bounded_structured_feedback() -> None:
    renderer = ToolResultRenderer()
    result = ToolResult(
        success=True,
        output={
            "schema_version": 1,
            "success": False,
            "failure_kind": "TEST_FAILURE",
            "exit_code": 1,
            "stdout_summary": (
                "________________ test_clear __________________\n"
                "FAILED tests/visible/test_phase_clear.py::test_clear - "
                "AssertionError: records were not cleared\n"
                "1 failed, 2 passed in 0.10s\n"
            ),
            "stderr_summary": "",
        },
    )

    rendered = renderer.render("run_tests", result)

    feedback = rendered.output["repair_feedback"]
    assert isinstance(feedback, dict)
    assert feedback["schema_version"] == 1
    assert feedback["phase"] == "DEVELOPMENT_TEST_FAILURE"
    assert feedback["failed_node_ids"] == [
        "tests/visible/test_phase_clear.py::test_clear"
    ]
    assert feedback["assertion_summaries"] == [
        "AssertionError: records were not cleared"
    ]
    assert feedback["exit_code"] == 1
    assert "re-read" in feedback["next_actions"][0]
