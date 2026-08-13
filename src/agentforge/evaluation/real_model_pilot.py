import argparse
import asyncio
import hashlib
import os
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from datetime import date
from decimal import Decimal
from pathlib import Path

from pydantic import SecretStr

from agentforge.domain.enums import EvaluationStudyStatus
from agentforge.evaluation.costs import PricingSnapshot
from agentforge.evaluation.environment import build_fixed_test_environment
from agentforge.evaluation.formal_fixtures import FormalFixtureLoader
from agentforge.evaluation.pilot_factory import PilotRuntimeFactory
from agentforge.evaluation.pilot_runner import PilotRunner
from agentforge.evaluation.pilot_workspace import PilotWorkspaceManager
from agentforge.evaluation.prompts import (
    BASELINE_PROMPT_VARIANT,
    INVARIANT_GUIDANCE_PROMPT_VARIANT,
    TASK_CONTRACT_GUIDANCE_PROMPT_VARIANT,
    TASK_DIAGNOSTIC_PROMPT_VARIANT,
)
from agentforge.evaluation.protocol_persistence import (
    EvaluationProtocolRepository,
)
from agentforge.evaluation.provider_factory import (
    MockEvaluationProviderFactory,
    OpenAIEvaluationProviderFactory,
)
from agentforge.evaluation.public_artifacts import PublicArtifactScanner
from agentforge.evaluation.real_model_gate import (
    RealModelExecutionGate,
    SourceProvenanceReader,
)
from agentforge.evaluation.run_manifest import (
    EvaluationRunManifest,
    EvaluationRunManifestError,
    load_evaluation_run_manifest,
    save_evaluation_run_manifest,
)
from agentforge.evaluation.source_provenance import (
    SourceProvenanceCollector,
)
from agentforge.evaluation.study_builder import EvaluationStudyBuilder
from agentforge.evaluation.study_manifest import (
    PrivateStudyManifest,
    load_private_study_manifest,
    save_private_study_manifest,
)
from agentforge.evaluation.study_models import (
    EvaluationStudy,
    RealModelAuthorization,
)
from agentforge.evaluation.study_persistence import EvaluationStudyRepository
from agentforge.evaluation.study_reports import (
    EvaluationStudyReportBuilder,
    render_evaluation_study_json,
    render_evaluation_study_markdown,
)
from agentforge.evaluation.study_runner import (
    EvaluationStudyRegistrar,
    EvaluationStudyRunner,
    StudyCampaignExecutor,
)
from agentforge.models.base import ModelProvider
from agentforge.models.domain import ModelProviderConfig
from agentforge.persistence.database import Database

_TERMINAL_STUDIES = frozenset(
    {
        EvaluationStudyStatus.COMPLETED,
        EvaluationStudyStatus.COMPLETED_WITH_INFRASTRUCTURE_GAPS,
        EvaluationStudyStatus.ABORTED_CONFIGURATION,
        EvaluationStudyStatus.INDETERMINATE,
    }
)

CampaignExecutorBuilder = Callable[
    [Database, RealModelExecutionGate, SecretStr, Path],
    StudyCampaignExecutor,
]


