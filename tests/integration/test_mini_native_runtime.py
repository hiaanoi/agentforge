from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from agentforge.application.contracts import ProfilePurpose
from agentforge.application.runtime_factory import RuntimeAssemblyRequest, RuntimeComponentFactory
from agentforge.context.models import ContextPolicy
from agentforge.domain.enums import EventType, ProcessExecutionStatus, RunStatus
from agentforge.domain.repair import BudgetProfile, RepairDifficulty, RepairTaskPolicy
from agentforge.models.base import ModelRequest, parse_model_output
from agentforge.models.domain import ModelBudget, ModelResponse
from agentforge.persistence.database import Database
from agentforge.persistence.legacy_evaluator import LegacyEvaluatorEventRepository
from agentforge.persistence.profile_trust import ProfileKernel
from agentforge.persistence.repair_workflow import RepairWorkflow
from agentforge.repair_engines.models import RepairEngineKind
from agentforge.runtime.mutations import EvaluatorOnlyUnboundSourcePolicy
from agentforge.tools.paths import WorkspacePathResolver
from agentforge.tools.testing.profiles import TestProfileDefinition as ProfileDefinition
from agentforge.tools.testing.profiles import TestProfileRegistry as ProfileRegistry


class _ScriptedProvider:
    name = "scripted"
    journal_identity = "scripted/mini-native"

    def __init__(self, target_sha: str) -> None:
        self._target_sha = target_sha
        self._responses = [
            {"type": "tool_call", "tool": "read_file", "arguments": {"path": "src/value.py"}},
            {
                "type": "tool_call",
                "tool": "edit_file",
                "arguments": {
                    "path": "src/value.py",
                    "old_text": "VALUE = 0\n",
                    "new_text": "VALUE = 1\n",
                    "expected_sha256": target_sha,
                },
            },
            {"type": "tool_call", "tool": "run_tests", "arguments": {"profile_id": "visible"}},
            {
                "type": "tool_call",
                "tool": "edit_file",
                "arguments": {
                    "path": "src/value.py",
                    "old_text": "VALUE = 1\n",
                    "new_text": "VALUE = 2\n",
                    "expected_sha256": hashlib.sha256(b"VALUE = 1\n").hexdigest(),
                },
            },
            {"type": "tool_call", "tool": "run_tests", "arguments": {"profile_id": "visible"}},
            {"type": "final", "answer": "fixed"},
        ]

    async def generate(self, request: ModelRequest) -> ModelResponse:
        del request
        return ModelResponse(
            action=parse_model_output(self._responses.pop(0)),
            provider=self.name,
            model="mini-native",
            duration_ms=0,
            attempt_count=1,
        )


