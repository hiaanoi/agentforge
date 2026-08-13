import hashlib
import json
import shutil
import tempfile
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agentforge.application.contracts import ProfilePurpose, VerificationRuntimeMode
from agentforge.domain.enums import ConfigSourceKind
from agentforge.domain.repair import (
    BudgetProfile,
    RepairDifficulty,
    RepairTaskPolicy,
    fixed_budget,
)
from agentforge.evaluation.baseline_models import ExpectedBaselineFailure
from agentforge.evaluation.protocol import canonical_digest
from agentforge.evaluation.workspace import WorkspaceBaselineBuilder
from agentforge.tools.paths import WorkspacePathResolver
from agentforge.tools.testing.profiles import (
    TestProfileDefinition,
    is_reserved_verification_environment,
)


class FormalRepairPrompt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=4_000)
    success_conditions: tuple[str, ...] = Field(min_length=1, max_length=20)


class FormalFixtureManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    fixture_schema_version: str
    task_id: str = Field(pattern=r"^[a-z0-9-]+$", max_length=200)
    source_kind: str
    difficulty: str
    license: str
    source_urls: tuple[str, ...]
    buggy_revision: str | None
    fixed_revision: str | None
    network_required: bool
    external_service_required: bool
    editable_paths: tuple[str, ...]
    protected_paths: tuple[str, ...]
    reference_files: tuple[str, ...]
    test_commands: dict[str, tuple[str, ...]]
    expected_outcomes: dict[str, str]
    expected_baseline_failure: ExpectedBaselineFailure
    repair_prompt: FormalRepairPrompt
    limits: dict[str, int]
    immutable_file_sha256: dict[str, str]
    task_root: str = Field(exclude=True)

    @model_validator(mode="after")
    def validate_contract(self) -> Self:
        if self.fixture_schema_version != "1.0.0":
            raise ValueError("Unsupported formal Fixture schema")
        if self.network_required or self.external_service_required:
            raise ValueError("Formal Fixtures must be offline and local")
        if self.expected_outcomes != {"buggy": "FAIL", "reference": "PASS"}:
            raise ValueError("Formal Fixture outcomes are invalid")
        if set(self.test_commands) != {"visible", "hidden"}:
            raise ValueError("Formal Fixture requires visible and hidden profiles")
        return self

    @property
    def root(self) -> Path:
        return Path(self.task_root)

    @property
    def visible_command(self) -> tuple[str, ...]:
        return self.test_commands["visible"]

    @property
    def hidden_command(self) -> tuple[str, ...]:
        return self.test_commands["hidden"]

    @property
    def asset_digest(self) -> str:
        return compute_task_asset_digest(self.root)

    def to_policy(self, *, path_case_sensitive: bool) -> RepairTaskPolicy:
        difficulty = RepairDifficulty(self.difficulty)
        sizes = [
            (self.root / Path(relative)).stat().st_size
            for relative in self.editable_paths
        ]
        max_single = max(4_096, max(sizes) * 2)
        max_total = max(8_192, max_single, sum(sizes) * 2)
        budget_profile = BudgetProfile(difficulty.value)
        budget = fixed_budget(budget_profile)
        return RepairTaskPolicy(
            task_id=self.task_id,
            policy_version=1,
            difficulty=difficulty,
            budget_profile=budget_profile,
            allowed_write_paths=self.editable_paths,
            forbidden_write_paths=(),
            protected_paths=("tests/**",),
            allowed_development_test_profiles=(f"visible-{self.task_id}",),
            final_verification_profile_id=f"hidden-{self.task_id}",
            allow_file_creation=False,
            allowed_create_paths=(),
            max_created_files=0,
            max_changed_files=len(self.editable_paths),
            max_total_changed_bytes=max_total,
            max_single_file_changed_bytes=max_single,
            max_model_calls=budget.max_model_calls,
            max_read_calls=budget.max_read_calls,
            max_edit_attempts=budget.max_edit_attempts,
            max_test_runs=budget.max_test_runs,
            max_completion_corrections=budget.max_completion_corrections,
            max_policy_violations=budget.max_policy_violations,
            max_wall_time_seconds=budget.max_wall_time_seconds,
            path_case_sensitive=path_case_sensitive,
        )

    def profile_template_digest(
        self,
        *,
        executable: str,
        allowed_env: dict[str, str],
    ) -> str:
        environment_template = {
            **allowed_env,
            "PYTHONPATH": "{MODEL_WORKSPACE}/workspace",
            "TEMP": "{ATTEMPT_ROOT}/runtime_tmp",
            "TMP": "{ATTEMPT_ROOT}/runtime_tmp",
        }
        return canonical_digest(
            {
                "executable": str(Path(executable).resolve(strict=True)),
                "visible_argv": list(self.visible_command),
                "hidden_argv": list(self.hidden_command),
                "allowed_env": dict(sorted(environment_template.items())),
                "timeout_seconds": self.limits["timeout_seconds"],
                "max_output_bytes": self.limits["max_output_chars"],
                "profile_version": 1,
            }
        )


