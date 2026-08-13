import hashlib
import json
import os
import re
import shutil
import stat
import unicodedata
from pathlib import Path, PurePosixPath, PureWindowsPath

from pydantic import ConfigDict, Field

from agentforge.application.contracts import ProfilePurpose, VerificationRuntimeMode
from agentforge.domain.enums import ConfigSourceKind, ToolErrorCode
from agentforge.domain.errors import (
    DuplicateTestProfileError,
    TestProfileBindingMismatchError,
    ToolExecutionError,
)
from agentforge.domain.models import DomainModel
from agentforge.domain.test_execution import (
    TestApprovalBinding,
    TestExecutionPlan,
    TestProfile,
    redact_argv_for_review,
)
from agentforge.tools.paths import WorkspacePathResolver

_SENSITIVE_ENV_COMPONENTS = frozenset(
    {
        "APIKEY",
        "CREDENTIAL",
        "CREDENTIALS",
        "PASSWORD",
        "PRIVATEKEY",
        "SECRET",
        "SSH",
        "TOKEN",
    }
)
_SENSITIVE_ENV_PREFIXES = (
    "ANTHROPIC_",
    "AWS_",
    "AZURE_",
    "GCP_",
    "GITHUB_",
    "GITLAB_",
    "GOOGLE_",
    "OPENAI_",
)
_MAX_EXECUTABLE_BYTES = 512 * 1024 * 1024
_EXECUTABLE_READ_CHUNK_BYTES = 1024 * 1024
_CAPSULE_MARKERS = ("{SOURCE}", "{VERIFIER}", "{SCRATCH}")
_PORTABLE_SEGMENT = r"[A-Za-z0-9_][A-Za-z0-9_-]*(?:\.[A-Za-z0-9_][A-Za-z0-9_-]*)*"
_CAPSULE_REFERENCE = re.compile(
    rf"^(?:{'|'.join(re.escape(item) for item in _CAPSULE_MARKERS)})"
    rf"(?:/{_PORTABLE_SEGMENT}(?:/{_PORTABLE_SEGMENT})*)?$"
)
_SAFE_VERIFICATION_LITERAL = re.compile(
    r"^[A-Za-z0-9_][A-Za-z0-9_-]*$"
)
_SAFE_VERIFICATION_FLAG = re.compile(r"^-{1,2}[A-Za-z][A-Za-z0-9_-]*$")
_SAFE_MODULE_LITERAL = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$"
)
_RESERVED_VERIFICATION_ENV = frozenset(
    {
        "PYTHONBREAKPOINT",
        "PYTHONCASEOK",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONEXECUTABLE",
        "PYTHONHOME",
        "PYTHONINSPECT",
        "PYTHONNOUSERSITE",
        "PYTHONPATH",
        "PYTHONPLATLIBDIR",
        "PYTHONSAFEPATH",
        "PYTHONSTARTUP",
        "PYTHONUSERBASE",
        "PYTEST_ADDOPTS",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD",
        "PYTEST_PLUGINS",
    }
)


def is_reserved_verification_environment(name: str) -> bool:
    return name.upper() in _RESERVED_VERIFICATION_ENV


class TestProfileDefinition(DomainModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True, frozen=True)

    profile_id: str = Field(pattern=r"^[a-z_][a-z0-9_-]*$", max_length=100)
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=500)
    executable: str = Field(min_length=1, max_length=4096)
    argv: tuple[str, ...] = Field(default_factory=tuple, max_length=99)
    cwd: str = Field(min_length=1, max_length=4096)
    allowed_env: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: float = Field(gt=0, le=3600)
    max_output_bytes: int = Field(gt=0, le=10_000_000)
    enabled: bool = True
    profile_version: int = Field(gt=0)
    purpose: ProfilePurpose = ProfilePurpose.DEVELOPMENT
    runtime_mode: VerificationRuntimeMode = VerificationRuntimeMode.SYSTEM_RUNTIME
    verifier_root: str | None = Field(default=None, max_length=4096)
    config_source_identity: str = Field(
        default="operator:in-memory", min_length=1, max_length=500
    )
    config_source_kind: ConfigSourceKind = ConfigSourceKind.BUILTIN
    config_source_digest: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )


