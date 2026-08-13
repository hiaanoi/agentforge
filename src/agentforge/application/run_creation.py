from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, TypeAlias
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from agentforge.application.commands import StartRun as ProductStartRun
from agentforge.application.config import ProductConfig
from agentforge.application.contracts import ReceiptStatus
from agentforge.application.kernel_errors import (
    IdempotencyConflictError,
    IncompleteRunBundleError,
    InvalidReceiptTransitionError,
    PersistenceBoundaryError,
)
from agentforge.application.product_workspace import (
    PreparedProductWorkspace,
    ProductWorkspaceCapture,
    WorkspaceBaselineStore,
)
from agentforge.application.runtime_factory import RuntimeComponents
from agentforge.domain.enums import EventType, RunFailureCode, RunStatus
from agentforge.domain.models import Run, normalize_utc
from agentforge.domain.repair import (
    RepairCompletionStatus,
    RepairTaskPolicy,
    RepairTerminationReason,
)
from agentforge.models.domain import ModelBudget
from agentforge.persistence.application_uow import ApplicationUnitOfWork
from agentforge.persistence.database import Database
from agentforge.persistence.event_log import (
    ConversationCommandAuthority,
    EventLog,
    RunCreationAuthority,
    RunLeaseAuthority,
)
from agentforge.persistence.product_tables import (
    ApplicationCommandReceiptRow,
    ConversationRow,
    WorkspaceSourceBindingRow,
)
from agentforge.persistence.receipts import ReceiptRecord, ReceiptStore, request_digest
from agentforge.persistence.repair_terminal import terminalize_repair_in_session
from agentforge.persistence.source_revisions import DIGEST_ALGORITHM_VERSION
from agentforge.persistence.tables import (
    EventRow,
    ModelRuntimeStateRow,
    RepairStateRow,
    RepairTaskPolicyRow,
    RunRow,
)
from agentforge.tools.testing.profiles import TestProfileRegistry


