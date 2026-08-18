from __future__ import annotations

import hashlib
import os
import stat
import struct
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import exists, select, update
from sqlalchemy.orm import Session

from agentforge.application.kernel_errors import (
    SourceRevisionConflictError,
    WorkspaceDigestError,
)
from agentforge.domain.models import utc_now
from agentforge.persistence.product_tables import WorkspaceSourceBindingRow
from agentforge.persistence.run_leases import RunLeaseStore, claim_bound_write

if TYPE_CHECKING:
    from agentforge.persistence.event_log import RunLeaseAuthority

DIGEST_ALGORITHM_VERSION = 1
BOUND_SOURCE_REVISION_SEMANTICS = "BOUND_REVISION_V1"
UNBOUND_SOURCE_REVISION_SEMANTICS = "UNBOUND_EVALUATOR_ONLY"
_DIGEST_PATTERN = r"^[0-9a-f]{64}$"
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_EXCLUDED_DIRECTORY_NAMES = frozenset(
    {
        ".agentforge",
        ".git",
        ".mypy_cache",
        ".nox",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
        "venv",
    }
)


@dataclass(frozen=True, slots=True)
class WorkspaceDigestLimits:
    # Counts every directory entry observed, including an excluded directory itself;
    # descendants of an excluded directory are never enumerated and therefore not counted.
    max_entries: int = 50_000
    max_files: int = 20_000
    max_file_bytes: int = 16 * 1024 * 1024
    max_total_bytes: int = 256 * 1024 * 1024

    def __post_init__(self) -> None:
        if any(
            type(value) is not int or value <= 0
            for value in (
                self.max_entries,
                self.max_files,
                self.max_file_bytes,
                self.max_total_bytes,
            )
        ):
            raise ValueError("workspace digest limits must be positive exact integers")


@dataclass(frozen=True, slots=True)
class WorkspaceDigestEntry:
    relative_path: str
    size_bytes: int
    content_sha256: str
    executable_bit: bool = False
    content_kind: Literal["TEXT", "BINARY"] = "BINARY"
    entry_kind: Literal["REGULAR_FILE", "SYMLINK"] = "REGULAR_FILE"


@dataclass(frozen=True, slots=True)
class WorkspaceSnapshot:
    algorithm_version: int
    digest: str
    entries: tuple[WorkspaceDigestEntry, ...]


class SourceRevision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    run_id: UUID
    initial_source_digest: str = Field(pattern=_DIGEST_PATTERN)
    expected_source_digest: str = Field(pattern=_DIGEST_PATTERN)
    source_revision_number: int = Field(ge=0)
    digest_algorithm_version: Literal[1]

    @model_validator(mode="before")
    @classmethod
    def require_exact_integers(cls, value: Any) -> Any:
        if isinstance(value, dict):
            for name in ("source_revision_number", "digest_algorithm_version"):
                if name in value and type(value[name]) is not int:
                    raise ValueError(f"{name} must be an exact integer")
        return value


class MutationRecoveryAction(StrEnum):
    RETRY = "RETRY"
    FINALIZE = "FINALIZE"
    MARK_INDETERMINATE = "MARK_INDETERMINATE"


def source_revision_audit(source_bound: bool, *, actual_digest_verified: bool) -> tuple[str, bool]:
    semantics = (
        BOUND_SOURCE_REVISION_SEMANTICS if source_bound else UNBOUND_SOURCE_REVISION_SEMANTICS
    )
    return semantics, source_bound and actual_digest_verified


def source_revision_summary(
    source_bound: bool, *, actual_digest_verified: bool, message: str
) -> str:
    semantics, verified = source_revision_audit(
        source_bound, actual_digest_verified=actual_digest_verified
    )
    # Transitional v4 encoding. A later schema version should persist these audit
    # facts in dedicated columns instead of parsing a human-readable summary.
    return f"{semantics}|source_verified={str(verified).lower()}: {message}"


