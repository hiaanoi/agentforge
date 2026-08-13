from datetime import date
from decimal import Decimal
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

from agentforge.evaluation.protocol import canonical_digest
from agentforge.evaluation.telemetry_models import EvaluationRunTelemetry

_MILLION = Decimal(1_000_000)
_COST_QUANTUM = Decimal("0.000001")


class PricingSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    model_id: str = Field(min_length=1, max_length=200)
    currency: Literal["USD"] = "USD"
    effective_date: date
    source_url: HttpUrl
    input_per_million: Decimal = Field(ge=Decimal("0"))
    cached_input_per_million: Decimal | None = Field(
        default=None,
        ge=Decimal("0"),
    )
    output_per_million: Decimal = Field(ge=Decimal("0"))
    pricing_digest: str = Field(default="", pattern=r"^$|^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_digest(self) -> Self:
        if any(
            (
                self.source_url.username,
                self.source_url.password,
                self.source_url.query,
                self.source_url.fragment,
            )
        ):
            raise ValueError(
                "Pricing source URL cannot contain private components"
            )
        expected = canonical_digest(
            self.model_dump(mode="json", exclude={"pricing_digest"})
        )
        if self.pricing_digest and self.pricing_digest != expected:
            raise ValueError("Pricing digest does not match immutable facts")
        object.__setattr__(self, "pricing_digest", expected)
        return self


class EvaluationCost(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    model_id: str = Field(min_length=1, max_length=200)
    pricing_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    complete: bool
    currency: Literal["USD"]
    amount: Decimal | None = Field(default=None, ge=Decimal("0"))

    @model_validator(mode="after")
    def validate_completeness(self) -> Self:
        if self.complete != (self.amount is not None):
            raise ValueError("Complete cost must contain an amount")
        return self


def estimate_cost(
    telemetry: EvaluationRunTelemetry,
    pricing: PricingSnapshot,
) -> EvaluationCost:
    if telemetry.model_id != pricing.model_id:
        raise ValueError("Telemetry model does not match pricing model")
    if not telemetry.usage_complete:
        return _incomplete_cost(pricing)
    if (
        telemetry.cached_input_tokens > 0
        and pricing.cached_input_per_million is None
    ):
        return _incomplete_cost(pricing)

    uncached_input = max(
        0,
        telemetry.input_tokens - telemetry.cached_input_tokens,
    )
    cached_rate = pricing.cached_input_per_million or Decimal("0")
    amount = (
        Decimal(uncached_input) * pricing.input_per_million
        + Decimal(telemetry.cached_input_tokens) * cached_rate
        + Decimal(telemetry.output_tokens) * pricing.output_per_million
    ) / _MILLION
    return EvaluationCost(
        model_id=pricing.model_id,
        pricing_digest=pricing.pricing_digest,
        complete=True,
        currency=pricing.currency,
        amount=amount.quantize(_COST_QUANTUM),
    )


def _incomplete_cost(pricing: PricingSnapshot) -> EvaluationCost:
    return EvaluationCost(
        model_id=pricing.model_id,
        pricing_digest=pricing.pricing_digest,
        complete=False,
        currency=pricing.currency,
        amount=None,
    )
