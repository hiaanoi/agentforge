from __future__ import annotations

import ast
import inspect
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from agentforge.models.executor import ModelExecutor
from agentforge.persistence import (
    approval_workflow,
    model_workflow,
    mutation_workflow,
    repair_workflow,
    repositories,
    source_revisions,
    test_execution_workflow,
)
from agentforge.persistence.event_log import EventLog
from agentforge.runtime.engine import AgentRuntime
from agentforge.runtime.test_execution import (
    TestExecutionCoordinator as _TestExecutionCoordinator,
)
from agentforge.tools.executor import ToolExecutor

_OWNED_SURFACES: tuple[tuple[type[Any], tuple[str, ...]], ...] = (
    (repositories.RunRepository, ("save",)),
    (repositories.CheckpointRepository, ("save",)),
    (
        approval_workflow.ApprovalWorkflow,
        (
            "pause_for_approval",
            "claim_resume",
            "persist_consumed",
            "claim_consumed_continuation",
            "mark_indeterminate",
            "cancel",
        ),
    ),
    (
        mutation_workflow.MutationWorkflow,
        (
            "ensure_prepared",
            "claim_resume",
            "mark_committed",
            "mark_failed",
            "mark_indeterminate",
            "recover_writing",
        ),
    ),
    (
        test_execution_workflow.TestExecutionWorkflow,
        (
            "ensure_created",
            "claim_resume",
            "finish",
            "fail_before_start",
            "mark_indeterminate",
            "finalize_control_cancel",
        ),
    ),
    (
        model_workflow.ModelWorkflow,
        (
            "prepare_attempt",
            "claim_dispatch",
            "recover_attempt",
            "complete_attempt",
            "fail_attempt",
            "record_retry",
            "record_provider_deviation",
        ),
    ),
    (
        repair_workflow.RepairWorkflow,
        (
            "consume_budget",
            "record_policy_violation",
            "mark_final_answer_received",
            "observe_mutation",
            "observe_test",
            "record_diff_validation",
            "record_completion_correction",
            "mark_pending_final_verification",
            "transition_terminal",
        ),
    ),
    (source_revisions.SourceRevisionStore, ("require_actual", "advance")),
)


def _method_node(owner: type[Any], name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    source = inspect.getsource(inspect.getmodule(owner))
    tree = ast.parse(source)
    class_node = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == owner.__name__
    )
    return next(
        node
        for node in class_node.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    )


@pytest.mark.parametrize(
    ("owner", "method_name"),
    [
        (owner, method_name)
        for owner, method_names in _OWNED_SURFACES
        for method_name in method_names
    ],
)
def test_every_owned_write_requires_explicit_run_authority(
    owner: type[Any], method_name: str
) -> None:
    parameter = inspect.signature(getattr(owner, method_name)).parameters["authority"]

    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is inspect.Parameter.empty
    assert "RunLeaseAuthority" in str(parameter.annotation)
    assert "None" not in str(parameter.annotation)


@pytest.mark.parametrize(
    ("owner", "method_name"),
    [
        (owner, method_name)
        for owner, method_names in _OWNED_SURFACES
        for method_name in method_names
    ],
)
def test_owned_write_neither_reads_ambient_authority_nor_acquires_a_lease(
    owner: type[Any], method_name: str
) -> None:
    method = _method_node(owner, method_name)
    calls = {ast.unparse(node.func) for node in ast.walk(method) if isinstance(node, ast.Call)}

    assert not any(name.endswith("current_run_authority") for name in calls)
    assert not any(name.endswith((".acquire", ".acquire_in_session")) for name in calls)


def test_event_repository_is_read_only_and_event_log_owns_production_writes() -> None:
    assert not hasattr(repositories.EventRepository, "append")
    assert not hasattr(repositories.EventRepository, "append_with_bound_authority")

    authority = inspect.signature(EventLog.append).parameters["authority"]
    assert authority.default is inspect.Parameter.empty


@pytest.mark.parametrize(
    "module",
    (
        repositories,
        approval_workflow,
        mutation_workflow,
        test_execution_workflow,
        model_workflow,
        repair_workflow,
        source_revisions,
    ),
)
def test_owned_modules_have_no_optional_or_ambient_run_authority(
    module: ModuleType,
) -> None:
    source = inspect.getsource(module)

    assert "RunLeaseAuthority | None" not in source
    assert "current_run_authority" not in source


def test_product_application_does_not_import_evaluator_only_adapters() -> None:
    root = Path("src/agentforge/application")
    for path in root.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert "legacy_evaluator" not in source
        assert "LegacyEvaluator" not in source
        # The shared factory has one private evaluator construction seam for
        # PilotRuntimeFactory; its public build path is product-only.
        if path.name == "runtime_factory.py":
            assert source.count("_evaluator_only_build") == 1
        else:
            assert "_evaluator_only_" not in source


def test_production_has_no_ambient_run_authority() -> None:
    root = Path("src/agentforge")
    forbidden = ("ContextVar", "current_run_authority", "bind_run_authority")
    violations = {
        path: token
        for path in root.rglob("*.py")
        for token in forbidden
        if token in path.read_text(encoding="utf-8")
    }

    assert violations == {}


def test_runtime_does_not_infer_resume_phase_from_optional_bindings() -> None:
    method = _method_node(AgentRuntime, "_resume_command")
    calls = {ast.unparse(node.func) for node in ast.walk(method) if isinstance(node, ast.Call)}

    assert not any(name.endswith("binding_for_approval") for name in calls)


@pytest.mark.parametrize(
    ("module", "function_names"),
    (
        (
            __import__("agentforge.models.executor", fromlist=["dummy"]),
            {"generate"},
        ),
        (
            __import__("agentforge.runtime.test_execution", fromlist=["dummy"]),
            {"execute_approved"},
        ),
        (
            __import__("agentforge.tools.executor", fromlist=["dummy"]),
            {"execute", "execute_managed"},
        ),
    ),
)
def test_async_owned_components_require_live_ownership(
    module: ModuleType,
    function_names: set[str],
) -> None:
    tree = ast.parse(inspect.getsource(module))
    functions = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name in function_names
    }

    assert set(functions) == function_names
    for function in functions.values():
        parameters = {argument.arg for argument in function.args.args + function.args.kwonlyargs}
        assert "ownership" in parameters
        assert "authority" not in parameters


@pytest.mark.parametrize(
    ("owner", "method_name"),
    (
        (ModelExecutor, "generate"),
        (_TestExecutionCoordinator, "execute_approved"),
        (ToolExecutor, "execute"),
        (ToolExecutor, "execute_managed"),
    ),
)
def test_async_owned_components_never_write_with_a_pre_await_authority_snapshot(
    owner: type[Any], method_name: str
) -> None:
    method = _method_node(owner, method_name)
    snapshots: dict[str, int] = {}
    for node in ast.walk(method):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Attribute)
            and isinstance(node.value.value, ast.Name)
            and node.value.value.id == "ownership"
            and node.value.attr == "authority"
        ):
            snapshots[node.targets[0].id] = node.lineno

    await_lines = [node.lineno for node in ast.walk(method) if isinstance(node, ast.Await)]
    stale_writes = [
        (node.lineno, keyword.value.id)
        for node in ast.walk(method)
        if isinstance(node, ast.Call)
        for keyword in node.keywords
        if keyword.arg == "authority"
        and isinstance(keyword.value, ast.Name)
        and keyword.value.id in snapshots
        and any(
            snapshots[keyword.value.id] < await_line < node.lineno for await_line in await_lines
        )
    ]

    assert stale_writes == []
