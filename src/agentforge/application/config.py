from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tomllib
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Self, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    field_serializer,
    field_validator,
    model_validator,
)


class UnsafeConfigurationError(RuntimeError):
    """A stable error for configuration that is malformed or unsafe."""

    def __init__(self) -> None:
        super().__init__("product configuration rejected")


class ConfigSource(StrEnum):
    SAFE_DEFAULT = "safe_default"
    ENVIRONMENT = "environment"
    USER = "user"
    PROJECT = "project"
    CLI = "cli"


PRECEDENCE = (
    ConfigSource.SAFE_DEFAULT,
    ConfigSource.ENVIRONMENT,
    ConfigSource.USER,
    ConfigSource.PROJECT,
    ConfigSource.CLI,
)

_PROFILE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_CONFIG_FIELD_ORDER = ("database_path", "model", "max_steps", "profile_ids")
_CONFIG_FIELDS = frozenset(_CONFIG_FIELD_ORDER)
_SECRET_FIELDS = frozenset(
    {
        "api_key",
        "apikey",
        "openai_api_key",
        "anthropic_api_key",
        "password",
        "secret",
        "token",
        "access_token",
    }
)
_ENVIRONMENT_FIELDS = {
    "AGENTFORGE_DATABASE_PATH": "database_path",
    "AGENTFORGE_MODEL": "model",
    "AGENTFORGE_MAX_STEPS": "max_steps",
    "AGENTFORGE_PROFILE_IDS": "profile_ids",
}
_MAX_CONFIG_BYTES = 1024 * 1024
_REPARSE_POINT_ATTRIBUTE = 0x400


@dataclass(frozen=True)
class _FrozenConfigSources(Mapping[str, ConfigSource]):
    entries: tuple[tuple[str, ConfigSource], ...]

    def __getitem__(self, key: str) -> ConfigSource:
        for field, source in self.entries:
            if field == key:
                return source
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        return (field for field, _ in self.entries)

    def __len__(self) -> int:
        return len(self.entries)

    def __deepcopy__(self, memo: dict[int, object]) -> _FrozenConfigSources:
        return self


class ProductConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    database_path: Path
    model: str = Field(min_length=1, max_length=256)
    max_steps: int = Field(gt=0, le=100)
    profile_ids: tuple[str, ...]
    sources: Mapping[str, ConfigSource]
    effective_config_digest: str = Field(pattern=_SHA256_PATTERN)

    @field_validator("sources", mode="before")
    @classmethod
    def validate_source_shape(
        cls, value: object, info: ValidationInfo
    ) -> dict[str, ConfigSource]:
        if not isinstance(value, Mapping) or set(value) != _CONFIG_FIELDS:
            raise ValueError("invalid configuration provenance")
        if info.mode == "json":
            try:
                if any(type(source) is not str for source in value.values()):
                    raise ValueError("invalid configuration provenance")
                return {
                    field: ConfigSource(value[field]) for field in _CONFIG_FIELD_ORDER
                }
            except (TypeError, ValueError):
                raise ValueError("invalid configuration provenance") from None
        if any(type(source) is not ConfigSource for source in value.values()):
            raise ValueError("invalid configuration provenance")
        return {field: value[field] for field in _CONFIG_FIELD_ORDER}

    @field_validator("sources")
    @classmethod
    def freeze_sources(
        cls, value: Mapping[str, ConfigSource]
    ) -> Mapping[str, ConfigSource]:
        return _FrozenConfigSources(
            tuple((field, value[field]) for field in _CONFIG_FIELD_ORDER)
        )

    @field_serializer("sources")
    def serialize_sources(
        self, value: Mapping[str, ConfigSource]
    ) -> dict[str, str]:
        return {field: value[field].value for field in _CONFIG_FIELD_ORDER}

    @field_validator("profile_ids")
    @classmethod
    def validate_profile_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value) or any(
            re.fullmatch(_PROFILE_ID_PATTERN, profile_id) is None for profile_id in value
        ):
            raise ValueError("invalid profile identity")
        # Profile selection is a set-like runtime binding.  Canonicalizing the
        # representation makes equivalent CLI/TOML orderings produce one
        # durable configuration digest and match the registry's ordered output.
        return tuple(sorted(value))

    @model_validator(mode="after")
    def validate_effective_config_digest(self) -> Self:
        expected = _effective_config_digest(
            self.database_path,
            model=self.model,
            max_steps=self.max_steps,
            profile_ids=self.profile_ids,
        )
        if self.effective_config_digest != expected:
            raise ValueError("effective configuration digest mismatch")
        return self

    def model_copy(
        self,
        *,
        update: Mapping[str, Any] | None = None,
        deep: bool = False,
    ) -> Self:
        if update is not None:
            raise TypeError("frozen product configuration does not accept updates")
        return super().model_copy(deep=deep)

    def copy(
        self,
        *,
        include: Any = None,
        exclude: Any = None,
        update: dict[str, Any] | None = None,
        deep: bool = False,
    ) -> Self:
        """Compatibility copy boundary for Pydantic's deprecated API."""
        if include is not None or exclude is not None or update is not None:
            raise TypeError("frozen product configuration does not accept copy changes")
        return self


