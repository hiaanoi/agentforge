import asyncio
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agentforge.application.bootstrap import ProductRuntimeDefinitionLoader
from agentforge.application.config import ProductConfigLoader
from agentforge.domain.enums import RunStatus
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


def test_mini_native_does_not_fall_through_to_native_loop() -> None:
    run_id = uuid4()
    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime._repair_engine = RepairEngineKind.MINI_NATIVE
    runtime._runs = SimpleNamespace(
        get=lambda requested_run_id: SimpleNamespace(
            run_id=requested_run_id,
            status=RunStatus.CREATED,
        )
    )

    with pytest.raises(RuntimeError, match="registered but not wired yet"):
        asyncio.run(runtime._execute_owned(run_id, ownership=None))
