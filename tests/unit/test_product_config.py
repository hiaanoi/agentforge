from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterator, Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from agentforge.application.config import (
    ConfigSource,
    ProductConfig,
    ProductConfigLoader,
    UnsafeConfigurationError,
)


def _write_user_config(user_root: Path, content: str) -> None:
    user_root.mkdir(parents=True, exist_ok=True)
    (user_root / "config.toml").write_text(content, encoding="utf-8")


def _write_project_config(workspace: Path, content: str) -> None:
    config_dir = workspace / ".agentforge"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.toml").write_text(content, encoding="utf-8")


class _IterationBomb(Mapping[str, object]):
    def __iter__(self) -> Iterator[str]:
        raise RuntimeError("iteration-secret")

    def __len__(self) -> int:
        return 1

    def __getitem__(self, key: str) -> object:
        return "unused"


class _GetItemBomb(Mapping[str, object]):
    def __iter__(self) -> Iterator[str]:
        return iter(("model",))

    def __len__(self) -> int:
        return 1

    def __getitem__(self, key: str) -> object:
        raise RuntimeError("getitem-secret")


class _KeysBomb:
    def keys(self) -> object:
        raise RuntimeError("keys-secret")


class _HostileString(str):
    def __str__(self) -> str:
        raise RuntimeError("hostile-string-secret")