class ProductConfigLoader:
    """Load strict, non-secret product configuration from trusted layers."""

    def __init__(self, *, user_root: Path | None = None) -> None:
        self._user_root = user_root or (Path.home() / ".config" / "agentforge")

    def load(
        self,
        workspace: Path,
        *,
        cli: Mapping[str, object] | None = None,
    ) -> ProductConfig:
        try:
            workspace_root = _safe_workspace(workspace)
            layers: tuple[tuple[ConfigSource, Mapping[str, object]], ...] = (
                (ConfigSource.SAFE_DEFAULT, _safe_defaults()),
                (ConfigSource.ENVIRONMENT, _environment_config()),
                (
                    ConfigSource.USER,
                    _load_toml(self._user_root / "config.toml", root=self._user_root),
                ),
                (
                    ConfigSource.PROJECT,
                    _load_toml(
                        workspace_root / ".agentforge" / "config.toml",
                        root=workspace_root,
                    ),
                ),
                (
                    ConfigSource.CLI,
                    _cli_config(cli),
                ),
            )
            effective: dict[str, object] = {}
            sources: dict[str, ConfigSource] = {}
            for source, values in layers:
                normalized = _validate_layer(values)
                for field, value in normalized.items():
                    effective[field] = value
                    sources[field] = source

            effective["database_path"] = _workspace_path(
                workspace_root, effective["database_path"]
            )
            database_path = cast(Path, effective["database_path"])
            model = cast(str, effective["model"])
            max_steps = cast(int, effective["max_steps"])
            profile_ids = tuple(sorted(cast(tuple[str, ...], effective["profile_ids"])))
            digest = _effective_config_digest(
                database_path,
                model=model,
                max_steps=max_steps,
                profile_ids=profile_ids,
            )
            return ProductConfig(
                database_path=database_path,
                model=model,
                max_steps=max_steps,
                profile_ids=profile_ids,
                sources=sources,
                effective_config_digest=digest,
            )
        except UnsafeConfigurationError:
            raise
        except (OSError, UnicodeError, ValueError, TypeError, ValidationError):
            raise UnsafeConfigurationError() from None


def _safe_defaults() -> dict[str, object]:
    return {
        "database_path": ".agentforge/agentforge.db",
        "model": "gpt-5-mini",
        "max_steps": 20,
        "profile_ids": (),
    }


