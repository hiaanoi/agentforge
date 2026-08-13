import hashlib
import sys
from pathlib import Path

import pytest

from agentforge.evaluation.protocol import (
    ContextPolicyBinding,
    EvaluationProtocol,
    ModelBudgetBinding,
    PlatformBinding,
    ProviderBinding,
    ReplacementPolicy,
)
from agentforge.evaluation.protocol_persistence import (
    EvaluationProtocolRepository,
    ProtocolConflictError,
)
from agentforge.persistence.database import Database

SHA = "a" * 64


def protocol(
    *,
    name: str = "offline-protocol",
    task_prompt: str = "Repair it.",
) -> EvaluationProtocol:
    executable = Path(sys.executable).resolve()
    system_prompt = "Use only the constrained repair tools."
    return EvaluationProtocol(
        protocol_name=name,
        execution_mode="OFFLINE_TEST",
        task_id="self-durable-double-consumption",
        fixture_registry_digest=SHA,
        fixture_asset_digest=SHA,
        expected_baseline_fingerprint_digest=SHA,
        task_policy_digest=SHA,
        test_profile_template_digest=SHA,
        provider_binding=ProviderBinding(
            provider="mock",
            model_id="deterministic-model",
            timeout_seconds=30,
            max_retries=1,
            store=False,
            max_output_tokens=2_000,
            multi_tool_response_policy="SEQUENTIAL_READ_ONLY",
            max_function_calls_per_response=8,
        ),
        model_budget=ModelBudgetBinding(
            max_model_requests=10,
            max_retries=1,
            max_total_tokens=50_000,
        ),
        system_prompt_version=1,
        system_prompt=system_prompt,
        task_prompt=task_prompt,
        tool_schema_digest=SHA,
        context_policy=ContextPolicyBinding(
            version="1",
            system_prompt_version="1",
            system_instructions=system_prompt,
        ),
        completion_correction_mode="DEFAULT",
        repetition_count=3,
        replacement_policy=ReplacementPolicy(
            max_replacements_per_slot=1,
            replaceable_failure_categories=("MODEL_TIMEOUT",),
        ),
        platform_binding=PlatformBinding(
            os_family="WINDOWS" if sys.platform == "win32" else "POSIX",
            python_implementation=sys.implementation.name,
            python_version=".".join(str(item) for item in sys.version_info[:3]),
            executable_path=str(executable),
            executable_sha256=hashlib.sha256(executable.read_bytes()).hexdigest(),
        ),
        real_model_authorized=False,
    )


def test_protocol_repository_round_trips_and_registers_idempotently(
    tmp_path: Path,
) -> None:
    database = Database.from_path(tmp_path / "protocol.sqlite3")
    database.create_schema()
    repository = EvaluationProtocolRepository(database)
    value = protocol()

    assert repository.register(value) == value
    assert repository.register(value) == value
    assert repository.get(value.protocol_digest) == value
    assert repository.get_by_name(value.protocol_name) == value

    events = repository.list_audit_events(value.protocol_digest)
    assert len(events) == 1
    assert events[0].event_type == "EVALUATION_PROTOCOL_REGISTERED"
    assert events[0].payload == {
        "protocol_digest": value.protocol_digest,
        "protocol_name": value.protocol_name,
        "schema_version": 1,
        "task_id": value.task_id,
        "execution_mode": "OFFLINE_TEST",
        "provider": "mock",
        "model_id": "deterministic-model",
        "fixture_registry_digest": SHA,
        "fixture_asset_digest": SHA,
    }


def test_protocol_name_cannot_be_rebound_to_different_facts(tmp_path: Path) -> None:
    database = Database.from_path(tmp_path / "protocol.sqlite3")
    database.create_schema()
    repository = EvaluationProtocolRepository(database)
    repository.register(protocol())

    with pytest.raises(ProtocolConflictError, match="name"):
        repository.register(protocol(task_prompt="A different frozen prompt."))


def test_protocol_audit_and_storage_do_not_contain_credentials(tmp_path: Path) -> None:
    database = Database.from_path(tmp_path / "protocol.sqlite3")
    database.create_schema()
    repository = EvaluationProtocolRepository(database)
    value = repository.register(protocol())

    serialized = " ".join(
        [
            value.model_dump_json(),
            *(
                event.model_dump_json()
                for event in repository.list_audit_events(value.protocol_digest)
            ),
        ]
    ).casefold()

    assert "api_key" not in serialized
    assert "openai_api_key" not in serialized
    assert "must-not-enter-protocol" not in serialized
