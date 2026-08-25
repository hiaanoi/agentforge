from pathlib import Path

from agentforge.application.bootstrap import ProductRuntimeDefinitionLoader
from agentforge.application.config import ProductConfigLoader
from agentforge.evaluation.verified10_support import agentforge_config, agentforge_runtime
from agentforge.repair_engines.models import RepairEngineKind


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
