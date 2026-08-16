from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from agentforge.application.app import AgentApplication
from agentforge.application.commands import DecideApproval, ResumeRun, StartRun
from agentforge.application.config import ProductConfigLoader
from agentforge.application.contracts import OutcomeStatus, ProfilePurpose
from agentforge.application.doctor import Doctor
from agentforge.application.errors import ApplicationErrorCode, ApplicationFailure
from agentforge.application.events import ProductEvent
from agentforge.application.queries import (
    DoctorReport,
    ExportRunDetails,
    PendingApprovals,
    ProfileTrustDetails,
    RunDetails,
)
from agentforge.application.run_creation import ProductStartRunAssembler
from agentforge.application.runtime_factory import (
    RuntimeAssemblyRequest,
    RuntimeComponentFactory,
    SupervisorIdentity,
)
from agentforge.context.models import ContextPolicy
from agentforge.domain.enums import ApprovalStatus
from agentforge.domain.repair import BudgetProfile, RepairDifficulty, RepairTaskPolicy
from agentforge.domain.test_execution import TestProfile as DomainTestProfile
from agentforge.evaluation.public_artifacts import PublicArtifactScanner
from agentforge.models.base import ModelRequest
from agentforge.models.domain import ModelBudget, ModelResponse
from agentforge.models.mock import MockModelProvider
from agentforge.persistence.database import Database
from agentforge.persistence.legacy_evaluator import LegacyEvaluatorEventRepository
from agentforge.persistence.product_tables import (
    ApplicationCommandReceiptRow,
    WorkspaceSourceBindingRow,
)
from agentforge.persistence.profile_trust import ProfileKernel
from agentforge.persistence.repair_workflow import RepairWorkflow
from agentforge.persistence.resume_workflow import ResumeRunWorkflow
from agentforge.persistence.run_leases import RunLeaseStore
from agentforge.persistence.tables import (
    EventRow,
    ProcessExecutionRow,
    RunRow,
    WorkspaceBaselineRow,
)
from agentforge.process.base import (
    ProcessTreeSupervisor,
    SupervisorOutcome,
    SupervisorStatus,
    empty_captured_stream,
)
from agentforge.runtime.engine import AgentRuntime
from agentforge.tools.paths import WorkspacePathResolver
from agentforge.tools.testing.profiles import TestProfileDefinition as ProfileDefinition
from agentforge.tools.testing.profiles import TestProfileRegistry as ProfileRegistry


@dataclass
class Fixture:
    app: AgentApplication
    database: Database
    workspace: Path
    provider: MockModelProvider


class SlowMockProvider(MockModelProvider):
    def __init__(self, release: asyncio.Event) -> None:
        super().__init__([{"type": "final", "answer": "done"}])
        self._release = release
        self.started = asyncio.Event()

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.started.set()
        await self._release.wait()
        return await super().generate(request)


class BlockingSequenceProvider(MockModelProvider):
    def __init__(self, release: asyncio.Event) -> None:
        super().__init__(
            [
                {"type": "tool_call", "tool": "run_tests", "arguments": {"profile_id": "visible"}},
                {"type": "final", "answer": "done"},
            ]
        )
        self._release = release
        self.blocked = asyncio.Event()

    async def generate(self, request: ModelRequest) -> ModelResponse:
        if self.requests:
            self.blocked.set()
            await self._release.wait()
        return await super().generate(request)


class DeterministicSupervisor:
    def run(self, profile: DomainTestProfile) -> SupervisorOutcome:
        return SupervisorOutcome(
            status=SupervisorStatus.EXITED,
            root_pid=None,
            process_group_id=None,
            job_id=None,
            exit_code=0,
            stdout=empty_captured_stream(),
            stderr=empty_captured_stream(),
            duration_ms=1,
            termination_reason=None,
            termination_result="natural_exit",
            termination_confirmed=True,
        )

    def cancel(self, reason: str = "cancelled") -> bool:
        return True


def deterministic_supervisor_factory() -> ProcessTreeSupervisor:
    return DeterministicSupervisor()


def _policy() -> RepairTaskPolicy:
    return RepairTaskPolicy(
        task_id="product-application",
        policy_version=1,
        difficulty=RepairDifficulty.ENGINEERING,
        budget_profile=BudgetProfile.ENGINEERING,
        allowed_write_paths=("src/**",),
        forbidden_write_paths=(".git/**",),
        protected_paths=("tests/**",),
        allowed_development_test_profiles=("visible",),
        final_verification_profile_id="hidden",
        allow_file_creation=True,
        allowed_create_paths=("src/**",),
        max_created_files=2,
        max_changed_files=4,
        max_total_changed_bytes=1_048_576,
        max_single_file_changed_bytes=1_048_576,
        max_model_calls=10,
        max_read_calls=35,
        max_edit_attempts=4,
        max_test_runs=5,
        max_completion_corrections=1,
        max_policy_violations=2,
        max_wall_time_seconds=600,
        path_case_sensitive=os.path.normcase("A") != os.path.normcase("a"),
    )


