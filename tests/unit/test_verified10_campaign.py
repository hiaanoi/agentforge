import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from pydantic import ValidationError

import agentforge.evaluation.verified10_campaign as campaign_module
from agentforge.application.bootstrap import ProductRuntimeDefinitionLoader
from agentforge.application.config import ProductConfigLoader
from agentforge.evaluation.swebench_prediction import SWEbenchInstanceBinding, SWEbenchPrediction
from agentforge.evaluation.verified10_campaign import (
    ATTEMPT_STATUS_VALUES,
    EXPECTED_INSTANCE_IDS,
    FORBIDDEN_GENERATION_FIELDS,
    AttemptFailureClass,
    AttemptStatus,
    BenchmarkArm,
    BenchmarkAttemptRecord,
    CampaignArtifactError,
    CampaignExecutionError,
    Verified10Protocol,
    Verified10ProtocolError,
    Verified10Task,
    canonical_digest,
    finalize_verified10_campaign,
    load_attempt_ledger,
    load_verified10_protocol,
    project_public_task,
    public_task_sha256,
    save_attempt_ledger,
    validate_public_task,
)
from agentforge.evaluation.verified10_runner import (
    CampaignCommand,
    DockerImageBinding,
    MiniSourceVerifier,
    Verified10Campaign,
    select_and_validate_public_rows,
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


def test_protocol_digest_has_independent_known_encoding_and_mutation_sensitivity() -> None:
    protocol = load_verified10_protocol(PROTOCOL_PATH)
    payload = protocol.model_dump(mode="json")
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )
    expected = hashlib.sha256(encoded).hexdigest()
    assert expected == "d57db5029157ff9eea5f722c8977834ff98e7facd24eec7470e7fbcb48e3d231"
    assert protocol.protocol_sha256 == expected
    reordered = {key: payload[key] for key in reversed(tuple(payload))}
    assert (
        hashlib.sha256(
            json.dumps(
                reordered, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode()
        ).hexdigest()
        == expected
    )
    mutated = json.loads(json.dumps(payload))
    mutated["mini_budget"]["step_limit"] = 51
    assert canonical_digest(mutated) != expected


def test_prior_artifact_mapping_is_explicitly_checked_in_the_test() -> None:
    protocol = load_verified10_protocol(PROTOCOL_PATH)
    assert protocol.prior_baseline.artifacts.model_dump(mode="json") == {
        "selection_sha256": "9c385f13580c05e3cb5590e2abb43b278fa8ed99597315d7010f6953785ca9c0",
        "experiment_protocol_sha256": (
            "b511f1c6c7974f7b36ed1eb4ba9ee7435a6dcf21211b7ed5e879fe9fcc06c62a"
        ),
        "agentforge_predictions_sha256": (
            "2e98e29e37f5c42598fe055c1db5972a44bec9d5d68ad3721cf9e00c40efaf48"
        ),
        "mini_swe_agent_predictions_sha256": (
            "42fcc0377abdb3538a12ea814a862b9ddd46dfb69fef544058a817134373e1fb"
        ),
        "agentforge_official_report_sha256": (
            "a9c19d540f1170d9026161c4bdf997c1b487bd000208f3fe8b2044b5ddea0243"
        ),
        "mini_swe_agent_official_report_sha256": (
            "9d6274d5ad446dde3cf000276466da8a00bd0203e01fffdc8ebe7a7a0ca48c9f"
        ),
    }


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


def test_protocol_rejects_legal_but_wrong_prior_artifact_sha256() -> None:
    payload = json.loads(PROTOCOL_PATH.read_text(encoding="utf-8"))
    payload["prior_baseline"]["artifacts"]["selection_sha256"] = "d" * 64

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


def test_public_hash_runtime_positive_control_then_single_field_mutation() -> None:
    row = {
        "instance_id": "django__django-12419",
        "repo": "django/django",
        "base_commit": "7fa1a93c6c8109010a6ff3f604fda83b604e0e97",
        "problem_statement": "public issue",
    }
    assert public_task_sha256(**row) == (
        "917d1e57ab2b7527a08df04522802a4d917facade27965b9b6a110534b6000be"
    )
    task = Verified10Task(
        instance_id=row["instance_id"],
        repo=row["repo"],
        base_commit=row["base_commit"],
        public_task_sha256="917d1e57ab2b7527a08df04522802a4d917facade27965b9b6a110534b6000be",
    )
    assert validate_public_task(row, task) == row
    row["problem_statement"] = "mutated public issue"
    with pytest.raises(ValueError, match=r"hash|mismatch"):
        validate_public_task(row, task)


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
            arm=BenchmarkArm.AGENTFORGE,
            instance_id=EXPECTED_INSTANCE_IDS[0],
            attempt_index=2,
            status=AttemptStatus.PLANNED,
        )
    with pytest.raises(ValidationError):
        record.status = AttemptStatus.COMPLETED  # type: ignore[misc]