def _expected_digest(
    database_path: Path,
    *,
    model: str,
    max_steps: int,
    profile_ids: tuple[str, ...],
) -> str:
    effective = {
        "database_path": str(database_path),
        "max_steps": max_steps,
        "model": model,
        "profile_ids": list(profile_ids),
    }
    return hashlib.sha256(
        json.dumps(effective, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def test_config_precedence_and_sources(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    user_root = tmp_path / "user"
    _write_user_config(user_root, 'model = "user"\nmax_steps = 20\n')
    _write_project_config(workspace, 'model = "project"\nmax_steps = 30\n')
    monkeypatch.setenv("AGENTFORGE_MODEL", "environment")

    loaded = ProductConfigLoader(user_root=user_root).load(
        workspace, cli={"model": "cli"}
    )

    assert loaded.model == "cli"
    assert loaded.max_steps == 30
    assert loaded.sources["model"] is ConfigSource.CLI
    assert loaded.sources["max_steps"] is ConfigSource.PROJECT
    assert loaded.sources["profile_ids"] is ConfigSource.SAFE_DEFAULT


def test_all_none_cli_values_are_absent_and_preserve_each_fallback_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    user_root = tmp_path / "user"
    monkeypatch.setenv("AGENTFORGE_MODEL", "environment-model")
    _write_user_config(user_root, "max_steps = 17\n")
    _write_project_config(
        workspace,
        'database_path = "project/state.db"\nprofile_ids = ["dev", "verify"]\n',
    )

    loaded = ProductConfigLoader(user_root=user_root).load(
        workspace,
        cli={
            "database_path": None,
            "model": None,
            "max_steps": None,
            "profile_ids": None,
        },
    )

    assert loaded.model == "environment-model"
    assert loaded.max_steps == 17
    assert loaded.database_path == (workspace / "project/state.db").resolve()
    assert loaded.profile_ids == ("dev", "verify")
    assert dict(loaded.sources) == {
        "database_path": ConfigSource.PROJECT,
        "model": ConfigSource.ENVIRONMENT,
        "max_steps": ConfigSource.USER,
        "profile_ids": ConfigSource.PROJECT,
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("database_path", "cli/state.db"),
        ("model", "cli-model"),
        ("max_steps", 9),
        ("profile_ids", ["cli-profile"]),
    ],
)
def test_each_non_none_cli_value_overrides_and_records_cli_source(
    tmp_path: Path, field: str, value: object
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    loaded = ProductConfigLoader(user_root=tmp_path / "user").load(
        workspace, cli={field: value}
    )

    assert loaded.sources[field] is ConfigSource.CLI


@pytest.mark.parametrize("field", ["unknown", "api_key", "openai_api_key"])
def test_unknown_or_secret_cli_keys_are_rejected_before_none_filtering(
    tmp_path: Path, field: str
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(UnsafeConfigurationError) as caught:
        ProductConfigLoader(user_root=tmp_path / "user").load(
            workspace, cli={field: None}
        )

    assert field not in str(caught.value)


@pytest.mark.parametrize(
    ("mapping", "secret"),
    [
        (_IterationBomb(), "iteration-secret"),
        (_GetItemBomb(), "getitem-secret"),
        (_KeysBomb(), "keys-secret"),
    ],
)
def test_hostile_cli_mapping_exceptions_are_safely_rejected(
    tmp_path: Path, mapping: object, secret: str
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(UnsafeConfigurationError) as caught:
        ProductConfigLoader(user_root=tmp_path / "user").load(
            workspace, cli=mapping  # type: ignore[arg-type]
        )

    assert secret not in str(caught.value)


def test_non_string_cli_key_is_safely_rejected(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(UnsafeConfigurationError):
        ProductConfigLoader(user_root=tmp_path / "user").load(
            workspace, cli={object(): None}  # type: ignore[dict-item]
        )


def test_database_path_rejects_hostile_string_subclass_without_leak(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(UnsafeConfigurationError) as caught:
        ProductConfigLoader(user_root=tmp_path / "user").load(
            workspace,
            cli={"database_path": _HostileString("state/hostile.db")},
        )

    assert "hostile-string-secret" not in str(caught.value)


@pytest.mark.parametrize("layer", ["user", "project", "cli"])
def test_non_environment_config_rejects_secret_fields(tmp_path: Path, layer: str) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    user_root = tmp_path / "user"
    loader = ProductConfigLoader(user_root=user_root)
    cli: dict[str, object] = {}
    if layer == "user":
        _write_user_config(user_root, 'openai_api_key = "do-not-leak"\n')
    elif layer == "project":
        _write_project_config(workspace, 'api_key = "do-not-leak"\n')
    else:
        cli = {"api_key": "do-not-leak"}

    with pytest.raises(UnsafeConfigurationError) as caught:
        loader.load(workspace, cli=cli)

    assert "do-not-leak" not in str(caught.value)


def test_api_key_is_not_a_product_config_field_or_digest_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("OPENAI_API_KEY", "first-secret")
    first = ProductConfigLoader(user_root=tmp_path / "user").load(workspace)
    monkeypatch.setenv("OPENAI_API_KEY", "second-secret")
    second = ProductConfigLoader(user_root=tmp_path / "user").load(workspace)

    assert first.effective_config_digest == second.effective_config_digest
    dumped = first.model_dump(mode="json")
    assert set(dumped) == {
        "database_path",
        "model",
        "max_steps",
        "profile_ids",
        "sources",
        "effective_config_digest",
    }
    assert "first-secret" not in json.dumps(dumped)
    assert "second-secret" not in json.dumps(dumped)
    assert "secret" not in repr(first)
    assert all("api_key" not in field.lower() for field in first.sources)


def test_effective_digest_is_canonical_nonsecret_configuration(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    loaded = ProductConfigLoader(user_root=tmp_path / "user").load(
        workspace,
        cli={"model": "gpt-test", "max_steps": 7, "profile_ids": ["dev", "verify"]},
    )
    expected = _expected_digest(
        loaded.database_path,
        model="gpt-test",
        max_steps=7,
        profile_ids=("dev", "verify"),
    )

    assert loaded.effective_config_digest == expected


@pytest.mark.parametrize(
    ("cli", "environment"),
    [
        ({"unknown": "x"}, {}),
        ({"max_steps": "4"}, {}),
        ({"max_steps": True}, {}),
        ({"max_steps": 0}, {}),
        ({"max_steps": 101}, {}),
        ({"profile_ids": ["bad id"]}, {}),
        ({}, {"AGENTFORGE_MAX_STEPS": "four"}),
    ],
)
def test_unknown_keys_type_drift_bounds_and_profile_ids_are_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cli: dict[str, object],
    environment: dict[str, str],
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    with pytest.raises(UnsafeConfigurationError):
        ProductConfigLoader(user_root=tmp_path / "user").load(workspace, cli=cli)


@pytest.mark.parametrize(
    "content",
    [
        'model = "one"\nmodel = "two"\n',
        'model = [unterminated\n',
        'unknown = "field"\n',
        'max_steps = "12"\n',
        '[nested]\nmodel = "forbidden"\n',
    ],
)
def test_malformed_duplicate_unknown_and_type_drift_toml_is_rejected(
    tmp_path: Path, content: str
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_project_config(workspace, content)

    with pytest.raises(UnsafeConfigurationError):
        ProductConfigLoader(user_root=tmp_path / "user").load(workspace)


@pytest.mark.parametrize("configured", ["../outside.db", "nested/../../outside.db"])
def test_database_path_cannot_escape_workspace(tmp_path: Path, configured: str) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(UnsafeConfigurationError):
        ProductConfigLoader(user_root=tmp_path / "user").load(
            workspace, cli={"database_path": configured}
        )


def test_database_path_is_resolved_under_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    loaded = ProductConfigLoader(user_root=tmp_path / "user").load(
        workspace, cli={"database_path": "state/product.sqlite3"}
    )

    assert loaded.database_path == (workspace / "state/product.sqlite3").resolve()
    assert loaded.sources["database_path"] is ConfigSource.CLI


def test_existing_database_hardlink_to_outside_is_rejected(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside-secret.db"
    outside.write_bytes(b"database")
    linked = workspace / "linked.db"
    try:
        os.link(outside, linked)
    except OSError:
        pytest.skip("hard links are unavailable")

    with pytest.raises(UnsafeConfigurationError) as caught:
        ProductConfigLoader(user_root=tmp_path / "user").load(
            workspace, cli={"database_path": "linked.db"}
        )

    assert "outside-secret" not in str(caught.value)


@pytest.mark.parametrize("layer", ["user", "project"])
def test_config_file_hardlink_is_rejected(tmp_path: Path, layer: str) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    user_root = tmp_path / "user"
    outside = tmp_path / "outside-secret.toml"
    outside.write_text('model = "untrusted"\n', encoding="utf-8")
    if layer == "user":
        user_root.mkdir()
        config_path = user_root / "config.toml"
    else:
        config_dir = workspace / ".agentforge"
        config_dir.mkdir()
        config_path = config_dir / "config.toml"
    try:
        os.link(outside, config_path)
    except OSError:
        pytest.skip("hard links are unavailable")

    with pytest.raises(UnsafeConfigurationError) as caught:
        ProductConfigLoader(user_root=user_root).load(workspace)

    assert "outside-secret" not in str(caught.value)


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlinks unavailable")
def test_database_path_rejects_existing_symlink_ancestor(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = workspace / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is not permitted")

    with pytest.raises(UnsafeConfigurationError):
        ProductConfigLoader(user_root=tmp_path / "user").load(
            workspace, cli={"database_path": "linked/product.db"}
        )


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlinks unavailable")
def test_project_config_rejects_symlink(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.toml"
    outside.write_text('model = "outside"\n', encoding="utf-8")
    config_dir = workspace / ".agentforge"
    config_dir.mkdir()
    try:
        (config_dir / "config.toml").symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation is not permitted")

    with pytest.raises(UnsafeConfigurationError):
        ProductConfigLoader(user_root=tmp_path / "user").load(workspace)


def test_product_config_is_frozen_and_strict(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    loaded = ProductConfigLoader(user_root=tmp_path / "user").load(workspace)

    with pytest.raises(ValidationError):
        loaded.model = "changed"
    with pytest.raises(ValidationError):
        ProductConfig.model_validate(
            {
                "database_path": workspace / "db",
                "model": "model",
                "max_steps": 10,
                "profile_ids": ("dev",),
                "sources": loaded.sources,
                "effective_config_digest": "0" * 64,
                "unexpected": True,
            }
        )


def test_product_config_sources_are_deeply_immutable(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    loaded = ProductConfigLoader(user_root=tmp_path / "user").load(workspace)

    with pytest.raises(TypeError):
        loaded.sources["model"] = ConfigSource.CLI  # type: ignore[index]
    with pytest.raises(TypeError):
        del loaded.sources["model"]  # type: ignore[attr-defined]

    assert loaded.model_dump(mode="json")["sources"] == {
        "database_path": ConfigSource.SAFE_DEFAULT.value,
        "model": ConfigSource.SAFE_DEFAULT.value,
        "max_steps": ConfigSource.SAFE_DEFAULT.value,
        "profile_ids": ConfigSource.SAFE_DEFAULT.value,
    }


def test_product_config_json_roundtrip_and_deepcopy_preserve_frozen_provenance(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    loaded = ProductConfigLoader(user_root=tmp_path / "user").load(workspace)

    round_tripped = ProductConfig.model_validate_json(loaded.model_dump_json())
    copied = deepcopy(loaded)

    assert round_tripped == loaded
    assert copied == loaded
    assert round_tripped.model_dump_json() == loaded.model_dump_json()
    assert copied.sources is loaded.sources
    with pytest.raises(TypeError):
        round_tripped.sources["model"] = ConfigSource.CLI  # type: ignore[index]


def test_direct_product_config_rejects_digest_mismatch_and_stale_fields(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    loaded = ProductConfigLoader(user_root=tmp_path / "user").load(workspace)
    sources = dict(loaded.sources)

    with pytest.raises(ValidationError):
        ProductConfig(
            database_path=loaded.database_path,
            model=loaded.model,
            max_steps=loaded.max_steps,
            profile_ids=loaded.profile_ids,
            sources=sources,
            effective_config_digest="0" * 64,
        )
    with pytest.raises(ValidationError):
        ProductConfig(
            database_path=loaded.database_path,
            model="changed-model",
            max_steps=loaded.max_steps,
            profile_ids=loaded.profile_ids,
            sources=sources,
            effective_config_digest=loaded.effective_config_digest,
        )


@pytest.mark.parametrize(
    "update",
    [
        {"model": "changed"},
        {"sources": {"model": ConfigSource.CLI}},
        {"effective_config_digest": "0" * 64},
    ],
)
def test_frozen_product_config_rejects_all_model_copy_updates(
    tmp_path: Path, update: Mapping[str, Any]
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    loaded = ProductConfigLoader(user_root=tmp_path / "user").load(workspace)

    with pytest.raises(TypeError):
        loaded.model_copy(update=update)

    shallow = loaded.model_copy()
    deep = loaded.model_copy(deep=True)
    assert shallow == loaded
    assert deep == loaded
    assert hash(shallow) == hash(loaded) == hash(deep)


@pytest.mark.parametrize(
    "update",
    [
        {},
        {"model": "changed"},
        {"sources": {"model": ConfigSource.CLI}},
        {"effective_config_digest": "0" * 64},
    ],
)
def test_legacy_copy_rejects_every_update(
    tmp_path: Path, update: dict[str, Any]
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    loaded = ProductConfigLoader(user_root=tmp_path / "user").load(workspace)

    with pytest.raises(TypeError):
        loaded.copy(update=update)


def test_legacy_copy_without_selection_or_update_is_safe(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    loaded = ProductConfigLoader(user_root=tmp_path / "user").load(workspace)

    assert loaded.copy() == loaded
    assert loaded.copy(deep=True) == loaded
    with pytest.raises(TypeError):
        loaded.copy(include={"model"})
    with pytest.raises(TypeError):
        loaded.copy(exclude={"sources"})


@pytest.mark.parametrize(
    "sources",
    [
        {
            "database_path": ConfigSource.CLI,
            "model": ConfigSource.CLI,
            "max_steps": ConfigSource.CLI,
        },
        {
            "database_path": ConfigSource.CLI,
            "model": ConfigSource.CLI,
            "max_steps": ConfigSource.CLI,
            "profile_ids": ConfigSource.CLI,
            "extra": ConfigSource.CLI,
        },
        {
            "database_path": ConfigSource.CLI,
            "model": "cli",
            "max_steps": ConfigSource.CLI,
            "profile_ids": ConfigSource.CLI,
        },
    ],
)
def test_direct_product_config_rejects_incomplete_or_invalid_sources(
    tmp_path: Path, sources: dict[str, object]
) -> None:
    with pytest.raises(ValidationError):
        ProductConfig(
            database_path=tmp_path / "db",
            model="model",
            max_steps=10,
            profile_ids=("dev",),
            sources=sources,  # type: ignore[arg-type]
            effective_config_digest="0" * 64,
        )


@pytest.mark.parametrize("layer", ["user", "project"])
def test_config_rejects_ancestor_swap_even_when_file_identity_is_preserved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    layer: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    user_root = tmp_path / "user"
    if layer == "user":
        _write_user_config(user_root, 'model = "trusted"\n')
        config_path = user_root / "config.toml"
        swapped_parent = user_root
    else:
        _write_project_config(workspace, 'model = "trusted"\n')
        config_path = workspace / ".agentforge" / "config.toml"
        swapped_parent = workspace / ".agentforge"
    real_open = os.open
    did_swap = False

    def swap_parent_then_open(path: str | bytes | os.PathLike[str], flags: int) -> int:
        nonlocal did_swap
        requested = Path(os.fsdecode(path))
        if not did_swap and requested == config_path:
            did_swap = True
            old_parent = swapped_parent.with_name("stolen-secret-parent")
            swapped_parent.rename(old_parent)
            swapped_parent.mkdir()
            os.link(old_parent / "config.toml", config_path)
        return real_open(path, flags)

    monkeypatch.setattr(os, "open", swap_parent_then_open)

    with pytest.raises(UnsafeConfigurationError) as caught:
        ProductConfigLoader(user_root=user_root).load(workspace)

    assert did_swap
    assert "stolen-secret-parent" not in str(caught.value)