def source_revision_audit_from_summary(summary: str | None) -> tuple[str, bool]:
    if not summary:
        return source_revision_audit(False, actual_digest_verified=False)
    prefix, separator, body = summary.partition(":")
    if separator != ":" or any(
        marker in body
        for marker in (
            f"{BOUND_SOURCE_REVISION_SEMANTICS}|source_verified=",
            f"{UNBOUND_SOURCE_REVISION_SEMANTICS}|source_verified=",
        )
    ):
        return source_revision_audit(False, actual_digest_verified=False)
    if prefix == f"{BOUND_SOURCE_REVISION_SEMANTICS}|source_verified=true":
        return source_revision_audit(True, actual_digest_verified=True)
    if prefix == f"{BOUND_SOURCE_REVISION_SEMANTICS}|source_verified=false":
        return source_revision_audit(True, actual_digest_verified=False)
    if prefix == f"{UNBOUND_SOURCE_REVISION_SEMANTICS}|source_verified=false":
        return source_revision_audit(False, actual_digest_verified=False)
    return source_revision_audit(False, actual_digest_verified=False)


def classify_writing_recovery(
    actual: str, *, before: str, expected_after: str
) -> MutationRecoveryAction:
    if actual == before:
        return MutationRecoveryAction.RETRY
    if actual == expected_after:
        return MutationRecoveryAction.FINALIZE
    return MutationRecoveryAction.MARK_INDETERMINATE