def test_enum_attempt_record_json_roundtrip() -> None:
    protocol = load_verified10_protocol(PROTOCOL_PATH)
    record = BenchmarkAttemptRecord(
        protocol_sha256=protocol.protocol_sha256,
        arm=BenchmarkArm.MINI_SWE_AGENT,
        instance_id=EXPECTED_INSTANCE_IDS[0],
        attempt_index=1,
        status=AttemptStatus.COMPLETED,
    )
    assert BenchmarkAttemptRecord.model_validate_json(record.model_dump_json()) == record


def test_attempt_record_carries_typed_failure_and_private_telemetry() -> None:
    protocol = load_verified10_protocol(PROTOCOL_PATH)
    record = BenchmarkAttemptRecord(
        protocol_sha256=protocol.protocol_sha256,
        arm=BenchmarkArm.AGENTFORGE,
        instance_id=EXPECTED_INSTANCE_IDS[0],
        attempt_index=1,
        status=AttemptStatus.FAILED,
        failure_class=AttemptFailureClass.TIMEOUT,
        model_calls=3,
        steps=8,
        provider_prompt_tokens=10,
        provider_completion_tokens=20,
        approval_count=1,
        edit_count=2,
        test_count=3,
        wall_time_seconds=4.5,
        trajectory_path="trajectories/one.json",
        trajectory_sha256="a" * 64,
    )
    assert record.failure_class is AttemptFailureClass.TIMEOUT
    assert record.trajectory_path == "trajectories/one.json"
    assert BenchmarkAttemptRecord.model_validate_json(record.model_dump_json()) == record


def test_attempt_ledger_save_load_roundtrip(tmp_path: Path) -> None:
    protocol = load_verified10_protocol(PROTOCOL_PATH)
    record = BenchmarkAttemptRecord(
        protocol_sha256=protocol.protocol_sha256,
        arm=BenchmarkArm.AGENTFORGE,
        instance_id=EXPECTED_INSTANCE_IDS[0],
        attempt_index=1,
        status=AttemptStatus.FAILED,
        failure_class=AttemptFailureClass.MODEL_FAILED,
    )
    path = tmp_path / "ledger.json"

    save_attempt_ledger(path, [record])

    assert load_attempt_ledger(path) == (record,)


def test_attempt_ledger_save_revalidates_model_copy_forgery(tmp_path: Path) -> None:
    protocol = load_verified10_protocol(PROTOCOL_PATH)
    record = BenchmarkAttemptRecord(
        protocol_sha256=protocol.protocol_sha256,
        arm=BenchmarkArm.AGENTFORGE,
        instance_id=EXPECTED_INSTANCE_IDS[0],
        attempt_index=1,
        status=AttemptStatus.FAILED,
        failure_class=AttemptFailureClass.MODEL_FAILED,
    ).model_copy(update={"model_calls": -1})

    with pytest.raises(CampaignArtifactError):
        save_attempt_ledger(tmp_path / "ledger.json", [record])