class RealModelPilotApplication:
    def __init__(
        self,
        repository_root: Path,
        *,
        environment: Mapping[str, str] | None = None,
        source_collector: SourceProvenanceReader | None = None,
        provider_builder: Callable[[ModelProviderConfig], ModelProvider]
        | None = None,
        campaign_executor_builder: CampaignExecutorBuilder | None = None,
        output: Callable[[str], None] = print,
    ) -> None:
        self._repository_root = repository_root.resolve(strict=True)
        self._environment = environment if environment is not None else os.environ
        self._source_collector = (
            source_collector or SourceProvenanceCollector()
        )
        self._provider_builder = provider_builder
        self._campaign_executor_builder = campaign_executor_builder
        self._output = output

    def run(self, argv: Sequence[str]) -> int:
        arguments = _parser().parse_args(list(argv))
        state_root = self._resolve_state_root(arguments.state_dir)
        if arguments.command == "prepare":
            return self._prepare(arguments, state_root)
        if arguments.command in {"run", "recover"}:
            return self._execute(
                state_root,
                confirmation_digest=arguments.confirm,
                recover=arguments.command == "recover",
                canary=False,
            )
        if arguments.command == "canary":
            return self._execute(
                state_root,
                confirmation_digest=arguments.confirm,
                recover=True,
                canary=True,
                canary_task_id=arguments.task_id,
            )
        if arguments.command == "report":
            return self._report(
                state_root,
                Path(arguments.output_dir),
            )
        raise RuntimeError("Unknown real-model Pilot command")

    def _prepare(
        self,
        arguments: argparse.Namespace,
        state_root: Path,
    ) -> int:
        state_root.mkdir(parents=True, exist_ok=True)
        database = Database.from_path(state_root / "study.sqlite3")
        database.create_schema()
        try:
            source = self._source_collector.collect(self._repository_root)
            runtime_factory = PilotRuntimeFactory(
                database,
                MockEvaluationProviderFactory([]),
                executable=Path(sys.executable),
                allowed_env=build_fixed_test_environment(
                    self._environment
                ),
                prompt_variant=arguments.prompt_variant,
            )
            pricing = _pricing_snapshot(arguments)
            build = EvaluationStudyBuilder(
                runtime_factory,
                self._repository_root / "evaluation" / "fixtures",
            ).build(
                model_id=arguments.model,
                response_model_id=arguments.response_model,
                source_provenance=source,
                pricing_snapshot=pricing,
            )
            study = EvaluationStudyRegistrar(database).register(build)
            manifest = PrivateStudyManifest.from_build(
                study.study_id,
                build,
            )
            save_private_study_manifest(
                state_root / "study_manifest.json",
                manifest,
            )
            save_evaluation_run_manifest(
                state_root / "run_manifest.json",
                EvaluationRunManifest.from_build(
                    study.study_id,
                    build,
                    source,
                ),
            )
        finally:
            database.close()
        self._output(f"study_definition_digest={manifest.definition_digest}")
        self._output(
            "private_manifest="
            + _display_path(
                self._repository_root,
                state_root / "study_manifest.json",
            )
        )
        return 0

    def _execute(
        self,
        state_root: Path,
        *,
        confirmation_digest: str,
        recover: bool,
        canary: bool,
        canary_task_id: str | None = None,
    ) -> int:
        manifest, database = self._load_state(state_root)
        try:
            studies = EvaluationStudyRepository(database)
            study = studies.get_study(manifest.study_id)
            definition = studies.get_definition(manifest.study_id)
            if definition.definition_digest != manifest.definition_digest:
                raise RuntimeError("Study definition does not match manifest")
            authorization = RealModelAuthorization(
                study_definition_digest=definition.definition_digest,
                protocol_digests=definition.protocol_digests,
                model_id=definition.model_id,
                response_model_id=definition.response_model_id,
                network_access_acknowledged=True,
            )
            if study.status is EvaluationStudyStatus.DRAFT:
                gate_study = EvaluationStudy.model_validate(
                    {
                        **study.model_dump(mode="json"),
                        "authorization_digest": (
                            authorization.authorization_digest
                        ),
                        "status": EvaluationStudyStatus.AUTHORIZED.value,
                        "record_version": study.record_version + 1,
                    }
                )
            else:
                persisted = studies.get_authorization(study.study_id)
                if persisted != authorization:
                    raise RuntimeError(
                        "Persisted real-model authorization has drifted"
                    )
                gate_study = study
            protocols_repository = EvaluationProtocolRepository(database)
            protocols = tuple(
                protocols_repository.get(digest)
                for digest in manifest.protocol_digests
            )
            api_key = SecretStr(self._environment.get("OPENAI_API_KEY", ""))
            gate = RealModelExecutionGate(
                definition,
                authorization,
                self._repository_root,
                source_collector=self._source_collector,
                environment=self._environment,
            )
            gate.validate(
                gate_study,
                protocols,
                operator_confirmation_digest=confirmation_digest,
                api_key=api_key,
            )
            if study.status is EvaluationStudyStatus.DRAFT:
                study = studies.authorize(
                    study.study_id,
                    authorization,
                    expected_version=study.record_version,
                )
            if study.status in _TERMINAL_STUDIES:
                self._output(f"study_status={study.status.value}")
                return 0
            executor = (
                self._campaign_executor_builder(
                    database,
                    gate,
                    api_key,
                    state_root,
                )
                if self._campaign_executor_builder is not None
                else self._build_campaign_executor(
                    database,
                    gate,
                    api_key,
                    state_root,
                    protocol_digests=manifest.protocol_digests,
                )
            )
            if canary:
                if study.status is EvaluationStudyStatus.AUTHORIZED:
                    study = studies.start(
                        study.study_id,
                        expected_version=study.record_version,
                    )
                bindings = studies.list_campaign_bindings(study.study_id)
                matching = [
                    binding
                    for binding in bindings
                    if canary_task_id is None
                    or binding.task_id == canary_task_id
                ]
                if not matching:
                    raise RuntimeError(
                        "Requested Canary task is not bound to this Study"
                    )
                run_canary = getattr(executor, "run_canary_slot", None)
                if not callable(run_canary):
                    raise RuntimeError(
                        "Configured Campaign executor does not support canary"
                    )
                result = asyncio.run(run_canary(matching[0].campaign_id))
                self._output(f"canary_campaign_status={result.campaign.status.value}")
                self._output(f"study_status={study.status.value}")
                return 0
            runner = EvaluationStudyRunner(database, executor)
            result = asyncio.run(
                runner.recover(study.study_id)
                if recover
                else runner.run(study.study_id)
            )
            self._output(f"study_status={result.study.status.value}")
            return 0
        finally:
            database.close()

    def _report(self, state_root: Path, output_dir: Path) -> int:
        manifest, database = self._load_state(state_root)
        try:
            report = EvaluationStudyReportBuilder(database).build(
                manifest.study_id,
                manifest.pricing_snapshot,
            )
            protocols = tuple(
                EvaluationProtocolRepository(database).get(digest)
                for digest in manifest.protocol_digests
            )
            scanner = PublicArtifactScanner(
                forbidden_prompt_texts=tuple(
                    text
                    for protocol in protocols
                    for text in (
                        protocol.system_prompt,
                        protocol.task_prompt,
                    )
                )
            )
            rendered_json = render_evaluation_study_json(
                report,
                scanner=scanner,
            )
            rendered_markdown = render_evaluation_study_markdown(
                report,
                scanner=scanner,
            )
            target = (
                output_dir
                if output_dir.is_absolute()
                else self._repository_root / output_dir
            ).resolve()
            _atomic_write(target / "study_report.json", rendered_json + "\n")
            _atomic_write(target / "study_report.md", rendered_markdown)
        finally:
            database.close()
        self._output(f"report_digest={report.report_digest}")
        self._output(
            "public_report_dir="
            + _display_path(self._repository_root, target)
        )
        return 0

    def _build_campaign_executor(
        self,
        database: Database,
        gate: RealModelExecutionGate,
        api_key: SecretStr,
        state_root: Path,
        protocol_digests: tuple[str, str, str, str],
    ) -> StudyCampaignExecutor:
        provider_factory = OpenAIEvaluationProviderFactory(
            api_key,
            execution_gate=gate,
            provider_builder=self._provider_builder,
        )
        first_protocol = EvaluationProtocolRepository(database).get(
            protocol_digests[0]
        )
        prompt_variant = {
            1: BASELINE_PROMPT_VARIANT,
            2: INVARIANT_GUIDANCE_PROMPT_VARIANT,
            3: TASK_DIAGNOSTIC_PROMPT_VARIANT,
            4: TASK_CONTRACT_GUIDANCE_PROMPT_VARIANT,
        }.get(first_protocol.system_prompt_version)
        if prompt_variant is None:
            raise ValueError("Unsupported persisted system prompt version")
        runtime_factory = PilotRuntimeFactory(
            database,
            provider_factory,
            executable=Path(sys.executable),
            allowed_env=build_fixed_test_environment(self._environment),
            prompt_variant=prompt_variant,
        )
        manifests = {
            task_id: self._repository_root
            / "evaluation"
            / "fixtures"
            / "tasks"
            / task_id
            for task_id in _task_ids()
        }
        for task_id, path in manifests.items():
            manifest = FormalFixtureLoader().load(path)
            if manifest.task_id != task_id:
                raise RuntimeError("Formal Fixture task binding has drifted")
        return PilotRunner(
            database,
            runtime_factory,
            PilotWorkspaceManager(self._workspace_root(state_root)),
            manifests,
        )

    @staticmethod
    def _workspace_root(state_root: Path) -> Path:
        state_digest = hashlib.sha256(
            str(state_root.resolve()).encode("utf-8")
        ).hexdigest()[:16]
        return (
            Path(tempfile.gettempdir()).resolve()
            / "agentforge-pilots"
            / state_digest
            / "workspaces"
        )

    def _load_state(
        self,
        state_root: Path,
    ) -> tuple[PrivateStudyManifest, Database]:
        manifest = load_private_study_manifest(
            state_root / "study_manifest.json"
        )
        run_manifest = load_evaluation_run_manifest(
            state_root / "run_manifest.json"
        )
        if (
            run_manifest.study_id != manifest.study_id
            or run_manifest.definition_digest != manifest.definition_digest
            or run_manifest.protocol_digests != manifest.protocol_digests
            or run_manifest.model_id != manifest.model_id
            or run_manifest.response_model_id != manifest.response_model_id
            or run_manifest.pricing_digest
            != manifest.pricing_snapshot.pricing_digest
        ):
            raise EvaluationRunManifestError(
                "Evaluation Run Manifest does not match Study Manifest"
            )
        database_path = state_root / "study.sqlite3"
        if not database_path.is_file():
            raise RuntimeError("Private Study database is missing")
        database = Database.from_path(database_path)
        try:
            definition = EvaluationStudyRepository(database).get_definition(
                manifest.study_id
            )
            if (
                definition.definition_digest != manifest.definition_digest
                or definition.protocol_digests != manifest.protocol_digests
                or definition.model_id != manifest.model_id
                or definition.response_model_id != manifest.response_model_id
                or definition.pricing_snapshot_digest
                != manifest.pricing_snapshot.pricing_digest
            ):
                raise RuntimeError(
                    "Private Study manifest does not match durable state"
                )
        except Exception:
            database.close()
            raise
        return manifest, database

    def _resolve_state_root(self, value: str) -> Path:
        path = Path(value)
        return (
            path if path.is_absolute() else self._repository_root / path
        ).resolve()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_real_model_pilot",
        description="Evaluator-only AgentForge real-model Portfolio Pilot",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    prepare = subcommands.add_parser("prepare")
    prepare.add_argument("--state-dir", default=".agentforge/m7b2_4")
    prepare.add_argument("--model", required=True)
    prepare.add_argument("--response-model", required=True)
    prepare.add_argument("--pricing-date", required=True)
    prepare.add_argument("--pricing-source", required=True)
    prepare.add_argument("--input-per-million", required=True)
    prepare.add_argument("--cached-input-per-million")
    prepare.add_argument("--output-per-million", required=True)
    prepare.add_argument(
        "--prompt-variant",
        choices=(
            BASELINE_PROMPT_VARIANT,
            INVARIANT_GUIDANCE_PROMPT_VARIANT,
            TASK_DIAGNOSTIC_PROMPT_VARIANT,
            TASK_CONTRACT_GUIDANCE_PROMPT_VARIANT,
        ),
        default=BASELINE_PROMPT_VARIANT,
    )
    for name in ("run", "recover"):
        command = subcommands.add_parser(name)
        command.add_argument("--state-dir", default=".agentforge/m7b2_4")
        command.add_argument("--confirm", required=True)
    canary = subcommands.add_parser(
        "canary",
        help="Execute one recoverable slot from the first campaign",
    )
    canary.add_argument("--state-dir", default=".agentforge/m7b2_4")
    canary.add_argument("--confirm", required=True)
    canary.add_argument(
        "--task-id",
        help="Optional registered task id; defaults to the first campaign",
    )
    report = subcommands.add_parser("report")
    report.add_argument("--state-dir", default=".agentforge/m7b2_4")
    report.add_argument(
        "--output-dir",
        default="evaluation/results/milestone_07b2_4",
    )
    return parser


def _pricing_snapshot(arguments: argparse.Namespace) -> PricingSnapshot:
    return PricingSnapshot(
        model_id=arguments.model,
        effective_date=date.fromisoformat(arguments.pricing_date),
        source_url=arguments.pricing_source,
        input_per_million=Decimal(arguments.input_per_million),
        cached_input_per_million=(
            Decimal(arguments.cached_input_per_million)
            if arguments.cached_input_per_million is not None
            else None
        ),
        output_per_million=Decimal(arguments.output_per_million),
    )


def _task_ids() -> tuple[str, str, str, str]:
    from agentforge.evaluation.study_models import B2_4_TASK_ORDER

    return B2_4_TASK_ORDER


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8", newline="\n")
        os.replace(temporary, path)
    except OSError:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _display_path(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root).as_posix()
    except ValueError:
        return path.resolve().name
