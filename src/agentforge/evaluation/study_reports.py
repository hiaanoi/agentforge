import json
import math
import re
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from decimal import Decimal
from statistics import fmean, median
from typing import Literal, Self, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agentforge.domain.enums import (
    EvaluationAttemptStatus,
    EvaluationCampaignStatus,
    EvaluationSlotStatus,
    EvaluationStudyStatus,
)
from agentforge.domain.repair import RepairCompletionStatus
from agentforge.evaluation.campaign_persistence import (
    EvaluationCampaignRepository,
)
from agentforge.evaluation.costs import PricingSnapshot, estimate_cost
from agentforge.evaluation.models import RepairEvaluationRun
from agentforge.evaluation.pass_at_k import estimate_pass_at_k
from agentforge.evaluation.persistence import EvaluationRunRepository
from agentforge.evaluation.protocol import (
    EvaluationProtocol,
    canonical_digest,
)
from agentforge.evaluation.protocol_persistence import (
    EvaluationProtocolRepository,
)
from agentforge.evaluation.public_artifacts import PublicArtifactScanner
from agentforge.evaluation.study_models import (
    B2_4_TASK_ORDER,
    EvaluationStudySummary,
)
from agentforge.evaluation.study_persistence import (
    EvaluationStudyRepository,
)
from agentforge.evaluation.telemetry_models import EvaluationRunTelemetry
from agentforge.evaluation.telemetry_persistence import (
    EvaluationTelemetryRepository,
)
from agentforge.persistence.database import Database

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_SAFE_CATEGORY = re.compile(r"^[A-Z][A-Z0-9_]{0,99}$")
_LIMITATIONS = (
    "Small sample: four cropped repair Fixtures with three repetitions each.",
    "This is a non-official AgentForge evaluation, not an official benchmark score.",
    "Results apply only to the bound model, Provider settings, source, dependencies, and platform.",
    "Provider-side requests are not exactly-once across a process crash.",
    "Test process controls are not an operating-system sandbox.",
)


class StudyReportBuildError(RuntimeError):
    pass


