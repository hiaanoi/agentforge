from uuid import UUID, uuid4

from pydantic import ConfigDict, Field, model_validator

from agentforge.domain.enums import MutationExecutionStatus
from agentforge.domain.models import ApprovalRequired, DomainModel, UtcDatetime, utc_now


class MutationPlan(DomainModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True, frozen=True)

    tool_name: str = Field(min_length=1, max_length=100)
    target_path: str = Field(min_length=1, max_length=4096)
    target_existed: bool
    before_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    expected_after_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    bytes_written: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_before_state(self) -> "MutationPlan":
        if self.target_existed != (self.before_sha256 is not None):
            raise ValueError("Existing targets require a before hash; absent targets forbid one")
        return self


class MutationApprovalBinding(MutationPlan):
    approval_id: UUID
    run_id: UUID
    checkpoint_id: UUID
    tool_call_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: UtcDatetime = Field(default_factory=utc_now)


class MutationApprovalRequired(ApprovalRequired):
    mutation_plan: MutationPlan


class MutationExecutionRecord(DomainModel):
    execution_id: UUID = Field(default_factory=uuid4)
    run_id: UUID
    approval_id: UUID
    tool_call_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    tool_name: str = Field(min_length=1, max_length=100)
    target_path: str = Field(min_length=1, max_length=4096)
    before_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    expected_after_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    before_workspace_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_after_workspace_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    actual_after_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    bytes_written: int = Field(default=0, ge=0)
    status: MutationExecutionStatus = MutationExecutionStatus.PREPARED
    result_summary: str | None = Field(default=None, max_length=500)
    created_at: UtcDatetime = Field(default_factory=utc_now)
    updated_at: UtcDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_committed_hash(self) -> "MutationExecutionRecord":
        if (
            self.status is MutationExecutionStatus.COMMITTED
            and self.actual_after_sha256 != self.expected_after_sha256
        ):
            raise ValueError("Committed mutations require the expected after hash")
        return self
