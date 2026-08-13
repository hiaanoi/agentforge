import math
from typing import Literal, Self
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agentforge.domain.enums import (
    EvaluationCampaignStatus,
    EvaluationStudyStatus,
    EventType,
)
from agentforge.domain.models import UtcDatetime, utc_now
from agentforge.evaluation.protocol import canonical_digest
from agentforge.models.identity import is_exact_or_dated_openai_snapshot

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_GIT_COMMIT_PATTERN = r"^[0-9a-f]{40,64}$"
_TERMINAL_STUDY_STATUSES = frozenset(
    {
        EvaluationStudyStatus.COMPLETED,
        EvaluationStudyStatus.COMPLETED_WITH_INFRASTRUCTURE_GAPS,
        EvaluationStudyStatus.ABORTED_CONFIGURATION,
        EvaluationStudyStatus.INDETERMINATE,
    }
)

B2_4_TASK_ORDER: tuple[str, str, str, str] = (
    "quixbugs-shortest-path-length",
    "bugsinpy-black-21",
    "swebench-pytest-10051",
    "self-durable-double-consumption",
)


class EvaluationStudyDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    study_name: str = Field(min_length=1, max_length=200)
    task_ids: tuple[str, str, str, str]
    protocol_digests: tuple[str, str, str, str]
    provider_configuration_digest: str = Field(pattern=_SHA256_PATTERN)
    model_id: str = Field(min_length=1, max_length=200)
    response_model_id: str = Field(default="", max_length=200)
    repetitions_per_task: Literal[3] = 3
    planned_scoring_slots: Literal[12] = 12
    runtime_source_digest: str = Field(pattern=_SHA256_PATTERN)
    git_commit_sha: str = Field(pattern=_GIT_COMMIT_PATTERN)
    git_worktree_clean: Literal[True] = True
    pyproject_sha256: str = Field(pattern=_SHA256_PATTERN)
    uv_lock_sha256: str = Field(pattern=_SHA256_PATTERN)
    fixture_registry_digest: str = Field(pattern=_SHA256_PATTERN)
    platform_binding_digest: str = Field(pattern=_SHA256_PATTERN)
    pricing_snapshot_digest: str = Field(pattern=_SHA256_PATTERN)
    definition_digest: str = Field(
        default="",
        pattern=r"^$|^[0-9a-f]{64}$",
    )

    @model_validator(mode="after")
    def validate_definition(self) -> Self:
        object.__setattr__(
            self,
            "response_model_id",
            self.response_model_id or self.model_id,
        )
        if not is_exact_or_dated_openai_snapshot(
            self.model_id,
            self.response_model_id,
        ):
            raise ValueError("Study response model is outside the requested model family")
        if self.task_ids != B2_4_TASK_ORDER:
            raise ValueError("Study task order does not match B2.4")
        if len(set(self.protocol_digests)) != 4:
            raise ValueError("Study requires four unique Protocol digests")
        if any(
            len(item) != 64
            or any(character not in "0123456789abcdef" for character in item)
            for item in self.protocol_digests
        ):
            raise ValueError("Study Protocol digests must be SHA-256 values")
        expected = canonical_digest(
            self.model_dump(mode="json", exclude={"definition_digest"})
        )
        if self.definition_digest and self.definition_digest != expected:
            raise ValueError("Study definition digest does not match facts")
        object.__setattr__(self, "definition_digest", expected)
        return self