class WorkspaceDigester:
    """Versioned byte-exact workspace digest with fail-closed traversal."""

    def __init__(
        self,
        *,
        limits: WorkspaceDigestLimits | None = None,
        algorithm_version: int = DIGEST_ALGORITHM_VERSION,
    ) -> None:
        if type(algorithm_version) is not int or algorithm_version != DIGEST_ALGORITHM_VERSION:
            raise WorkspaceDigestError()
        self._limits = limits or WorkspaceDigestLimits()
        self._algorithm_version = algorithm_version

    @staticmethod
    def content_digest(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    def digest(self, root: Path) -> str:
        return self.snapshot(root).digest

    def snapshot(self, root: Path) -> WorkspaceSnapshot:
        return self._snapshot(
            root,
            exclude_directory=lambda parts: parts[-1] in _EXCLUDED_DIRECTORY_NAMES,
        )

    def inventory_snapshot(
        self,
        root: Path,
        *,
        exclude_directory: Callable[[tuple[str, ...]], bool],
    ) -> WorkspaceSnapshot:
        """Capture a caller-defined inventory with the same hardened traversal.

        The caller receives no filesystem handles and cannot relax the root/link,
        collision, TOCTOU, or bounded-resource checks.  This is deliberately a
        product seam rather than the evaluator's legacy read-by-path scanner.
        """

        return self._snapshot(root, exclude_directory=exclude_directory)

    def _snapshot(
        self,
        root: Path,
        *,
        exclude_directory: Callable[[tuple[str, ...]], bool],
    ) -> WorkspaceSnapshot:
        try:
            root_path, root_chain_before = self._validate_root_chain(root)
            root_before = os.lstat(root_path)
            self._require_directory(root_before)
            entries: list[WorkspaceDigestEntry] = []
            collision_keys: set[str] = set()
            total_bytes = 0
            total_entries = 0

            def visit(
                directory: Path,
                relative_parts: tuple[str, ...],
                enumerated_stat: os.stat_result,
            ) -> None:
                nonlocal total_bytes, total_entries
                directory_before = os.lstat(directory)
                self._require_directory(directory_before)
                self._require_same_identity(enumerated_stat, directory_before, directory=True)
                with os.scandir(directory) as scanned:
                    children = sorted(tuple(scanned), key=lambda item: os.fsencode(item.name))
                for child in children:
                    total_entries += 1
                    if total_entries > self._limits.max_entries:
                        raise WorkspaceDigestError()
                    child_stat = child.stat(follow_symlinks=False)
                    self._reject_reparse(child_stat)
                    child_path = Path(child.path)
                    child_path_before = os.lstat(child_path)
                    raw_name = child.name
                    normalized_name = unicodedata.normalize("NFC", raw_name)
                    if not normalized_name or "/" in normalized_name or "\x00" in normalized_name:
                        raise WorkspaceDigestError()
                    child_parts = (*relative_parts, normalized_name)
                    relative = "/".join(child_parts)
                    collision_key = unicodedata.normalize("NFC", relative).casefold()
                    if collision_key in collision_keys:
                        raise WorkspaceDigestError()
                    collision_keys.add(collision_key)
                    mode = child_stat.st_mode
                    if stat.S_ISLNK(mode):
                        target_bytes = self._read_safe_posix_symlink_target(
                            child_path,
                            child_stat,
                            root_path=root_path,
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
                                content_sha256=self.content_digest(
                                    b"agentforge-symlink-target-v1\0" + target_bytes
                                ),
                                entry_kind="SYMLINK",
                            )
                        )
                        continue
                    self._require_same_object(child_stat, child_path_before)
                    if stat.S_ISDIR(mode):
                        if exclude_directory(child_parts):
                            continue
                        visit(child_path, child_parts, child_path_before)
                        child_path_after = os.lstat(child_path)
                        self._require_same_identity(
                            child_path_before, child_path_after, directory=True
                        )
                        continue
                    if not stat.S_ISREG(mode):
                        raise WorkspaceDigestError()
                    if len(entries) >= self._limits.max_files:
                        raise WorkspaceDigestError()
                    data = self._read_regular_file(child_path, child_path_before)
                    total_bytes += len(data)
                    if total_bytes > self._limits.max_total_bytes:
                        raise WorkspaceDigestError()
                    entries.append(
                        WorkspaceDigestEntry(
                            relative_path=relative,
                            size_bytes=len(data),
                            content_sha256=self.content_digest(data),
                            executable_bit=bool(
                                child_stat.st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
                            ),
                            content_kind=self._content_kind(data),
                        )
                    )
                directory_after = os.lstat(directory)
                self._require_same_identity(directory_before, directory_after, directory=True)

            visit(root_path, (), root_before)
            validated_root, root_chain_after = self._validate_root_chain(root)
            if validated_root != root_path or len(root_chain_after) != len(root_chain_before):
                raise WorkspaceDigestError()
            for before_component, after_component in zip(
                root_chain_before, root_chain_after, strict=True
            ):
                self._require_same_object(before_component, after_component)
            root_after = os.lstat(root_path)
            self._require_same_identity(root_before, root_after, directory=True)
            ordered = tuple(sorted(entries, key=self._entry_sort_key))
            return WorkspaceSnapshot(
                algorithm_version=self._algorithm_version,
                digest=self._digest_entries(ordered),
                entries=ordered,
            )
        except WorkspaceDigestError:
            raise
        except (OSError, RuntimeError, UnicodeError, ValueError):
            raise WorkspaceDigestError() from None

    @classmethod
    def _validate_root_chain(cls, root: Path) -> tuple[Path, tuple[os.stat_result, ...]]:
        raw = os.fspath(root)
        if not isinstance(raw, (str, bytes)) or not raw or "\x00" in os.fsdecode(raw):
            raise WorkspaceDigestError()
        candidate = Path(root)
        if not candidate.is_absolute() or not candidate.anchor:
            raise WorkspaceDigestError()
        relative_parts = candidate.parts[1:]
        if any(part in {"", ".", ".."} for part in relative_parts):
            raise WorkspaceDigestError()
        current = Path(candidate.anchor)
        anchor_stat = os.lstat(current)
        cls._require_directory(anchor_stat)
        chain = [anchor_stat]
        for index, part in enumerate(relative_parts):
            current = current / part
            component = os.lstat(current)
            cls._reject_link_or_reparse(component)
            if index < len(relative_parts) - 1 and not stat.S_ISDIR(component.st_mode):
                raise WorkspaceDigestError()
            chain.append(component)
        return candidate, tuple(chain)

    def project_digest(
        self,
        snapshot: WorkspaceSnapshot,
        *,
        relative_path: str,
        size_bytes: int,
        content_sha256: str,
    ) -> str:
        if (
            snapshot.algorithm_version != self._algorithm_version
            or type(size_bytes) is not int
            or size_bytes < 0
            or size_bytes > self._limits.max_file_bytes
            or not self._valid_digest(content_sha256)
        ):
            raise WorkspaceDigestError()
        normalized = self._normalize_relative_path(relative_path)
        replacement = WorkspaceDigestEntry(normalized, size_bytes, content_sha256)
        by_path = {entry.relative_path: entry for entry in snapshot.entries}
        by_path[normalized] = replacement
        collision_keys: set[str] = set()
        for path in by_path:
            key = unicodedata.normalize("NFC", path).casefold()
            if key in collision_keys:
                raise WorkspaceDigestError()
            collision_keys.add(key)
        entries = tuple(sorted(by_path.values(), key=self._entry_sort_key))
        if len(entries) > self._limits.max_files:
            raise WorkspaceDigestError()
        if sum(entry.size_bytes for entry in entries) > self._limits.max_total_bytes:
            raise WorkspaceDigestError()
        return self._digest_entries(entries)

    def _read_regular_file(self, path: Path, scanned_stat: os.stat_result) -> bytes:
        if scanned_stat.st_size > self._limits.max_file_bytes:
            raise WorkspaceDigestError()
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            opened_before = os.fstat(descriptor)
            self._reject_link_or_reparse(opened_before)
            if not stat.S_ISREG(opened_before.st_mode):
                raise WorkspaceDigestError()
            self._require_same_identity(scanned_stat, opened_before)
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = os.read(descriptor, min(65_536, self._limits.max_file_bytes + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > self._limits.max_file_bytes:
                    raise WorkspaceDigestError()
            opened_after = os.fstat(descriptor)
            self._require_same_identity(opened_before, opened_after)
            path_after = os.lstat(path)
            self._require_same_identity(opened_after, path_after)
            data = b"".join(chunks)
            if len(data) != opened_after.st_size:
                raise WorkspaceDigestError()
            return data
        finally:
            os.close(descriptor)

    def _read_safe_posix_symlink_target(
        self,
        path: Path,
        scanned_stat: os.stat_result,
        *,
        root_path: Path,
    ) -> bytes:
        if os.name != "posix":
            raise WorkspaceDigestError()
        link_before = os.lstat(path)
        self._require_same_symlink_object(scanned_stat, link_before)
        target_text = os.readlink(path)
        link_after = os.lstat(path)
        self._require_same_symlink_object(link_before, link_after)
        if type(target_text) is not str or "\x00" in target_text or os.path.isabs(target_text):
            raise WorkspaceDigestError()
        candidate = os.path.normpath(os.path.join(os.fspath(path.parent), target_text))
        try:
            if os.path.commonpath((os.fspath(root_path), candidate)) != os.fspath(root_path):
                raise WorkspaceDigestError()
        except ValueError:
            raise WorkspaceDigestError() from None
        return os.fsencode(target_text)

    def _digest_entries(self, entries: tuple[WorkspaceDigestEntry, ...]) -> str:
        digest = hashlib.sha256()
        digest.update(b"agentforge-workspace-source\x00")
        digest.update(struct.pack(">I", self._algorithm_version))
        digest.update(struct.pack(">Q", len(entries)))
        for entry in entries:
            path_bytes = entry.relative_path.encode("utf-8")
            digest.update(b"L" if entry.entry_kind == "SYMLINK" else b"F")
            digest.update(struct.pack(">I", len(path_bytes)))
            digest.update(path_bytes)
            digest.update(struct.pack(">Q", entry.size_bytes))
            digest.update(bytes.fromhex(entry.content_sha256))
        return digest.hexdigest()

    @staticmethod
    def _entry_sort_key(entry: WorkspaceDigestEntry) -> bytes:
        return entry.relative_path.encode("utf-8")

    @staticmethod
    def _content_kind(data: bytes) -> Literal["TEXT", "BINARY"]:
        if b"\x00" in data:
            return "BINARY"
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            return "BINARY"
        return "TEXT"

    @staticmethod
    def _normalize_relative_path(value: str) -> str:
        if type(value) is not str or not value or "\x00" in value:
            raise WorkspaceDigestError()
        portable = value.replace("\\", "/")
        parts = portable.split("/")
        if any(part in {"", ".", ".."} for part in parts):
            raise WorkspaceDigestError()
        normalized = "/".join(unicodedata.normalize("NFC", part) for part in parts)
        return normalized

    @staticmethod
    def _valid_digest(value: object) -> bool:
        if type(value) is not str or len(value) != 64:
            return False
        try:
            bytes.fromhex(value)
        except ValueError:
            return False
        return value == value.lower()

    @staticmethod
    def _reject_link_or_reparse(value: os.stat_result) -> None:
        if stat.S_ISLNK(value.st_mode):
            raise WorkspaceDigestError()
        WorkspaceDigester._reject_reparse(value)

    @staticmethod
    def _reject_reparse(value: os.stat_result) -> None:
        if bool(getattr(value, "st_file_attributes", 0) & _REPARSE_POINT):
            raise WorkspaceDigestError()

    @classmethod
    def _require_same_symlink_object(cls, before: os.stat_result, after: os.stat_result) -> None:
        cls._reject_reparse(before)
        cls._reject_reparse(after)
        if not stat.S_ISLNK(before.st_mode) or not stat.S_ISLNK(after.st_mode):
            raise WorkspaceDigestError()
        stable_fields = ("st_mode", "st_size", "st_mtime_ns")
        if any(getattr(before, field) != getattr(after, field) for field in stable_fields):
            raise WorkspaceDigestError()
        for identity_field in ("st_dev", "st_ino"):
            before_identity = getattr(before, identity_field)
            after_identity = getattr(after, identity_field)
            if before_identity and after_identity and before_identity != after_identity:
                raise WorkspaceDigestError()

    @classmethod
    def _require_directory(cls, value: os.stat_result) -> None:
        cls._reject_link_or_reparse(value)
        if not stat.S_ISDIR(value.st_mode):
            raise WorkspaceDigestError()

    @classmethod
    def _require_same_identity(
        cls,
        before: os.stat_result,
        after: os.stat_result,
        *,
        directory: bool = False,
    ) -> None:
        cls._reject_link_or_reparse(after)
        stable_fields: tuple[str, ...] = ("st_mode", "st_size", "st_mtime_ns")
        if directory:
            stable_fields = (*stable_fields, "st_ctime_ns")
        if any(getattr(before, field) != getattr(after, field) for field in stable_fields):
            raise WorkspaceDigestError()
        for identity_field in ("st_dev", "st_ino"):
            before_identity = getattr(before, identity_field)
            after_identity = getattr(after, identity_field)
            if before_identity and after_identity and before_identity != after_identity:
                raise WorkspaceDigestError()
        if directory and not stat.S_ISDIR(after.st_mode):
            raise WorkspaceDigestError()

    @classmethod
    def _require_same_object(cls, before: os.stat_result, after: os.stat_result) -> None:
        cls._reject_link_or_reparse(after)
        if before.st_mode != after.st_mode:
            raise WorkspaceDigestError()
        for identity_field in ("st_dev", "st_ino"):
            before_identity = getattr(before, identity_field)
            after_identity = getattr(after, identity_field)
            if before_identity and after_identity and before_identity != after_identity:
                raise WorkspaceDigestError()


class SourceRevisionStore:
    """Session-bound source revision queries and CAS updates; never commits."""

    def _evaluator_formal_bootstrap(
        self,
        session: Session,
        run_id: UUID,
        *,
        workspace_root: Path,
        expected_initial_digest: str,
        config_digest: str,
        profile_digest: str,
    ) -> SourceRevision:
        """Bind one formal evaluator Run to a freshly verified source baseline.

        This is a narrow bridge for the formal pilot's legacy Run constructor.
        It computes the authoritative digest itself and is intentionally not used
        by other evaluator or product assembly paths.
        """

        try:
            root = workspace_root.resolve(strict=True)
        except OSError:
            raise SourceRevisionConflictError() from None
        if (
            not root.is_dir()
            or not WorkspaceDigester._valid_digest(expected_initial_digest)
            or not WorkspaceDigester._valid_digest(config_digest)
            or not WorkspaceDigester._valid_digest(profile_digest)
            or WorkspaceDigester().digest(root) != expected_initial_digest
        ):
            raise SourceRevisionConflictError()
        existing = session.get(WorkspaceSourceBindingRow, str(run_id))
        if existing is not None:
            revision = self.get(session, run_id)
            if (
                existing.workspace_root_identity != str(root)
                or revision.initial_source_digest != expected_initial_digest
                or existing.config_digest != config_digest
                or existing.profile_digest != profile_digest
            ):
                raise SourceRevisionConflictError()
            return revision
        now = utc_now()
        session.add(
            WorkspaceSourceBindingRow(
                run_id=str(run_id),
                workspace_root_identity=str(root),
                git_head=None,
                initial_source_digest=expected_initial_digest,
                expected_source_digest=expected_initial_digest,
                source_revision_number=0,
                digest_algorithm_version=DIGEST_ALGORITHM_VERSION,
                config_digest=config_digest,
                profile_digest=profile_digest,
                created_at=now,
                updated_at=now,
            )
        )
        session.flush()
        return self.get(session, run_id)

    def get(self, session: Session, run_id: UUID) -> SourceRevision:
        row = session.get(WorkspaceSourceBindingRow, str(run_id))
        if row is None:
            raise SourceRevisionConflictError()
        if row.digest_algorithm_version != DIGEST_ALGORITHM_VERSION:
            raise SourceRevisionConflictError()
        try:
            return SourceRevision(
                run_id=UUID(row.run_id),
                initial_source_digest=row.initial_source_digest,
                expected_source_digest=row.expected_source_digest,
                source_revision_number=row.source_revision_number,
                digest_algorithm_version=1,
            )
        except (ValueError, TypeError):
            raise SourceRevisionConflictError() from None

    def find(self, session: Session, run_id: UUID) -> SourceRevision | None:
        row = session.scalar(
            select(WorkspaceSourceBindingRow).where(WorkspaceSourceBindingRow.run_id == str(run_id))
        )
        if row is None:
            return None
        return self.get(session, run_id)

    def require_actual(
        self,
        session: Session,
        run_id: UUID,
        actual_digest: str,
        *,
        workspace_root_identity: str,
        authority: RunLeaseAuthority,
    ) -> SourceRevision:
        claim_bound_write(session, run_id, authority)
        row = session.get(WorkspaceSourceBindingRow, str(run_id))
        if row is None or row.workspace_root_identity != workspace_root_identity:
            raise SourceRevisionConflictError()
        revision = self.get(session, run_id)
        if revision.expected_source_digest != actual_digest:
            raise SourceRevisionConflictError()
        return revision

    def advance(
        self,
        session: Session,
        run_id: UUID,
        *,
        before_digest: str,
        expected_after_digest: str,
        expected_revision_number: int,
        authority: RunLeaseAuthority,
    ) -> SourceRevision:
        claim_bound_write(session, run_id, authority)
        if (
            not WorkspaceDigester._valid_digest(before_digest)
            or not WorkspaceDigester._valid_digest(expected_after_digest)
            or type(expected_revision_number) is not int
            or expected_revision_number < 0
        ):
            raise SourceRevisionConflictError()
        result = session.execute(
            update(WorkspaceSourceBindingRow)
            .where(
                WorkspaceSourceBindingRow.run_id == str(run_id),
                WorkspaceSourceBindingRow.expected_source_digest == before_digest,
                WorkspaceSourceBindingRow.source_revision_number == expected_revision_number,
                WorkspaceSourceBindingRow.digest_algorithm_version == DIGEST_ALGORITHM_VERSION,
                exists().where(
                    *RunLeaseStore.write_conditions(authority, now=RunLeaseStore(None).now(session))
                ),
            )
            .values(
                expected_source_digest=expected_after_digest,
                source_revision_number=expected_revision_number + 1,
                updated_at=utc_now(),
            )
        )
        changed = int(getattr(result, "rowcount", 0) or 0)
        if changed != 1:
            raise SourceRevisionConflictError()
        return self.get(session, run_id)