def build_fixture(
    tmp_path: Path,
    responses: list[object] | None = None,
    *,
    provider: MockModelProvider | None = None,
) -> Fixture:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    (workspace / "src").mkdir(exist_ok=True)
    (workspace / "tests").mkdir(exist_ok=True)
    (workspace / "src" / "example.py").write_text("VALUE = 1\n", encoding="utf-8")
    (workspace / ".gitignore").write_text(
        ".agentforge/\n.pytest_cache/\n__pycache__/\n", encoding="utf-8"
    )
    if not (workspace / ".git").exists():
        for arguments in (
            ("git", "init", "-q"),
            ("git", "config", "user.email", "fixture@example.invalid"),
            ("git", "config", "user.name", "AgentForge Fixture"),
            ("git", "add", "."),
            ("git", "commit", "-qm", "fixture"),
        ):
            subprocess.run(arguments, cwd=workspace, check=True)
    verifier = tmp_path / "verifier"
    verifier.mkdir(exist_ok=True)
    (verifier / "test_ok.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    config = ProductConfigLoader(user_root=tmp_path / "user").load(
        workspace,
        cli={
            "database_path": ".agentforge/agentforge.db",
            "model": "mock",
            "profile_ids": ("hidden", "visible"),
        },
    )
    database = Database.from_path(
        config.database_path,
        artifact_root=(tmp_path / "verification-artifacts").resolve(),
    )
    database.create_schema()
    profiles = ProfileRegistry(WorkspacePathResolver(workspace))
    for profile_id, purpose, argv, verifier_root in (
        ("visible", ProfilePurpose.DEVELOPMENT, ("-c", "pass"), None),
        ("hidden", ProfilePurpose.VERIFICATION, ("-m", "pytest", "{VERIFIER}"), str(verifier)),
    ):
        profiles.register(
            ProfileDefinition(
                profile_id=profile_id,
                name=profile_id,
                description=f"{profile_id} profile",
                executable=sys.executable,
                argv=argv,
                cwd=".",
                timeout_seconds=10,
                max_output_bytes=4096,
                profile_version=1,
                purpose=purpose,
                verifier_root=verifier_root,
            )
        )
    kernel = ProfileKernel(database, profiles)
    for profile in profiles.list_enabled():
        kernel.trust(
            kernel.challenge(profile.profile_id, purpose=profile.purpose),
            command_id=uuid4(),
        )
    provider = provider or MockModelProvider(
        responses or [{"type": "final", "answer": "done"}]
    )
    workflow = RepairWorkflow(database)
    budget = ModelBudget(max_model_requests=3, max_retries=0)
    request = RuntimeAssemblyRequest(
        database=database,
        workspace=workspace,
        provider=provider,
        policy=_policy(),
        context_policy=ContextPolicy(system_instructions="product fixture"),
        model_budget=budget,
        profiles=profiles,
        events=LegacyEvaluatorEventRepository(database),
        repair_workflow=workflow,
        max_output_chars=20_000,
        supervisor_factory=deterministic_supervisor_factory,
        supervisor_identity=SupervisorIdentity(
            implementation="tests.agent_application.DeterministicSupervisor",
            implementation_version="1",
            config_digest="d" * 64,
        ),
    )
    components = RuntimeComponentFactory().build(request)
    assembler = ProductStartRunAssembler(
        config=config,
        workspace=workspace,
        components=components,
        profiles=profiles,
        repair_policy=request.policy,
        model_budget=budget,
    )
    return Fixture(
        AgentApplication(
            database,
            components.runtime,
            start_assembler=assembler,
            profiles=profiles,
        ),
        database,
        workspace,
        provider,
    )


async def collect(stream: AsyncIterator[object]) -> list[object]:
    return [event async for event in stream]


async def wait_for_receipts(database: Database, command_ids: tuple[UUID, ...]) -> None:
    async with asyncio.timeout(10):
        while True:
            with database.session() as session:
                if all(
                    session.get(ApplicationCommandReceiptRow, str(command_id)) is not None
                    for command_id in command_ids
                ):
                    return
            await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_start_is_atomic_replays_cursor_and_never_uses_legacy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = build_fixture(tmp_path)
    command = StartRun(command_id=uuid4(), task="repair", workspace=fixture.workspace)
    monkeypatch.setattr(
        AgentRuntime,
        "create_run",
        lambda *args, **kwargs: pytest.fail("legacy creation reached"),
    )
    first = await collect(fixture.app.stream(command))
    assert first
    run_id = first[0].run_id  # type: ignore[attr-defined]
    with fixture.database.session() as session:
        assert session.scalar(select(func.count(RunRow.run_id))) == 1
        assert session.scalar(select(func.count(WorkspaceBaselineRow.baseline_id))) == 1
        source = session.get(WorkspaceSourceBindingRow, str(run_id))
    assert source is not None
    (fixture.workspace / "src" / "example.py").write_text("VALUE = 2\n", encoding="utf-8")
    replay = await collect(fixture.app.stream(command, after_cursor=first[0].cursor))  # type: ignore[attr-defined]
    assert all(event.cursor > first[0].cursor for event in replay)  # type: ignore[attr-defined]
    with fixture.database.session() as session:
        assert session.scalar(select(func.count(RunRow.run_id))) == 1


