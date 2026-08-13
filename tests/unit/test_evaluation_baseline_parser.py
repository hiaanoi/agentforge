import hashlib

import pytest
from pydantic import ValidationError

from agentforge.evaluation.baseline_models import (
    BaselineExecutionStatus,
    BaselineFailureReason,
    ExpectedBaselineFailure,
)
from agentforge.evaluation.baseline_parser import PytestBaselineResultParser
from agentforge.process.base import SupervisorOutcome, SupervisorStatus
from agentforge.process.streaming import CapturedStream


def stream(text: str) -> CapturedStream:
    data = text.encode("utf-8")
    return CapturedStream(
        retained_bytes=data,
        summary=text,
        sha256_digest=hashlib.sha256(data).hexdigest(),
        size=len(data),
        truncated=False,
    )


def outcome(stdout: str, *, exit_code: int = 1) -> SupervisorOutcome:
    return SupervisorOutcome(
        status=SupervisorStatus.EXITED,
        root_pid=123,
        process_group_id=None,
        job_id="job-baseline",
        exit_code=exit_code,
        stdout=stream(stdout),
        stderr=stream(""),
        duration_ms=10,
        termination_reason=None,
        termination_result="natural_exit",
        termination_confirmed=True,
    )


def expectation(*node_ids: str) -> ExpectedBaselineFailure:
    return ExpectedBaselineFailure(failed_node_ids=node_ids)


def test_expected_failure_digest_is_deterministic_and_normalizes_paths() -> None:
    windows = expectation(r"tests\visible\test_flow.py::test_once")
    posix = expectation("tests/visible/test_flow.py::test_once")

    assert windows.failed_node_ids == ("tests/visible/test_flow.py::test_once",)
    assert windows.fingerprint_digest == posix.fingerprint_digest


def test_expected_failure_rejects_duplicates_and_hidden_nodes() -> None:
    with pytest.raises(ValidationError):
        expectation("tests/visible/test_flow.py::test_once")
        ExpectedBaselineFailure(
            failed_node_ids=(
                "tests/visible/test_flow.py::test_once",
                r"tests\visible\test_flow.py::test_once",
            )
        )
    with pytest.raises(ValidationError, match="visible"):
        expectation("tests/hidden/test_flow.py::test_once")


def test_parser_verifies_exact_expected_failure_set() -> None:
    result = PytestBaselineResultParser().evaluate(
        outcome(
            "FAILED tests\\visible\\test_flow.py::test_once - AssertionError\n1 failed in 0.03s\n"
        ),
        expectation("tests/visible/test_flow.py::test_once"),
    )

    assert result.status is BaselineExecutionStatus.VERIFIED_EXPECTED_FAILURE
    assert result.failure_reason is None
    assert result.failed_node_ids == ("tests/visible/test_flow.py::test_once",)
    assert result.safe_summary.failed_node_ids == result.failed_node_ids


def test_parser_accepts_native_windows_pytest_line_endings() -> None:
    node = "tests/visible/test_flow.py::test_once"

    result = PytestBaselineResultParser().evaluate(
        outcome(f"FAILED {node}\r\n1 failed in 0.03s\r\n"),
        expectation(node),
    )

    assert result.status is BaselineExecutionStatus.VERIFIED_EXPECTED_FAILURE


@pytest.mark.parametrize(
    ("stdout", "exit_code", "reason"),
    [
        ("1 passed in 0.02s\n", 0, BaselineFailureReason.UNEXPECTED_PASS),
        (
            "FAILED tests/visible/test_flow.py::test_other - AssertionError\n",
            1,
            BaselineFailureReason.FAILURE_FINGERPRINT_MISMATCH,
        ),
        (
            "ERROR collecting tests/visible/test_flow.py\n",
            2,
            BaselineFailureReason.COLLECTION_OR_IMPORT_ERROR,
        ),
        ("pytest failed without node id\n", 1, BaselineFailureReason.FAILURE_OUTPUT_UNPARSABLE),
    ],
)
def test_parser_blocks_invalid_baselines(
    stdout: str,
    exit_code: int,
    reason: BaselineFailureReason,
) -> None:
    result = PytestBaselineResultParser().evaluate(
        outcome(stdout, exit_code=exit_code),
        expectation("tests/visible/test_flow.py::test_once"),
    )

    assert result.status is BaselineExecutionStatus.BLOCKED
    assert result.failure_reason is reason


def test_safe_summary_redacts_paths_and_credentials_and_is_bounded() -> None:
    secret = "OPENAI_API_KEY=sk-sensitive-value"
    text = (
        "FAILED tests/visible/test_flow.py::test_once - AssertionError\n"
        f"C:\\Users\\person\\repo\\source.py {secret}\n" + "x" * 20_000
    )

    result = PytestBaselineResultParser(max_safe_summary_chars=1000).evaluate(
        outcome(text),
        expectation("tests/visible/test_flow.py::test_once"),
    )
    serialized = result.safe_summary.model_dump_json()

    assert "sk-sensitive-value" not in serialized
    assert "C:\\Users\\person" not in serialized
    assert len(result.safe_summary.diagnostic_summary) <= 1000
    assert result.safe_summary.truncated is True


def test_safe_summary_does_not_retain_traceback_source_lines() -> None:
    node = "tests/visible/test_flow.py::test_once"
    text = (
        "________________ test_once ________________\n"
        ">       process_private_fixture('raw fixture content')\n"
        "source.py:20: AssertionError\n"
        "E       AssertionError: expected one dispatch\n"
        f"FAILED {node} - AssertionError\n"
    )

    result = PytestBaselineResultParser().evaluate(outcome(text), expectation(node))

    assert "raw fixture content" not in result.safe_summary.diagnostic_summary
    assert "expected one dispatch" in result.safe_summary.diagnostic_summary