class RealModelAuthorization(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    study_definition_digest: str = Field(pattern=_SHA256_PATTERN)
    protocol_digests: tuple[str, str, str, str]
    model_id: str = Field(min_length=1, max_length=200)
    response_model_id: str = Field(default="", max_length=200)
    maximum_campaigns: Literal[4] = 4
    maximum_planned_slots: Literal[12] = 12
    maximum_replacements_per_slot: Literal[1] = 1
    network_access_acknowledged: Literal[True] = True
    authorization_digest: str = Field(
        default="",
        pattern=r"^$|^[0-9a-f]{64}$",
    )

    @model_validator(mode="after")
    def validate_authorization_digest(self) -> Self:
        object.__setattr__(
            self,
            "response_model_id",
            self.response_model_id or self.model_id,
        )
        if not is_exact_or_dated_openai_snapshot(
            self.model_id,
            self.response_model_id,
        ):
            raise ValueError(
                "Authorization response model is outside the requested model family"
            )
        if len(set(self.protocol_digests)) != 4:
            raise ValueError("Authorization requires four unique Protocols")
        if any(
            len(item) != 64
            or any(character not in "0123456789abcdef" for character in item)
            for item in self.protocol_digests
        ):
            raise ValueError("Authorization Protocol digests must be SHA-256 values")
        expected = canonical_digest(
            self.model_dump(mode="json", exclude={"authorization_digest"})
        )
        if (
            self.authorization_digest
            and self.authorization_digest != expected
        ):
            raise ValueError("Authorization digest does not match facts")
        object.__setattr__(self, "authorization_digest", expected)
        return self


class EvaluationStudy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    study_id: UUID = Field(default_factory=uuid4)
    definition_digest: str = Field(pattern=_SHA256_PATTERN)
    authorization_digest: str | None = Field(
        default=None,
        pattern=_SHA256_PATTERN,
    )
    status: EvaluationStudyStatus = EvaluationStudyStatus.DRAFT
    record_version: int = Field(default=1, gt=0)
    created_at: UtcDatetime = Field(default_factory=utc_now)
    updated_at: UtcDatetime = Field(default_factory=utc_now)
    completed_at: UtcDatetime | None = None

    @model_validator(mode="after")
    def validate_state_facts(self) -> Self:
        terminal = self.status in _TERMINAL_STUDY_STATUSES
        if terminal != (self.completed_at is not None):
            raise ValueError("Study terminal status must match completed_at")
        if (
            self.status is EvaluationStudyStatus.DRAFT
            and self.authorization_digest is not None
        ):
            raise ValueError("Draft Study cannot carry authorization")
        if (
            self.status is not EvaluationStudyStatus.DRAFT
            and self.authorization_digest is None
        ):
            raise ValueError("Non-draft Study requires authorization")
        return self


class EvaluationStudyCampaignBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    study_id: UUID
    task_id: str = Field(min_length=1, max_length=200)
    task_order: int = Field(ge=0, lt=4)
    protocol_digest: str = Field(pattern=_SHA256_PATTERN)
    campaign_id: UUID
    campaign_status: EvaluationCampaignStatus | None = None
    completed_at: UtcDatetime | None = None

    @model_validator(mode="after")
    def validate_completion(self) -> Self:
        if (self.campaign_status is None) != (self.completed_at is None):
            raise ValueError(
                "Campaign completion status must match completed_at"
            )
        return self


class EvaluationStudyEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: UUID = Field(default_factory=uuid4)
    study_id: UUID
    event_type: EventType
    sequence_number: int = Field(gt=0)
    payload: dict[str, str | int | bool | None] = Field(default_factory=dict)
    created_at: UtcDatetime = Field(default_factory=utc_now)


class EvaluationStudySummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    planned_slots: Literal[12] = 12
    scored_slots: int = Field(ge=0, le=12)
    successful_slots: int = Field(ge=0, le=12)
    infrastructure_invalid_slots: int = Field(ge=0, le=12)
    indeterminate_slots: int = Field(ge=0, le=12)
    scoring_coverage: float = Field(ge=0, le=1)
    scored_success_rate: float | None = Field(default=None, ge=0, le=1)
    planned_slot_success_rate: float = Field(ge=0, le=1)
    macro_pass_at_1: float | None = Field(default=None, ge=0, le=1)
    pass_at_1_eligible_task_count: int = Field(ge=0, le=4)
    macro_pass_at_3: float | None = Field(default=None, ge=0, le=1)
    pass_at_3_eligible_task_count: int = Field(ge=0, le=4)
    first_attempt_success_count: int = Field(ge=0, le=4)
    any_success_in_3_count: int = Field(ge=0, le=4)
    stable_success_count: int = Field(ge=0, le=4)

    @model_validator(mode="after")
    def validate_denominators(self) -> Self:
        if (
            self.scored_slots
            + self.infrastructure_invalid_slots
            + self.indeterminate_slots
            > self.planned_slots
        ):
            raise ValueError("Study slot counts do not reconcile")
        if self.successful_slots > self.scored_slots:
            raise ValueError("Successful slots cannot exceed scored slots")
        expected_coverage = self.scored_slots / self.planned_slots
        expected_planned_rate = self.successful_slots / self.planned_slots
        expected_scored_rate = (
            self.successful_slots / self.scored_slots
            if self.scored_slots
            else None
        )
        if not math.isclose(
            self.scoring_coverage,
            expected_coverage,
            abs_tol=1e-12,
        ):
            raise ValueError("Study scoring coverage rate does not reconcile")
        if not math.isclose(
            self.planned_slot_success_rate,
            expected_planned_rate,
            abs_tol=1e-12,
        ):
            raise ValueError("Study planned success rate does not reconcile")
        if expected_scored_rate is None:
            if self.scored_success_rate is not None:
                raise ValueError("Empty scored denominator requires a null rate")
        elif self.scored_success_rate is None or not math.isclose(
            self.scored_success_rate,
            expected_scored_rate,
            abs_tol=1e-12,
        ):
            raise ValueError("Study scored success rate does not reconcile")
        if self.pass_at_1_eligible_task_count > self.scored_slots:
            raise ValueError("pass@1 eligibility exceeds scored slots")
        if 3 * self.pass_at_3_eligible_task_count > self.scored_slots:
            raise ValueError("pass@3 eligibility exceeds scored slots")
        if (
            self.pass_at_1_eligible_task_count
            + 2 * self.pass_at_3_eligible_task_count
            > self.scored_slots
        ):
            raise ValueError(
                "Scored slots fall below aggregate task eligibility topology"
            )
        if self.scored_slots > (
            2 * self.pass_at_1_eligible_task_count
            + self.pass_at_3_eligible_task_count
        ):
            raise ValueError(
                "Scored slots exceed aggregate task eligibility topology"
            )
        if self.pass_at_1_eligible_task_count == 0:
            if self.macro_pass_at_1 is not None:
                raise ValueError("No pass@1-eligible tasks require a null macro")
        elif self.macro_pass_at_1 is None:
            raise ValueError("pass@1-eligible tasks require a macro")
        elif self.successful_slots == 0 and not math.isclose(
            self.macro_pass_at_1,
            0,
            abs_tol=1e-12,
        ):
            raise ValueError("Zero successful slots require a zero macro pass@1")
        elif self.successful_slots == self.scored_slots and not math.isclose(
            self.macro_pass_at_1,
            1,
            abs_tol=1e-12,
        ):
            raise ValueError(
                "All scored slots successful require a unit macro pass@1"
            )
        if self.pass_at_3_eligible_task_count != 4:
            if self.macro_pass_at_3 is not None:
                raise ValueError("Incomplete pass@3 eligibility requires a null macro")
        elif self.macro_pass_at_3 is None:
            raise ValueError("Four pass@3-eligible tasks require a macro")
        elif not math.isclose(
            self.macro_pass_at_3,
            self.any_success_in_3_count / 4,
            abs_tol=1e-12,
        ):
            raise ValueError("pass@3 macro does not reconcile with any-success")
        if self.first_attempt_success_count > self.any_success_in_3_count:
            raise ValueError("First-attempt successes must also have any success")
        if self.first_attempt_success_count > self.successful_slots:
            raise ValueError("First-attempt successes exceed successful slots")
        if self.any_success_in_3_count > self.pass_at_1_eligible_task_count:
            raise ValueError("Any-success tasks exceed pass@1-eligible tasks")
        if self.any_success_in_3_count > self.successful_slots:
            raise ValueError("Any-success tasks exceed successful slots")
        if self.stable_success_count > self.any_success_in_3_count:
            raise ValueError("Stable-success tasks must also have any success")
        if self.stable_success_count > self.pass_at_3_eligible_task_count:
            raise ValueError("Stable-success tasks exceed pass@3-eligible tasks")
        if 3 * self.stable_success_count > self.successful_slots:
            raise ValueError("Stable-success tasks require three successful slots each")
        if (
            self.any_success_in_3_count + 2 * self.stable_success_count
            > self.successful_slots
        ):
            raise ValueError(
                "Successful slots fall below aggregate outcome topology"
            )
        if self.successful_slots > (
            2 * self.any_success_in_3_count + self.stable_success_count
        ):
            raise ValueError(
                "Successful slots exceed aggregate outcome topology"
            )
        return self


def validate_real_model_authorization(
    definition: EvaluationStudyDefinition,
    authorization: RealModelAuthorization,
    *,
    study: EvaluationStudy | None = None,
) -> None:
    if (
        authorization.study_definition_digest
        != definition.definition_digest
        or authorization.protocol_digests != definition.protocol_digests
        or authorization.model_id != definition.model_id
        or authorization.response_model_id != definition.response_model_id
    ):
        raise ValueError("Real-model authorization binding does not match Study")
    if study is not None and (
        study.definition_digest != definition.definition_digest
        or study.authorization_digest != authorization.authorization_digest
    ):
        raise ValueError("Study authorization binding does not match")


def validate_study_campaign_bindings(
    definition: EvaluationStudyDefinition,
    study: EvaluationStudy,
    bindings: tuple[
        EvaluationStudyCampaignBinding,
        EvaluationStudyCampaignBinding,
        EvaluationStudyCampaignBinding,
        EvaluationStudyCampaignBinding,
    ],
) -> tuple[
    EvaluationStudyCampaignBinding,
    EvaluationStudyCampaignBinding,
    EvaluationStudyCampaignBinding,
    EvaluationStudyCampaignBinding,
]:
    if len(bindings) != 4:
        raise ValueError("Study requires exactly four Campaign bindings")
    if [binding.task_order for binding in bindings] != [0, 1, 2, 3]:
        raise ValueError("Study Campaign binding order does not match")
    if any(binding.study_id != study.study_id for binding in bindings):
        raise ValueError("Campaign binding crosses Study identity")
    if tuple(binding.task_id for binding in bindings) != definition.task_ids:
        raise ValueError("Campaign binding task order does not match")
    if (
        tuple(binding.protocol_digest for binding in bindings)
        != definition.protocol_digests
    ):
        raise ValueError("Campaign binding Protocol order does not match")
    if len({binding.campaign_id for binding in bindings}) != 4:
        raise ValueError("Study Campaign IDs must be unique")
    return bindings