def test_queries_and_doctor_are_safe_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = build_fixture(tmp_path)
    secret = "sk-doctor-must-not-leak"
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("doctor started a subprocess"),
    )

    async def forbidden_model(*args: object, **kwargs: object) -> object:
        pytest.fail("doctor called the model")

    monkeypatch.setattr(fixture.provider, "generate", forbidden_model)
    before = fixture.app.business_row_counts()
    report = fixture.app.query(DoctorReport(workspace=fixture.workspace))
    assert fixture.app.business_row_counts() == before
    PublicArtifactScanner().validate(report.model_dump_json())
    assert secret not in report.model_dump_json()
    assert {check.check for check in report.checks} == {
        "schema", "workspace", "source", "openai_environment", "git", "profile_bindings"
    }
    with pytest.raises(ApplicationFailure) as raised:
        fixture.app.query(RunDetails(run_id=uuid4()))
    assert raised.value.error.code is ApplicationErrorCode.NOT_FOUND


def test_doctor_never_creates_a_missing_sqlite_database(tmp_path: Path) -> None:
    missing = tmp_path / "missing" / "agentforge.db"
    database = Database.from_path(missing)

    report = Doctor(database).report(tmp_path)

    schema = next(check for check in report.checks if check.check == "schema")
    assert schema.status.value == "FAIL"
    assert not missing.exists()


def test_doctor_reports_only_the_selected_provider_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "deepseek-doctor-must-not-leak"
    monkeypatch.setenv("DEEPSEEK_API_KEY", secret)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    database = Database.from_path(tmp_path / "missing.db")

    report = Doctor(database, provider_kind="deepseek").report(tmp_path)

    checks = {check.check: check for check in report.checks}
    assert "deepseek_environment" in checks
    assert "openai_environment" not in checks
    assert checks["deepseek_environment"].status.value == "PASS"
    assert secret not in report.model_dump_json()


def test_mock_doctor_requires_no_remote_credentials(tmp_path: Path) -> None:
    database = Database.from_path(tmp_path / "missing.db")

    report = Doctor(database, provider_kind="mock").report(tmp_path)

    check = next(item for item in report.checks if item.check == "mock_environment")
    assert check.status.value == "PASS"


@pytest.mark.asyncio
async def test_application_close_is_idempotent_and_does_not_close_database(
    tmp_path: Path,
) -> None:
    fixture = build_fixture(tmp_path)

    async with fixture.app:
        pass
    await fixture.app.aclose()

    assert fixture.app.business_row_counts()["runs"] == 0


@pytest.mark.asyncio
async def test_all_queries_are_deterministic_for_durable_facts(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path)
    command = StartRun(command_id=uuid4(), task="repair", workspace=fixture.workspace)
    events = await collect(fixture.app.stream(command))
    run_id = events[0].run_id  # type: ignore[attr-defined]
    details = fixture.app.query(RunDetails(run_id=run_id))
    exported = fixture.app.query(ExportRunDetails(run_id=run_id))
    approvals = fixture.app.query(PendingApprovals(run_id=run_id))
    trust = fixture.app.query(
        ProfileTrustDetails(
            workspace=fixture.workspace,
            profile_id="visible",
            purpose=ProfilePurpose.DEVELOPMENT,
        )
    )
    assert fixture.app.query(RunDetails(run_id=run_id)) == details
    assert details.run_id == exported.run_id == run_id
    assert approvals.approvals == ()
    assert trust.trusted is True
    PublicArtifactScanner().validate(exported.model_dump_json())


@pytest.mark.asyncio
async def test_concurrent_same_command_has_one_bundle_and_driver(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path)
    command = StartRun(command_id=uuid4(), task="repair", workspace=fixture.workspace)
    first, second = await asyncio.gather(
        collect(fixture.app.stream(command)),
        collect(fixture.app.stream(command)),
    )
    assert first[0].run_id == second[0].run_id  # type: ignore[attr-defined]
    with fixture.database.session() as session:
        assert session.scalar(select(func.count(RunRow.run_id))) == 1
        assert session.scalar(select(func.count(WorkspaceBaselineRow.baseline_id))) == 1
    assert len(fixture.app._drivers) == 1


