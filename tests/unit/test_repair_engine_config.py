import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

from agentforge.application.bootstrap import ProductRuntimeDefinitionLoader
from agentforge.application.config import ProductConfigLoader
from agentforge.domain.enums import EventType, RunStatus
from agentforge.domain.models import Run
from agentforge.evaluation.verified10_support import agentforge_config, agentforge_runtime
from agentforge.repair_engines.models import RepairEngineKind
from agentforge.runtime.engine import AgentRuntime


def _load_runtime(tmp_path: Path, *, engine: str):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = workspace / ".agentforge"
    state.mkdir()
    verifier = tmp_path / "verifier"
    verifier.mkdir()
    runtime = agentforge_runtime("repair-engine-test", verifier).replace(
        "[provider]", f'repair_engine = "{engine}"\n\n[provider]', 1
    )
    (state / "config.toml").write_text(agentforge_config(), encoding="utf-8")
    (state / "runtime.toml").write_text(runtime, encoding="utf-8")
    config = ProductConfigLoader(user_root=tmp_path / "user").load(
        workspace,
        cli={
            "database_path": ".agentforge/agentforge.db",
            "model": "deepseek-v4-flash",
            "max_steps": 80,
            "profile_ids": ("compile", "default", "verify"),
        },
    )
    return ProductRuntimeDefinitionLoader().load(workspace, config=config)


def test_runtime_definition_selects_mini_linear_engine(tmp_path: Path) -> None:
    definition = _load_runtime(tmp_path, engine="mini_linear")

    assert definition.repair_engine.value == "mini_linear"


def test_runtime_definition_accepts_mini_native_engine(tmp_path: Path) -> None:
    definition = _load_runtime(tmp_path, engine="mini_native")

    assert definition.repair_engine is RepairEngineKind.MINI_NATIVE
    assert definition.repair_engine is not RepairEngineKind.MINI_LINEAR


def test_mini_native_dispatches_to_wired_loop() -> None:
    run_id = uuid4()
    run = Run(run_id=run_id, task="repair with mini-native")
    authority = SimpleNamespace(run_id=run_id)
    ownership = SimpleNamespace(authority=authority)
    get = Mock(return_value=run)
    save = Mock()
    append_event = Mock()
    run_mini_native = AsyncMock(return_value=run)
    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime._repair_engine = RepairEngineKind.MINI_NATIVE
    runtime._runs = SimpleNamespace(get=get, save=save)
    runtime._append_event = append_event
    runtime._run_mini_native = run_mini_native

    dispatched = asyncio.run(runtime._execute_owned(run_id, ownership=ownership))

    assert dispatched is run
    assert run.status is RunStatus.RUNNING
    get.assert_called_once_with(run_id)
    save.assert_called_once_with(run, authority=authority)
    append_event.assert_called_once_with(ownership, run_id, EventType.RUN_STARTED)
    run_mini_native.assert_awaited_once_with(ownership, run)
