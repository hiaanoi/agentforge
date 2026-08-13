import hashlib
import json
import shutil
from pathlib import Path
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from agentforge.domain.models import UtcDatetime, utc_now
from agentforge.evaluation.formal_fixtures import FormalFixtureManifest
from agentforge.evaluation.workspace import WorkspaceBaselineBuilder
from agentforge.tools.paths import WorkspacePathResolver


class PilotWorkspaceBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    campaign_id: UUID
    slot_id: UUID
    attempt_id: UUID
    protocol_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    fixture_asset_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class PilotWorkspaceLease(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    lease_schema_version: int = 1
    lease_id: UUID
    campaign_id: UUID
    slot_id: UUID
    attempt_id: UUID
    protocol_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    fixture_asset_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    model_workspace: Path
    hidden_test_root: Path
    lease_file: Path
    workspace_root_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    initial_workspace_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    hidden_test_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    nonce: UUID
    created_at: UtcDatetime

    @property
    def binding(self) -> PilotWorkspaceBinding:
        return PilotWorkspaceBinding(
            campaign_id=self.campaign_id,
            slot_id=self.slot_id,
            attempt_id=self.attempt_id,
            protocol_digest=self.protocol_digest,
            fixture_asset_digest=self.fixture_asset_digest,
        )


class PilotWorkspaceManager:
    def __init__(self, root: Path) -> None:
        self._root = root.resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    def create(
        self,
        manifest: FormalFixtureManifest,
        binding: PilotWorkspaceBinding,
    ) -> PilotWorkspaceLease:
        if manifest.asset_digest != binding.fixture_asset_digest:
            raise ValueError("Fixture asset digest does not match workspace binding")
        attempt_root = self._attempt_root(binding)
        attempt_root.mkdir(parents=True, exist_ok=False)
        model_workspace = attempt_root / "model_workspace"
        hidden_test_root = attempt_root / "evaluator_hidden" / "tests" / "hidden"
        lease_file = attempt_root / "lease.json"
        try:
            shutil.copytree(
                manifest.root / "workspace",
                model_workspace / "workspace",
                symlinks=False,
            )
            shutil.copytree(
                manifest.root / "tests" / "visible",
                model_workspace / "tests" / "visible",
                symlinks=False,
            )
            shutil.copytree(
                manifest.root / "tests" / "hidden",
                hidden_test_root,
                symlinks=False,
            )
            workspace_scan = WorkspaceBaselineBuilder(
                WorkspacePathResolver(model_workspace)
            ).scan()
            hidden_scan = WorkspaceBaselineBuilder(
                WorkspacePathResolver(hidden_test_root)
            ).scan()
            lease = PilotWorkspaceLease(
                lease_id=uuid4(),
                **binding.model_dump(),
                model_workspace=model_workspace.resolve(strict=True),
                hidden_test_root=hidden_test_root.resolve(strict=True),
                lease_file=lease_file.resolve(),
                workspace_root_digest=self._path_digest(model_workspace),
                initial_workspace_digest=workspace_scan.root_digest,
                hidden_test_digest=hidden_scan.root_digest,
                nonce=uuid4(),
                created_at=utc_now(),
            )
            with lease_file.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(
                    json.dumps(
                        lease.model_dump(mode="json"),
                        ensure_ascii=True,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                )
            return lease
        except Exception:
            self._remove_attempt_root(attempt_root)
            raise

    def reopen(
        self,
        lease_id: UUID,
        binding: PilotWorkspaceBinding,
    ) -> PilotWorkspaceLease:
        lease = self.reopen_for_binding(binding)
        if lease.lease_id != lease_id:
            raise ValueError("Pilot workspace lease ID does not match")
        return lease

    def reopen_for_binding(
        self,
        binding: PilotWorkspaceBinding,
    ) -> PilotWorkspaceLease:
        attempt_root = self._attempt_root(binding)
        lease_file = attempt_root / "lease.json"
        try:
            raw = json.loads(lease_file.read_text(encoding="utf-8"))
            lease = PilotWorkspaceLease.model_validate(raw)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise ValueError("Pilot workspace lease is invalid") from exc
        if lease.binding != binding:
            raise ValueError("Pilot workspace lease binding does not match")
        expected_model = (attempt_root / "model_workspace").resolve(strict=True)
        expected_hidden = (
            attempt_root / "evaluator_hidden" / "tests" / "hidden"
        ).resolve(strict=True)
        if (
            lease.model_workspace.resolve(strict=True) != expected_model
            or lease.hidden_test_root.resolve(strict=True) != expected_hidden
            or lease.lease_file.resolve(strict=True) != lease_file.resolve(strict=True)
            or lease.workspace_root_digest != self._path_digest(expected_model)
        ):
            raise ValueError("Pilot workspace lease paths do not match")
        WorkspaceBaselineBuilder(WorkspacePathResolver(expected_model)).scan()
        hidden_scan = WorkspaceBaselineBuilder(
            WorkspacePathResolver(expected_hidden)
        ).scan()
        if hidden_scan.root_digest != lease.hidden_test_digest:
            raise ValueError("Evaluator hidden tests changed after lease creation")
        return lease

    def cleanup(self, lease: PilotWorkspaceLease, *, terminal: bool) -> bool:
        if not terminal:
            raise ValueError("Pilot workspace cleanup requires a terminal attempt")
        attempt_root = lease.lease_file.parent
        if not attempt_root.exists():
            return False
        persisted = self.reopen(lease.lease_id, lease.binding)
        if persisted != lease:
            raise ValueError("Pilot workspace lease ownership does not match")
        self._remove_attempt_root(attempt_root)
        return True

    def _attempt_root(self, binding: PilotWorkspaceBinding) -> Path:
        candidate = (
            self._root
            / f"c-{binding.campaign_id.hex[:8]}"
            / f"s-{binding.slot_id.hex[:8]}"
            / f"a-{binding.attempt_id.hex[:8]}"
        ).resolve()
        if self._root != candidate and self._root not in candidate.parents:
            raise ValueError("Pilot workspace path escapes the manager root")
        return candidate

    def _remove_attempt_root(self, attempt_root: Path) -> None:
        resolved = attempt_root.resolve()
        if self._root == resolved or self._root not in resolved.parents:
            raise ValueError("Pilot workspace cleanup target is unsafe")
        if resolved.exists():
            shutil.rmtree(resolved)
        parent = resolved.parent
        while parent != self._root and self._root in parent.parents:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent

    @staticmethod
    def _path_digest(path: Path) -> str:
        return hashlib.sha256(
            str(path.resolve()).encode("utf-8")
        ).hexdigest()