@pytest.mark.asyncio
async def test_stream_close_detaches_without_cancelling_driver(tmp_path: Path) -> None:
    release = asyncio.Event()
    provider = SlowMockProvider(release)
    fixture = build_fixture(tmp_path, provider=provider)
    command = StartRun(command_id=uuid4(), task="repair", workspace=fixture.workspace)
    stream = cast(AsyncGenerator[ProductEvent, None], fixture.app.stream(command))
    created = await anext(stream)
    await stream.aclose()
    release.set()
    assert created.run_id is not None
    driver = fixture.app._drivers[created.run_id]
    await asyncio.wait_for(asyncio.shield(driver), timeout=5)
    assert provider.requests
    with fixture.database.session() as session:
        assert (
            session.scalar(
                select(func.count(EventRow.global_cursor)).where(
                    EventRow.run_id == str(created.run_id)
                )
            )
            or 0
        ) > 1


@pytest.mark.asyncio
async def test_pause_recreate_approve_resume_reaches_verified(tmp_path: Path) -> None:
    first = build_fixture(
        tmp_path,
        [{"type": "tool_call", "tool": "run_tests", "arguments": {"profile_id": "visible"}}],
    )
    start = StartRun(command_id=uuid4(), task="repair", workspace=first.workspace)
    started = await collect(first.app.stream(start))
    run_id = started[0].run_id  # type: ignore[attr-defined]
    assert first.app.query(PendingApprovals(run_id=run_id)).approvals
    first.database.close()

    recreated = build_fixture(
        tmp_path,
        [{"type": "final", "answer": "verified repair"}],
    )
    for _ in range(5):
        details = recreated.app.query(RunDetails(run_id=run_id))
        if details.lifecycle_status.value == "TERMINAL":
            break
        pending = recreated.app.query(PendingApprovals(run_id=run_id)).approvals
        if pending:
            approval = pending[0]
            await collect(
                recreated.app.stream(
                    DecideApproval(
                        command_id=uuid4(),
                        approval_id=approval.approval_id,
                        status=ApprovalStatus.APPROVED,
                    )
                )
            )
        resume = ResumeRun(command_id=uuid4(), run_id=run_id)
        await collect(recreated.app.stream(resume))
        with recreated.database.session() as session:
            receipt = session.get(ApplicationCommandReceiptRow, str(resume.command_id))
            assert receipt is not None and receipt.status == "COMPLETED"
        assert RunLeaseStore(recreated.database).current(run_id) is None
    details = recreated.app.query(RunDetails(run_id=run_id))
    assert details.outcome_status is OutcomeStatus.VERIFIED
    with recreated.database.session() as session:
        receipt = session.get(ApplicationCommandReceiptRow, str(start.command_id))
        assert receipt is not None and receipt.status == "COMPLETED"
    reopened = build_fixture(tmp_path)
    replay = await collect(reopened.app.stream(start))
    assert replay and replay[0].run_id == run_id  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_workspace_drift_after_capture_blocks_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = build_fixture(tmp_path)
    original = fixture.app._creation.create

    def drift(*args: object, **kwargs: object) -> object:
        result = original(*args, **kwargs)  # type: ignore[arg-type]
        (fixture.workspace / "src" / "example.py").write_text("VALUE = 2\n", encoding="utf-8")
        return result

    monkeypatch.setattr(fixture.app._creation, "create", drift)
    command = StartRun(command_id=uuid4(), task="repair", workspace=fixture.workspace)
    with pytest.raises(ApplicationFailure):
        await collect(fixture.app.stream(command))
    assert fixture.provider.requests == []
    with fixture.database.session() as session:
        receipt = session.get(ApplicationCommandReceiptRow, str(command.command_id))
        assert receipt is not None and receipt.status == "FAILED"
        run = session.get(RunRow, receipt.result_scope_id)
        assert run is not None and run.status == "FAILED"
    replay = await collect(build_fixture(tmp_path).app.stream(command))
    assert replay and str(replay[-1].run_id) == receipt.result_scope_id  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_start_observer_reconciles_terminal_run_before_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = build_fixture(tmp_path)
    command = StartRun(command_id=uuid4(), task="repair", workspace=owner.workspace)
    monkeypatch.setattr(owner.app._creation, "finalize_observed_run", lambda _: None)

    await collect(owner.app.stream(command))
    with owner.database.session() as session:
        receipt = session.get(ApplicationCommandReceiptRow, str(command.command_id))
        assert receipt is not None and receipt.status == "IN_PROGRESS"

    observer = build_fixture(tmp_path)
    await collect(observer.app.stream(command))
    with observer.database.session() as session:
        receipt = session.get(ApplicationCommandReceiptRow, str(command.command_id))
        assert receipt is not None and receipt.status in {"COMPLETED", "FAILED"}


