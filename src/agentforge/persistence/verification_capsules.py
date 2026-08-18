from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import unicodedata
from pathlib import Path, PurePosixPath, PureWindowsPath
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from agentforge.application.kernel_errors import WorkspaceDigestError
from agentforge.domain.test_execution import TestProfile
from agentforge.persistence.source_revisions import (
    _EXCLUDED_DIRECTORY_NAMES,
    DIGEST_ALGORITHM_VERSION,
    WorkspaceDigestEntry,
    WorkspaceDigester,
    WorkspaceDigestLimits,
)

_CAPSULE_MARKERS = ("{SOURCE}", "{VERIFIER}", "{SCRATCH}")
_PORTABLE_SEGMENT = r"[A-Za-z0-9_][A-Za-z0-9_-]*(?:\.[A-Za-z0-9_][A-Za-z0-9_-]*)*"
_CAPSULE_REFERENCE = re.compile(
    rf"^({'|'.join(re.escape(item) for item in _CAPSULE_MARKERS)})"
    rf"(?:/({_PORTABLE_SEGMENT}(?:/{_PORTABLE_SEGMENT})*))?$"
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


class VerificationCapsule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    capsule_id: UUID
    execution_id: UUID
    root: Path
    source_root: Path
    verifier_root: Path
    scratch_root: Path
    source_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    verifier_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    algorithm_version: int = Field(default=DIGEST_ALGORITHM_VERSION, ge=1)


class VerificationCapsuleBuilder:
    """Build bounded per-execution copies; sealing is integrity evidence, not an ACL claim."""

    def __init__(
        self,
        store_root: Path,
        *,
        limits: WorkspaceDigestLimits | None = None,
        digester: WorkspaceDigester | None = None,
    ) -> None:
        if not store_root.is_absolute():
            raise WorkspaceDigestError()
        if digester is not None and limits is not None and limits != digester._limits:
            raise ValueError("capsule limits must match the injected workspace digester")
        self._store_root = store_root
        self._limits = limits or (
            digester._limits if digester is not None else WorkspaceDigestLimits()
        )
        self._digester = digester or WorkspaceDigester(limits=self._limits)

    def capture(
        self,
        *,
        execution_id: UUID,
        source_root: Path,
        verifier_root: Path,
        capsule_id: UUID | None = None,
    ) -> VerificationCapsule:
        source = self._validated_input_root(source_root)
        verifier = self._validated_input_root(verifier_root)
        if self._overlaps(source, verifier):
            raise WorkspaceDigestError()
        store, store_chain = self._prepare_store(source, verifier)
        capsule_id = capsule_id or uuid4()
        staging = store / f".staging-{capsule_id}"
        published = store / str(capsule_id)
        try:
            os.mkdir(staging, 0o700)
            source_target = staging / "source"
            verifier_target = staging / "verifier"
            scratch = staging / "scratch"
            os.mkdir(source_target, 0o700)
            os.mkdir(verifier_target, 0o700)
            os.mkdir(scratch, 0o700)
            source_digest = self._copy_tree(source, source_target)
            verifier_digest = self._copy_tree(verifier, verifier_target)
            manifest = {
                "algorithm_version": DIGEST_ALGORITHM_VERSION,
                "capsule_id": str(capsule_id),
                "execution_id": str(execution_id),
                "source_digest": source_digest,
                "verifier_digest": verifier_digest,
            }
            self._write_manifest(staging / "capsule.json", manifest)
            self._fsync_directory(staging)
            self._revalidate_store(store, store_chain)
            if published.exists():
                raise WorkspaceDigestError()
            os.rename(staging, published)
            self._fsync_directory(store)
            self._revalidate_store(store, store_chain)
            capsule = VerificationCapsule(
                capsule_id=capsule_id,
                execution_id=execution_id,
                root=published,
                source_root=published / "source",
                verifier_root=published / "verifier",
                scratch_root=published / "scratch",
                source_digest=source_digest,
                verifier_digest=verifier_digest,
            )
            self.verify(capsule)
            self._seal_tree(capsule.source_root)
            self._seal_tree(capsule.verifier_root)
            (capsule.root / "capsule.json").chmod(0o400)
            return capsule
        except WorkspaceDigestError:
            self._discard_staging(staging)
            raise
        except (OSError, RuntimeError, UnicodeError, ValueError):
            self._discard_staging(staging)
            raise WorkspaceDigestError() from None

    def verify(self, capsule: VerificationCapsule) -> None:
        try:
            expected_root = self._store_root / str(capsule.capsule_id)
            if capsule.root != expected_root or not capsule.root.is_dir():
                raise WorkspaceDigestError()
            manifest = json.loads((capsule.root / "capsule.json").read_text("utf-8"))
            expected_manifest = {
                "algorithm_version": capsule.algorithm_version,
                "capsule_id": str(capsule.capsule_id),
                "execution_id": str(capsule.execution_id),
                "source_digest": capsule.source_digest,
                "verifier_digest": capsule.verifier_digest,
            }
            if manifest != expected_manifest:
                raise WorkspaceDigestError()
            if self._digester.digest(capsule.source_root) != capsule.source_digest:
                raise WorkspaceDigestError()
            if self._digester.digest(capsule.verifier_root) != capsule.verifier_digest:
                raise WorkspaceDigestError()
        except WorkspaceDigestError:
            raise
        except (OSError, RuntimeError, UnicodeError, ValueError, json.JSONDecodeError):
            raise WorkspaceDigestError() from None

    def launch_profile(
        self, profile: TestProfile, capsule: VerificationCapsule
    ) -> TestProfile:
        mounts = {
            "{SOURCE}": capsule.source_root,
            "{VERIFIER}": capsule.verifier_root,
            "{SCRATCH}": capsule.scratch_root,
        }
        expanded: list[str] = [profile.executable_path]
        previous: str | None = None
        for argument in profile.argv[1:]:
            match = _CAPSULE_REFERENCE.fullmatch(argument)
            if match is not None:
                marker, suffix = match.groups()
                root = mounts[marker]
                replacement = str(
                    self._validated_mount_target(
                        root, () if suffix is None else tuple(suffix.split("/"))
                    )
                )
            elif self._safe_command_literal(argument, previous=previous):
                replacement = argument
            else:
                raise WorkspaceDigestError()
            expanded.append(replacement)
            previous = argument
        if any(
            name.upper() in _RESERVED_VERIFICATION_ENV
            or not self._safe_environment_value(value)
            for name, value in profile.allowed_env.items()
        ):
            raise WorkspaceDigestError()
        environment = dict(profile.allowed_env)
        python_source_root = capsule.source_root
        formal_workspace_source = capsule.source_root / "workspace"
        if formal_workspace_source.is_dir():
            # Formal evaluator workspaces keep model-visible source below the
            # fixed ``workspace`` mount. The selected directory is inside the
            # already sealed source artifact, never an active-workspace path.
            python_source_root = formal_workspace_source
        environment.update(
            {
                "PYTHONPATH": str(python_source_root),
                "PYTHONNOUSERSITE": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            }
        )
        if os.name == "nt":
            # SYSTEM_RUNTIME is explicitly NON_HERMETIC. CPython on Windows
            # requires these non-secret OS roots to initialize core extension
            # modules; copy only this closed allowlist into the ephemeral launch
            # profile, never into trusted profile facts or receipts.
            environment.update(
                {
                    name: value
                    for name in ("SYSTEMROOT", "WINDIR")
                    if (value := os.environ.get(name)) is not None
                }
            )
        return profile.model_copy(
            update={
                "argv": tuple(expanded),
                "cwd": str(capsule.source_root),
                "allowed_env": environment,
            }
        )

    @staticmethod
    def _safe_command_literal(argument: str, *, previous: str | None) -> bool:
        return bool(
            argument == unicodedata.normalize("NFC", argument)
            and "\x00" not in argument
            and "\\" not in argument
            and "/" not in argument
            and ":" not in argument
            and not PurePosixPath(argument).is_absolute()
            and not PureWindowsPath(argument).is_absolute()
            and (
                _SAFE_VERIFICATION_FLAG.fullmatch(argument)
                or (
                    previous == "-m" and _SAFE_MODULE_LITERAL.fullmatch(argument)
                )
            )
        )

    @staticmethod
    def _safe_environment_value(value: str) -> bool:
        return bool(
            value == unicodedata.normalize("NFC", value)
            and "\x00" not in value
            and "\\" not in value
            and "/" not in value
            and ":" not in value
            and _SAFE_VERIFICATION_LITERAL.fullmatch(value)
        )

    @staticmethod
    def _validated_mount_target(root: Path, parts: tuple[str, ...]) -> Path:
        candidate = root.joinpath(*parts)
        root_key = os.path.normcase(os.path.abspath(root))
        candidate_key = os.path.normcase(os.path.abspath(candidate))
        try:
            if os.path.commonpath((root_key, candidate_key)) != root_key:
                raise WorkspaceDigestError()
            resolved_root = root.resolve(strict=True)
            resolved_candidate = candidate.resolve(strict=False)
            if os.path.commonpath(
                (
                    os.path.normcase(str(resolved_root)),
                    os.path.normcase(str(resolved_candidate)),
                )
            ) != os.path.normcase(str(resolved_root)):
                raise WorkspaceDigestError()
        except (OSError, RuntimeError, ValueError):
            raise WorkspaceDigestError() from None
        return candidate

    def _copy_tree(self, source: Path, target: Path) -> str:
        entries: list[WorkspaceDigestEntry] = []
        collision_keys: set[str] = set()
        total_entries = 0
        total_bytes = 0

        def visit(source_dir: Path, target_dir: Path, parts: tuple[str, ...]) -> None:
            nonlocal total_entries, total_bytes
            before = os.lstat(source_dir)
            self._require_directory(before)
            with os.scandir(source_dir) as scanned:
                children = sorted(tuple(scanned), key=lambda item: os.fsencode(item.name))
            for child in children:
                total_entries += 1
                if total_entries > self._limits.max_entries:
                    raise WorkspaceDigestError()
                child_stat = child.stat(follow_symlinks=False)
                name = unicodedata.normalize("NFC", child.name)
                if not name or name != child.name or "/" in name or "\x00" in name:
                    raise WorkspaceDigestError()
                child_parts = (*parts, name)
                relative = "/".join(child_parts)
                collision = relative.casefold()
                if collision in collision_keys:
                    raise WorkspaceDigestError()
                collision_keys.add(collision)
                source_path = Path(child.path)
                target_path = target_dir / name
                if stat.S_ISLNK(child_stat.st_mode):
                    target_bytes = self._copy_symlink(
                        source_path,
                        target_path,
                        child_stat,
                        root=source,
                    )
                    if len(entries) >= self._limits.max_files:
                        raise WorkspaceDigestError()
                    if len(target_bytes) > self._limits.max_file_bytes:
                        raise WorkspaceDigestError()
                    total_bytes += len(target_bytes)
                    if total_bytes > self._limits.max_total_bytes:
                        raise WorkspaceDigestError()
                    entries.append(
                        WorkspaceDigestEntry(
                            relative_path=relative,
                            size_bytes=len(target_bytes),
                            content_sha256=self._digester.content_digest(
                                b"agentforge-symlink-target-v1\0" + target_bytes
                            ),
                            entry_kind="SYMLINK",
                        )
                    )
                    continue
                self._reject_link_reparse_or_special(child_stat, allow_directory=True)
                if stat.S_ISDIR(child_stat.st_mode):
                    if name in _EXCLUDED_DIRECTORY_NAMES:
                        continue
                    os.mkdir(target_path, 0o700)
                    visit(source_path, target_path, child_parts)
                    continue
                if not stat.S_ISREG(child_stat.st_mode):
                    raise WorkspaceDigestError()
                if len(entries) >= self._limits.max_files:
                    raise WorkspaceDigestError()
                size, digest = self._copy_regular(source_path, target_path, child_stat)
                total_bytes += size
                if total_bytes > self._limits.max_total_bytes:
                    raise WorkspaceDigestError()
                entries.append(WorkspaceDigestEntry(relative, size, digest))
            after = os.lstat(source_dir)
            self._require_same_identity(before, after)

        visit(source, target, ())
        ordered = tuple(sorted(entries, key=self._digester._entry_sort_key))
        return self._digester._digest_entries(ordered)

    def _copy_symlink(
        self,
        source: Path,
        target: Path,
        scanned: os.stat_result,
        *,
        root: Path,
    ) -> bytes:
        """Copy only an already-approved link payload, never its referent bytes."""

        source_before = os.lstat(source)
        WorkspaceDigester._require_same_symlink_object(scanned, source_before)
        target_bytes = self._digester._read_safe_posix_symlink_target(
            source, source_before, root_path=root
        )
        source_after = os.lstat(source)
        WorkspaceDigester._require_same_symlink_object(source_before, source_after)
        target_text = os.fsdecode(target_bytes)
        try:
            os.lstat(target)
        except FileNotFoundError:
            pass
        else:
            raise WorkspaceDigestError()
        os.symlink(target_text, target)
        target_before = os.lstat(target)
        if not stat.S_ISLNK(target_before.st_mode) or os.readlink(target) != target_text:
            raise WorkspaceDigestError()
        target_after = os.lstat(target)
        WorkspaceDigester._require_same_symlink_object(target_before, target_after)
        source_final = os.lstat(source)
        WorkspaceDigester._require_same_symlink_object(source_after, source_final)
        return target_bytes

    def _copy_regular(
        self, source: Path, target: Path, scanned: os.stat_result
    ) -> tuple[int, str]:
        if scanned.st_size > self._limits.max_file_bytes:
            raise WorkspaceDigestError()
        read_flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        write_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        source_fd = os.open(source, read_flags)
        target_fd: int | None = None
        try:
            opened = os.fstat(source_fd)
            self._require_same_identity(scanned, opened)
            if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
                raise WorkspaceDigestError()
            target_fd = os.open(target, write_flags, 0o600)
            digest = hashlib.sha256()
            total = 0
            while True:
                chunk = os.read(source_fd, min(65_536, self._limits.max_file_bytes + 1 - total))
                if not chunk:
                    break
                total += len(chunk)
                if total > self._limits.max_file_bytes:
                    raise WorkspaceDigestError()
                digest.update(chunk)
                view = memoryview(chunk)
                while view:
                    written = os.write(target_fd, view)
                    if written <= 0:
                        raise WorkspaceDigestError()
                    view = view[written:]
            os.fsync(target_fd)
            source_after = os.fstat(source_fd)
            path_after = os.lstat(source)
            self._require_same_identity(opened, source_after)
            self._require_same_identity(source_after, path_after)
            if total != source_after.st_size:
                raise WorkspaceDigestError()
            target_stat = os.fstat(target_fd)
            if target_stat.st_nlink != 1 or target_stat.st_size != total:
                raise WorkspaceDigestError()
            return total, digest.hexdigest()
        finally:
            if target_fd is not None:
                os.close(target_fd)
            os.close(source_fd)

    def _prepare_store(
        self, source: Path, verifier: Path
    ) -> tuple[Path, tuple[os.stat_result, ...]]:
        store = Path(os.path.abspath(self._store_root))
        for input_root in (source, verifier):
            if self._overlaps(store, input_root):
                raise WorkspaceDigestError()
        store.mkdir(mode=0o700, parents=True, exist_ok=True)
        validated, chain = WorkspaceDigester._validate_root_chain(store)
        if validated != store:
            raise WorkspaceDigestError()
        item = chain[-1]
        self._require_directory(item)
        self._reject_link_reparse_or_special(item, allow_directory=True)
        return store, chain

    @staticmethod
    def _revalidate_store(
        store: Path, expected: tuple[os.stat_result, ...]
    ) -> None:
        validated, current = WorkspaceDigester._validate_root_chain(store)
        if validated != store or len(current) != len(expected):
            raise WorkspaceDigestError()
        for before, after in zip(expected, current, strict=True):
            if (
                (before.st_dev and after.st_dev and before.st_dev != after.st_dev)
                or (before.st_ino and after.st_ino and before.st_ino != after.st_ino)
                or stat.S_IFMT(before.st_mode) != stat.S_IFMT(after.st_mode)
            ):
                raise WorkspaceDigestError()

    @staticmethod
    def _validated_input_root(root: Path) -> Path:
        if not root.is_absolute():
            raise WorkspaceDigestError()
        validated, _ = WorkspaceDigester._validate_root_chain(root)
        if not stat.S_ISDIR(os.lstat(validated).st_mode):
            raise WorkspaceDigestError()
        return validated

    @staticmethod
    def _overlaps(left: Path, right: Path) -> bool:
        left_key = os.path.normcase(str(left))
        right_key = os.path.normcase(str(right))
        try:
            return os.path.commonpath((left_key, right_key)) in {left_key, right_key}
        except ValueError:
            return False

    @staticmethod
    def _reject_link_reparse_or_special(
        item: os.stat_result, *, allow_directory: bool = False
    ) -> None:
        reparse = getattr(item, "st_file_attributes", 0) & getattr(
            stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400
        )
        allowed = stat.S_ISREG(item.st_mode) or (allow_directory and stat.S_ISDIR(item.st_mode))
        if stat.S_ISLNK(item.st_mode) or reparse or not allowed:
            raise WorkspaceDigestError()

    @staticmethod
    def _require_directory(item: os.stat_result) -> None:
        if not stat.S_ISDIR(item.st_mode):
            raise WorkspaceDigestError()

    @staticmethod
    def _require_same_identity(before: os.stat_result, after: os.stat_result) -> None:
        if (
            (before.st_dev and after.st_dev and before.st_dev != after.st_dev)
            or (before.st_ino and after.st_ino and before.st_ino != after.st_ino)
            or stat.S_IFMT(before.st_mode) != stat.S_IFMT(after.st_mode)
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
        ):
            raise WorkspaceDigestError()

    @staticmethod
    def _write_manifest(path: Path, payload: dict[str, object]) -> None:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            data = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
            view = memoryview(data)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise WorkspaceDigestError()
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        if os.name == "nt":
            return
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _seal_tree(root: Path) -> None:
        for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
            item = os.lstat(path)
            if stat.S_ISLNK(item.st_mode):
                continue
            VerificationCapsuleBuilder._chmod_no_follow(
                path, 0o500 if stat.S_ISDIR(item.st_mode) else 0o400
            )
        root_item = os.lstat(root)
        if stat.S_ISLNK(root_item.st_mode):
            raise WorkspaceDigestError()
        VerificationCapsuleBuilder._chmod_no_follow(root, 0o500)

    @staticmethod
    def _chmod_no_follow(path: Path, mode: int) -> None:
        # Windows' Python cannot request no-follow chmod.  Sealing is integrity
        # evidence rather than an ACL claim, so decline the chmod instead of
        # falling back to an operation that could traverse a reparse point.
        if os.chmod not in os.supports_follow_symlinks:
            return
        os.chmod(path, mode, follow_symlinks=False)

    @staticmethod
    def _discard_staging(staging: Path) -> None:
        try:
            staging_item = os.lstat(staging)
        except FileNotFoundError:
            return
        if not stat.S_ISDIR(staging_item.st_mode):
            return
        for path in sorted(staging.rglob("*"), key=lambda item: len(item.parts), reverse=True):
            try:
                item = os.lstat(path)
                if stat.S_ISDIR(item.st_mode):
                    path.rmdir()
                else:
                    path.unlink()
            except OSError:
                continue
        try:
            staging.rmdir()
        except OSError:
            pass
