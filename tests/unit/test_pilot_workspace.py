import json
from pathlib import Path
from uuid import uuid4

import pytest

from agentforge.evaluation.formal_fixtures import FormalFixtureLoader
from agentforge.evaluation.pilot_workspace import (
    PilotWorkspaceBinding,
    PilotWorkspaceManager,
)

ROOT = Path(__file__).resolve().parents[2]
TASK_ROOT = (
    ROOT
    / "evaluation"
    / "fixtures"
    / "tasks"
    / "self-durable-double-consumption"
)
SHA = "a" * 64


def binding() -> PilotWorkspaceBinding:
    return PilotWorkspaceBinding(
        campaign_id=uuid4(),
        slot_id=uuid4(),
        attempt_id=uuid4(),
        protocol_digest=SHA,
        fixture_asset_digest=FormalFixtureLoader().load(TASK_ROOT).asset_digest,
    )


def test_workspace_lease_is_fresh_isolated_and_reopenable(tmp_path: Path) -> None:
    manifest = FormalFixtureLoader().load(TASK_ROOT)
    manager = PilotWorkspaceManager(tmp_path / "agentforge-pilots")
    first_binding = binding()

    lease = manager.create(manifest, first_binding)
    reopened = manager.reopen(lease.lease_id, first_binding)

    assert reopened == lease
    assert lease.model_workspace.is_dir()
    assert (lease.model_workspace / "workspace" / "parcel_flow" / "store.py").is_file()
    assert (lease.model_workspace / "tests" / "visible").is_dir()
    assert not (lease.model_workspace / "tests" / "hidden").exists()
    assert not (lease.model_workspace / "reference").exists()
    assert lease.hidden_test_root.is_dir()
    assert lease.lease_file.parent == lease.model_workspace.parent
    assert not lease.lease_file.is_relative_to(lease.model_workspace)

    second = manager.create(manifest, binding())
    assert second.model_workspace != lease.model_workspace
    assert second.workspace_root_digest != lease.workspace_root_digest


def test_workspace_lease_rejects_duplicate_or_mismatched_identity(
    tmp_path: Path,
) -> None:
    manifest = FormalFixtureLoader().load(TASK_ROOT)
    manager = PilotWorkspaceManager(tmp_path / "agentforge-pilots")
    expected = binding()
    lease = manager.create(manifest, expected)

    with pytest.raises(FileExistsError):
        manager.create(manifest, expected)
    with pytest.raises(ValueError, match="binding"):
        manager.reopen(
            lease.lease_id,
            expected.model_copy(update={"protocol_digest": "b" * 64}),
        )
    with pytest.raises(ValueError, match="lease"):
        manager.reopen(uuid4(), expected)


def test_workspace_lease_rejects_tampered_metadata(tmp_path: Path) -> None:
    manifest = FormalFixtureLoader().load(TASK_ROOT)
    manager = PilotWorkspaceManager(tmp_path / "agentforge-pilots")
    expected = binding()
    lease = manager.create(manifest, expected)
    payload = json.loads(lease.lease_file.read_text(encoding="utf-8"))
    payload["protocol_digest"] = "b" * 64
    lease.lease_file.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="binding"):
        manager.reopen(lease.lease_id, expected)


def test_workspace_cleanup_requires_terminal_owned_lease(tmp_path: Path) -> None:
    manifest = FormalFixtureLoader().load(TASK_ROOT)
    manager = PilotWorkspaceManager(tmp_path / "agentforge-pilots")
    expected = binding()
    lease = manager.create(manifest, expected)

    with pytest.raises(ValueError, match="terminal"):
        manager.cleanup(lease, terminal=False)
    assert lease.model_workspace.exists()

    manager.cleanup(lease, terminal=True)
    assert not lease.model_workspace.parent.exists()
    assert manager.cleanup(lease, terminal=True) is False