@pytest.mark.asyncio
async def test_resume_observer_reconciles_terminal_run_before_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = build_fixture(
        tmp_path,
        [
            {"type": "tool_call", "tool": "run_tests", "arguments": {"profile_id": "visible"}},
            {"type": "final", "answer": "done"},
        ],
    )
    started = await collect(
        owner.app.stream(StartRun(command_id=uuid4(), task="repair", workspace=owner.workspace))
    )
    run_id = started[0].run_id  # type: ignore[attr-defined]
    approval = owner.app.query(PendingApprovals(run_id=run_id)).approvals[0]
    await collect(
        owner.app.stream(
            DecideApproval(
                command_id=uuid4(), approval_id=approval.approval_id, status=ApprovalStatus.APPROVED
            )
        )
    )
    command = ResumeRun(command_id=uuid4(), run_id=run_id)
    original_terminalize = ResumeRunWorkflow.terminalize
    monkeypatch.setattr(ResumeRunWorkflow, "terminalize", lambda *_: None)
    stream = owner.app.stream(command)
    await anext(stream)
    await asyncio.wait_for(owner.app._resume_tasks[command.command_id], timeout=2)
    await stream.aclose()
    monkeypatch.setattr(ResumeRunWorkflow, "terminalize", original_terminalize)
    with owner.database.session() as session:
        receipt = session.get(ApplicationCommandReceiptRow, str(command.command_id))
        assert receipt is not None and receipt.status == "IN_PROGRESS"

    observer = build_fixture(tmp_path)
    await collect(observer.app.stream(command))
    with observer.database.session() as session:
        receipt = session.get(ApplicationCommandReceiptRow, str(command.command_id))
        assert receipt is not None and receipt.status in {"COMPLETED", "FAILED"}


@pytest.mark.asyncio
async def test_background_exception_maps_to_safe_catalog_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = build_fixture(tmp_path)

    async def explode(run_id: object) -> object:
        raise RuntimeError("OPENAI_API_KEY=super-secret")

    monkeypatch.setattr(fixture.app._runtime, "execute", explode)
    command = StartRun(command_id=uuid4(), task="repair", workspace=fixture.workspace)
    with pytest.raises(ApplicationFailure) as raised:
        await collect(fixture.app.stream(command))
    assert raised.value.error.code is ApplicationErrorCode.INTERNAL_ERROR
    serialized = raised.value.error.model_dump_json()
    assert "super-secret" not in serialized
    PublicArtifactScanner().validate(serialized)


@pytest.mark.asyncio
async def test_distinct_concurrent_resumes_have_terminal_receipts_and_one_driver(
    tmp_path: Path,
) -> None:
    release = asyncio.Event()
    provider = BlockingSequenceProvider(release)
    fixture = build_fixture(tmp_path, provider=provider)
    started = await collect(
        fixture.app.stream(
            StartRun(command_id=uuid4(), task="repair", workspace=fixture.workspace)
        )
    )
    run_id = started[0].run_id  # type: ignore[attr-defined]
    approval = fixture.app.query(PendingApprovals(run_id=run_id)).approvals[0]
    await collect(
        fixture.app.stream(
            DecideApproval(
                command_id=uuid4(),
                approval_id=approval.approval_id,
                status=ApprovalStatus.APPROVED,
            )
        )
    )
    commands = (
        ResumeRun(command_id=uuid4(), run_id=run_id),
        ResumeRun(command_id=uuid4(), run_id=run_id),
    )
    streams = [asyncio.create_task(collect(fixture.app.stream(item))) for item in commands]
    await asyncio.wait_for(provider.blocked.wait(), timeout=10)
    await wait_for_receipts(
        fixture.database, tuple(item.command_id for item in commands)
    )
    with fixture.database.session() as session:
        assert all(
            session.get(ApplicationCommandReceiptRow, str(item.command_id)) is not None
            for item in commands
        )
    release.set()
    _, pending = await asyncio.wait(streams, timeout=10)
    if pending:
        with fixture.database.session() as session:
            states = {
                str(item.command_id): session.get(
                    ApplicationCommandReceiptRow, str(item.command_id)
                ).status
                for item in commands
            }
            run_row = session.get(RunRow, str(run_id))
            assert run_row is not None
            run_status = run_row.status
        stacks = {
            str(command_id): [
                f"{frame.f_code.co_name}:{frame.f_lineno}"
                for frame in task.get_stack()
            ]
            for command_id, task in fixture.app._resume_tasks.items()
        }
        pytest.fail(
            "resume streams did not quiesce: "
            f"receipts={states}, run={run_status}, tasks={stacks}"
        )
    results = [task.result() for task in streams]
    assert all(isinstance(result, list) for result in results)
    with fixture.database.session() as session:
        receipts = [
            session.get(ApplicationCommandReceiptRow, str(item.command_id))
            for item in commands
        ]
        assert all(
            receipt is not None
            and receipt.status in {"COMPLETED", "FAILED", "INDETERMINATE"}
            for receipt in receipts
        )
        assert {receipt.status for receipt in receipts if receipt is not None} == {
            "COMPLETED",
            "FAILED",
        }
        assert session.scalar(select(func.count(ProcessExecutionRow.execution_id))) == 1
        resumed = session.scalars(
            select(EventRow).where(
                EventRow.run_id == str(run_id), EventRow.event_type == "RUN_RESUMED"
            )
        ).all()
        assert len([event for event in resumed if event.payload.get("command_id")]) == 1
    assert RunLeaseStore(fixture.database).current(run_id) is None


