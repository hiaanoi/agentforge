import hashlib
import json
import re

from agentforge.evaluation.baseline_models import (
    BaselineEvaluation,
    BaselineExecutionStatus,
    BaselineFailureReason,
    BaselineFailureSummary,
    ExpectedBaselineFailure,
    normalize_node_id,
)
from agentforge.process.base import SupervisorOutcome, SupervisorStatus

_FAILED_NODE = re.compile(r"(?m)^FAILED[ \t]+(?P<node>\S+?)(?:[ \t]+-[ \t]+.*)?\r?$")
_ABSOLUTE_WINDOWS_PATH = re.compile(r"(?i)\b[A-Z]:\\[^\r\n\t ]+")
_ABSOLUTE_POSIX_PATH = re.compile(r"(?<![\w.])/(?:[^\s/]+/)+[^\s]+")
_SECRET = re.compile(
    r"(?i)\b(?:[A-Z0-9_]*(?:API_KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)[A-Z0-9_]*)"
    r"\s*=\s*[^\s]+"
)


class PytestBaselineResultParser:
    def __init__(self, *, max_safe_summary_chars: int = 8_000) -> None:
        if not 1 <= max_safe_summary_chars <= 8_000:
            raise ValueError("Safe baseline summary limit must be between 1 and 8000")
        self._max_safe_summary_chars = max_safe_summary_chars

    def evaluate(
        self,
        outcome: SupervisorOutcome,
        expected: ExpectedBaselineFailure,
    ) -> BaselineEvaluation:
        combined = "\n".join(
            item for item in (outcome.stdout.summary, outcome.stderr.summary) if item
        )
        node_ids = tuple(
            sorted(
                {
                    normalize_node_id(match.group("node"))
                    for match in _FAILED_NODE.finditer(combined)
                }
            )
        )
        reason = self._failure_reason(outcome, combined, node_ids, expected)
        status = (
            BaselineExecutionStatus.VERIFIED_EXPECTED_FAILURE
            if reason is None
            else (
                BaselineExecutionStatus.INDETERMINATE
                if reason is BaselineFailureReason.PROCESS_OUTCOME_INDETERMINATE
                else BaselineExecutionStatus.BLOCKED
            )
        )
        actual_digest = self._actual_digest(outcome.exit_code, node_ids) if node_ids else None
        summary_text, summary_truncated = self._safe_diagnostic(combined)
        safe_summary = BaselineFailureSummary(
            exit_code=outcome.exit_code if outcome.exit_code is not None else -1,
            failed_node_ids=node_ids,
            failure_count=len(node_ids),
            diagnostic_summary=summary_text,
            stdout_digest=outcome.stdout.sha256_digest,
            stderr_digest=outcome.stderr.sha256_digest,
            truncated=(outcome.stdout.truncated or outcome.stderr.truncated or summary_truncated),
        )
        return BaselineEvaluation(
            status=status,
            failure_reason=reason,
            failed_node_ids=node_ids,
            actual_fingerprint_digest=actual_digest,
            safe_summary=safe_summary,
        )

    @staticmethod
    def _failure_reason(
        outcome: SupervisorOutcome,
        combined: str,
        node_ids: tuple[str, ...],
        expected: ExpectedBaselineFailure,
    ) -> BaselineFailureReason | None:
        if outcome.status is SupervisorStatus.INDETERMINATE or not outcome.termination_confirmed:
            return BaselineFailureReason.PROCESS_OUTCOME_INDETERMINATE
        if outcome.status is SupervisorStatus.TIMEOUT:
            return BaselineFailureReason.TIMEOUT
        if outcome.status is SupervisorStatus.CANCELLED:
            return BaselineFailureReason.CANCELLED
        if outcome.status is SupervisorStatus.LAUNCH_FAILED:
            return BaselineFailureReason.LAUNCH_FAILURE
        if outcome.exit_code == 0:
            return BaselineFailureReason.UNEXPECTED_PASS
        if re.search(
            r"(?mi)^(?:ERROR collecting|E\s+ImportError:|E\s+ModuleNotFoundError:)", combined
        ):
            return BaselineFailureReason.COLLECTION_OR_IMPORT_ERROR
        if not node_ids:
            return BaselineFailureReason.FAILURE_OUTPUT_UNPARSABLE
        if set(node_ids) != set(expected.failed_node_ids):
            return BaselineFailureReason.FAILURE_FINGERPRINT_MISMATCH
        return None

    def _safe_diagnostic(self, value: str) -> tuple[str, bool]:
        redacted = _SECRET.sub("[REDACTED_SECRET]", value)
        redacted = _ABSOLUTE_WINDOWS_PATH.sub("<absolute_path>", redacted)
        redacted = _ABSOLUTE_POSIX_PATH.sub("<absolute_path>", redacted)
        source_lines = redacted.splitlines()
        retained_lines = [
            line[:500]
            for line in source_lines
            if line.startswith("FAILED ")
            or line.startswith("ERROR collecting ")
            or re.match(r"^E(?:\s|$)", line)
        ]
        summary = "\n".join(retained_lines)
        omitted = len(retained_lines) != len(source_lines)
        if len(summary) <= self._max_safe_summary_chars:
            return summary, omitted
        return summary[: self._max_safe_summary_chars], True

    @staticmethod
    def _actual_digest(exit_code: int | None, node_ids: tuple[str, ...]) -> str:
        canonical = json.dumps(
            {"exit_code": exit_code, "failed_node_ids": sorted(node_ids)},
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