def test_attempt_record_rejects_inconsistent_status_and_failure_class() -> None:
    protocol = load_verified10_protocol(PROTOCOL_PATH)
    kwargs = {
        "protocol_sha256": protocol.protocol_sha256,
        "arm": BenchmarkArm.AGENTFORGE,
        "instance_id": EXPECTED_INSTANCE_IDS[0],
        "attempt_index": 1,
    }
    with pytest.raises(ValidationError):
        BenchmarkAttemptRecord(**kwargs, status=AttemptStatus.FAILED)
    with pytest.raises(ValidationError):
        BenchmarkAttemptRecord(
            **kwargs,
            status=AttemptStatus.PLANNED,
            failure_class=AttemptFailureClass.MODEL_FAILED,
        )
    with pytest.raises(ValidationError):
        BenchmarkAttemptRecord(
            **kwargs,
            status=AttemptStatus.RUNNING,
            trajectory_path="../../secret.json",
        )


def test_finalizer_revalidates_frozen_protocol_before_writes(tmp_path: Path) -> None:
    protocol = load_verified10_protocol(PROTOCOL_PATH)
    shrunk = protocol.model_copy(update={"tasks": protocol.tasks[:-1]})

    with pytest.raises(CampaignArtifactError):
        finalize_verified10_campaign(
            shrunk,
            [],
            [],
            tmp_path / "predictions.json",
            tmp_path / "ledger.json",
        )


def test_finalizer_exports_ten_public_rows_and_private_ledger(tmp_path: Path) -> None:
    protocol = load_verified10_protocol(PROTOCOL_PATH)
    model_identity = protocol.model
    predictions = []
    attempts = []
    failure_classes = (
        AttemptFailureClass.NONE,
        AttemptFailureClass.EMPTY,
        AttemptFailureClass.COMPATIBILITY_FAILED,
        AttemptFailureClass.MODEL_FAILED,
        AttemptFailureClass.TIMEOUT,
    )
    for index, task in enumerate(protocol.tasks):
        binding = SWEbenchInstanceBinding(
            instance_id=task.instance_id, repo=task.repo, base_commit=task.base_commit
        )
        if index == 0:
            patch = "diff --git a/x b/x\n"
            prediction = SWEbenchPrediction(
                instance_id=task.instance_id,
                model_name_or_path="agentforge:" + model_identity,
                model_patch=patch,
                base_commit=task.base_commit,
                patch_sha256=hashlib.sha256(patch.encode()).hexdigest(),
            )
            status = AttemptStatus.COMPLETED
            failure_class = AttemptFailureClass.NONE
            patch_hash = prediction.patch_sha256
        else:
            prediction = SWEbenchPrediction.empty(binding, model_identity)
            status = AttemptStatus.COMPLETED if index == 1 else AttemptStatus.FAILED
            failure_class = failure_classes[index % len(failure_classes)]
            if status is AttemptStatus.FAILED and failure_class is AttemptFailureClass.NONE:
                failure_class = AttemptFailureClass.MODEL_FAILED
            patch_hash = prediction.patch_sha256
        predictions.append(prediction)
        attempts.append(
            BenchmarkAttemptRecord(
                protocol_sha256=protocol.protocol_sha256,
                arm=BenchmarkArm.AGENTFORGE,
                instance_id=task.instance_id,
                attempt_index=1,
                status=status,
                failure_class=failure_class,
                prediction_patch_sha256=patch_hash,
                trajectory_path=f"trajectories/{index}.json",
            )
        )
    result = finalize_verified10_campaign(
        protocol,
        list(reversed(attempts)),
        list(reversed(predictions)),
        tmp_path / "predictions.json",
        tmp_path / "ledger.json",
    )
    public = json.loads((tmp_path / "predictions.json").read_text())
    ledger = json.loads((tmp_path / "ledger.json").read_text())
    assert [row["instance_id"] for row in public] == list(EXPECTED_INSTANCE_IDS)
    assert [row["instance_id"] for row in ledger] == list(EXPECTED_INSTANCE_IDS)
    assert len(ledger) == 10
    assert all(set(row) == {"instance_id", "model_name_or_path", "model_patch"} for row in public)
    assert (
        result.predictions_sha256
        == hashlib.sha256((tmp_path / "predictions.json").read_bytes()).hexdigest()
    )
    assert (
        result.ledger_sha256 == hashlib.sha256((tmp_path / "ledger.json").read_bytes()).hexdigest()
    )
    with pytest.raises(CampaignArtifactError, match="exist"):
        finalize_verified10_campaign(
            protocol,
            attempts,
            predictions,
            tmp_path / "predictions.json",
            tmp_path / "ledger.json",
        )
    with pytest.raises(CampaignArtifactError, match="same"):
        finalize_verified10_campaign(
            protocol,
            attempts,
            predictions,
            tmp_path / "same.json",
            tmp_path / "same.json",
        )


