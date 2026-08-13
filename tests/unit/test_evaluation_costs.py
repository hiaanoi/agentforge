from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from agentforge.evaluation.costs import (
    PricingSnapshot,
    estimate_cost,
)
from agentforge.evaluation.telemetry_models import EvaluationRunTelemetry

SHA = "d" * 64


def _telemetry(
    *,
    model_id: str = "gpt-test",
    usage_complete: bool = True,
    input_tokens: int = 1_000,
    output_tokens: int = 500,
    total_tokens: int = 1_500,
    cached_input_tokens: int = 200,
    reasoning_tokens: int = 100,
) -> EvaluationRunTelemetry:
    return EvaluationRunTelemetry(
        evaluation_run_id=uuid4(),
        run_id=uuid4(),
        campaign_id=uuid4(),
        attempt_id=uuid4(),
        protocol_digest=SHA,
        model_id=model_id,
        logical_model_calls=1,
        physical_model_requests=1,
        completed_model_requests=1,
        failed_model_requests=0,
        retry_count=0,
        usage_complete=usage_complete,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        cached_input_tokens=cached_input_tokens,
        reasoning_tokens=reasoning_tokens,
        successful_provider_duration_ms=10,
        maximum_provider_duration_ms=10,
        model_attempt_elapsed_ms=10,
        provider_deviation_count=0,
        normalized_multi_tool_response_count=0,
        returned_function_call_count=0,
        discarded_function_call_count=0,
        model_protocol_failure_count=0,
        tool_requested_count=0,
        tool_completed_count=0,
        tool_failed_count=0,
        read_call_count=0,
        mutation_requested_count=0,
        mutation_committed_count=0,
        mutation_failed_count=0,
        managed_test_requested_count=0,
        managed_test_completed_count=0,
        managed_test_failed_count=0,
        managed_test_timeout_count=0,
        approval_requested_count=0,
        approval_granted_count=0,
        approval_rejected_count=0,
        context_compaction_count=0,
        completion_correction_count=0,
        policy_violation_count=0,
    )


def _pricing(
    *,
    model_id: str = "gpt-test",
    cached_input_per_million: str | None = "2",
) -> PricingSnapshot:
    return PricingSnapshot(
        model_id=model_id,
        currency="USD",
        effective_date=date(2026, 7, 1),
        source_url="https://example.com/provider-pricing",
        input_per_million="10",
        cached_input_per_million=cached_input_per_million,
        output_per_million="30",
    )


def test_pricing_digest_is_deterministic_and_binds_every_rate() -> None:
    first = _pricing()
    repeated = _pricing()
    changed = _pricing(cached_input_per_million="3")

    assert first == repeated
    assert first.pricing_digest == repeated.pricing_digest
    assert first.pricing_digest != changed.pricing_digest
    assert len(first.pricing_digest) == 64


@pytest.mark.parametrize(
    "source_url",
    [
        "https://user:password@example.com/pricing",
        "https://example.com/pricing?token=private",
        "https://example.com/pricing#private",
    ],
)
def test_pricing_source_rejects_non_public_url_components(
    source_url: str,
) -> None:
    data = _pricing().model_dump(mode="json")
    data["source_url"] = source_url
    data["pricing_digest"] = ""

    with pytest.raises(ValidationError):
        PricingSnapshot.model_validate(data)


def test_cost_prices_cached_uncached_and_output_tokens_exactly_once() -> None:
    cost = estimate_cost(_telemetry(), _pricing())

    assert cost.complete is True
    assert cost.currency == "USD"
    assert cost.amount == Decimal("0.023400")
    assert cost.model_id == "gpt-test"


def test_reasoning_tokens_are_reported_but_not_double_billed() -> None:
    without_reasoning = estimate_cost(
        _telemetry(reasoning_tokens=0),
        _pricing(),
    )
    with_reasoning = estimate_cost(
        _telemetry(reasoning_tokens=400),
        _pricing(),
    )

    assert with_reasoning.amount == without_reasoning.amount


def test_missing_usage_marks_cost_incomplete_without_zero_amount() -> None:
    cost = estimate_cost(
        _telemetry(usage_complete=False),
        _pricing(),
    )

    assert cost.complete is False
    assert cost.amount is None


def test_missing_cached_rate_marks_nonzero_cached_usage_incomplete() -> None:
    cost = estimate_cost(
        _telemetry(cached_input_tokens=1),
        _pricing(cached_input_per_million=None),
    )

    assert cost.complete is False
    assert cost.amount is None


def test_model_identity_mismatch_is_rejected() -> None:
    with pytest.raises(ValueError, match="model"):
        estimate_cost(_telemetry(model_id="other-model"), _pricing())


@pytest.mark.parametrize(
    "updates",
    [
        {"input_per_million": "-1"},
        {"cached_input_per_million": "-1"},
        {"output_per_million": "-1"},
        {"currency": "EUR"},
    ],
)
def test_negative_rates_and_unsupported_currency_are_rejected(
    updates: dict[str, object],
) -> None:
    data = _pricing().model_dump(mode="json")
    data.update(updates)
    data["pricing_digest"] = ""

    with pytest.raises(ValidationError):
        PricingSnapshot.model_validate(data)


def test_decimal_amount_serializes_stably_as_a_string() -> None:
    first = estimate_cost(_telemetry(), _pricing())
    second = estimate_cost(_telemetry(), _pricing())

    assert first.model_dump(mode="json")["amount"] == "0.023400"
    assert second.model_dump(mode="json")["amount"] == "0.023400"


def test_cached_usage_cannot_exceed_input_usage() -> None:
    with pytest.raises(ValidationError, match=r"(?i)cached"):
        _telemetry(input_tokens=10, cached_input_tokens=11)