class _RunBundleCommand(BaseModel):
    """Strict common contract for either product Run creation entrypoint."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    command_id: UUID
    task: str = Field(min_length=1)
    max_steps: int = Field(gt=0)
    max_tool_calls: int = Field(ge=0)
    model_provider: str = Field(min_length=1)
    model_budget: ModelBudget
    workspace_root_identity: str = Field(min_length=1)
    git_head: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}([0-9a-f]{24})?$")
    initial_source_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    digest_algorithm_version: int = Field(gt=0)
    config_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    profile_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    repair_policy: RepairTaskPolicy
    baseline_id: UUID
    baseline_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="before")
    @classmethod
    def require_exact_integer_inputs(cls, value: Any) -> Any:
        if not isinstance(value, Mapping):
            return value
        for name in (
            "schema_version",
            "max_steps",
            "max_tool_calls",
            "digest_algorithm_version",
        ):
            if name in value and type(value[name]) is not int:
                raise ValueError(f"{name} must be an exact integer")
        if "schema_version" in value and value["schema_version"] != 1:
            raise ValueError("schema_version must be exactly 1")
        cls._require_nested_integers(
            value.get("model_budget"),
            (
                "max_model_requests",
                "max_retries",
                "max_output_tokens_per_request",
                "max_total_input_tokens",
                "max_total_output_tokens",
                "max_total_tokens",
            ),
        )
        cls._require_nested_integers(
            value.get("repair_policy"),
            (
                "policy_version",
                "max_created_files",
                "max_changed_files",
                "max_total_changed_bytes",
                "max_single_file_changed_bytes",
                "max_model_calls",
                "max_read_calls",
                "max_edit_attempts",
                "max_test_runs",
                "max_completion_corrections",
                "max_policy_violations",
                "max_wall_time_seconds",
            ),
        )
        cls._require_nested_booleans(
            value.get("repair_policy"),
            ("allow_file_creation", "path_case_sensitive"),
        )
        return value

    @staticmethod
    def _require_nested_integers(value: object, names: tuple[str, ...]) -> None:
        if not isinstance(value, Mapping):
            return
        for name in names:
            item = value.get(name)
            if item is not None and type(item) is not int:
                raise ValueError(f"{name} must be an exact integer")

    @staticmethod
    def _require_nested_booleans(value: object, names: tuple[str, ...]) -> None:
        if not isinstance(value, Mapping):
            return
        for name in names:
            if name in value and type(value[name]) is not bool:
                raise ValueError(f"{name} must be an exact boolean")

    @field_validator("git_head", mode="before")
    @classmethod
    def normalize_git_object_id(cls, value: object) -> object:
        if value is None:
            return None
        if type(value) is not str or len(value) not in {40, 64}:
            raise ValueError("git_head must be a SHA-1 or SHA-256 object ID")
        try:
            int(value, 16)
        except ValueError:
            raise ValueError("git_head must be hexadecimal") from None
        return value.lower()


class StartRun(_RunBundleCommand):
    """Internal A1 command for a standalone product Run."""

    @property
    def command_type(self) -> str:
        return "START_RUN"


@dataclass(frozen=True, slots=True)
class ProductStartRunAssembler:
    """Closed product provenance bridge from configuration/runtime facts to A1 creation."""

    config: ProductConfig
    workspace: Path
    components: RuntimeComponents
    profiles: TestProfileRegistry
    repair_policy: RepairTaskPolicy
    model_budget: ModelBudget

    def __post_init__(self) -> None:
        root = self.workspace.resolve(strict=True)
        if (
            self.components.workspace != root
            or self.components.workspace_resolver.workspace != root
            or self.profiles.workspace_root != root
        ):
            raise ValueError("product workspace does not match runtime assembly")
        if self.components.profiles is not self.profiles:
            raise ValueError("product profiles do not match runtime assembly")
        if self.components.repair_policy != self.repair_policy:
            raise ValueError("product repair policy does not match runtime assembly")
        if self.components.model_budget != self.model_budget:
            raise ValueError("product model budget does not match runtime assembly")

    def prepare(self, command: ProductStartRun) -> PreparedStartRun:
        root = self.workspace.resolve(strict=True)
        if (
            self.components.workspace != root
            or self.components.workspace_resolver.workspace != root
            or self.profiles.workspace_root != root
        ):
            raise ValueError("product workspace does not match runtime assembly")
        binding = self.components.provider_binding
        if (
            self.components.provider.name != binding.name
            or self.components.provider.journal_identity != binding.journal_identity
            or not binding.journal_identity.startswith(f"{binding.name}/")
        ):
            raise ValueError("product provider changed after runtime assembly")
        if command.workspace.resolve(strict=True) != root:
            raise ValueError("StartRun workspace does not match product assembly")
        configured_database = self.config.database_path.resolve(strict=False)
        actual_database = self.components.database.path
        if actual_database is None or configured_database != actual_database:
            raise ValueError("product database does not match runtime assembly")
        if self.config.model != self.components.configured_model_id:
            raise ValueError("product model does not match runtime assembly")
        enabled = self.profiles.list_enabled()
        enabled_ids = tuple(profile.profile_id for profile in enabled)
        if not self.config.profile_ids or self.config.profile_ids != enabled_ids:
            raise ValueError("product profiles do not match runtime assembly")
        if enabled_ids != self.components.configured_profile_ids:
            raise ValueError("runtime profile bindings changed after assembly")
        profile_digest = hashlib.sha256(
            json.dumps(
                [profile.profile_digest for profile in enabled],
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        prepared_workspace = ProductWorkspaceCapture().capture(
            root, task_id=self.repair_policy.task_id, command_id=command.command_id
        )
        config_digest = hashlib.sha256(
            json.dumps(
                {
                    "effective_config_digest": self.config.effective_config_digest,
                    "runtime_binding_digest": self.components.common_binding_digest,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        assembled = StartRun(
            command_id=command.command_id,
            task=command.task,
            max_steps=self.config.max_steps,
            max_tool_calls=(
                self.repair_policy.max_read_calls
                + self.repair_policy.max_edit_attempts
                + self.repair_policy.max_test_runs
            ),
            model_provider=self.components.model_provider_name,
            model_budget=self.model_budget,
            workspace_root_identity=str(root),
            git_head=None,
            initial_source_digest=prepared_workspace.source_digest,
            digest_algorithm_version=DIGEST_ALGORITHM_VERSION,
            config_digest=config_digest,
            profile_digest=profile_digest,
            repair_policy=self.repair_policy,
            baseline_id=prepared_workspace.baseline.baseline_id,
            baseline_digest=prepared_workspace.baseline.root_digest,
        )
        return PreparedStartRun(command=assembled, workspace=prepared_workspace)

    def assemble(self, command: ProductStartRun) -> StartRun:
        """Compatibility adapter; product entrypoints should use prepare()."""

        return self.prepare(command).command


@dataclass(frozen=True, slots=True)
class PreparedStartRun:
    command: StartRun
    workspace: PreparedProductWorkspace


class SubmitMessageRun(_RunBundleCommand):
    """Internal B seam for a user message and its unique execution Run."""

    conversation_id: UUID
    client_message_id: UUID
    content: str = Field(min_length=1)

    @property
    def command_type(self) -> str:
        return "SUBMIT_MESSAGE"


RunBundleCommand: TypeAlias = StartRun | SubmitMessageRun


class RunCreationFailpoint(StrEnum):
    AFTER_RECEIPT = "AFTER_RECEIPT"
    AFTER_BASELINE = "AFTER_BASELINE"
    AFTER_RUN = "AFTER_RUN"
    AFTER_REPAIR = "AFTER_REPAIR"
    AFTER_MODEL_STATE = "AFTER_MODEL_STATE"
    AFTER_SOURCE_BINDING = "AFTER_SOURCE_BINDING"
    AFTER_INITIAL_EVENT = "AFTER_INITIAL_EVENT"
    AFTER_RECEIPT_IN_PROGRESS = "AFTER_RECEIPT_IN_PROGRESS"
    AFTER_COMMIT = "AFTER_COMMIT"


class SimulatedProcessCrash(RuntimeError):
    pass


RecoveryDisposition = Literal[
    "CREATED",
    "REATTACH_CREATED",
    "REATTACH_RUNNING",
    "PAUSED",
    "TERMINAL",
    "UNKNOWN",
]


@dataclass(frozen=True, slots=True)
class RunCreationResult:
    run: Run
    receipt: ReceiptRecord
    recovery: RecoveryDisposition

    @property
    def run_id(self) -> UUID:
        return self.run.run_id


class RunCreationWorkflow:
    """Create or recover one complete product Run in one outer transaction."""

    def __init__(
        self,
        database: Database,
        receipts: ReceiptStore | None = None,
        events: EventLog | None = None,
    ) -> None:
        self._database = database
        self._receipts = receipts or ReceiptStore()
        self._events = events or EventLog()

    def create(
        self,
        command: StartRun,
        *,
        prepared_workspace: PreparedProductWorkspace | None = None,
        failpoint: RunCreationFailpoint | None = None,
    ) -> RunCreationResult:
        try:
            return self._create(
                command, prepared_workspace=prepared_workspace, failpoint=failpoint
            )
        except SQLAlchemyError:
            raise PersistenceBoundaryError() from None

    def _create(
        self,
        command: StartRun,
        *,
        prepared_workspace: PreparedProductWorkspace | None = None,
        failpoint: RunCreationFailpoint | None = None,
    ) -> RunCreationResult:
        result: RunCreationResult
        with ApplicationUnitOfWork(self._database) as uow:
            session = uow.session
            receipt = self._receipts.accept(session, command)
            self._crash(failpoint, RunCreationFailpoint.AFTER_RECEIPT)
            if prepared_workspace is None:
                self._crash(failpoint, RunCreationFailpoint.AFTER_BASELINE)
            if receipt.result_scope_id is not None:
                result = self._load_existing(session, receipt, command)
                uow.commit()
                return result
            if prepared_workspace is not None:
                if (
                    prepared_workspace.source_digest != command.initial_source_digest
                    or prepared_workspace.baseline.baseline_id != command.baseline_id
                    or prepared_workspace.baseline.root_digest != command.baseline_digest
                ):
                    raise IncompleteRunBundleError()
                WorkspaceBaselineStore().put(session, prepared_workspace.baseline)
                self._crash(failpoint, RunCreationFailpoint.AFTER_BASELINE)
            result = self.initialize_bundle(
                session,
                command,
                receipt,
                failpoint=failpoint,
            )
            uow.commit()
        self._crash(failpoint, RunCreationFailpoint.AFTER_COMMIT)
        return result

    def initialize_bundle(
        self,
        session: Session,
        command: RunBundleCommand,
        receipt: ReceiptRecord,
        *,
        failpoint: RunCreationFailpoint | None = None,
    ) -> RunCreationResult:
        try:
            return self._initialize_bundle(
                session, command, receipt, failpoint=failpoint
            )
        except SQLAlchemyError:
            raise PersistenceBoundaryError() from None

    def _initialize_bundle(
        self,
        session: Session,
        command: RunBundleCommand,
        receipt: ReceiptRecord,
        *,
        failpoint: RunCreationFailpoint | None = None,
    ) -> RunCreationResult:
        """Initialize the bundle in the caller's Session; never commit or open one."""
        self._validate_receipt(session, command, receipt)
        if type(command) is StartRun:
            if receipt.result_scope_type == "RUN" and receipt.result_scope_id is not None:
                return self._load_existing(session, receipt, command)
            if (
                receipt.status is not ReceiptStatus.ACCEPTED
                or receipt.result_scope_type is not None
                or receipt.result_scope_id is not None
            ):
                raise InvalidReceiptTransitionError()
        else:
            assert type(command) is SubmitMessageRun
            if (
                receipt.status not in {ReceiptStatus.ACCEPTED, ReceiptStatus.IN_PROGRESS}
                or receipt.result_scope_type != "CONVERSATION"
                or receipt.result_scope_id != str(command.conversation_id)
            ):
                raise InvalidReceiptTransitionError()
            receipt = self._ensure_submit_message_fact(session, command, receipt)
            existing_run_id = self._find_command_run(session, command.command_id)
            if existing_run_id is not None:
                return self._load_run_bundle(session, receipt, existing_run_id, command)
        run = Run(
            task=command.task,
            max_steps=command.max_steps,
            max_tool_calls=command.max_tool_calls,
            model_provider=command.model_provider,
        )
        session.add(self._run_to_row(run))
        session.flush()
        self._crash(failpoint, RunCreationFailpoint.AFTER_RUN)

        now = run.created_at
        policy = command.repair_policy
        session.add(
            RepairTaskPolicyRow(
                run_id=str(run.run_id),
                task_id=policy.task_id,
                policy_version=policy.policy_version,
                policy_digest=policy.policy_digest,
                policy_data=policy.model_dump(mode="json"),
                created_at=now,
            )
        )
        session.add(
            RepairStateRow(
                run_id=str(run.run_id),
                task_id=policy.task_id,
                policy_digest=policy.policy_digest,
                status=RepairCompletionStatus.RUNNING.value,
                model_calls_used=0,
                read_calls_used=0,
                edit_attempts_used=0,
                test_runs_used=0,
                completion_corrections_used=0,
                policy_violations=0,
                started_at=now,
                deadline_at=now + timedelta(seconds=policy.max_wall_time_seconds),
                baseline_id=str(command.baseline_id),
                baseline_digest=command.baseline_digest,
                last_mutation_execution_id=None,
                last_mutation_committed_at=None,
                last_development_test_execution_id=None,
                last_development_test_success=None,
                last_development_test_completed_at=None,
                final_verification_execution_id=None,
                final_verification_success=None,
                final_verification_completed_at=None,
                last_diff_validation_id=None,
                final_workspace_digest=None,
                final_diff_digest=None,
                latest_source_verified=False,
                pending_final_verification=False,
                final_answer_received=False,
                failure_reason=None,
                state_version=1,
                updated_at=now,
            )
        )
        session.flush()
        self._crash(failpoint, RunCreationFailpoint.AFTER_REPAIR)

        budget = command.model_budget
        session.add(
            ModelRuntimeStateRow(
                run_id=str(run.run_id),
                model_request_count=0,
                input_tokens=0,
                output_tokens=0,
                total_tokens=0,
                cached_input_tokens=0,
                reasoning_tokens=0,
                max_model_requests=budget.max_model_requests,
                max_retries=budget.max_retries,
                max_output_tokens_per_request=budget.max_output_tokens_per_request,
                max_total_input_tokens=budget.max_total_input_tokens,
                max_total_output_tokens=budget.max_total_output_tokens,
                max_total_tokens=budget.max_total_tokens,
                updated_at=now,
            )
        )
        session.flush()
        self._crash(failpoint, RunCreationFailpoint.AFTER_MODEL_STATE)

        session.add(
            WorkspaceSourceBindingRow(
                run_id=str(run.run_id),
                workspace_root_identity=command.workspace_root_identity,
                git_head=command.git_head,
                initial_source_digest=command.initial_source_digest,
                expected_source_digest=command.initial_source_digest,
                source_revision_number=0,
                digest_algorithm_version=command.digest_algorithm_version,
                config_digest=command.config_digest,
                profile_digest=command.profile_digest,
                created_at=now,
                updated_at=now,
            )
        )
        session.flush()
        self._crash(failpoint, RunCreationFailpoint.AFTER_SOURCE_BINDING)

        self._events.append(
            session,
            RunCreationAuthority(run.run_id),
            EventType.RUN_CREATED,
            {
                "task_digest": hashlib.sha256(command.task.encode("utf-8")).hexdigest(),
                "command_id": str(command.command_id),
                "command_type": command.command_type,
            },
        )
        self._crash(failpoint, RunCreationFailpoint.AFTER_INITIAL_EVENT)
        if type(command) is StartRun:
            result_receipt = self._receipts.mark_in_progress(
                session,
                command.command_id,
                run.run_id,
                at=now,
            )
        else:
            result_receipt = receipt
        self._crash(failpoint, RunCreationFailpoint.AFTER_RECEIPT_IN_PROGRESS)
        return RunCreationResult(run=run, receipt=result_receipt, recovery="CREATED")

    def _ensure_submit_message_fact(
        self,
        session: Session,
        command: SubmitMessageRun,
        receipt: ReceiptRecord,
    ) -> ReceiptRecord:
        conversation = session.get(
            ConversationRow,
            str(command.conversation_id),
            populate_existing=True,
        )
        if conversation is None:
            raise InvalidReceiptTransitionError()
        if receipt.status is ReceiptStatus.ACCEPTED:
            version = conversation.version
            self._events.append(
                session,
                ConversationCommandAuthority(
                    str(command.conversation_id), version, command.command_id
                ),
                "MESSAGE_ACCEPTED",
                self._message_fact_payload(command, version),
            )
            return self._receipts.get(session, command.command_id)
        events = session.scalars(
            select(EventRow).where(
                EventRow.scope_type == "CONVERSATION",
                EventRow.scope_id == str(command.conversation_id),
                EventRow.event_type == "MESSAGE_ACCEPTED",
            )
        ).all()
        matching = [
            event
            for event in events
            if event.payload.get("command_id") == str(command.command_id)
        ]
        if len(matching) != 1:
            raise IncompleteRunBundleError()
        raw_version = matching[0].payload.get("conversation_version")
        if type(raw_version) is not int:
            raise IncompleteRunBundleError()
        claimed_version = raw_version
        if (
            type(conversation.version) is not int
            or conversation.version < claimed_version + 1
            or matching[0].payload
            != self._message_fact_payload(command, claimed_version)
        ):
            raise IncompleteRunBundleError()
        return self._receipts.get(session, command.command_id)

    @staticmethod
    def _message_fact_payload(
        command: SubmitMessageRun, conversation_version: int
    ) -> dict[str, str | int]:
        return {
            "command_id": str(command.command_id),
            "client_message_id": str(command.client_message_id),
            "message_digest": hashlib.sha256(command.content.encode("utf-8")).hexdigest(),
            "conversation_version": conversation_version,
        }

    def _validate_receipt(
        self,
        session: Session,
        command: RunBundleCommand,
        receipt: ReceiptRecord,
    ) -> None:
        persisted = self._receipts.get(session, receipt.command_id)
        if persisted != receipt:
            if (
                persisted.command_id != receipt.command_id
                or persisted.command_type != receipt.command_type
                or persisted.request_digest != receipt.request_digest
            ):
                raise IdempotencyConflictError()
            raise InvalidReceiptTransitionError()
        if (
            persisted.command_id != command.command_id
            or persisted.command_type != command.command_type
            or persisted.request_digest != request_digest(command)
        ):
            raise IdempotencyConflictError()

    @staticmethod
    def _find_command_run(session: Session, command_id: UUID) -> str | None:
        events = session.scalars(
            select(EventRow).where(EventRow.event_type == EventType.RUN_CREATED.value)
        ).all()
        matching = [
            event.run_id
            for event in events
            if event.payload.get("command_id") == str(command_id)
        ]
        if len(matching) > 1 or (matching and matching[0] is None):
            raise IncompleteRunBundleError()
        return matching[0] if matching else None

    def transition_terminal(
        self,
        command_id: UUID,
        terminal: ReceiptStatus,
        *,
        authority: RunLeaseAuthority,
    ) -> RunCreationResult:
        """Commit the first terminal Event, Run status and Receipt as one fact."""
        if terminal not in {
            ReceiptStatus.COMPLETED,
            ReceiptStatus.FAILED,
            ReceiptStatus.INDETERMINATE,
        }:
            raise InvalidReceiptTransitionError()
        with ApplicationUnitOfWork(self._database) as uow:
            session = uow.session
            receipt = self._receipts.get(session, command_id)
            if receipt.status in {
                ReceiptStatus.COMPLETED,
                ReceiptStatus.FAILED,
                ReceiptStatus.INDETERMINATE,
            }:
                if receipt.status is not terminal:
                    raise InvalidReceiptTransitionError()
                result = self._load_existing(session, receipt, None)
                uow.commit()
                return result
            if (
                receipt.status is not ReceiptStatus.IN_PROGRESS
                or receipt.result_scope_type != "RUN"
                or receipt.result_scope_id is None
            ):
                raise InvalidReceiptTransitionError()
            run_id = UUID(receipt.result_scope_id)
            if authority.run_id != run_id:
                raise InvalidReceiptTransitionError()
            row = session.get(RunRow, str(run_id))
            if row is None:
                raise IncompleteRunBundleError()
            event_type = (
                EventType.RUN_COMPLETED
                if terminal is ReceiptStatus.COMPLETED
                else EventType.RUN_FAILED
            )
            fact = self._events.append(
                session,
                authority,
                event_type,
                (
                    {"outcome": ReceiptStatus.COMPLETED.value}
                    if terminal is ReceiptStatus.COMPLETED
                    else {
                        "code": (
                            RunFailureCode.COMMAND_FAILED.value
                            if terminal is ReceiptStatus.FAILED
                            else RunFailureCode.COMMAND_INDETERMINATE.value
                        )
                    }
                ),
            )
            terminal_at = fact.created_at
            row.status = (
                RunStatus.COMPLETED.value
                if terminal is ReceiptStatus.COMPLETED
                else RunStatus.FAILED.value
            )
            row.updated_at = terminal_at
            terminalize_repair_in_session(
                session,
                run_id,
                status={
                    ReceiptStatus.COMPLETED: RepairCompletionStatus.UNVERIFIED_FINAL,
                    ReceiptStatus.FAILED: RepairCompletionStatus.RUNTIME_FAILURE,
                    ReceiptStatus.INDETERMINATE: RepairCompletionStatus.INDETERMINATE,
                }[terminal],
                reason={
                    ReceiptStatus.COMPLETED: RepairTerminationReason.LATEST_MUTATION_NOT_VERIFIED,
                    ReceiptStatus.FAILED: RepairTerminationReason.RUNTIME_FAILURE,
                    ReceiptStatus.INDETERMINATE: RepairTerminationReason.INDETERMINATE_SIDE_EFFECT,
                }[terminal],
                at=terminal_at,
            )
            receipt = {
                ReceiptStatus.COMPLETED: self._receipts.complete,
                ReceiptStatus.FAILED: self._receipts.fail,
                ReceiptStatus.INDETERMINATE: self._receipts.mark_indeterminate,
            }[terminal](session, command_id, at=terminal_at)
            session.flush()
            result = RunCreationResult(
                run=self._run_to_domain(row),
                receipt=receipt,
                recovery="TERMINAL",
            )
            uow.commit()
            return result

    def finalize_observed_run(self, run_id: UUID) -> ReceiptRecord | None:
        """Close the Start receipt from an already durable terminal Run fact."""

        with ApplicationUnitOfWork(self._database) as uow:
            session = uow.session
            row = session.get(RunRow, str(run_id))
            repair = session.get(RepairStateRow, str(run_id))
            receipt_row = session.scalar(
                select(ApplicationCommandReceiptRow).where(
                    ApplicationCommandReceiptRow.command_type == "START_RUN",
                    ApplicationCommandReceiptRow.result_scope_type == "RUN",
                    ApplicationCommandReceiptRow.result_scope_id == str(run_id),
                )
            )
            if row is None or repair is None or receipt_row is None:
                raise IncompleteRunBundleError()
            receipt = self._receipts.get(session, UUID(receipt_row.command_id))
            if receipt.status in {
                ReceiptStatus.COMPLETED,
                ReceiptStatus.FAILED,
                ReceiptStatus.INDETERMINATE,
            }:
                return receipt
            if row.status not in {
                RunStatus.COMPLETED.value,
                RunStatus.FAILED.value,
                RunStatus.CANCELLED.value,
            }:
                return None
            if repair.status == RepairCompletionStatus.INDETERMINATE.value:
                terminalizer = self._receipts.mark_indeterminate
            elif row.status == RunStatus.COMPLETED.value:
                terminalizer = self._receipts.complete
            else:
                terminalizer = self._receipts.fail
            terminal = terminalizer(
                session, receipt.command_id, at=normalize_utc(row.updated_at)
            )
            uow.commit()
            return terminal

    def _load_existing(
        self,
        session: Session,
        receipt: ReceiptRecord,
        command: RunBundleCommand | None,
    ) -> RunCreationResult:
        if receipt.result_scope_type != "RUN" or receipt.result_scope_id is None:
            raise IncompleteRunBundleError()
        return self._load_run_bundle(
            session, receipt, receipt.result_scope_id, command
        )

    def _load_run_bundle(
        self,
        session: Session,
        receipt: ReceiptRecord,
        run_id: str,
        command: RunBundleCommand | None,
    ) -> RunCreationResult:
        row = session.get(RunRow, run_id)
        raw_policy_json = session.scalar(
            text(
                "SELECT policy_data FROM repair_task_policies WHERE run_id = :run_id"
            ),
            {"run_id": run_id},
        )
        if type(raw_policy_json) is not str:
            raise IncompleteRunBundleError()
        try:
            raw_policy_value = json.loads(
                raw_policy_json,
                object_pairs_hook=self._unique_json_object,
            )
        except (TypeError, ValueError):
            raise IncompleteRunBundleError() from None
        if type(raw_policy_value) is not dict:
            raise IncompleteRunBundleError()
        policy = session.get(RepairTaskPolicyRow, run_id)
        repair = session.get(RepairStateRow, run_id)
        model = session.get(ModelRuntimeStateRow, run_id)
        source = session.get(WorkspaceSourceBindingRow, run_id)
        creation_events = session.scalars(
            select(EventRow).where(
                EventRow.run_id == run_id,
                EventRow.event_type == EventType.RUN_CREATED.value,
            )
        ).all()
        initial_event = creation_events[0] if len(creation_events) == 1 else None
        if (
            row is None
            or policy is None
            or repair is None
            or model is None
            or source is None
            or initial_event is None
        ):
            raise IncompleteRunBundleError()
        if (
            initial_event.payload.get("command_id") != str(receipt.command_id)
            or initial_event.payload.get("command_type") != receipt.command_type
        ):
            raise IncompleteRunBundleError()
        self._validate_bundle_facts(
            receipt,
            command,
            row,
            policy,
            raw_policy_json,
            repair,
            model,
            source,
            initial_event,
        )
        run = self._run_to_domain(row)
        self._validate_terminal_fact(session, receipt, run)
        recovery = self._recovery_disposition(receipt.status, run.status)
        return RunCreationResult(run=run, receipt=receipt, recovery=recovery)

    @staticmethod
    def _validate_bundle_facts(
        receipt: ReceiptRecord,
        command: RunBundleCommand | None,
        run: RunRow,
        policy: RepairTaskPolicyRow,
        raw_policy_json: str,
        repair: RepairStateRow,
        model: ModelRuntimeStateRow,
        source: WorkspaceSourceBindingRow,
        event: EventRow,
    ) -> None:
        if not (
            policy.run_id == repair.run_id == model.run_id == source.run_id == run.run_id
            and event.schema_version == 1
            and event.scope_type == "RUN"
            and event.scope_id == run.run_id
            and event.run_id == run.run_id
            and event.sequence_number == 1
            and event.event_type == EventType.RUN_CREATED.value
            and repair.task_id == policy.task_id
            and repair.policy_digest == policy.policy_digest
        ):
            raise IncompleteRunBundleError()
        try:
            raw_policy = json.loads(
                raw_policy_json,
                object_pairs_hook=RunCreationWorkflow._unique_json_object,
            )
            persisted_policy = RepairTaskPolicy.model_validate_json(
                raw_policy_json, strict=True
            )
        except (TypeError, ValueError):
            raise IncompleteRunBundleError() from None
        canonical_policy = persisted_policy.model_dump(mode="json")
        if (
            type(raw_policy) is not dict
            or RunCreationWorkflow._canonical_json(raw_policy)
            != RunCreationWorkflow._canonical_json(canonical_policy)
            or RunCreationWorkflow._canonical_json(policy.policy_data)
            != RunCreationWorkflow._canonical_json(canonical_policy)
            or persisted_policy.task_id != policy.task_id
            or persisted_policy.policy_version != policy.policy_version
            or persisted_policy.policy_digest != policy.policy_digest
        ):
            raise IncompleteRunBundleError()
        if command is None:
            return
        budget = command.model_budget
        expected_payload = {
            "task_digest": hashlib.sha256(command.task.encode("utf-8")).hexdigest(),
            "command_id": str(command.command_id),
            "command_type": command.command_type,
        }
        if not (
            run.task == command.task
            and run.max_steps == command.max_steps
            and run.max_tool_calls == command.max_tool_calls
            and run.model_provider == command.model_provider
            and source.workspace_root_identity == command.workspace_root_identity
            and source.git_head == command.git_head
            and source.initial_source_digest == command.initial_source_digest
            and source.digest_algorithm_version == command.digest_algorithm_version
            and source.config_digest == command.config_digest
            and source.profile_digest == command.profile_digest
            and source.source_revision_number >= 0
            and (
                source.source_revision_number != 0
                or source.expected_source_digest == command.initial_source_digest
            )
            and model.max_model_requests == budget.max_model_requests
            and model.max_retries == budget.max_retries
            and model.max_output_tokens_per_request
            == budget.max_output_tokens_per_request
            and model.max_total_input_tokens == budget.max_total_input_tokens
            and model.max_total_output_tokens == budget.max_total_output_tokens
            and model.max_total_tokens == budget.max_total_tokens
            and persisted_policy == command.repair_policy
            and repair.baseline_id == str(command.baseline_id)
            and repair.baseline_digest == command.baseline_digest
            and event.payload == expected_payload
        ):
            raise IncompleteRunBundleError()

    @staticmethod
    def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON object key")
            result[key] = value
        return result

    @staticmethod
    def _canonical_json(value: Any) -> str:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    @staticmethod
    def _validate_terminal_fact(
        session: Session,
        receipt: ReceiptRecord,
        run: Run,
    ) -> None:
        terminal_mapping = {
            ReceiptStatus.COMPLETED: (RunStatus.COMPLETED, EventType.RUN_COMPLETED),
            ReceiptStatus.FAILED: (RunStatus.FAILED, EventType.RUN_FAILED),
            ReceiptStatus.INDETERMINATE: (RunStatus.FAILED, EventType.RUN_FAILED),
        }
        expected = terminal_mapping.get(receipt.status)
        if expected is None:
            return
        expected_run_status, expected_event_type = expected
        if run.status is not expected_run_status:
            raise IncompleteRunBundleError()
        events = session.scalars(
            select(EventRow).where(
                EventRow.run_id == str(run.run_id),
                EventRow.event_type == expected_event_type.value,
            )
        ).all()
        if receipt.status is ReceiptStatus.COMPLETED:
            matching = [
                event
                for event in events
                if event.payload == {"outcome": ReceiptStatus.COMPLETED.value}
            ]
        else:
            expected_code = (
                RunFailureCode.COMMAND_FAILED.value
                if receipt.status is ReceiptStatus.FAILED
                else RunFailureCode.COMMAND_INDETERMINATE.value
            )
            matching = [
                event for event in events if event.payload == {"code": expected_code}
            ]
        if len(matching) != 1:
            raise IncompleteRunBundleError()
        terminal_at = normalize_utc(matching[0].created_at)
        if terminal_at != receipt.updated_at or terminal_at != run.updated_at:
            raise IncompleteRunBundleError()

    @staticmethod
    def _recovery_disposition(
        receipt_status: ReceiptStatus, run_status: RunStatus
    ) -> RecoveryDisposition:
        if receipt_status in {
            ReceiptStatus.COMPLETED,
            ReceiptStatus.FAILED,
            ReceiptStatus.INDETERMINATE,
        }:
            return "TERMINAL"
        if receipt_status is not ReceiptStatus.IN_PROGRESS:
            return "UNKNOWN"
        if run_status is RunStatus.CREATED:
            return "REATTACH_CREATED"
        if run_status is RunStatus.RUNNING:
            return "REATTACH_RUNNING"
        if run_status in {RunStatus.WAITING_APPROVAL, RunStatus.PAUSED}:
            return "PAUSED"
        return "UNKNOWN"

    @staticmethod
    def _crash(selected: RunCreationFailpoint | None, current: RunCreationFailpoint) -> None:
        if selected is current:
            raise SimulatedProcessCrash(current.value)

    @staticmethod
    def _run_to_row(run: Run) -> RunRow:
        return RunRow(
            run_id=str(run.run_id),
            task=run.task,
            status=run.status.value,
            current_step=run.current_step,
            max_steps=run.max_steps,
            tool_call_count=run.tool_call_count,
            max_tool_calls=run.max_tool_calls,
            model_provider=run.model_provider,
            created_at=run.created_at,
            updated_at=run.updated_at,
            total_token_usage=run.total_token_usage,
            estimated_cost=run.estimated_cost,
            error_message=run.error_message,
            final_output=run.final_output,
            next_event_sequence=1,
            event_sequence_version=0,
        )

    @staticmethod
    def _run_to_domain(row: RunRow) -> Run:
        return Run(
            run_id=UUID(row.run_id),
            task=row.task,
            status=RunStatus(row.status),
            current_step=row.current_step,
            max_steps=row.max_steps,
            tool_call_count=row.tool_call_count,
            max_tool_calls=row.max_tool_calls,
            model_provider=row.model_provider,
            created_at=normalize_utc(row.created_at),
            updated_at=normalize_utc(row.updated_at),
            total_token_usage=row.total_token_usage,
            estimated_cost=row.estimated_cost,
            error_message=row.error_message,
            final_output=row.final_output,
        )