class PublicScoredRun(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    repetition_index: int = Field(ge=0, lt=3)
    attempt_number: int = Field(gt=0)
    final_status: RepairCompletionStatus
    verified_success: bool
    failure_category: str | None = Field(default=None, max_length=100)
    final_workspace_digest: str | None = Field(
        default=None,
        pattern=_SHA256_PATTERN,
    )
    final_diff_digest: str | None = Field(
        default=None,
        pattern=_SHA256_PATTERN,
    )
    model_calls: int = Field(ge=0)
    read_calls: int = Field(ge=0)
    edit_attempts: int = Field(ge=0)
    test_runs: int = Field(ge=0)
    wall_time_ms: int = Field(ge=0)
    total_tokens: int = Field(ge=0)


class PublicSlotStudyRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    repetition_index: int = Field(ge=0, lt=3)
    slot_status: EvaluationSlotStatus
    attempt_count: int = Field(ge=0, le=2)
    replacement_count: int = Field(ge=0, le=1)
    selected_run: PublicScoredRun | None = None

    @model_validator(mode="after")
    def validate_slot_facts(self) -> Self:
        accepted = self.slot_status is EvaluationSlotStatus.ACCEPTED
        if accepted != (self.selected_run is not None):
            raise ValueError(
                "Accepted public slot must contain one selected result"
            )
        if self.replacement_count > max(0, self.attempt_count - 1):
            raise ValueError("Public slot replacement count is invalid")
        if self.selected_run is not None and (
            self.selected_run.repetition_index != self.repetition_index
            or self.selected_run.attempt_number > self.attempt_count
        ):
            raise ValueError("Public slot selected result does not match")
        return self


class PublicTaskStudyReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str = Field(pattern=r"^[a-z0-9-]+$", max_length=200)
    task_order: int = Field(ge=0, lt=4)
    source_category: str = Field(
        default="UNSPECIFIED",
        pattern=r"^[A-Z][A-Z0-9_]{0,99}$",
    )
    protocol_digest: str = Field(pattern=_SHA256_PATTERN)
    campaign_status: EvaluationCampaignStatus | None
    fixture_asset_digest: str = Field(pattern=_SHA256_PATTERN)
    task_policy_digest: str = Field(pattern=_SHA256_PATTERN)
    system_prompt_digest: str = Field(pattern=_SHA256_PATTERN)
    task_prompt_digest: str = Field(pattern=_SHA256_PATTERN)
    tool_schema_digest: str = Field(pattern=_SHA256_PATTERN)
    planned_slots: Literal[3] = 3
    scored_slots: int = Field(ge=0, le=3)
    successful_slots: int = Field(ge=0, le=3)
    infrastructure_invalid_slots: int = Field(ge=0, le=3)
    indeterminate_slots: int = Field(ge=0, le=3)
    unexecuted_slots: int = Field(ge=0, le=3)
    scoring_coverage: float = Field(ge=0, le=1, allow_inf_nan=False)
    scored_success_rate: float | None = Field(
        default=None, ge=0, le=1, allow_inf_nan=False
    )
    planned_slot_success_rate: float = Field(ge=0, le=1, allow_inf_nan=False)
    pass_at_1: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    pass_at_3: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    first_attempt_success: bool
    majority_success: bool
    stable_success: bool
    any_success_in_3: bool
    logical_model_calls: int = Field(ge=0)
    physical_model_requests: int = Field(ge=0)
    retry_count: int = Field(ge=0)
    read_call_count: int = Field(ge=0)
    mutation_committed_count: int = Field(ge=0)
    managed_test_completed_count: int = Field(ge=0)
    wall_time_ms: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    usage_complete: bool
    estimated_cost_complete: bool
    estimated_cost: Decimal | None = Field(default=None, ge=Decimal("0"))
    provider_deviation_count: int = Field(ge=0)
    normalized_multi_tool_response_count: int = Field(ge=0)
    mean_logical_model_calls_per_scored_run: float | None = Field(
        default=None,
        ge=0,
        allow_inf_nan=False,
    )
    mean_physical_model_requests_per_scored_run: float | None = Field(
        default=None,
        ge=0,
        allow_inf_nan=False,
    )
    mean_retries_per_scored_run: float | None = Field(
        default=None, ge=0, allow_inf_nan=False
    )
    mean_reads_per_scored_run: float | None = Field(
        default=None, ge=0, allow_inf_nan=False
    )
    mean_edits_per_scored_run: float | None = Field(
        default=None, ge=0, allow_inf_nan=False
    )
    mean_development_tests_per_scored_run: float | None = Field(
        default=None,
        ge=0,
        allow_inf_nan=False,
    )
    median_wall_time_ms: float | None = Field(
        default=None, ge=0, allow_inf_nan=False
    )
    mean_total_tokens_per_scored_run: float | None = Field(
        default=None,
        ge=0,
        allow_inf_nan=False,
    )
    provider_deviation_rate: float = Field(ge=0, allow_inf_nan=False)
    multi_tool_normalization_rate: float = Field(ge=0, allow_inf_nan=False)
    raw_infrastructure_attempt_count: int = Field(ge=0)
    replacement_count: int = Field(ge=0)
    policy_block_rate: float = Field(ge=0, le=1, allow_inf_nan=False)
    budget_exhaustion_rate: float = Field(ge=0, le=1, allow_inf_nan=False)
    protocol_error_rate: float = Field(ge=0, le=1, allow_inf_nan=False)
    visible_test_failure_rate: float = Field(ge=0, le=1, allow_inf_nan=False)
    hidden_test_failure_rate: float = Field(ge=0, le=1, allow_inf_nan=False)
    model_quality_failure_distribution: dict[str, int]
    infrastructure_failure_distribution: dict[str, int]
    slots: tuple[
        PublicSlotStudyRecord,
        PublicSlotStudyRecord,
        PublicSlotStudyRecord,
    ]
    scored_runs: tuple[PublicScoredRun, ...] = ()

    @model_validator(mode="after")
    def validate_denominators(self) -> Self:
        if tuple(slot.repetition_index for slot in self.slots) != (0, 1, 2):
            raise ValueError("Task report slots must preserve repetition order")
        accepted_slots = tuple(
            slot
            for slot in self.slots
            if slot.slot_status is EvaluationSlotStatus.ACCEPTED
        )
        if (
            len(accepted_slots) != self.scored_slots
            or sum(
                slot.slot_status is EvaluationSlotStatus.INVALID
                for slot in self.slots
            )
            != self.infrastructure_invalid_slots
            or sum(
                slot.slot_status is EvaluationSlotStatus.INDETERMINATE
                for slot in self.slots
            )
            != self.indeterminate_slots
            or tuple(
                cast(PublicScoredRun, slot.selected_run)
                for slot in accepted_slots
            )
            != self.scored_runs
            or sum(slot.replacement_count for slot in self.slots)
            != self.replacement_count
        ):
            raise ValueError("Task report slot facts do not reconcile")
        if (
            self.scored_slots
            + self.infrastructure_invalid_slots
            + self.indeterminate_slots
            + self.unexecuted_slots
            != self.planned_slots
        ):
            raise ValueError("Task report slot counts do not reconcile")
        if self.successful_slots > self.scored_slots:
            raise ValueError("Task successes cannot exceed scored slots")
        if self.successful_slots != sum(
            run.verified_success for run in self.scored_runs
        ):
            raise ValueError(
                "Task successful slots do not reconcile with scored runs"
            )
        expected_scored_rate = (
            self.successful_slots / self.scored_slots
            if self.scored_slots
            else None
        )
        if not math.isclose(
            self.scoring_coverage,
            self.scored_slots / self.planned_slots,
            abs_tol=1e-12,
        ) or not math.isclose(
            self.planned_slot_success_rate,
            self.successful_slots / self.planned_slots,
            abs_tol=1e-12,
        ):
            raise ValueError("Task report rates do not reconcile")
        if expected_scored_rate is None:
            if self.scored_success_rate is not None:
                raise ValueError("Empty scored denominator requires null rate")
        elif self.scored_success_rate is None or not math.isclose(
            self.scored_success_rate,
            expected_scored_rate,
            abs_tol=1e-12,
        ):
            raise ValueError("Task scored success rate does not reconcile")
        if self.any_success_in_3 != (self.successful_slots > 0):
            raise ValueError("Task any-success flag does not reconcile")
        first_slot = self.slots[0]
        first_success = bool(
            first_slot.selected_run is not None
            and first_slot.selected_run.verified_success
        )
        if self.first_attempt_success != first_success:
            raise ValueError(
                "Task first-attempt-success flag does not reconcile"
            )
        expected_pass_at_1 = (
            estimate_pass_at_k(
                total=self.scored_slots,
                successful=self.successful_slots,
                k=1,
            )
            if self.scored_slots
            else None
        )
        if expected_pass_at_1 is None:
            if self.pass_at_1 is not None:
                raise ValueError("Empty scored denominator requires null pass@1")
        elif self.pass_at_1 is None or not math.isclose(
            self.pass_at_1,
            expected_pass_at_1,
            abs_tol=1e-12,
        ):
            raise ValueError("Task pass@1 does not reconcile")
        expected_pass_at_3 = (
            estimate_pass_at_k(
                total=self.planned_slots,
                successful=self.successful_slots,
                k=3,
            )
            if self.scored_slots == self.planned_slots
            else None
        )
        if expected_pass_at_3 is None:
            if self.pass_at_3 is not None:
                raise ValueError("Incomplete task requires null pass@3")
        elif self.pass_at_3 is None or not math.isclose(
            self.pass_at_3,
            expected_pass_at_3,
            abs_tol=1e-12,
        ):
            raise ValueError("Task pass@3 does not reconcile")
        if self.pass_at_3 is not None and not math.isclose(
            self.pass_at_3,
            float(self.any_success_in_3),
            abs_tol=1e-12,
        ):
            raise ValueError("Task pass@3 must match any-success")
        if self.majority_success != (self.successful_slots >= 2):
            raise ValueError("Task majority-success flag does not reconcile")
        if self.stable_success != (
            self.successful_slots == self.planned_slots
        ):
            raise ValueError("Task stable-success flag does not reconcile")
        if self.estimated_cost_complete != (
            self.estimated_cost is not None
        ):
            raise ValueError("Task cost completeness does not reconcile")
        mean_values = (
            self.mean_logical_model_calls_per_scored_run,
            self.mean_physical_model_requests_per_scored_run,
            self.mean_retries_per_scored_run,
            self.mean_reads_per_scored_run,
            self.mean_edits_per_scored_run,
            self.mean_development_tests_per_scored_run,
            self.median_wall_time_ms,
            self.mean_total_tokens_per_scored_run,
        )
        if self.scored_slots == 0 and any(
            value is not None for value in mean_values
        ):
            raise ValueError(
                "Empty scored denominator requires null task means"
            )
        if self.scored_slots > 0 and any(
            value is None for value in mean_values
        ):
            raise ValueError(
                "Scored task report requires every scored-run mean"
            )
        _validate_distribution(self.model_quality_failure_distribution)
        _validate_distribution(self.infrastructure_failure_distribution)
        return self


class PublicStudyAggregate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    planned_slots: Literal[12] = 12
    scored_slots: int = Field(ge=0, le=12)
    successful_slots: int = Field(ge=0, le=12)
    infrastructure_invalid_slots: int = Field(ge=0, le=12)
    indeterminate_slots: int = Field(ge=0, le=12)
    unexecuted_slots: int = Field(ge=0, le=12)
    scoring_coverage: float = Field(ge=0, le=1, allow_inf_nan=False)
    scored_success_rate: float | None = Field(
        default=None, ge=0, le=1, allow_inf_nan=False
    )
    planned_slot_success_rate: float = Field(ge=0, le=1, allow_inf_nan=False)
    macro_pass_at_1: float | None = Field(
        default=None, ge=0, le=1, allow_inf_nan=False
    )
    pass_at_1_eligible_task_count: int = Field(ge=0, le=4)
    macro_pass_at_3: float | None = Field(
        default=None, ge=0, le=1, allow_inf_nan=False
    )
    pass_at_3_eligible_task_count: int = Field(ge=0, le=4)
    first_attempt_success_count: int = Field(ge=0, le=4)
    any_success_in_3_count: int = Field(ge=0, le=4)
    stable_success_count: int = Field(ge=0, le=4)
    logical_model_calls: int = Field(ge=0)
    physical_model_requests: int = Field(ge=0)
    retry_count: int = Field(ge=0)
    read_call_count: int = Field(ge=0)
    mutation_committed_count: int = Field(ge=0)
    managed_test_completed_count: int = Field(ge=0)
    wall_time_ms: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    usage_complete: bool
    estimated_cost_complete: bool
    estimated_cost: Decimal | None = Field(default=None, ge=Decimal("0"))
    provider_deviation_count: int = Field(ge=0)
    normalized_multi_tool_response_count: int = Field(ge=0)
    raw_infrastructure_attempt_count: int = Field(ge=0)
    replacement_count: int = Field(ge=0)
    model_quality_failure_distribution: dict[str, int]
    infrastructure_failure_distribution: dict[str, int]

    @model_validator(mode="after")
    def validate_aggregate(self) -> Self:
        EvaluationStudySummary(
            scored_slots=self.scored_slots,
            successful_slots=self.successful_slots,
            infrastructure_invalid_slots=self.infrastructure_invalid_slots,
            indeterminate_slots=self.indeterminate_slots,
            scoring_coverage=self.scoring_coverage,
            scored_success_rate=self.scored_success_rate,
            planned_slot_success_rate=self.planned_slot_success_rate,
            macro_pass_at_1=self.macro_pass_at_1,
            pass_at_1_eligible_task_count=self.pass_at_1_eligible_task_count,
            macro_pass_at_3=self.macro_pass_at_3,
            pass_at_3_eligible_task_count=self.pass_at_3_eligible_task_count,
            first_attempt_success_count=self.first_attempt_success_count,
            any_success_in_3_count=self.any_success_in_3_count,
            stable_success_count=self.stable_success_count,
        )
        if (
            self.scored_slots
            + self.infrastructure_invalid_slots
            + self.indeterminate_slots
            + self.unexecuted_slots
            != self.planned_slots
        ):
            raise ValueError("Study report slot counts do not reconcile")
        if self.estimated_cost_complete != (
            self.estimated_cost is not None
        ):
            raise ValueError("Study cost completeness does not reconcile")
        _validate_distribution(self.model_quality_failure_distribution)
        _validate_distribution(self.infrastructure_failure_distribution)
        return self


class PublicProviderSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: Literal["openai"] = "openai"
    timeout_seconds: float = Field(allow_inf_nan=False)
    max_retries: int
    store: Literal[False] = False
    max_output_tokens: int
    multi_tool_response_policy: str
    max_function_calls_per_response: int
    configuration_digest: str = Field(pattern=_SHA256_PATTERN)


class PublicPricingFacts(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    pricing_digest: str = Field(pattern=_SHA256_PATTERN)
    currency: Literal["USD"] = "USD"
    effective_date: str
    source_url: str
    input_per_million: Decimal
    cached_input_per_million: Decimal | None
    output_per_million: Decimal


class PublicEvaluationStudyReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    report_schema_version: Literal[2] = 2
    study_id: UUID
    study_status: EvaluationStudyStatus
    study_definition_digest: str = Field(pattern=_SHA256_PATTERN)
    protocol_digests: tuple[str, str, str, str]
    model_id: str
    response_model_id: str
    git_commit_sha: str
    runtime_source_digest: str = Field(pattern=_SHA256_PATTERN)
    pyproject_sha256: str = Field(pattern=_SHA256_PATTERN)
    uv_lock_sha256: str = Field(pattern=_SHA256_PATTERN)
    fixture_registry_digest: str = Field(pattern=_SHA256_PATTERN)
    platform_binding_digest: str = Field(pattern=_SHA256_PATTERN)
    os_family: str
    python_implementation: str
    python_version: str
    provider: PublicProviderSettings
    pricing: PublicPricingFacts
    summary: PublicStudyAggregate
    tasks: tuple[
        PublicTaskStudyReport,
        PublicTaskStudyReport,
        PublicTaskStudyReport,
        PublicTaskStudyReport,
    ]
    limitations: tuple[str, ...] = _LIMITATIONS
    report_digest: str = Field(default="", pattern=r"^$|^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_report(self) -> Self:
        if tuple(task.task_id for task in self.tasks) != B2_4_TASK_ORDER:
            raise ValueError("Public report task order does not match B2.4")
        if (
            tuple(task.protocol_digest for task in self.tasks)
            != self.protocol_digests
        ):
            raise ValueError("Public report Protocol bindings do not match")
        if aggregate_study_summary(self.tasks) != self.summary:
            raise ValueError("Public report aggregate does not match tasks")
        expected = canonical_digest(
            self.model_dump(mode="json", exclude={"report_digest"})
        )
        if self.report_digest and self.report_digest != expected:
            raise ValueError("Public report digest does not match facts")
        object.__setattr__(self, "report_digest", expected)
        return self


def aggregate_study_summary(
    tasks: Sequence[PublicTaskStudyReport],
) -> PublicStudyAggregate:
    if len(tasks) != 4 or [task.task_order for task in tasks] != [0, 1, 2, 3]:
        raise ValueError("Study report requires four ordered task reports")
    scored = sum(task.scored_slots for task in tasks)
    successful = sum(task.successful_slots for task in tasks)
    costs_complete = all(task.estimated_cost_complete for task in tasks)
    pass_at_1_values = tuple(
        task.pass_at_1 for task in tasks if task.pass_at_1 is not None
    )
    pass_at_3_values = tuple(
        task.pass_at_3 for task in tasks if task.pass_at_3 is not None
    )
    return PublicStudyAggregate(
        scored_slots=scored,
        successful_slots=successful,
        infrastructure_invalid_slots=sum(
            task.infrastructure_invalid_slots for task in tasks
        ),
        indeterminate_slots=sum(
            task.indeterminate_slots for task in tasks
        ),
        unexecuted_slots=sum(task.unexecuted_slots for task in tasks),
        scoring_coverage=scored / 12,
        scored_success_rate=successful / scored if scored else None,
        planned_slot_success_rate=successful / 12,
        macro_pass_at_1=(
            sum(pass_at_1_values) / len(pass_at_1_values)
            if pass_at_1_values
            else None
        ),
        pass_at_1_eligible_task_count=len(pass_at_1_values),
        macro_pass_at_3=(
            sum(pass_at_3_values) / 4
            if len(pass_at_3_values) == 4
            else None
        ),
        pass_at_3_eligible_task_count=len(pass_at_3_values),
        first_attempt_success_count=sum(
            task.first_attempt_success for task in tasks
        ),
        any_success_in_3_count=sum(task.any_success_in_3 for task in tasks),
        stable_success_count=sum(task.stable_success for task in tasks),
        logical_model_calls=sum(task.logical_model_calls for task in tasks),
        physical_model_requests=sum(
            task.physical_model_requests for task in tasks
        ),
        retry_count=sum(task.retry_count for task in tasks),
        read_call_count=sum(task.read_call_count for task in tasks),
        mutation_committed_count=sum(
            task.mutation_committed_count for task in tasks
        ),
        managed_test_completed_count=sum(
            task.managed_test_completed_count for task in tasks
        ),
        wall_time_ms=sum(task.wall_time_ms for task in tasks),
        input_tokens=sum(task.input_tokens for task in tasks),
        output_tokens=sum(task.output_tokens for task in tasks),
        total_tokens=sum(task.total_tokens for task in tasks),
        usage_complete=all(task.usage_complete for task in tasks),
        estimated_cost_complete=costs_complete,
        estimated_cost=(
            sum(
                (
                    cast(Decimal, task.estimated_cost)
                    for task in tasks
                ),
                start=Decimal("0"),
            )
            if costs_complete
            else None
        ),
        provider_deviation_count=sum(
            task.provider_deviation_count for task in tasks
        ),
        normalized_multi_tool_response_count=sum(
            task.normalized_multi_tool_response_count for task in tasks
        ),
        raw_infrastructure_attempt_count=sum(
            task.raw_infrastructure_attempt_count for task in tasks
        ),
        replacement_count=sum(task.replacement_count for task in tasks),
        model_quality_failure_distribution=_merge_distributions(
            task.model_quality_failure_distribution for task in tasks
        ),
        infrastructure_failure_distribution=_merge_distributions(
            task.infrastructure_failure_distribution for task in tasks
        ),
    )


class EvaluationStudyReportBuilder:
    def __init__(self, database: Database) -> None:
        self._studies = EvaluationStudyRepository(database)
        self._protocols = EvaluationProtocolRepository(database)
        self._campaigns = EvaluationCampaignRepository(database)
        self._runs = EvaluationRunRepository(database)
        self._telemetry = EvaluationTelemetryRepository(database)

    def build(
        self,
        study_id: UUID,
        pricing: PricingSnapshot,
        *,
        source_categories: Mapping[str, str] | None = None,
    ) -> PublicEvaluationStudyReport:
        study = self._studies.get_study(study_id)
        definition = self._studies.get_definition(study_id)
        if pricing.model_id != definition.model_id:
            raise StudyReportBuildError(
                "Pricing model does not match the Study"
            )
        if pricing.pricing_digest != definition.pricing_snapshot_digest:
            raise StudyReportBuildError(
                "Pricing snapshot does not match the Study"
            )
        if pricing.source_url.query or pricing.source_url.fragment:
            raise StudyReportBuildError(
                "Public pricing URL cannot contain query or fragment data"
            )
        bindings = self._studies.list_campaign_bindings(study_id)
        if len(bindings) != 4:
            raise StudyReportBuildError(
                "Study report requires four Campaign bindings"
            )
        protocols = tuple(
            self._protocols.get(binding.protocol_digest)
            for binding in bindings
        )
        self._validate_common_protocol_facts(protocols, definition.model_id)
        categories = source_categories or _default_source_categories()
        tasks = tuple(
            self._build_task(
                index,
                protocols[index],
                bindings[index].campaign_id,
                bindings[index].campaign_status,
                pricing,
                categories.get(protocols[index].task_id, "UNSPECIFIED"),
            )
            for index in range(4)
        )
        typed_tasks = cast(
            tuple[
                PublicTaskStudyReport,
                PublicTaskStudyReport,
                PublicTaskStudyReport,
                PublicTaskStudyReport,
            ],
            tasks,
        )
        provider = protocols[0].provider_binding
        platform = protocols[0].platform_binding
        report = PublicEvaluationStudyReport(
            study_id=study.study_id,
            study_status=study.status,
            study_definition_digest=definition.definition_digest,
            protocol_digests=definition.protocol_digests,
            model_id=definition.model_id,
            response_model_id=definition.response_model_id,
            git_commit_sha=definition.git_commit_sha,
            runtime_source_digest=definition.runtime_source_digest,
            pyproject_sha256=definition.pyproject_sha256,
            uv_lock_sha256=definition.uv_lock_sha256,
            fixture_registry_digest=definition.fixture_registry_digest,
            platform_binding_digest=definition.platform_binding_digest,
            os_family=platform.os_family,
            python_implementation=platform.python_implementation,
            python_version=platform.python_version,
            provider=PublicProviderSettings(
                timeout_seconds=provider.timeout_seconds,
                max_retries=provider.max_retries,
                store=False,
                max_output_tokens=cast(int, provider.max_output_tokens),
                multi_tool_response_policy=(
                    provider.multi_tool_response_policy.value
                ),
                max_function_calls_per_response=(
                    provider.max_function_calls_per_response
                ),
                configuration_digest=provider.configuration_digest,
            ),
            pricing=PublicPricingFacts(
                pricing_digest=pricing.pricing_digest,
                effective_date=pricing.effective_date.isoformat(),
                source_url=str(pricing.source_url),
                input_per_million=pricing.input_per_million,
                cached_input_per_million=(
                    pricing.cached_input_per_million
                ),
                output_per_million=pricing.output_per_million,
            ),
            summary=aggregate_study_summary(typed_tasks),
            tasks=typed_tasks,
        )
        PublicArtifactScanner(
            forbidden_prompt_texts=tuple(
                text
                for protocol in protocols
                for text in (protocol.system_prompt, protocol.task_prompt)
            )
        ).validate(report.model_dump_json())
        return report

    def _build_task(
        self,
        task_order: int,
        protocol: EvaluationProtocol,
        campaign_id: UUID,
        bound_campaign_status: EvaluationCampaignStatus | None,
        pricing: PricingSnapshot,
        source_category: str,
    ) -> PublicTaskStudyReport:
        campaign = self._campaigns.get_campaign(campaign_id)
        if (
            campaign.protocol_digest != protocol.protocol_digest
            or campaign.task_id != protocol.task_id
            or (
                bound_campaign_status is not None
                and bound_campaign_status is not campaign.status
            )
        ):
            raise StudyReportBuildError(
                "Study Campaign report binding has drifted"
            )
        slots = self._campaigns.list_slots(campaign.campaign_id)
        attempts_by_slot = {
            slot.slot_id: self._campaigns.list_attempts(slot.slot_id)
            for slot in slots
        }
        attempts = [
            attempt
            for slot_attempts in attempts_by_slot.values()
            for attempt in slot_attempts
        ]
        runs = self._runs.list_for_campaign(campaign.campaign_id)
        telemetry = self._telemetry.list_for_campaign(campaign.campaign_id)
        telemetry_by_run = {
            item.evaluation_run_id: item for item in telemetry
        }
        if len(telemetry_by_run) != len(telemetry) or any(
            run.evaluation_run_id not in telemetry_by_run for run in runs
        ):
            raise StudyReportBuildError(
                "Evaluation telemetry is incomplete or conflicting"
            )
        runs_by_id = {run.evaluation_run_id: run for run in runs}
        selected: list[RepairEvaluationRun] = []
        for slot in slots:
            if slot.status is not EvaluationSlotStatus.ACCEPTED:
                continue
            if (
                slot.selected_evaluation_run_id is None
                or slot.selected_evaluation_run_id not in runs_by_id
            ):
                raise StudyReportBuildError(
                    "Selected evaluation result is missing"
                )
            selected.append(runs_by_id[slot.selected_evaluation_run_id])
        selected.sort(key=lambda run: run.repetition_index)
        selected_telemetry = [
            telemetry_by_run[run.evaluation_run_id] for run in selected
        ]
        public_scored_runs = tuple(
            self._public_scored_run(
                run,
                telemetry_by_run[run.evaluation_run_id],
            )
            for run in selected
        )
        public_runs_by_id = {
            run.evaluation_run_id: public
            for run, public in zip(
                selected,
                public_scored_runs,
                strict=True,
            )
        }
        public_slots = cast(
            tuple[
                PublicSlotStudyRecord,
                PublicSlotStudyRecord,
                PublicSlotStudyRecord,
            ],
            tuple(
                PublicSlotStudyRecord(
                    repetition_index=slot.repetition_index,
                    slot_status=slot.status,
                    attempt_count=len(attempts_by_slot[slot.slot_id]),
                    replacement_count=sum(
                        attempt.predecessor_attempt_id is not None
                        for attempt in attempts_by_slot[slot.slot_id]
                    ),
                    selected_run=(
                        public_runs_by_id[slot.selected_evaluation_run_id]
                        if slot.selected_evaluation_run_id is not None
                        else None
                    ),
                )
                for slot in slots
            ),
        )
        costs = [estimate_cost(item, pricing) for item in telemetry]
        cost_complete = all(cost.complete for cost in costs)
        successful = sum(run.verified_success for run in selected)
        scored_count = len(selected)
        infrastructure_slots = sum(
            slot.status is EvaluationSlotStatus.INVALID for slot in slots
        )
        indeterminate_slots = sum(
            slot.status is EvaluationSlotStatus.INDETERMINATE for slot in slots
        )
        unexecuted_slots = (
            3 - scored_count - infrastructure_slots - indeterminate_slots
        )
        quality_failures = Counter(
            run.failure_category or run.final_status.value
            for run in selected
            if not run.verified_success
        )
        infrastructure_failures = Counter(
            attempt.failure_category or "UNCLASSIFIED_INFRASTRUCTURE"
            for attempt in attempts
            if attempt.status is EvaluationAttemptStatus.INVALID
            and attempt.infrastructure_failure
        )
        return PublicTaskStudyReport(
            task_id=protocol.task_id,
            task_order=task_order,
            source_category=source_category,
            protocol_digest=protocol.protocol_digest,
            campaign_status=(
                bound_campaign_status
                if bound_campaign_status is not None
                else campaign.status
            ),
            fixture_asset_digest=protocol.fixture_asset_digest,
            task_policy_digest=protocol.task_policy_digest,
            system_prompt_digest=protocol.system_prompt_digest,
            task_prompt_digest=protocol.task_prompt_digest,
            tool_schema_digest=protocol.tool_schema_digest,
            scored_slots=scored_count,
            successful_slots=successful,
            infrastructure_invalid_slots=infrastructure_slots,
            indeterminate_slots=indeterminate_slots,
            unexecuted_slots=unexecuted_slots,
            scoring_coverage=scored_count / 3,
            scored_success_rate=(
                successful / scored_count if scored_count else None
            ),
            planned_slot_success_rate=successful / 3,
            pass_at_1=(
                estimate_pass_at_k(
                    total=scored_count,
                    successful=successful,
                    k=1,
                )
                if scored_count
                else None
            ),
            pass_at_3=(
                estimate_pass_at_k(
                    total=3,
                    successful=successful,
                    k=3,
                )
                if scored_count == 3
                else None
            ),
            first_attempt_success=(
                public_slots[0].selected_run is not None
                and public_slots[0].selected_run.verified_success
            ),
            majority_success=successful >= 2,
            stable_success=successful == 3,
            any_success_in_3=successful > 0,
            logical_model_calls=_sum_telemetry(
                telemetry,
                "logical_model_calls",
            ),
            physical_model_requests=_sum_telemetry(
                telemetry,
                "physical_model_requests",
            ),
            retry_count=_sum_telemetry(telemetry, "retry_count"),
            read_call_count=_sum_telemetry(telemetry, "read_call_count"),
            mutation_committed_count=_sum_telemetry(
                telemetry,
                "mutation_committed_count",
            ),
            managed_test_completed_count=_sum_telemetry(
                telemetry,
                "managed_test_completed_count",
            ),
            wall_time_ms=sum(run.wall_time_ms for run in runs),
            input_tokens=_sum_telemetry(telemetry, "input_tokens"),
            output_tokens=_sum_telemetry(telemetry, "output_tokens"),
            total_tokens=_sum_telemetry(telemetry, "total_tokens"),
            usage_complete=all(item.usage_complete for item in telemetry),
            estimated_cost_complete=cost_complete,
            estimated_cost=(
                sum(
                    (cast(Decimal, cost.amount) for cost in costs),
                    start=Decimal("0"),
                )
                if cost_complete
                else None
            ),
            provider_deviation_count=_sum_telemetry(
                telemetry,
                "provider_deviation_count",
            ),
            normalized_multi_tool_response_count=_sum_telemetry(
                telemetry,
                "normalized_multi_tool_response_count",
            ),
            mean_logical_model_calls_per_scored_run=_mean_telemetry(
                selected_telemetry,
                "logical_model_calls",
            ),
            mean_physical_model_requests_per_scored_run=_mean_telemetry(
                selected_telemetry,
                "physical_model_requests",
            ),
            mean_retries_per_scored_run=_mean_telemetry(
                selected_telemetry,
                "retry_count",
            ),
            mean_reads_per_scored_run=(
                fmean(run.read_calls for run in selected)
                if selected
                else None
            ),
            mean_edits_per_scored_run=(
                fmean(run.edit_attempts for run in selected)
                if selected
                else None
            ),
            mean_development_tests_per_scored_run=(
                fmean(run.test_runs for run in selected)
                if selected
                else None
            ),
            median_wall_time_ms=(
                median(run.wall_time_ms for run in selected)
                if selected
                else None
            ),
            mean_total_tokens_per_scored_run=_mean_telemetry(
                selected_telemetry,
                "total_tokens",
            ),
            provider_deviation_rate=_per_physical_request_rate(
                telemetry,
                "provider_deviation_count",
            ),
            multi_tool_normalization_rate=_per_physical_request_rate(
                telemetry,
                "normalized_multi_tool_response_count",
            ),
            raw_infrastructure_attempt_count=sum(
                attempt.status is EvaluationAttemptStatus.INVALID
                and attempt.infrastructure_failure
                for attempt in attempts
            ),
            replacement_count=sum(
                attempt.predecessor_attempt_id is not None
                for attempt in attempts
            ),
            policy_block_rate=_run_rate(
                selected,
                lambda run: run.final_status
                is RepairCompletionStatus.POLICY_BLOCKED,
            ),
            budget_exhaustion_rate=_run_rate(
                selected,
                lambda run: run.final_status
                is RepairCompletionStatus.BUDGET_EXHAUSTED,
            ),
            protocol_error_rate=_run_rate(
                selected,
                lambda run: run.final_status
                is RepairCompletionStatus.MODEL_PROTOCOL_ERROR,
            ),
            visible_test_failure_rate=_run_rate(
                selected,
                lambda run: run.failure_category
                == "DEVELOPMENT_TEST_FAILED",
            ),
            hidden_test_failure_rate=_run_rate(
                selected,
                lambda run: run.failure_category
                == "FINAL_HIDDEN_TEST_FAILED",
            ),
            model_quality_failure_distribution=dict(
                sorted(quality_failures.items())
            ),
            infrastructure_failure_distribution=dict(
                sorted(infrastructure_failures.items())
            ),
            slots=public_slots,
            scored_runs=public_scored_runs,
        )

    @staticmethod
    def _public_scored_run(
        run: RepairEvaluationRun,
        telemetry: EvaluationRunTelemetry,
    ) -> PublicScoredRun:
        return PublicScoredRun(
            repetition_index=run.repetition_index,
            attempt_number=run.attempt_number,
            final_status=run.final_status,
            verified_success=run.verified_success,
            failure_category=run.failure_category,
            final_workspace_digest=run.final_workspace_digest,
            final_diff_digest=run.final_diff_digest,
            model_calls=run.model_calls,
            read_calls=run.read_calls,
            edit_attempts=run.edit_attempts,
            test_runs=run.test_runs,
            wall_time_ms=run.wall_time_ms,
            total_tokens=telemetry.total_tokens,
        )

    @staticmethod
    def _validate_common_protocol_facts(
        protocols: Sequence[EvaluationProtocol],
        model_id: str,
    ) -> None:
        if len(protocols) != 4:
            raise StudyReportBuildError(
                "Study report requires four Protocols"
            )
        first = protocols[0]
        if any(
            protocol.provider_binding != first.provider_binding
            or protocol.platform_binding != first.platform_binding
            or protocol.provider_binding.model_id != model_id
            for protocol in protocols
        ):
            raise StudyReportBuildError(
                "Study Protocol Provider or platform facts diverged"
            )


def render_public_study_json(
    summary: PublicStudyAggregate,
    tasks: Sequence[PublicTaskStudyReport],
    *,
    scanner: PublicArtifactScanner,
) -> str:
    _validate_summary_matches_tasks(summary, tasks)
    payload = {
        "report_schema_version": 2,
        "summary": summary.model_dump(mode="json"),
        "tasks": [task.model_dump(mode="json") for task in tasks],
        "limitations": list(_LIMITATIONS),
    }
    rendered = json.dumps(
        payload,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    scanner.validate(rendered)
    return rendered


def render_evaluation_study_json(
    report: PublicEvaluationStudyReport,
    *,
    scanner: PublicArtifactScanner,
) -> str:
    rendered = json.dumps(
        report.model_dump(mode="json"),
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    scanner.validate(rendered)
    return rendered


def render_public_study_markdown(
    summary: PublicStudyAggregate,
    tasks: Sequence[PublicTaskStudyReport],
    *,
    scanner: PublicArtifactScanner,
) -> str:
    _validate_summary_matches_tasks(summary, tasks)
    lines = [
        "# AgentForge Real-Model Portfolio Study",
        "",
        f"- Planned slots: {summary.planned_slots}",
        f"- score-eligible slots: {summary.scored_slots}",
        (
            "- Raw successful/scored slots: "
            f"{summary.successful_slots}/{summary.scored_slots}"
        ),
        (
            "- Planned/scored/invalid/indeterminate/unexecuted slots: "
            f"{summary.planned_slots}/{summary.scored_slots}/"
            f"{summary.infrastructure_invalid_slots}/"
            f"{summary.indeterminate_slots}/{summary.unexecuted_slots}"
        ),
        f"- Replacement attempts: {summary.replacement_count}",
        (
            "- Scoring coverage: "
            f"{_percentage(summary.scoring_coverage)}"
        ),
        (
            "- Scored success rate: "
            + (
                _percentage(summary.scored_success_rate)
                if summary.scored_success_rate is not None
                else "n/a"
            )
        ),
        (
            "- Planned-slot success rate: "
            f"{_percentage(summary.planned_slot_success_rate)}"
        ),
        (
            "- Macro pass@1: "
            + (
                _percentage(summary.macro_pass_at_1)
                if summary.macro_pass_at_1 is not None
                else "N/A"
            )
            + (
                " ("
                f"{summary.pass_at_1_eligible_task_count} eligible tasks)"
            )
        ),
        (
            "- Macro pass@3: "
            + (
                _percentage(summary.macro_pass_at_3)
                if summary.macro_pass_at_3 is not None
                else "N/A"
            )
            + (
                " ("
                f"{summary.pass_at_3_eligible_task_count} eligible tasks)"
            )
        ),
        (
            "- First-attempt success count: "
            f"{summary.first_attempt_success_count}/4"
        ),
        (
            "- Any-success-in-3 count: "
            f"{summary.any_success_in_3_count}/4"
        ),
        f"- Stable-success count: {summary.stable_success_count}/4",
        "",
        (
            "| Task | Planned | Scored | Successful/scored | pass@1 | pass@3 | "
            "First attempt | Any success in 3 | Majority | Stable | Invalid | "
            "Indeterminate | Unexecuted | Replacements |"
        ),
        (
            "| --- | ---: | ---: | ---: | ---: | ---: | --- | --- | --- | "
            "--- | ---: | ---: | ---: | ---: |"
        ),
    ]
    lines.extend(
        (
            f"| {task.task_id} | {task.planned_slots} | {task.scored_slots} | "
            f"{task.successful_slots}/{task.scored_slots} | "
            f"{_percentage(task.pass_at_1) if task.pass_at_1 is not None else 'N/A'} | "
            f"{_percentage(task.pass_at_3) if task.pass_at_3 is not None else 'N/A'} | "
            f"{'yes' if task.first_attempt_success else 'no'} | "
            f"{'yes' if task.any_success_in_3 else 'no'} | "
            f"{'yes' if task.majority_success else 'no'} | "
            f"{'yes' if task.stable_success else 'no'} | "
            f"{task.infrastructure_invalid_slots} | "
            f"{task.indeterminate_slots} | {task.unexecuted_slots} | "
            f"{task.replacement_count} |"
        )
        for task in tasks
    )
    lines.extend(
        [
            "",
            "## Planned Slots",
            "",
            "| Task | Repetition | Status | Attempts | Replacements | Final result |",
            "| --- | ---: | --- | ---: | ---: | --- |",
        ]
    )
    lines.extend(
        (
            f"| {task.task_id} | {slot.repetition_index} | "
            f"{slot.slot_status.value} | {slot.attempt_count} | "
            f"{slot.replacement_count} | "
            f"{slot.selected_run.final_status.value if slot.selected_run else 'n/a'} |"
        )
        for task in tasks
        for slot in task.slots
    )
    lines.extend(["", "## Limitations", ""])
    lines.extend(f"- {limitation}" for limitation in _LIMITATIONS)
    rendered = "\n".join(lines) + "\n"
    scanner.validate(rendered)
    return rendered


def render_evaluation_study_markdown(
    report: PublicEvaluationStudyReport,
    *,
    scanner: PublicArtifactScanner,
) -> str:
    estimated_cost = (
        str(report.summary.estimated_cost)
        if report.summary.estimated_cost is not None
        else "n/a"
    )
    prefix = [
        "# AgentForge Real-Model Portfolio Study",
        "",
        f"- Study status: {report.study_status.value}",
        f"- Study digest: `{report.study_definition_digest}`",
        f"- Report digest: `{report.report_digest}`",
        f"- Requested model: `{report.model_id}`",
        f"- Frozen response model: `{report.response_model_id}`",
        "",
        "## Bound Configuration",
        "",
        f"- Provider: `{report.provider.provider}`",
        (
            "- Provider configuration digest: "
            f"`{report.provider.configuration_digest}`"
        ),
        f"- Provider timeout seconds: {report.provider.timeout_seconds}",
        f"- Provider retries: {report.provider.max_retries}",
        f"- Provider output-token limit: {report.provider.max_output_tokens}",
        (
            "- Multi-tool response policy: "
            f"`{report.provider.multi_tool_response_policy}`"
        ),
        (
            "- Maximum function calls per response: "
            f"{report.provider.max_function_calls_per_response}"
        ),
        f"- Git commit: `{report.git_commit_sha}`",
        f"- Runtime source digest: `{report.runtime_source_digest}`",
        f"- pyproject digest: `{report.pyproject_sha256}`",
        f"- uv.lock digest: `{report.uv_lock_sha256}`",
        f"- Fixture registry digest: `{report.fixture_registry_digest}`",
        f"- Platform binding digest: `{report.platform_binding_digest}`",
        f"- Platform: `{report.os_family}`",
        (
            "- Python: "
            f"`{report.python_implementation} {report.python_version}`"
        ),
        "",
        "### Protocol Digests",
        "",
        *(
            f"- `{task_id}`: `{digest}`"
            for task_id, digest in zip(
                B2_4_TASK_ORDER,
                report.protocol_digests,
                strict=True,
            )
        ),
        "",
        "## Usage And Cost",
        "",
        f"- Logical model calls: {report.summary.logical_model_calls}",
        f"- Physical model requests: {report.summary.physical_model_requests}",
        f"- Provider retries: {report.summary.retry_count}",
        f"- Input tokens: {report.summary.input_tokens}",
        f"- Output tokens: {report.summary.output_tokens}",
        f"- Total tokens: {report.summary.total_tokens}",
        f"- Usage complete: {str(report.summary.usage_complete).lower()}",
        f"- Pricing digest: `{report.pricing.pricing_digest}`",
        f"- Pricing effective date: {report.pricing.effective_date}",
        f"- Pricing source: {report.pricing.source_url}",
        (
            "- Estimated cost complete: "
            f"{str(report.summary.estimated_cost_complete).lower()}"
        ),
        (
            f"- Estimated cost ({report.pricing.currency}): "
            f"{estimated_cost}"
        ),
        "",
    ]
    body = render_public_study_markdown(
        report.summary,
        report.tasks,
        scanner=PublicArtifactScanner(),
    )
    rendered = "\n".join(prefix) + body.split("\n", 2)[2]
    scanner.validate(rendered)
    return rendered


def _validate_distribution(distribution: Mapping[str, int]) -> None:
    if any(
        not _SAFE_CATEGORY.fullmatch(category) or count <= 0
        for category, count in distribution.items()
    ):
        raise ValueError("Public failure distribution contains unsafe facts")


def _validate_summary_matches_tasks(
    summary: PublicStudyAggregate,
    tasks: Sequence[PublicTaskStudyReport],
) -> None:
    expected = aggregate_study_summary(tasks)
    if summary != expected:
        raise ValueError("Study report summary does not reconcile with tasks")


def _merge_distributions(
    values: Iterable[Mapping[str, int]],
) -> dict[str, int]:
    merged: Counter[str] = Counter()
    for value in values:
        merged.update(value)
    return dict(sorted(merged.items()))


def _sum_telemetry(
    values: Sequence[EvaluationRunTelemetry],
    field: str,
) -> int:
    return sum(cast(int, getattr(value, field)) for value in values)


def _mean_telemetry(
    values: Sequence[EvaluationRunTelemetry],
    field: str,
) -> float | None:
    if not values:
        return None
    return fmean(cast(int, getattr(value, field)) for value in values)


def _per_physical_request_rate(
    values: Sequence[EvaluationRunTelemetry],
    field: str,
) -> float:
    requests = _sum_telemetry(values, "physical_model_requests")
    if requests == 0:
        return 0
    return _sum_telemetry(values, field) / requests


def _run_rate(
    runs: Sequence[RepairEvaluationRun],
    predicate: Callable[[RepairEvaluationRun], bool],
) -> float:
    if not runs:
        return 0
    return sum(predicate(run) for run in runs) / len(runs)


def _default_source_categories() -> dict[str, str]:
    return {
        "quixbugs-shortest-path-length": "QUIXBUGS",
        "bugsinpy-black-21": "BUGSINPY",
        "swebench-pytest-10051": "SWE_BENCH_CROPPED",
        "self-durable-double-consumption": "SELF_BUILT",
    }


def _percentage(value: float) -> str:
    return f"{value * 100:.2f}%"
