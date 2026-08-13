from pydantic import Field, JsonValue

from agentforge.domain.enums import PolicyOutcome
from agentforge.domain.models import DomainModel


class PolicyDecision(DomainModel):
    decision: PolicyOutcome
    reason: str = Field(min_length=1)
    matched_rule: str = Field(min_length=1)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)