def test_finalizer_preflights_ledger_parent_before_public_publish(tmp_path: Path) -> None:
    protocol = load_verified10_protocol(PROTOCOL_PATH)
    predictions = []
    attempts = []
    for task in protocol.tasks:
        binding = SWEbenchInstanceBinding(
            instance_id=task.instance_id, repo=task.repo, base_commit=task.base_commit
        )
        prediction = SWEbenchPrediction.empty(binding, protocol.model)
        predictions.append(prediction)
        attempts.append(
            BenchmarkAttemptRecord(
                protocol_sha256=protocol.protocol_sha256,
                arm=BenchmarkArm.AGENTFORGE,
                instance_id=task.instance_id,
                attempt_index=1,
                status=AttemptStatus.FAILED,
                failure_class=AttemptFailureClass.MODEL_FAILED,
                prediction_patch_sha256=prediction.patch_sha256,
            )
        )
    blocked_parent = tmp_path / "blocked-parent"
    blocked_parent.write_text("not a directory", encoding="utf-8")
    public_path = tmp_path / "public.json"
    with pytest.raises(CampaignArtifactError):
        finalize_verified10_campaign(
            protocol,
            attempts,
            predictions,
            public_path,
            blocked_parent / "ledger.json",
        )
    assert not public_path.exists()
    assert not public_path.with_name(f".{public_path.name}.tmp").exists()

    locked_public = tmp_path / "locked-public.json"
    lock_path = locked_public.with_name(f".{locked_public.name}.finalize.lock")
    lock_path.write_text("stale", encoding="utf-8")
    with pytest.raises(CampaignArtifactError, match=r"lock|progress"):
        finalize_verified10_campaign(
            protocol,
            attempts,
            predictions,
            locked_public,
            tmp_path / "locked-ledger.json",
        )
    assert not locked_public.exists()
    assert not (tmp_path / "locked-ledger.json").exists()


@pytest.mark.parametrize("conflict", ["public", "ledger"])
def test_concurrent_finalizers_have_one_publication_winner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, conflict: str
) -> None:
    protocol = load_verified10_protocol(PROTOCOL_PATH)
    predictions = []
    attempts = []
    for task in protocol.tasks:
        binding = SWEbenchInstanceBinding(
            instance_id=task.instance_id, repo=task.repo, base_commit=task.base_commit
        )
        prediction = SWEbenchPrediction.empty(binding, protocol.model)
        predictions.append(prediction)
        attempts.append(
            BenchmarkAttemptRecord(
                protocol_sha256=protocol.protocol_sha256,
                arm=BenchmarkArm.AGENTFORGE,
                instance_id=task.instance_id,
                attempt_index=1,
                status=AttemptStatus.FAILED,
                failure_class=AttemptFailureClass.MODEL_FAILED,
                prediction_patch_sha256=prediction.patch_sha256,
            )
        )
    public_paths = (
        [tmp_path / "concurrent.json"] * 2
        if conflict == "public"
        else [tmp_path / "concurrent-0.json", tmp_path / "concurrent-1.json"]
    )
    ledger_paths = (
        [tmp_path / "concurrent-ledger-0.json", tmp_path / "concurrent-ledger-1.json"]
        if conflict == "public"
        else [tmp_path / "concurrent-ledger.json"] * 2
    )
    barrier = threading.Barrier(2)
    acquire = campaign_module._acquire_finalize_locks

    def synchronized_acquire(
        public_target: Path, ledger_target: Path
    ) -> tuple[tuple[Path, int], ...]:
        barrier.wait(timeout=5)
        return acquire(public_target, ledger_target)

    monkeypatch.setattr(campaign_module, "_acquire_finalize_locks", synchronized_acquire)

    def run(index: int) -> str:
        try:
            finalize_verified10_campaign(
                protocol, attempts, predictions, public_paths[index], ledger_paths[index]
            )
        except CampaignArtifactError:
            return "failed"
        return "ok"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = sorted(pool.map(run, range(2)))

    assert outcomes == ["failed", "ok"]
    assert sum(path.is_file() for path in set(public_paths)) == 1
    assert sum(path.is_file() for path in set(ledger_paths)) == 1
    assert all(
        not path.with_name(f".{path.name}.finalize.lock").exists()
        for path in set(public_paths + ledger_paths)
    )
    assert not list(tmp_path.glob(".*.tmp"))


