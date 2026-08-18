import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from agentforge.evaluation.verified10_campaign import (
    ATTEMPT_STATUS_VALUES,
    EXPECTED_INSTANCE_IDS,
    FORBIDDEN_GENERATION_FIELDS,
    AttemptStatus,
    BenchmarkArm,
    BenchmarkAttemptRecord,
    Verified10Protocol,
    load_verified10_protocol,
    project_public_task,
    validate_public_dataset_rows,
)

ROOT = Path(__file__).parents[2]
PROTOCOL_PATH = ROOT / "evaluation" / "protocols" / "verified10-deepseek-flash-pass1.json"


def test_protocol_artifact_is_loadable_and_frozen() -> None:
    protocol = load_verified10_protocol(PROTOCOL_PATH)

    assert isinstance(protocol, Verified10Protocol)
    assert protocol.model == "deepseek-v4-flash"
    assert protocol.dataset_name == "princeton-nlp/SWE-bench_Verified"
    assert protocol.dataset_split == "test"
    assert protocol.attempts_per_instance == 1
    assert protocol.protocol_sha256 == protocol.canonical_digest()
    assert tuple(task.instance_id for task in protocol.tasks) == EXPECTED_INSTANCE_IDS
    assert protocol.generation_forbidden_fields == FORBIDDEN_GENERATION_FIELDS
    with pytest.raises(ValidationError):
        Verified10Protocol.model_validate({**protocol.model_dump(mode="json"), "unexpected": 1})
    with pytest.raises(ValidationError):
        protocol.dataset_split = "train"  # type: ignore[misc]


def test_enums_are_closed() -> None:
    assert {item.value for item in BenchmarkArm} == {"AGENTFORGE", "MINI_SWE_AGENT"}
    assert {item.value for item in AttemptStatus} == set(ATTEMPT_STATUS_VALUES)
    with pytest.raises(ValueError):
        BenchmarkArm("unknown")


def test_protocol_rejects_drifted_ids_budgets_and_generation_fields() -> None:
    payload = json.loads(PROTOCOL_PATH.read_text(encoding="utf-8"))
    payload["tasks"][0]["instance_id"] = "django__django-12419"
    with pytest.raises(ValidationError, match=r"instance|order|duplicate|frozen|selection"):
        Verified10Protocol.model_validate(payload)

    payload = json.loads(PROTOCOL_PATH.read_text(encoding="utf-8"))
    payload["agentforge_budget"]["run_steps"] = 81
    with pytest.raises(ValidationError, match=r"run_steps|80"):
        Verified10Protocol.model_validate(payload)

    payload = json.loads(PROTOCOL_PATH.read_text(encoding="utf-8"))
    payload["generation_forbidden_fields"] = ["patch"]
    with pytest.raises(ValidationError):
        Verified10Protocol.model_validate(payload)


@pytest.mark.parametrize(
    ("path", "replacement"),
    [
        (("tasks", 0, "selection_rank"), "a" * 64),
        (("tasks", 0, "public_task_sha256"), "b" * 64),
        (("dataset_fingerprint",), "another-public-fingerprint"),
        (("source_selection_sha256",), "c" * 64),
    ],
)
def test_protocol_rejects_legal_but_drifted_frozen_metadata(
    path: tuple[object, ...], replacement: str
) -> None:
    payload = json.loads(PROTOCOL_PATH.read_text(encoding="utf-8"))
    target: object = payload
    for key in path[:-1]:
        target = target[key]  # type: ignore[index]
    target[path[-1]] = replacement  # type: ignore[index]

    with pytest.raises(ValidationError):
        Verified10Protocol.model_validate(payload)


def test_protocol_rejects_legal_but_drifted_budget_source_url() -> None:
    payload = json.loads(PROTOCOL_PATH.read_text(encoding="utf-8"))
    payload["budget_rationale"][0]["source_url"] = "https://example.com/another-source"

    with pytest.raises(ValidationError):
        Verified10Protocol.model_validate(payload)


def test_public_projection_rejects_private_generation_fields() -> None:
    row = {
        "instance_id": EXPECTED_INSTANCE_IDS[0],
        "repo": "scikit-learn/scikit-learn",
        "base_commit": "1c8668b0a021832386470ddf740d834e02c66f69",
        "problem_statement": "public issue",
        "patch": "private",
    }
    with pytest.raises(ValueError, match=r"forbidden|private"):
        project_public_task(row)


def test_runtime_rows_are_checked_against_frozen_public_hashes() -> None:
    protocol = load_verified10_protocol(PROTOCOL_PATH)
    rows = [
        {
            "instance_id": task.instance_id,
            "repo": task.repo,
            "base_commit": task.base_commit,
            "problem_statement": f"issue {task.instance_id}",
        }
        for task in protocol.tasks
    ]
    rows[0]["problem_statement"] = "different public issue"
    with pytest.raises(ValueError, match=r"hash|mismatch"):
        validate_public_dataset_rows(rows, protocol)


def test_attempt_record_is_frozen_and_requires_one_based_attempt() -> None:
    protocol = load_verified10_protocol(PROTOCOL_PATH)
    record = BenchmarkAttemptRecord(
        protocol_sha256=protocol.protocol_sha256,
        arm=BenchmarkArm.AGENTFORGE,
        instance_id=EXPECTED_INSTANCE_IDS[0],
        attempt_index=1,
        status=AttemptStatus.PLANNED,
    )
    assert record.attempt_index == 1
    with pytest.raises(ValidationError):
        BenchmarkAttemptRecord(
            protocol_sha256=protocol.protocol_sha256,
            arm="AGENTFORGE",
            instance_id=EXPECTED_INSTANCE_IDS[0],
            attempt_index=2,
            status="PLANNED",
        )