class TestProfileRegistry:
    def __init__(self, resolver: WorkspacePathResolver) -> None:
        self._resolver = resolver
        self._profiles: dict[str, str] = {}

    @property
    def workspace_root(self) -> Path:
        return self._resolver.workspace

    @property
    def workspace_identity(self) -> str:
        return self._digest(str(self._resolver.workspace))

    def register(
        self,
        definition: TestProfileDefinition,
        *,
        trusted_search_path: str | None = None,
    ) -> TestProfile:
        if definition.profile_id in self._profiles:
            raise DuplicateTestProfileError(
                f"Test profile {definition.profile_id!r} is already registered"
            )
        executable = self._resolve_executable(
            definition.executable,
            trusted_search_path=trusted_search_path,
        )
        verifier_root = self._resolve_verifier_root(definition.verifier_root)
        self._validate_verification_configuration(definition, verifier_root)
        argv = (str(executable), *definition.argv)
        self._validate_argv(argv)
        cwd = self._resolve_cwd(definition.cwd)
        environment = self._validate_environment(definition.allowed_env)
        executable_digest = self._digest_executable(executable)
        argv_digest = self._digest(list(argv))
        cwd_digest = self._digest(str(cwd))
        environment_digest = self._digest(environment)
        config_source_digest = definition.config_source_digest or self._digest(
            {
                "config_source_identity": definition.config_source_identity,
                "config_source_kind": definition.config_source_kind.value,
                "definition": definition.model_dump(exclude={"config_source_digest"}),
            }
        )
        profile_digest = self._digest(
            {
                "profile_id": definition.profile_id,
                "name": definition.name,
                "description": definition.description,
                "executable_path": str(executable),
                "argv": list(argv),
                "cwd": str(cwd),
                "allowed_env": environment,
                "timeout_seconds": definition.timeout_seconds,
                "max_output_bytes": definition.max_output_bytes,
                "enabled": definition.enabled,
                "profile_version": definition.profile_version,
                "purpose": definition.purpose.value,
                "runtime_mode": definition.runtime_mode.value,
                "verifier_root": verifier_root,
                "executable_digest": executable_digest,
                "argv_digest": argv_digest,
                "cwd_digest": cwd_digest,
                "environment_digest": environment_digest,
                "config_source_identity": definition.config_source_identity,
                "config_source_kind": definition.config_source_kind.value,
                "config_source_digest": config_source_digest,
            }
        )
        profile = TestProfile(
            profile_id=definition.profile_id,
            name=definition.name,
            description=definition.description,
            executable_path=str(executable),
            argv=argv,
            cwd=str(cwd),
            allowed_env=environment,
            timeout_seconds=definition.timeout_seconds,
            max_output_bytes=definition.max_output_bytes,
            enabled=definition.enabled,
            profile_version=definition.profile_version,
            purpose=definition.purpose,
            runtime_mode=definition.runtime_mode,
            verifier_root=verifier_root,
            executable_digest=executable_digest,
            argv_digest=argv_digest,
            cwd_digest=cwd_digest,
            environment_digest=environment_digest,
            config_source_identity=definition.config_source_identity,
            config_source_kind=definition.config_source_kind,
            config_source_digest=config_source_digest,
            profile_digest=profile_digest,
        )
        self._profiles[profile.profile_id] = profile.model_dump_json()
        return self._copy(profile)

    def get(self, profile_id: str) -> TestProfile:
        serialized = self._profiles.get(profile_id)
        if serialized is None:
            raise ToolExecutionError(
                ToolErrorCode.TEST_PROFILE_NOT_FOUND,
                f"Test profile {profile_id!r} is not registered",
            )
        return TestProfile.model_validate_json(serialized)

    def list_enabled(self) -> list[TestProfile]:
        return [
            profile
            for profile_id in sorted(self._profiles)
            if (profile := self.get(profile_id)).enabled
        ]

    def prepare(self, profile_id: str) -> TestExecutionPlan:
        profile = self.get(profile_id)
        if not profile.enabled:
            raise ToolExecutionError(
                ToolErrorCode.TEST_PROFILE_DISABLED,
                f"Test profile {profile_id!r} is disabled",
            )
        self.require_executable_identity(profile)
        return TestExecutionPlan(
            profile_id=profile.profile_id,
            profile_version=profile.profile_version,
            profile_digest=profile.profile_digest,
            executable_path=profile.executable_path,
            argv_digest=profile.argv_digest,
            cwd=profile.cwd,
            environment_digest=profile.environment_digest,
        )

    def require_bound(self, binding: TestApprovalBinding) -> TestProfile:
        return self.require_plan(binding)

    def require_plan(self, binding: TestExecutionPlan) -> TestProfile:
        profile = self.get(binding.profile_id)
        if not profile.enabled:
            raise TestProfileBindingMismatchError(
                ToolErrorCode.TEST_PROFILE_MISMATCH,
                "enabled does not match the approved test profile",
            )
        self.require_executable_identity(profile)
        expected: dict[str, object] = {
            "profile_version": binding.profile_version,
            "profile_digest": binding.profile_digest,
            "executable_path": binding.executable_path,
            "argv_digest": binding.argv_digest,
            "cwd": binding.cwd,
            "environment_digest": binding.environment_digest,
        }
        for field, approved in expected.items():
            if getattr(profile, field) != approved:
                raise TestProfileBindingMismatchError(
                    ToolErrorCode.TEST_PROFILE_MISMATCH,
                    f"{field} does not match the approved test profile",
                )
        return profile

    def require_executable_identity(self, profile: TestProfile) -> None:
        try:
            actual = self.executable_digest(profile)
        except ToolExecutionError as exc:
            raise TestProfileBindingMismatchError(
                ToolErrorCode.TEST_PROFILE_MISMATCH,
                "executable_digest does not match the approved test profile",
            ) from exc
        if actual != profile.executable_digest:
            raise TestProfileBindingMismatchError(
                ToolErrorCode.TEST_PROFILE_MISMATCH,
                "executable_digest does not match the approved test profile",
            )

    def executable_digest(self, profile: TestProfile) -> str:
        return self._digest_executable(Path(profile.executable_path))

    @staticmethod
    def argv_review(profile: TestProfile) -> tuple[str, ...]:
        return redact_argv_for_review(profile.argv)

    @staticmethod
    def _copy(profile: TestProfile) -> TestProfile:
        return TestProfile.model_validate_json(profile.model_dump_json())

    @staticmethod
    def _resolve_executable(value: str, *, trusted_search_path: str | None) -> Path:
        raw = Path(value)
        if raw.is_absolute() or PureWindowsPath(value).is_absolute():
            candidate = raw
        else:
            resolved = shutil.which(
                value,
                path=trusted_search_path if trusted_search_path is not None else os.getenv("PATH"),
            )
            if resolved is None:
                raise ToolExecutionError(
                    ToolErrorCode.INVALID_TEST_PROFILE,
                    "Test profile executable could not be resolved",
                )
            candidate = Path(resolved)
        executable = Path(os.path.abspath(candidate))
        try:
            TestProfileRegistry._require_link_free_path(executable)
        except (OSError, RuntimeError) as exc:
            raise ToolExecutionError(
                ToolErrorCode.INVALID_TEST_PROFILE,
                "Test profile executable path is unsafe or missing",
            ) from exc
        try:
            executable_stat = os.lstat(executable)
        except OSError as exc:
            raise ToolExecutionError(
                ToolErrorCode.INVALID_TEST_PROFILE,
                "Test profile executable does not exist",
            ) from exc
        if not stat.S_ISREG(executable_stat.st_mode):
            raise ToolExecutionError(
                ToolErrorCode.INVALID_TEST_PROFILE,
                "Test profile executable must be a file",
            )
        return executable

    @staticmethod
    def _resolve_verifier_root(value: str | None) -> str | None:
        if value is None:
            return None
        candidate = Path(os.path.abspath(value))
        try:
            TestProfileRegistry._require_link_free_path(candidate)
            if not stat.S_ISDIR(os.lstat(candidate).st_mode):
                raise OSError("Verifier root is not a directory")
        except OSError as exc:
            raise ToolExecutionError(
                ToolErrorCode.INVALID_TEST_PROFILE,
                "Verification input root is unsafe or missing",
            ) from exc
        return str(candidate)

    def _validate_verification_configuration(
        self, definition: TestProfileDefinition, verifier_root: str | None
    ) -> None:
        if definition.purpose is not ProfilePurpose.VERIFICATION:
            return
        if (
            definition.runtime_mode is not VerificationRuntimeMode.SYSTEM_RUNTIME
            or verifier_root is None
        ):
            raise ToolExecutionError(
                ToolErrorCode.INVALID_TEST_PROFILE,
                "Verification profiles require SYSTEM_RUNTIME and verifier inputs",
            )
        if self._paths_overlap(self._resolver.workspace, Path(verifier_root)):
            raise ToolExecutionError(
                ToolErrorCode.INVALID_TEST_PROFILE,
                "Verification source and verifier roots must be disjoint",
            )
        previous: str | None = None
        for argument in definition.argv:
            normalized = unicodedata.normalize("NFC", argument)
            portable = PurePosixPath(argument)
            windows = PureWindowsPath(argument)
            if (
                argument != normalized
                or "\x00" in argument
                or "\\" in argument
                or ":" in argument
                or portable.is_absolute()
                or windows.is_absolute()
                or not (
                    _CAPSULE_REFERENCE.fullmatch(argument)
                    or _SAFE_VERIFICATION_FLAG.fullmatch(argument)
                    or (
                        previous == "-m"
                        and _SAFE_MODULE_LITERAL.fullmatch(argument)
                    )
                )
            ):
                raise ToolExecutionError(
                    ToolErrorCode.INVALID_TEST_PROFILE,
                    "Verification paths require typed portable capsule references",
                )
            previous = argument
        for name, value in definition.allowed_env.items():
            normalized = unicodedata.normalize("NFC", value)
            if (
                is_reserved_verification_environment(name)
                or value != normalized
                or "\x00" in value
                or "\\" in value
                or "/" in value
                or ":" in value
                or not _SAFE_VERIFICATION_LITERAL.fullmatch(value)
            ):
                raise ToolExecutionError(
                    ToolErrorCode.INVALID_TEST_PROFILE,
                    "Verification environment cannot reference paths outside its capsule",
                )

    @staticmethod
    def _paths_overlap(left: Path, right: Path) -> bool:
        left_key = os.path.normcase(os.path.abspath(left))
        right_key = os.path.normcase(os.path.abspath(right))
        try:
            return os.path.commonpath((left_key, right_key)) in {left_key, right_key}
        except ValueError:
            return False

    @staticmethod
    def _require_link_free_path(candidate: Path) -> None:
        parts = (*tuple(reversed(candidate.parents)), candidate)
        for part in parts:
            item = os.lstat(part)
            if stat.S_ISLNK(item.st_mode) or TestProfileRegistry._is_reparse_stat(item):
                raise OSError("Executable path cannot contain links or reparse points")

    @staticmethod
    def _digest_executable(executable: Path) -> str:
        try:
            TestProfileRegistry._require_link_free_path(executable)
            path_before = os.lstat(executable)
            if not stat.S_ISREG(path_before.st_mode):
                raise OSError("Executable must remain a regular file")
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(executable, flags)
            try:
                opened = os.fstat(descriptor)
                TestProfileRegistry._require_same_file_identity(path_before, opened)
                digest = hashlib.sha256()
                total = 0
                while True:
                    chunk = os.read(descriptor, _EXECUTABLE_READ_CHUNK_BYTES)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > _MAX_EXECUTABLE_BYTES:
                        raise OSError("Executable exceeds the digest bound")
                    digest.update(chunk)
                opened_after = os.fstat(descriptor)
                TestProfileRegistry._require_same_file_identity(opened, opened_after)
                TestProfileRegistry._require_unchanged_file(opened, opened_after)
            finally:
                os.close(descriptor)
            TestProfileRegistry._require_link_free_path(executable)
            path_after = os.lstat(executable)
            TestProfileRegistry._require_same_file_identity(opened_after, path_after)
            TestProfileRegistry._require_unchanged_file(
                opened_after, path_after, compare_ctime=False
            )
            TestProfileRegistry._require_link_free_path(executable)
            return digest.hexdigest()
        except (OSError, RuntimeError) as exc:
            raise ToolExecutionError(
                ToolErrorCode.INVALID_TEST_PROFILE,
                "Test profile executable identity could not be verified",
            ) from exc

    @staticmethod
    def _require_same_file_identity(
        before: os.stat_result, after: os.stat_result
    ) -> None:
        if (
            before.st_dev != after.st_dev
            or before.st_ino != after.st_ino
            or stat.S_IFMT(before.st_mode) != stat.S_IFMT(after.st_mode)
            or not stat.S_ISREG(after.st_mode)
        ):
            raise OSError("Executable identity changed while hashing")

    @staticmethod
    def _require_unchanged_file(
        before: os.stat_result,
        after: os.stat_result,
        *,
        compare_ctime: bool = True,
    ) -> None:
        if (
            before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or (compare_ctime and before.st_ctime_ns != after.st_ctime_ns)
        ):
            raise OSError("Executable changed while hashing")

    def _resolve_cwd(self, requested: str) -> Path:
        raw = requested.strip()
        windows = PureWindowsPath(raw)
        if (
            not raw
            or "\x00" in raw
            or Path(raw).is_absolute()
            or windows.is_absolute()
            or bool(windows.drive)
            or raw.startswith(("\\\\", "//"))
        ):
            raise ToolExecutionError(
                ToolErrorCode.INVALID_TEST_PROFILE,
                "Test profile cwd must be workspace-relative",
            )
        candidate = Path(os.path.abspath(self._resolver.workspace / raw))
        try:
            relative = candidate.relative_to(self._resolver.workspace)
        except ValueError as exc:
            raise ToolExecutionError(
                ToolErrorCode.INVALID_TEST_PROFILE,
                "Test profile cwd escapes the workspace",
            ) from exc
        current = self._resolver.workspace
        for part in relative.parts:
            current /= part
            if current.is_symlink() or self._is_reparse_point(current):
                raise ToolExecutionError(
                    ToolErrorCode.INVALID_TEST_PROFILE,
                    "Test profile cwd cannot contain symlinks or reparse points",
                )
        if not candidate.is_dir():
            raise ToolExecutionError(
                ToolErrorCode.INVALID_TEST_PROFILE,
                "Test profile cwd must be an existing directory",
            )
        return candidate

    @staticmethod
    def _validate_argv(argv: tuple[str, ...]) -> None:
        if any(not item or "\x00" in item for item in argv):
            raise ToolExecutionError(
                ToolErrorCode.INVALID_TEST_PROFILE,
                "Test profile argv contains an invalid item",
            )

    @staticmethod
    def _validate_environment(environment: dict[str, str]) -> dict[str, str]:
        validated: dict[str, str] = {}
        for name, value in environment.items():
            upper = name.upper()
            components = set(re.split(r"[^A-Z0-9]+", upper))
            compact = upper.replace("_", "")
            if (
                not name
                or "=" in name
                or "\x00" in name
                or "\x00" in value
                or upper.startswith(_SENSITIVE_ENV_PREFIXES)
                or bool(components & _SENSITIVE_ENV_COMPONENTS)
                or compact in _SENSITIVE_ENV_COMPONENTS
            ):
                raise ToolExecutionError(
                    ToolErrorCode.INVALID_TEST_PROFILE,
                    "Test profile environment contains a forbidden variable",
                )
            validated[name] = value
        return dict(sorted(validated.items()))

    @staticmethod
    def _is_reparse_point(candidate: Path) -> bool:
        try:
            item = os.lstat(candidate)
        except OSError:
            return False
        return TestProfileRegistry._is_reparse_stat(item)

    @staticmethod
    def _is_reparse_stat(item: os.stat_result) -> bool:
        attributes = getattr(item, "st_file_attributes", 0)
        marker = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        return bool(attributes & marker)

    @staticmethod
    def _digest(value: object) -> str:
        canonical = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