def test_finalizer_rejects_wrong_prediction_checkout_and_model_identity(tmp_path: Path) -> None:
    protocol = load_verified10_protocol(PROTOCOL_PATH)
    predictions = []
    attempts = []
    for task in protocol.tasks:
        binding = SWEbenchInstanceBinding(
            instance_id=task.instance_id, repo=task.repo, base_commit=task.base_commit
        )
        predictions.append(SWEbenchPrediction.empty(binding, protocol.model))
        attempts.append(
            BenchmarkAttemptRecord(
                protocol_sha256=protocol.protocol_sha256,
                arm=BenchmarkArm.AGENTFORGE,
                instance_id=task.instance_id,
                attempt_index=1,
                status=AttemptStatus.FAILED,
                failure_class=AttemptFailureClass.MODEL_FAILED,
                prediction_patch_sha256=predictions[-1].patch_sha256,
            )
        )
    wrong_checkout = predictions[0].model_copy(update={"base_commit": "0" * 40})
    with pytest.raises(CampaignArtifactError, match="base commit"):
        finalize_verified10_campaign(
            protocol,
            attempts,
            [wrong_checkout, *predictions[1:]],
            tmp_path / "bad-checkout.json",
            tmp_path / "bad-checkout-ledger.json",
        )
    wrong_identity = predictions[0].model_copy(
        update={"model_name_or_path": "agentforge:wrong-model"}
    )
    with pytest.raises(CampaignArtifactError, match="identity"):
        finalize_verified10_campaign(
            protocol,
            attempts,
            [wrong_identity, *predictions[1:]],
            tmp_path / "bad-identity.json",
            tmp_path / "bad-identity-ledger.json",
        )


def test_finalizer_recomputes_forged_model_copy_patch_digest(tmp_path: Path) -> None:
    protocol = load_verified10_protocol(PROTOCOL_PATH)
    predictions = []
    attempts = []
    for task in protocol.tasks:
        binding = SWEbenchInstanceBinding(
            instance_id=task.instance_id, repo=task.repo, base_commit=task.base_commit
        )
        prediction = SWEbenchPrediction.empty(binding, protocol.model)
        predictions.append(prediction)
        attempts.append(
            BenchmarkAttemptRecord(
                protocol_sha256=protocol.protocol_sha256,
                arm=BenchmarkArm.AGENTFORGE,
                instance_id=task.instance_id,
                attempt_index=1,
                status=AttemptStatus.FAILED,
                failure_class=AttemptFailureClass.MODEL_FAILED,
                prediction_patch_sha256=prediction.patch_sha256,
            )
        )
    forged = predictions[0].model_copy(update={"model_patch": "forged"})
    with pytest.raises(CampaignArtifactError, match=r"validation|patch digest"):
        finalize_verified10_campaign(
            protocol,
            attempts,
            [forged, *predictions[1:]],
            tmp_path / "forged.json",
            tmp_path / "forged-ledger.json",
        )