class FormalFixtureLoader:
    def load(self, task_root: Path) -> FormalFixtureManifest:
        root = task_root.resolve(strict=True)
        if not root.is_dir():
            raise ValueError("Formal Fixture root must be a directory")
        try:
            raw = json.loads((root / "task_manifest.json").read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("Formal Fixture manifest is invalid") from exc
        if not isinstance(raw, dict):
            raise ValueError("Formal Fixture manifest is invalid")
        manifest = FormalFixtureManifest.model_validate({**raw, "task_root": str(root)})
        self._verify_assets(manifest)
        return manifest

    @staticmethod
    def _verify_assets(manifest: FormalFixtureManifest) -> None:
        try:
            WorkspaceBaselineBuilder(WorkspacePathResolver(manifest.root)).scan()
        except RuntimeError as exc:
            raise ValueError("Formal Fixture contains an unsafe filesystem entry") from exc
        for relative, expected_digest in manifest.immutable_file_sha256.items():
            path = (manifest.root / Path(relative)).resolve(strict=True)
            if manifest.root not in path.parents:
                raise ValueError("Formal Fixture immutable path escapes its root")
            if hashlib.sha256(path.read_bytes()).hexdigest() != expected_digest:
                raise ValueError("Formal Fixture immutable asset digest mismatch")


class FormalFixturePilotWorkspace:
    def __init__(
        self,
        temporary_directory: tempfile.TemporaryDirectory[str],
        manifest: FormalFixtureManifest,
        root: Path,
        hidden_test_root: Path,
    ) -> None:
        self._temporary_directory = temporary_directory
        self.manifest = manifest
        self.root = root
        self.hidden_test_root = hidden_test_root

    @classmethod
    def create(cls, manifest: FormalFixtureManifest) -> "FormalFixturePilotWorkspace":
        temporary_directory = tempfile.TemporaryDirectory(prefix="agentforge-pilot-")
        container = Path(temporary_directory.name)
        root = container / "model_workspace"
        hidden_root = container / "evaluator_hidden" / "tests" / "hidden"
        try:
            shutil.copytree(manifest.root / "workspace", root / "workspace")
            shutil.copytree(manifest.root / "tests" / "visible", root / "tests" / "visible")
            shutil.copytree(manifest.root / "tests" / "hidden", hidden_root)
            WorkspaceBaselineBuilder(WorkspacePathResolver(root)).scan()
        except (OSError, RuntimeError) as exc:
            temporary_directory.cleanup()
            raise ValueError("Formal Pilot workspace preparation failed") from exc
        return cls(temporary_directory, manifest, root, hidden_root)

    def profile_definitions(
        self,
        *,
        executable: str,
        allowed_env: dict[str, str],
    ) -> tuple[TestProfileDefinition, TestProfileDefinition]:
        timeout = self.manifest.limits["timeout_seconds"]
        output_limit = self.manifest.limits["max_output_chars"]
        visible = TestProfileDefinition(
            profile_id=f"visible-{self.manifest.task_id}",
            name="Visible development tests",
            description="Evaluator-defined visible development profile",
            executable=executable,
            argv=self.manifest.visible_command,
            cwd=".",
            allowed_env=allowed_env,
            timeout_seconds=timeout,
            max_output_bytes=output_limit,
            profile_version=1,
            config_source_identity=(
                f"formal-fixture:{self.manifest.task_id}:manifest"
            ),
            config_source_kind=ConfigSourceKind.FORMAL_MANIFEST,
            config_source_digest=self.manifest.asset_digest,
        )
        hidden_argv = tuple(
            "{VERIFIER}" if item == "tests/hidden" else item
            for item in self.manifest.hidden_command
        )
        hidden_environment = {
            key: value
            for key, value in allowed_env.items()
            if not is_reserved_verification_environment(key)
            and not Path(value).is_absolute()
        }
        hidden = TestProfileDefinition(
            profile_id=f"hidden-{self.manifest.task_id}",
            name="Hidden final verification",
            description="Evaluator-defined hidden final profile",
            executable=executable,
            argv=hidden_argv,
            cwd=".",
            allowed_env=hidden_environment,
            timeout_seconds=timeout,
            max_output_bytes=output_limit,
            profile_version=1,
            purpose=ProfilePurpose.VERIFICATION,
            runtime_mode=VerificationRuntimeMode.SYSTEM_RUNTIME,
            verifier_root=str(self.hidden_test_root),
            config_source_identity=(
                f"formal-fixture:{self.manifest.task_id}:manifest"
            ),
            config_source_kind=ConfigSourceKind.FORMAL_MANIFEST,
            config_source_digest=self.manifest.asset_digest,
        )
        return visible, hidden

    def __enter__(self) -> "FormalFixturePilotWorkspace":
        return self

    def __exit__(self, *_: object) -> None:
        self._temporary_directory.cleanup()


def compute_task_asset_digest(task_root: Path) -> str:
    root = task_root.resolve(strict=True)
    return _tree_digest(root, tuple(path for path in root.rglob("*") if path.is_file()))


def compute_fixture_registry_digest(fixture_root: Path) -> str:
    root = fixture_root.resolve(strict=True)
    paths = [
        root / name
        for name in (
            "build_assets.py",
            "fixture_schema.json",
            "registry.json",
            "verify_fixtures.py",
        )
    ]
    paths.extend(path for path in (root / "tasks").rglob("*") if path.is_file())
    return _tree_digest(root, tuple(paths))


def _tree_digest(root: Path, paths: tuple[Path, ...]) -> str:
    digest = hashlib.sha256()
    for path in sorted(paths, key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()