@pytest.mark.asyncio
async def test_same_resume_command_across_apps_attaches_without_corrupting_owner(
    tmp_path: Path,
) -> None:
    release = asyncio.Event()
    owner_provider = BlockingSequenceProvider(release)
    owner = build_fixture(tmp_path, provider=owner_provider)
    started = await collect(
        owner.app.stream(
            StartRun(command_id=uuid4(), task="repair", workspace=owner.workspace)
        )
    )
    run_id = started[0].run_id  # type: ignore[attr-defined]
    approval = owner.app.query(PendingApprovals(run_id=run_id)).approvals[0]
    await collect(
        owner.app.stream(
            DecideApproval(
                command_id=uuid4(),
                approval_id=approval.approval_id,
                status=ApprovalStatus.APPROVED,
            )
        )
    )
    observer = build_fixture(tmp_path)
    command = ResumeRun(command_id=uuid4(), run_id=run_id)
    await owner.app._accept(command)
    await asyncio.wait_for(owner_provider.blocked.wait(), timeout=10)
    await observer.app._accept(command)
    owner_stream = asyncio.create_task(collect(owner.app.stream(command)))
    observer_stream = asyncio.create_task(collect(observer.app.stream(command)))
    with owner.database.session() as session:
        receipt = session.get(ApplicationCommandReceiptRow, str(command.command_id))
        assert receipt is not None and receipt.status == "IN_PROGRESS"
    assert observer.provider.requests == []
    detached = cast(AsyncGenerator[ProductEvent, None], observer.app.stream(command))
    await anext(detached)
    await detached.aclose()
    with owner.database.session() as session:
        receipt = session.get(ApplicationCommandReceiptRow, str(command.command_id))
        assert receipt is not None and receipt.status == "IN_PROGRESS"
    release.set()
    owner_events, observer_events = await asyncio.wait_for(
        asyncio.gather(owner_stream, observer_stream), timeout=10
    )
    assert [event.cursor for event in owner_events] == [  # type: ignore[attr-defined]
        event.cursor for event in observer_events  # type: ignore[attr-defined]
    ]
    with owner.database.session() as session:
        receipt = session.get(ApplicationCommandReceiptRow, str(command.command_id))
        assert receipt is not None and receipt.status == "COMPLETED"
        resumed = session.scalars(
            select(EventRow).where(
                EventRow.run_id == str(run_id),
                EventRow.event_type == "RUN_RESUMED",
            )
        ).all()
        assert len([event for event in resumed if event.payload.get("command_id")]) == 1
        watermark = next(
            event
            for event in resumed
            if event.payload.get("command_id") == str(command.command_id)
        )
        assert owner_events[0].cursor == watermark.global_cursor  # type: ignore[attr-defined]
        assert session.scalar(select(func.count(ProcessExecutionRow.execution_id))) == 1