def _effective_config_digest(
    database_path: Path,
    *,
    model: str,
    max_steps: int,
    profile_ids: tuple[str, ...],
) -> str:
    serialized = {
        "database_path": str(database_path),
        "max_steps": max_steps,
        "model": model,
        "profile_ids": list(profile_ids),
    }
    return hashlib.sha256(
        json.dumps(serialized, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _environment_config() -> dict[str, object]:
    values: dict[str, object] = {}
    for environment_name, field_name in _ENVIRONMENT_FIELDS.items():
        value = os.environ.get(environment_name)
        if value is None:
            continue
        if field_name == "max_steps":
            if not value.isascii() or not value.isdecimal():
                raise UnsafeConfigurationError()
            values[field_name] = int(value)
        elif field_name == "profile_ids":
            values[field_name] = tuple(part.strip() for part in value.split(","))
        else:
            values[field_name] = value
    return values


def _cli_config(cli: Mapping[str, object] | None) -> dict[str, object]:
    if cli is None:
        return {}
    try:
        values = dict(cli)
        _validate_layer_keys(values)
        return {field: value for field, value in values.items() if value is not None}
    except UnsafeConfigurationError:
        raise
    except Exception:
        raise UnsafeConfigurationError() from None


def _validate_layer(values: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(values, dict):
        raise UnsafeConfigurationError()
    _validate_layer_keys(values)

    normalized = dict(values)
    if "database_path" in normalized and type(normalized["database_path"]) is not str:
        raise UnsafeConfigurationError()
    if "model" in normalized and type(normalized["model"]) is not str:
        raise UnsafeConfigurationError()
    if "max_steps" in normalized and type(normalized["max_steps"]) is not int:
        raise UnsafeConfigurationError()
    if "profile_ids" in normalized:
        profile_ids = normalized["profile_ids"]
        if not isinstance(profile_ids, (list, tuple)) or any(
            type(profile_id) is not str for profile_id in profile_ids
        ):
            raise UnsafeConfigurationError()
        normalized["profile_ids"] = tuple(profile_ids)

    return normalized


def _validate_layer_keys(values: Mapping[str, object]) -> None:
    for key in values:
        if not isinstance(key, str) or _looks_secret(key):
            raise UnsafeConfigurationError()
    if set(values) - _CONFIG_FIELDS:
        raise UnsafeConfigurationError()


def _looks_secret(key: str) -> bool:
    normalized = key.casefold().replace("-", "_")
    return normalized in _SECRET_FIELDS or normalized.endswith("_api_key")


def _load_toml(path: Path, *, root: Path) -> dict[str, object]:
    path_absolute = path.absolute()
    before_chain = _capture_path_chain(path_absolute, root=root)
    if not before_chain or before_chain[-1].path != path_absolute:
        return {}
    before = before_chain[-1]
    if (
        not stat.S_ISREG(before.mode)
        or before.link_count != 1
        or before.size > _MAX_CONFIG_BYTES
    ):
        raise UnsafeConfigurationError()
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path_absolute, flags)
    try:
        opened = os.fstat(descriptor)
        if _metadata_identity(opened) != before.without_path:
            raise UnsafeConfigurationError()
        content = os.read(descriptor, _MAX_CONFIG_BYTES + 1)
        if len(content) > _MAX_CONFIG_BYTES or os.read(descriptor, 1):
            raise UnsafeConfigurationError()
        if _metadata_identity(os.fstat(descriptor)) != before.without_path:
            raise UnsafeConfigurationError()
    finally:
        os.close(descriptor)
    if _capture_path_chain(path_absolute, root=root) != before_chain:
        raise UnsafeConfigurationError()
    parsed = tomllib.loads(content.decode("utf-8"))
    if not isinstance(parsed, dict):
        raise UnsafeConfigurationError()
    return parsed


def _safe_workspace(workspace: Path) -> Path:
    requested = workspace.absolute()
    if not requested.exists() or not requested.is_dir():
        raise UnsafeConfigurationError()
    _reject_reparse(requested)
    resolved = requested.resolve(strict=True)
    if resolved != requested:
        raise UnsafeConfigurationError()
    return resolved


def _workspace_path(workspace: Path, configured: object) -> Path:
    if not isinstance(configured, str) or not configured or "\x00" in configured:
        raise UnsafeConfigurationError()
    candidate = Path(configured)
    joined = candidate if candidate.is_absolute() else workspace / candidate
    absolute = joined.absolute()
    try:
        absolute.relative_to(workspace)
    except ValueError:
        raise UnsafeConfigurationError() from None

    current = workspace
    relative_parts = absolute.relative_to(workspace).parts
    for part in relative_parts:
        current = current / part
        if current.exists() or current.is_symlink():
            _reject_reparse(current)
    try:
        target_metadata = absolute.stat(follow_symlinks=False)
    except FileNotFoundError:
        target_metadata = None
    if (
        target_metadata is not None
        and stat.S_ISREG(target_metadata.st_mode)
        and target_metadata.st_nlink != 1
    ):
        raise UnsafeConfigurationError()
    resolved = absolute.resolve(strict=False)
    try:
        resolved.relative_to(workspace)
    except ValueError:
        raise UnsafeConfigurationError() from None
    return resolved


@dataclass(frozen=True)
class _PathIdentity:
    path: Path
    device: int
    inode: int
    mode: int
    file_attributes: int
    link_count: int
    size: int
    modified_ns: int

    @property
    def without_path(self) -> tuple[int, int, int, int, int, int, int]:
        return (
            self.device,
            self.inode,
            self.mode,
            self.file_attributes,
            self.link_count,
            self.size,
            self.modified_ns,
        )


def _capture_path_chain(path: Path, *, root: Path) -> tuple[_PathIdentity, ...]:
    root_absolute = root.absolute()
    path_absolute = path.absolute()
    try:
        relative = path_absolute.relative_to(root_absolute)
    except ValueError:
        raise UnsafeConfigurationError() from None
    identities: list[_PathIdentity] = []
    current = root_absolute
    for part in (None, *relative.parts):
        if part is not None:
            current = current / part
        try:
            metadata = current.stat(follow_symlinks=False)
        except FileNotFoundError:
            break
        _reject_reparse_metadata(metadata)
        identity = _metadata_identity(metadata)
        identities.append(_PathIdentity(current, *identity))
    return tuple(identities)


def _metadata_identity(
    metadata: os.stat_result,
) -> tuple[int, int, int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        getattr(metadata, "st_file_attributes", 0),
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
    )


def _reject_reparse(path: Path) -> None:
    metadata = path.stat(follow_symlinks=False)
    _reject_reparse_metadata(metadata)


def _reject_reparse_metadata(metadata: os.stat_result) -> None:
    file_attributes = getattr(metadata, "st_file_attributes", 0)
    if stat.S_ISLNK(metadata.st_mode) or file_attributes & _REPARSE_POINT_ATTRIBUTE:
        raise UnsafeConfigurationError()
