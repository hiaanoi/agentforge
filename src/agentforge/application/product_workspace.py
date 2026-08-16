"""Product-owned immutable workspace capture and session-bound baseline store.

The evaluator's baseline builder is intentionally not used here: product start
must not open a second transaction and must scan safely while the product's
SQLite state lives below the workspace root.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from agentforge.evaluation.workspace import (
    FileContentKind,
    WorkspaceBaseline,
    WorkspaceFileBaseline,
    WorkspaceScan,
)
from agentforge.persistence.source_revisions import (
    _EXCLUDED_DIRECTORY_NAMES,
    DIGEST_ALGORITHM_VERSION,
    WorkspaceDigester,
    WorkspaceSnapshot,
)
from agentforge.persistence.tables import WorkspaceBaselineFileRow, WorkspaceBaselineRow

PRODUCT_BASELINE_MANIFEST_VERSION = 2
PRODUCT_BASELINE_POLICY_VERSION = 1
_BASELINE_NAMESPACE = UUID("fc2a88da-f84a-584f-9473-560a7d729bed")
_MANIFEST_DOMAIN = "agentforge-product-workspace-baseline-v2"


@dataclass(frozen=True, slots=True)
class PreparedProductWorkspace:
    """One physical capture projected into source-revision and repair facts."""

    source_digest: str
    baseline: WorkspaceBaseline


class ProductWorkspaceCapture:
    """Capture a bounded product workspace without evaluator dependencies."""

    def __init__(self, digester: WorkspaceDigester | None = None) -> None:
        self._digester = digester or WorkspaceDigester()

    def capture(
        self, root: Path, *, task_id: str, command_id: UUID
    ) -> PreparedProductWorkspace:
        if type(task_id) is not str or not task_id:
            raise ValueError("task_id must be a non-empty string")
        if not isinstance(command_id, UUID):
            raise TypeError("command_id must be a UUID")
        # Root runtime state and Git control data are not repairable source. We
        # exclude them before bounded reads so database churn and large Git packs
        # cannot race or exhaust the capture. Nested .agentforge is ordinary user
        # content and is therefore inventoried.
        resolved, inventory, baseline_files = self._inventory(root)
        source_entries = tuple(
            entry
            for entry in inventory.entries
            if not any(
                part in _EXCLUDED_DIRECTORY_NAMES
                for part in entry.relative_path.split("/")[:-1]
            )
        )
        source = WorkspaceSnapshot(
            algorithm_version=DIGEST_ALGORITHM_VERSION,
            digest=self._digester._digest_entries(source_entries),
            entries=source_entries,
        )
        root_digest = self._manifest_digest(baseline_files)
        baseline = WorkspaceBaseline(
            baseline_id=uuid5(_BASELINE_NAMESPACE, str(command_id)),
            task_id=task_id,
            workspace_root=str(resolved),
            root_digest=root_digest,
            manifest_version=PRODUCT_BASELINE_MANIFEST_VERSION,
            files=baseline_files,
        )
        return PreparedProductWorkspace(source_digest=source.digest, baseline=baseline)

    def scan(self, root: Path) -> WorkspaceScan:
        """Product-policy current manifest for diff validation."""

        resolved, _, files = self._inventory(root)
        return WorkspaceScan(
            workspace_root=str(resolved),
            root_digest=self._manifest_digest(files),
            files=files,
        )

    def matches_source(self, root: Path, expected_digest: str) -> bool:
        """Recompute the v1 source projection immediately before driver launch."""

        inventory = self._digester.inventory_snapshot(
            root, exclude_directory=self._excluded_inventory_directory
        )
        source_entries = tuple(
            entry
            for entry in inventory.entries
            if not any(
                part in _EXCLUDED_DIRECTORY_NAMES
                for part in entry.relative_path.split("/")[:-1]
            )
        )
        return self._digester._digest_entries(source_entries) == expected_digest

    def _inventory(
        self, root: Path
    ) -> tuple[Path, WorkspaceSnapshot, tuple[WorkspaceFileBaseline, ...]]:
        inventory = self._digester.inventory_snapshot(
            root, exclude_directory=self._excluded_inventory_directory
        )
        files = tuple(
            WorkspaceFileBaseline(
                relative_path=entry.relative_path,
                sha256=entry.content_sha256,
                size_bytes=entry.size_bytes,
                executable_bit=entry.executable_bit,
                content_kind=FileContentKind(entry.content_kind),
            )
            for entry in inventory.entries
        )
        return root.resolve(strict=True), inventory, files

    @staticmethod
    def _excluded_inventory_directory(parts: tuple[str, ...]) -> bool:
        return parts == (".agentforge",) or parts[-1] == ".git"

    @staticmethod
    def _manifest_digest(files: tuple[WorkspaceFileBaseline, ...]) -> str:
        payload = {
            "domain": _MANIFEST_DOMAIN,
            "inventory_policy_version": PRODUCT_BASELINE_POLICY_VERSION,
            "files": [item.model_dump(mode="json") for item in files],
        }
        canonical = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class WorkspaceBaselineStore:
    """Persist or prove the exact product baseline in a caller-owned Session."""

    def put(self, session: Session, baseline: WorkspaceBaseline) -> WorkspaceBaseline:
        self._validate_manifest(baseline)
        existing = session.get(WorkspaceBaselineRow, str(baseline.baseline_id))
        if existing is None:
            session.add(
                WorkspaceBaselineRow(
                    baseline_id=str(baseline.baseline_id),
                    task_id=baseline.task_id,
                    workspace_root=baseline.workspace_root,
                    root_digest=baseline.root_digest,
                    manifest_version=baseline.manifest_version,
                    created_at=baseline.created_at,
                )
            )
            session.flush()
            for index, entry in enumerate(baseline.files):
                session.add(
                    WorkspaceBaselineFileRow(
                        file_id=str(uuid5(baseline.baseline_id, f"file:{index}")),
                        baseline_id=str(baseline.baseline_id),
                        relative_path=entry.relative_path,
                        sha256=entry.sha256,
                        size_bytes=entry.size_bytes,
                        file_kind=entry.file_kind,
                        executable_bit=entry.executable_bit,
                        is_symlink=entry.is_symlink,
                        is_reparse_point=entry.is_reparse_point,
                        content_kind=entry.content_kind.value,
                    )
                )
            session.flush()
            return baseline
        self._require_exact(session, existing, baseline)
        return baseline

    def get(self, session: Session, baseline_id: UUID) -> WorkspaceBaseline:
        row = session.get(WorkspaceBaselineRow, str(baseline_id))
        if row is None:
            raise RuntimeError("Workspace baseline is missing")
        files = tuple(
            WorkspaceFileBaseline(
                relative_path=item.relative_path,
                sha256=item.sha256,
                size_bytes=item.size_bytes,
                file_kind=item.file_kind,
                executable_bit=item.executable_bit,
                is_symlink=item.is_symlink,
                is_reparse_point=item.is_reparse_point,
                content_kind=FileContentKind(item.content_kind),
            )
            for item in session.scalars(
                select(WorkspaceBaselineFileRow)
                .where(WorkspaceBaselineFileRow.baseline_id == str(baseline_id))
                .order_by(WorkspaceBaselineFileRow.relative_path)
            )
        )
        try:
            baseline = WorkspaceBaseline(
                baseline_id=UUID(row.baseline_id), task_id=row.task_id,
                workspace_root=row.workspace_root, root_digest=row.root_digest,
                manifest_version=row.manifest_version, created_at=row.created_at, files=files,
            )
            self._validate_manifest(baseline)
        except (TypeError, ValueError):
            raise RuntimeError("Workspace baseline conflict") from None
        return baseline

    def _require_exact(
        self, session: Session, existing: WorkspaceBaselineRow, baseline: WorkspaceBaseline
    ) -> None:
        if (
            existing.task_id != baseline.task_id
            or existing.workspace_root != baseline.workspace_root
            or existing.root_digest != baseline.root_digest
            or existing.manifest_version != baseline.manifest_version
        ):
            raise RuntimeError("Workspace baseline conflict")
        actual = self.get(session, baseline.baseline_id)
        if actual.files != baseline.files:
            raise RuntimeError("Workspace baseline conflict")

    @staticmethod
    def _manifest_digest(files: tuple[WorkspaceFileBaseline, ...]) -> str:
        return ProductWorkspaceCapture._manifest_digest(files)

    @classmethod
    def _validate_manifest(cls, baseline: WorkspaceBaseline) -> None:
        """Reject forged v2 manifests before they can enter a caller UoW."""
        if (
            type(baseline) is not WorkspaceBaseline
            or type(baseline.manifest_version) is not int
            or baseline.manifest_version != PRODUCT_BASELINE_MANIFEST_VERSION
            or type(baseline.files) is not tuple
            or any(type(entry) is not WorkspaceFileBaseline for entry in baseline.files)
        ):
            raise RuntimeError("Workspace baseline conflict")
        paths = tuple(entry.relative_path for entry in baseline.files)
        if paths != tuple(sorted(paths)) or len(set(paths)) != len(paths):
            raise RuntimeError("Workspace baseline conflict")
        try:
            expected_digest = cls._manifest_digest(baseline.files)
        except (AttributeError, TypeError, ValueError):
            raise RuntimeError("Workspace baseline conflict") from None
        if baseline.root_digest != expected_digest:
            raise RuntimeError("Workspace baseline conflict")


class ProductWorkspaceBaselineRepository:
    """Read-only product baseline loader; storage writes remain session-bound."""

    def __init__(self, database: object) -> None:
        from agentforge.persistence.database import Database

        if not isinstance(database, Database):
            raise TypeError("database must be a Database")
        self._database = database

    def get_baseline(self, baseline_id: UUID) -> WorkspaceBaseline:
        with self._database.session() as session:
            return WorkspaceBaselineStore().get(session, baseline_id)


class ProductWorkspaceScanner:
    """Scanner protocol adapter for the generic diff validator."""

    def __init__(self, root: Path, capture: ProductWorkspaceCapture | None = None) -> None:
        self._root = root
        self._capture = capture or ProductWorkspaceCapture()

    def scan(self) -> WorkspaceScan:
        return self._capture.scan(self._root)