def _request(tmp_path: Path) -> tuple[RuntimeAssemblyRequest, Path]:
    workspace = tmp_path / "workspace"
    (workspace / "src").mkdir(parents=True)
    (workspace / "tests").mkdir()
    (workspace / "src" / "value.py").write_bytes(b"VALUE = 0\n")
    (workspace / "tests" / "test_value.py").write_text(
        "from src.value import VALUE\n\ndef test_value():\n    assert VALUE == 2\n",
        encoding="utf-8",
    )
    for args in (
        ("init", "-q"),
        ("config", "user.email", "test@example.invalid"),
        ("config", "user.name", "Test"),
        ("add", "."),
        ("commit", "-qm", "baseline"),
    ):
        subprocess.run(("git", *args), cwd=workspace, check=True, capture_output=True)
    database = Database.from_path(workspace / ".agentforge" / "agentforge.sqlite3")
    database.create_schema()
    (workspace / ".agentforge" / "runtime-config.json").write_text(
        '{"private":true}\n', encoding="utf-8"
    )
    profiles = ProfileRegistry(WorkspacePathResolver(workspace))
    profiles.register(
        ProfileDefinition(
            profile_id="visible",
            name="visible",
            description="visible",
            executable=sys.executable,
            argv=(
                "-c",
                (
                    "from pathlib import Path; "
                    "raise SystemExit(Path('src/value.py').read_bytes() != b'VALUE = 2\\n')"
                ),
            ),
            cwd=".",
            timeout_seconds=30,
            max_output_bytes=100_000,
            profile_version=1,
            purpose=ProfilePurpose.DEVELOPMENT,
        )
    )
    verifier = tmp_path / "verifier"
    verifier.mkdir()
    profiles.register(
        ProfileDefinition(
            profile_id="hidden",
            name="hidden",
            description="hidden",
            executable=sys.executable,
            argv=("-m", "pytest", "{VERIFIER}"),
            cwd=".",
            timeout_seconds=30,
            max_output_bytes=100_000,
            profile_version=1,
            purpose=ProfilePurpose.VERIFICATION,
            verifier_root=str(verifier),
        )
    )
    kernel = ProfileKernel(database, profiles)
    for profile in profiles.list_enabled():
        kernel.trust(
            kernel.challenge(profile.profile_id, purpose=profile.purpose), command_id=uuid4()
        )
    policy = RepairTaskPolicy(
        task_id="mini-native",
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
    return RuntimeAssemblyRequest(
        database=database,
        workspace=workspace,
        provider=_ScriptedProvider(hashlib.sha256(b"VALUE = 0\n").hexdigest()),
        policy=policy,
        context_policy=ContextPolicy(system_instructions="repair"),
        model_budget=ModelBudget(max_model_requests=10, max_retries=0),
        profiles=profiles,
        events=LegacyEvaluatorEventRepository(database),
        repair_workflow=RepairWorkflow(database),
        max_output_chars=20_000,
    ), workspace


@pytest.mark.asyncio
async def test_mini_native_routes_read_write_test_and_publish_through_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, workspace = _request(tmp_path)
    components = RuntimeComponentFactory().build(
        replace(request, repair_engine=RepairEngineKind.MINI_NATIVE)
    )
    components.mutation_coordinator._source_policy = EvaluatorOnlyUnboundSourcePolicy()
    components.runtime._tools._repair_guard = None
    components.runtime._repairs = None
    run = components.runtime.create_run("repair", max_steps=10)

    waiting = await components.runtime.execute(run.run_id)

    assert waiting.status is RunStatus.WAITING_APPROVAL
    assert [
        approval.tool_name for approval in components.runtime.list_pending_approvals(run.run_id)
    ] == ["edit_file"]
    first = components.runtime.list_pending_approvals(run.run_id)[0]
    components.runtime.approve(first.approval_id)

    waiting = await components.runtime.resume(run.run_id)
    assert waiting.status is RunStatus.WAITING_APPROVAL
    assert components.runtime.list_pending_approvals(run.run_id)[0].tool_name == "run_tests"
    components.runtime.approve(components.runtime.list_pending_approvals(run.run_id)[0].approval_id)

    waiting = await components.runtime.resume(run.run_id)
    assert waiting.status is RunStatus.WAITING_APPROVAL
    assert components.runtime.list_pending_approvals(run.run_id)[0].tool_name == "edit_file"
    components.runtime.approve(components.runtime.list_pending_approvals(run.run_id)[0].approval_id)

    waiting = await components.runtime.resume(run.run_id)
    assert waiting.status is RunStatus.WAITING_APPROVAL
    assert components.runtime.list_pending_approvals(run.run_id)[0].tool_name == "run_tests"
    second_test = components.runtime.list_pending_approvals(run.run_id)[0]
    components.runtime.approve(second_test.approval_id)
    monkeypatch.setattr(
        components.test_coordinator,
        "list_executions",
        lambda run_id: (_ for _ in ()).throw(
            AssertionError(f"resume for {run_id} must use its durable snapshot verdict")
        ),
    )

    waiting = await components.runtime.resume(run.run_id)
    assert components.test_coordinator.get_for_approval(second_test.approval_id).status is (
        ProcessExecutionStatus.COMPLETED
    )
    assert waiting.status is RunStatus.WAITING_APPROVAL
    assert (
        components.runtime.list_pending_approvals(run.run_id)[0].tool_name
        == "publish_candidate_patch"
    )
    components.runtime.approve(components.runtime.list_pending_approvals(run.run_id)[0].approval_id)

    completed = await components.runtime.resume(run.run_id)
    assert completed.run_id == run.run_id
    assert completed.status is RunStatus.COMPLETED
    assert (workspace / "src" / "value.py").read_bytes() == b"VALUE = 2\n"
    assert len(components.mutation_coordinator.list_executions(run.run_id)) == 2
    events = request.events.list_for_run(run.run_id)
    assert {
        EventType.MODEL_REQUESTED,
        EventType.MODEL_RESPONDED,
        EventType.TOOL_REQUESTED,
        EventType.MUTATION_COMMITTED,
        EventType.TEST_FAILED,
        EventType.TEST_COMPLETED,
    }.issubset({event.event_type for event in events})
    model_requested = [
        event for event in events if event.event_type is EventType.MODEL_REQUESTED
    ]
    model_responded = [
        event for event in events if event.event_type is EventType.MODEL_RESPONDED
    ]
    model_dispatching = [
        event for event in events if event.event_type is EventType.MODEL_ATTEMPT_DISPATCHING
    ]
    assert len(model_requested) == len(model_dispatching) == len(model_responded) == 6
    assert all(
        requested.sequence_number < dispatching.sequence_number < responded.sequence_number
        for requested, dispatching, responded in zip(
            model_requested, model_dispatching, model_responded, strict=True
        )
    )
    assert [event.payload["model_call_id"] for event in model_requested] == [
        event.payload["model_call_id"] for event in model_responded
    ]
    assert all(
        isinstance(event.payload["request_digest"], str)
        and len(event.payload["request_digest"]) == 64
        and "repair" not in event.model_dump_json()
        for event in (*model_requested, *model_responded)
    )
    candidate = components.runtime._candidate_store.load(
        components.runtime._candidate_store.path_for(run.run_id)
    )
    assert [entry.target_path for entry in candidate.entries] == ["src/value.py"]
