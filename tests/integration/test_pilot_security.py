import sys
from pathlib import Path

import pytest
from test_pilot_runtime_factory import (
    ALLOWED_ENV,
    TASK_ROOT,
    frozen_protocol,
    prepare_attempt,
)

from agentforge.evaluation.formal_fixtures import FormalFixtureLoader
from agentforge.evaluation.pilot_factory import (
    PilotBindingMismatchError,
    PilotRuntimeFactory,
)
from agentforge.evaluation.provider_factory import MockEvaluationProviderFactory
from agentforge.persistence.database import Database
from agentforge.tools.repository.git_log import GitLogArguments
from agentforge.tools.repository.git_status import GitStatusArguments


class CountingProviderFactory(MockEvaluationProviderFactory):
    def __init__(self) -> None:
        super().__init__([])
        self.create_count = 0

    def create(self, protocol):  # type: ignore[no-untyped-def]
        self.create_count += 1
        return super().create(protocol)


def test_factory_rejects_drift_before_provider_creation(tmp_path: Path) -> None:
    database = Database.from_path(tmp_path / "drift.sqlite3")
    database.create_schema()
    providers = CountingProviderFactory()
    factory = PilotRuntimeFactory(
        database,
        providers,
        executable=Path(sys.executable),
        allowed_env=ALLOWED_ENV,
    )
    protocol = frozen_protocol(factory)
    manifest, _, _, attempt, lease = prepare_attempt(
        tmp_path,
        database,
        protocol,
    )
    drifted = protocol.model_copy(update={"tool_schema_digest": "f" * 64})

    with pytest.raises(PilotBindingMismatchError, match="protocol"):
        factory.prepare(drifted, manifest, attempt, lease)

    assert providers.create_count == 0
    database.close()


def test_factory_rejects_hidden_test_drift_before_provider_creation(
    tmp_path: Path,
) -> None:
    database = Database.from_path(tmp_path / "hidden-drift.sqlite3")
    database.create_schema()
    providers = CountingProviderFactory()
    factory = PilotRuntimeFactory(
        database,
        providers,
        executable=Path(sys.executable),
        allowed_env=ALLOWED_ENV,
    )
    protocol = frozen_protocol(factory)
    manifest, _, _, attempt, lease = prepare_attempt(
        tmp_path,
        database,
        protocol,
    )
    hidden_file = next(lease.hidden_test_root.rglob("*.py"))
    hidden_file.write_text(
        hidden_file.read_text(encoding="utf-8") + "\n# drift\n",
        encoding="utf-8",
    )

    with pytest.raises(PilotBindingMismatchError, match="hidden"):
        factory.prepare(protocol, manifest, attempt, lease)

    assert providers.create_count == 0
    database.close()


def test_model_workspace_excludes_hidden_reference_and_registration_surface(
    tmp_path: Path,
) -> None:
    database = Database.from_path(tmp_path / "security.sqlite3")
    database.create_schema()
    factory = PilotRuntimeFactory(
        database,
        MockEvaluationProviderFactory([]),
        executable=Path(sys.executable),
        allowed_env=ALLOWED_ENV,
    )
    protocol = frozen_protocol(factory)
    manifest, _, _, attempt, lease = prepare_attempt(
        tmp_path,
        database,
        protocol,
    )
    execution = factory.prepare(protocol, manifest, attempt, lease)

    assert not (lease.model_workspace / "tests" / "hidden").exists()
    assert not (lease.model_workspace / "reference").exists()
    assert not lease.hidden_test_root.is_relative_to(lease.model_workspace)
    assert "get_git_diff" not in execution.tool_names
    approved_git_tools = {"git_log", "git_status"}
    registered_git_tools = {
        name for name in execution.tool_names if "git" in name.casefold()
    }
    assert registered_git_tools == approved_git_tools
    assert not any(
        token in name
        for name in execution.tool_names
        for token in ("shell", "network", "profile", "command", "exec")
    )
    assert GitStatusArguments.model_json_schema()["properties"] == {}
    assert set(GitLogArguments.model_json_schema()["properties"]) == {"max_entries"}
    for arguments in (GitStatusArguments, GitLogArguments):
        schema = arguments.model_json_schema()
        serialized_schema = str(schema).casefold()
        assert not any(
            escape in serialized_schema for escape in ("argv", "command", "path")
        )
    assert not hasattr(execution, "profile_registry")
    assert not hasattr(execution, "register_profile")
    serialized = " ".join(
        event.model_dump_json()
        for event in execution.events.list_for_run(execution.run.run_id)
    )
    assert str(lease.hidden_test_root) not in serialized
    assert protocol.task_prompt not in serialized
    database.close()


@pytest.mark.parametrize(
    "name",
    ("OPENAI_API_KEY", "ACCESS_TOKEN", "CLIENT_SECRET", "SSH_AUTH_SOCK"),
)
def test_factory_rejects_sensitive_profile_environment(
    tmp_path: Path,
    name: str,
) -> None:
    database = Database.from_path(tmp_path / f"{name}.sqlite3")
    database.create_schema()
    factory = PilotRuntimeFactory(
        database,
        MockEvaluationProviderFactory([]),
        executable=Path(sys.executable),
        allowed_env={**ALLOWED_ENV, name: "must-not-enter-process"},
    )
    manifest = FormalFixtureLoader().load(TASK_ROOT)

    with pytest.raises(Exception, match="environment"):
        factory.inspect_manifest(manifest)
    database.close()