def test_finalizer_rejects_nonterminal_attempts(tmp_path: Path) -> None:
    protocol = load_verified10_protocol(PROTOCOL_PATH)
    predictions = []
    attempts = []
    for task in protocol.tasks:
        binding = SWEbenchInstanceBinding(
            instance_id=task.instance_id, repo=task.repo, base_commit=task.base_commit
        )
        prediction = SWEbenchPrediction.empty(binding, protocol.model)
        predictions.append(prediction)
        attempts.append(
            BenchmarkAttemptRecord(
                protocol_sha256=protocol.protocol_sha256,
                arm=BenchmarkArm.AGENTFORGE,
                instance_id=task.instance_id,
                attempt_index=1,
                status=AttemptStatus.FAILED,
                failure_class=AttemptFailureClass.MODEL_FAILED,
                prediction_patch_sha256=prediction.patch_sha256,
            )
        )
    for status in (AttemptStatus.PLANNED, AttemptStatus.RUNNING):
        nonterminal = [attempts[0].model_copy(update={"status": status}), *attempts[1:]]
        with pytest.raises(CampaignArtifactError, match=r"validation|terminal"):
            finalize_verified10_campaign(
                protocol,
                nonterminal,
                predictions,
                tmp_path / f"{status.value}.json",
                tmp_path / f"{status.value}-ledger.json",
            )


@pytest.mark.parametrize("content", [b"\x80", b"{", b"[]", b'{"schema_version": 999}'])
def test_loader_wraps_malformed_protocol_as_domain_error(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / "bad.json"
    path.write_bytes(content)
    with pytest.raises(Verified10ProtocolError) as info:
        load_verified10_protocol(path)
    assert info.value.__cause__ is None


def test_loader_wraps_missing_and_directory_as_domain_error(tmp_path: Path) -> None:
    with pytest.raises(Verified10ProtocolError):
        load_verified10_protocol(tmp_path / "missing.json")
    directory = tmp_path / "protocol.json"
    directory.mkdir()
    with pytest.raises(Verified10ProtocolError):
        load_verified10_protocol(directory)


def test_dataset_selection_filters_noise_orders_and_validates_hashes() -> None:
    tasks = tuple(
        Verified10Task(
            instance_id=f"owner__repo-{index}",
            repo="owner/repo",
            base_commit=f"{index:040x}",
            public_task_sha256=public_task_sha256(
                instance_id=f"owner__repo-{index}",
                repo="owner/repo",
                base_commit=f"{index:040x}",
                problem_statement=f"problem {index}",
            ),
        )
        for index in range(10)
    )
    protocol = type("SyntheticProtocol", (), {"tasks": tasks})()
    rows = [
        {
            "instance_id": task.instance_id,
            "repo": task.repo,
            "base_commit": task.base_commit,
            "problem_statement": f"problem {index}",
        }
        for index, task in reversed(tuple(enumerate(tasks)))
    ]
    rows.insert(
        3,
        {
            "instance_id": "noise__repo-99",
            "repo": "noise/repo",
            "base_commit": "f" * 40,
            "problem_statement": "noise",
        },
    )

    selected = select_and_validate_public_rows(rows, protocol)  # type: ignore[arg-type]

    assert [row["instance_id"] for row in selected] == [task.instance_id for task in tasks]
    assert len(selected) == 10
    with pytest.raises(ValueError, match="missing"):
        select_and_validate_public_rows(rows[:-2], protocol)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="duplicate"):
        select_and_validate_public_rows([*rows, rows[0]], protocol)  # type: ignore[arg-type]
    corrupted = [dict(row) for row in rows]
    target = next(row for row in corrupted if row["instance_id"] == tasks[0].instance_id)
    target["problem_statement"] = "changed"
    with pytest.raises(ValueError, match="hash mismatch"):
        select_and_validate_public_rows(corrupted, protocol)  # type: ignore[arg-type]


