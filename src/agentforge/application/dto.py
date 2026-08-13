"""Fail-closed Pydantic bases for public product contracts.

Pydantic's construction helpers intentionally bypass validators.  That is useful
for trusted persistence internals, but public product DTOs are serialization
boundaries and must never retain a forged value.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Self

from pydantic import BaseModel, ConfigDict


class PublicDto(BaseModel):
    """Immutable DTO that always revalidates at public copy/dump boundaries."""

    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, revalidate_instances="always"
    )

    @classmethod
    def model_construct(cls, _fields_set: set[str] | None = None, **values: Any) -> Self:
        raise TypeError("public DTOs must be validated")

    def model_copy(
        self, *, update: Mapping[str, Any] | None = None, deep: bool = False
    ) -> Self:
        if update is not None:
            raise TypeError("public DTOs do not permit update copies")
        return type(self).model_validate(BaseModel.model_dump(self, mode="python"))

    def copy(
        self,
        *,
        include: Any = None,
        exclude: Any = None,
        update: dict[str, Any] | None = None,
        deep: bool = False,
    ) -> Self:
        if include is not None or exclude is not None or update is not None:
            raise TypeError("public DTOs do not permit partial or update copies")
        return self.model_copy(deep=deep)

    def model_dump(self, **kwargs: Any) -> dict[str, Any]:
        validated = type(self).model_validate(BaseModel.model_dump(self, mode="python"))
        return BaseModel.model_dump(validated, **kwargs)

    def model_dump_json(self, **kwargs: Any) -> str:
        validated = type(self).model_validate(BaseModel.model_dump(self, mode="python"))
        return BaseModel.model_dump_json(validated, **kwargs)