@pytest.mark.asyncio
async def test_same_start_command_across_apps_has_one_owner_and_observer(
    tmp_path: Path,
) -> None:
    release = asyncio.Event()
    owner_provider = SlowMockProvider(release)
    owner = build_fixture(tmp_path, provider=owner_provider)
    observer = build_fixture(tmp_path)
    command = StartRun(command_id=uuid4(), task="repair", workspace=owner.workspace)
    _, run_id_text = await owner.app._accept(command)
    run_id = UUID(run_id_text)
    await asyncio.wait_for(owner_provider.started.wait(), timeout=10)
    await observer.app._accept(command)
    owner_stream = asyncio.create_task(collect(owner.app.stream(command)))
    observer_stream = asyncio.create_task(collect(observer.app.stream(command)))
    with owner.database.session() as session:
        receipt = session.get(ApplicationCommandReceiptRow, str(command.command_id))
        assert receipt is not None and receipt.status == "IN_PROGRESS"
    assert observer.provider.requests == []
    release.set()
    owner_events, observer_events = await asyncio.wait_for(
        asyncio.gather(owner_stream, observer_stream), timeout=10
    )
    assert [event.cursor for event in owner_events] == [  # type: ignore[attr-defined]
        event.cursor for event in observer_events  # type: ignore[attr-defined]
    ]
    # One product driver performs the main turn and final-verification turn;
    # the observer's provider remains entirely unused.
    assert len(owner_provider.requests) == 2
    with owner.database.session() as session:
        assert session.scalar(select(func.count(RunRow.run_id))) == 1
        started = session.scalars(
            select(EventRow).where(
                EventRow.run_id == str(run_id),
                EventRow.event_type == "RUN_STARTED",
            )
        ).all()
        assert len(started) == 1
        receipt = session.get(ApplicationCommandReceiptRow, str(command.command_id))
        # The provider has one response; the product correction turn therefore
        # reaches a durable failure, which closes Start consistently for both
        # applications.
        assert receipt is not None and receipt.status == "FAILED"


@pytest.mark.asyncio
@pytest.mark.parametrize("prior_status", ["CREATED", "RUNNING"])
async def test_start_observer_takes_over_an_expired_owner_without_replaying_running_work(
    tmp_path: Path, prior_status: str
) -> None:
    fixture = build_fixture(tmp_path)
    command = StartRun(command_id=uuid4(), task="repair", workspace=fixture.workspace)
    prepared = fixture.app._start_assembler.prepare(command)
    created = fixture.app._creation.create(
        prepared.command, prepared_workspace=prepared.workspace
    )
    if prior_status == "RUNNING":
        with fixture.database.session() as session:
            row = session.get(RunRow, str(created.run_id))
            assert row is not None
            row.status = "RUNNING"
    RunLeaseStore(fixture.database).acquire(
        created.run_id, owner_id="abandoned-start", ttl=timedelta(milliseconds=100)
    )

    await asyncio.wait_for(collect(fixture.app.stream(command)), timeout=10)

    with fixture.database.session() as session:
        receipt = session.get(ApplicationCommandReceiptRow, str(command.command_id))
        assert receipt is not None and receipt.status in {"COMPLETED", "FAILED", "INDETERMINATE"}
        if prior_status == "RUNNING":
            assert receipt.status == "INDETERMINATE"
    assert RunLeaseStore(fixture.database).current(created.run_id) is None
    if prior_status == "RUNNING":
        assert fixture.provider.requests == []
    else:
        # One adopted CREATED run drives the normal model + correction path;
        # no second observer may replay it.
        assert len(fixture.provider.requests) == 2


def test_each_agent_application_has_a_distinct_process_instance_identity(
    tmp_path: Path,
) -> None:
    first = build_fixture(tmp_path)
    second = build_fixture(tmp_path)

    assert first.app._instance_id != second.app._instance_id
    assert first.app._instance_id.version == 4
    assert second.app._instance_id.version == 4


@pytest.mark.asyncio
async def test_application_bookkeeping_is_bounded_after_many_failed_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = build_fixture(tmp_path)

    async def explode_start(*_: object, **__: object) -> object:
        raise RuntimeError("start driver failure")

    monkeypatch.setattr(fixture.app._runtime, "execute", explode_start)
    for _ in range(100):
        command = StartRun(command_id=uuid4(), task="repair", workspace=fixture.workspace)
        await fixture.app._accept(command)
        run_id = next(
            run_id
            for run_id, task in fixture.app._drivers.items()
            if not task.done()
        )
        await fixture.app._drivers[run_id]

    resume_commands = [ResumeRun(command_id=uuid4(), run_id=uuid4()) for _ in range(100)]
    for command in resume_commands:
        await fixture.app._accept(command)
        await fixture.app._resume_tasks[command.command_id]
    await asyncio.sleep(0)

    assert len(fixture.app._drivers) <= 64
    assert len(fixture.app._resume_tasks) <= 64
    assert len(fixture.app._driver_errors) <= 64
    assert len(fixture.app._command_errors) <= 64
    assert len(fixture.app._resume_locks) <= 64
    assert resume_commands[-1].command_id in fixture.app._command_errors

    await fixture.app.aclose()
    assert not fixture.app._drivers
    assert not fixture.app._resume_tasks
    assert not fixture.app._resume_locks
    assert not fixture.app._driver_errors
    assert not fixture.app._command_errors


@pytest.mark.asyncio
async def test_nonexistent_resume_stream_terminalizes_without_a_background_task_error(
    tmp_path: Path,
) -> None:
    fixture = build_fixture(tmp_path)
    command = ResumeRun(command_id=uuid4(), run_id=uuid4())

    with pytest.raises(ApplicationFailure):
        await asyncio.wait_for(collect(fixture.app.stream(command)), timeout=2)

    task = fixture.app._resume_tasks[command.command_id]
    assert task.done()
    assert task.exception() is None
    with fixture.database.session() as session:
        receipt = session.get(ApplicationCommandReceiptRow, str(command.command_id))
        assert receipt is not None and receipt.status == "FAILED"