def test_docker_binding_requires_unique_matching_repo_digest() -> None:
    digest = "a" * 64
    binding = DockerImageBinding.from_inspect(
        "docker.io/swebench/example:latest",
        json.dumps([f"docker.io/swebench/example@sha256:{digest}"]),
    )
    assert binding.digest_reference == f"docker.io/swebench/example@sha256:{digest}"
    with pytest.raises(CampaignExecutionError, match="unique"):
        DockerImageBinding.from_inspect(
            "docker.io/swebench/example:latest",
            json.dumps(
                [
                    f"docker.io/swebench/example@sha256:{digest}",
                    f"docker.io/swebench/example@sha256:{'b' * 64}",
                ]
            ),
        )
    with pytest.raises(CampaignExecutionError, match="match"):
        DockerImageBinding.from_inspect(
            "docker.io/swebench/example:latest",
            json.dumps([f"docker.io/other/example@sha256:{digest}"]),
        )


def test_campaign_command_repr_never_contains_environment_values() -> None:
    command = CampaignCommand(("mini",), environment={"OPENAI_API_KEY": "secret-value"})
    assert "secret-value" not in repr(command)
    assert command.environment_names == ("OPENAI_API_KEY",)


def test_real_mini_source_verifier_rejects_commit_and_lock_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    subprocess.run(("git", "init", "-q", str(tmp_path)), check=True)
    subprocess.run(
        ("git", "-C", str(tmp_path), "config", "user.email", "test@example.com"), check=True
    )
    subprocess.run(("git", "-C", str(tmp_path), "config", "user.name", "Test"), check=True)
    (tmp_path / "uv.lock").write_text("wrong", encoding="utf-8")
    package = tmp_path / "src" / "minisweagent"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text('__version__ = "2.4.6"\n', encoding="utf-8")
    builtin = package / "config" / "benchmarks"
    builtin.mkdir(parents=True)
    (builtin / "swebench.yaml").write_text("agent: {}\n", encoding="utf-8")
    subprocess.run(("git", "-C", str(tmp_path), "add", "."), check=True)
    subprocess.run(("git", "-C", str(tmp_path), "commit", "-qm", "fixture"), check=True)

    with pytest.raises(CampaignExecutionError, match="commit"):
        MiniSourceVerifier().verify(tmp_path)

    actual_head = subprocess.run(
        ("git", "-C", str(tmp_path), "rev-parse", "HEAD"),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    monkeypatch.setattr("agentforge.evaluation.verified10_support.MINI_COMMIT", actual_head)
    with pytest.raises(CampaignExecutionError, match=r"uv\.lock hash"):
        MiniSourceVerifier().verify(tmp_path)


def test_generated_agentforge_files_pass_product_loaders_without_secrets(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config_dir = workspace / ".agentforge"
    config_dir.mkdir()
    config_text = Verified10Campaign._agentforge_config()
    runtime_text = Verified10Campaign._agentforge_runtime(EXPECTED_INSTANCE_IDS[0])
    (config_dir / "config.toml").write_text(config_text, encoding="utf-8")
    (config_dir / "runtime.toml").write_text(runtime_text, encoding="utf-8")

    config = ProductConfigLoader(user_root=tmp_path / "empty-user").load(
        workspace,
        cli={
            "database_path": ".agentforge/agentforge.db",
            "model": "deepseek-v4-flash",
            "max_steps": 80,
            "profile_ids": ("compile", "verify"),
        },
    )
    runtime = ProductRuntimeDefinitionLoader().load(workspace, config=config)

    assert config.model == "deepseek-v4-flash"
    assert config.max_steps == 80
    assert runtime.profile_ids == ("compile", "verify")
    assert runtime.model_budget.model_dump() == {
        "max_model_requests": 52,
        "max_retries": 2,
        "max_output_tokens_per_request": 4096,
        "max_total_input_tokens": None,
        "max_total_output_tokens": None,
        "max_total_tokens": 600000,
    }
    assert "api_key" not in (config_text + runtime_text).casefold()
    assert "temperature" not in runtime_text.casefold()