@pytest.mark.asyncio
async def test_start_owner_failure_is_safe_and_observer_does_not_mutate_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = build_fixture(tmp_path)
    observer = build_fixture(tmp_path)
    command = StartRun(command_id=uuid4(), task="repair", workspace=owner.workspace)

    async def explode(*args: object, **kwargs: object) -> object:
        raise RuntimeError("owner secret")

    monkeypatch.setattr(owner.app._runtime, "execute", explode)
    owner_stream = owner.app.stream(command)
    created = await anext(owner_stream)
    results = await asyncio.gather(
        collect(owner_stream),
        collect(observer.app.stream(command)),
        return_exceptions=True,
    )
    assert sum(isinstance(result, ApplicationFailure) for result in results) == 1
    with owner.database.session() as session:
        receipt = session.get(ApplicationCommandReceiptRow, str(command.command_id))
        assert receipt is not None and receipt.status == "INDETERMINATE"
        assert session.scalar(select(func.count(RunRow.run_id))) == 1
    assert observer.provider.requests == []
    assert created.run_id is not None
    assert RunLeaseStore(owner.database).current(created.run_id) is None


@pytest.mark.parametrize(
    ("config_model", "profile_ids", "database_name"),
    [
        ("different-model", ("hidden", "visible"), "agentforge.db"),
        ("mock", ("visible",), "agentforge.db"),
        ("mock", ("hidden", "visible"), "different.db"),
    ],
)
def test_start_assembly_rejects_config_binding_mismatch_without_run_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    config_model: str,
    profile_ids: tuple[str, ...],
    database_name: str,
) -> None:
    fixture = build_fixture(tmp_path)
    config = ProductConfigLoader(user_root=tmp_path / "user").load(
        fixture.workspace,
        cli={
            "database_path": f".agentforge/{database_name}",
            "model": config_model,
            "profile_ids": profile_ids,
        },
    )
    assembler = ProductStartRunAssembler(
        config=config,
        workspace=fixture.workspace,
        components=fixture.app._start_assembler.components,
        profiles=fixture.app._start_assembler.profiles,
        repair_policy=fixture.app._start_assembler.repair_policy,
        model_budget=fixture.app._start_assembler.model_budget,
    )
    monkeypatch.setattr(
        "agentforge.application.run_creation.ProductWorkspaceCapture.capture",
        lambda *args, **kwargs: pytest.fail("binding mismatch reached workspace capture"),
    )
    with pytest.raises(ValueError):
        assembler.prepare(
            StartRun(command_id=uuid4(), task="repair", workspace=fixture.workspace)
        )
    with fixture.database.session() as session:
        assert session.scalar(select(func.count(RunRow.run_id))) == 0
        assert session.scalar(select(func.count(WorkspaceBaselineRow.baseline_id))) == 0


def test_start_assembly_rejects_runtime_workspace_binding_before_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = build_fixture(tmp_path)
    wrong_workspace = tmp_path / "wrong-workspace"
    wrong_workspace.mkdir()
    components = fixture.app._start_assembler.components.model_copy(
        update={"workspace": wrong_workspace.resolve()}
    )
    monkeypatch.setattr(
        "agentforge.application.run_creation.ProductWorkspaceCapture.capture",
        lambda *args, **kwargs: pytest.fail("workspace mismatch reached capture"),
    )

    with pytest.raises(ValueError, match="workspace"):
        ProductStartRunAssembler(
            config=fixture.app._start_assembler.config,
            workspace=fixture.workspace,
            components=components,
            profiles=fixture.app._start_assembler.profiles,
            repair_policy=fixture.app._start_assembler.repair_policy,
            model_budget=fixture.app._start_assembler.model_budget,
        )
    with fixture.database.session() as session:
        assert session.scalar(select(func.count(RunRow.run_id))) == 0


def test_start_assembly_rejects_mutated_live_provider_before_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = build_fixture(tmp_path)
    fixture.provider._model_id = "drifted"  # type: ignore[attr-defined]
    monkeypatch.setattr(
        "agentforge.application.run_creation.ProductWorkspaceCapture.capture",
        lambda *args, **kwargs: pytest.fail("provider drift reached capture"),
    )

    with pytest.raises(ValueError, match="provider"):
        fixture.app._start_assembler.prepare(
            StartRun(command_id=uuid4(), task="repair", workspace=fixture.workspace)
        )
    with fixture.database.session() as session:
        assert session.scalar(select(func.count(RunRow.run_id))) == 0
