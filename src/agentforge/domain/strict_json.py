"""Bounded validation and canonical compact JSON size accounting."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import cast

MAX_JSON_DEPTH = 64
MAX_JSON_NODES = 10_000
MAX_JSON_STRING_BYTES = 64_000
MAX_JSON_PAYLOAD_BYTES = 1_000_000
MAX_JSON_INTEGER_BITS = 3_000_000


class StrictJsonError(ValueError):
    """A value is not a bounded, strict JSON value."""


def canonical_json_size(value: object, *, allow_root_mapping: bool = False) -> int:
    """Validate a strict JSON value and return its compact UTF-8 JSON byte size.

    The count matches ``json.dumps(..., ensure_ascii=False, separators=(\",\", \":\"))``
    without building the serialized payload.  A non-``dict`` ``Mapping`` is accepted
    only at the root for persistence's compatibility seam.
    """
    nodes = 0
    active: set[int] = set()

    def add(total: int, amount: int) -> int:
        total += amount
        if total > MAX_JSON_PAYLOAD_BYTES:
            raise StrictJsonError()
        return total

    def string_size(item: str) -> int:
        raw_bytes = 0
        rendered_bytes = 2  # opening and closing quote
        for character in item:
            codepoint = ord(character)
            try:
                encoded_size = len(character.encode("utf-8"))
            except UnicodeError as error:
                raise StrictJsonError() from error
            raw_bytes += encoded_size
            if character == '"' or character == "\\":
                rendered_bytes += 2
            elif character in {"\b", "\t", "\n", "\f", "\r"}:
                rendered_bytes += 2
            elif codepoint <= 0x1F:
                rendered_bytes += 6
            else:
                rendered_bytes += encoded_size
            if raw_bytes > MAX_JSON_STRING_BYTES:
                raise StrictJsonError()
        return rendered_bytes

    def visit(item: object, depth: int, *, root: bool = False) -> int:
        nonlocal nodes
        nodes += 1
        if nodes > MAX_JSON_NODES or depth > MAX_JSON_DEPTH:
            raise StrictJsonError()
        if item is None:
            return 4
        if type(item) is bool:
            return 4 if item else 5
        if type(item) is int:
            if item.bit_length() > MAX_JSON_INTEGER_BITS:
                raise StrictJsonError()
            # Integer-to-text conversion itself may be unavailable for enormous,
            # otherwise in-bound Python integers.  Its bit length is a safe bound.
            if item.bit_length() > 14_000:
                return (item.bit_length() * 30_103) // 100_000 + 2
            return len(str(item))
        if type(item) is float:
            if not math.isfinite(item):
                raise StrictJsonError()
            return len(repr(item))
        if type(item) is str:
            return string_size(item)
        is_mapping = type(item) is dict or (
            root and allow_root_mapping and isinstance(item, Mapping)
        )
        if type(item) is not list and not is_mapping:
            raise StrictJsonError()
        identity = id(item)
        if identity in active:
            raise StrictJsonError()
        active.add(identity)
        try:
            total = 2
            if type(item) is list:
                for index, child in enumerate(item):
                    if index:
                        total = add(total, 1)
                    total = add(total, visit(child, depth + 1))
                return total
            for index, (key, child) in enumerate(cast_mapping(item).items()):
                if type(key) is not str:
                    raise StrictJsonError()
                if index:
                    total = add(total, 1)
                total = add(total, string_size(key))
                total = add(total, 1)
                total = add(total, visit(child, depth + 1))
            return total
        finally:
            active.remove(identity)

    try:
        return visit(value, 0, root=True)
    except StrictJsonError:
        raise
    except Exception as error:
        raise StrictJsonError() from error


def copy_and_measure_json_root_mapping(
    value: object,
) -> tuple[dict[str, object], int]:
    """Observe a root mapping once, returning its strict JSON snapshot and size.

    Persistence accepts arbitrary ``Mapping`` implementations only at the root.
    Iterating that mapping separately for validation and copying would let a stateful
    mapping present two different payloads, so this routine copies and accounts for
    the same entries in one traversal. Nested containers remain exact ``dict`` and
    ``list`` values, as required by the strict JSON contract.
    """
    if not isinstance(value, Mapping):
        raise StrictJsonError()

    nodes = 0
    active: set[int] = set()

    def add(total: int, amount: int) -> int:
        total += amount
        if total > MAX_JSON_PAYLOAD_BYTES:
            raise StrictJsonError()
        return total

    def string_size(item: str) -> int:
        raw_bytes = 0
        rendered_bytes = 2
        for character in item:
            codepoint = ord(character)
            try:
                encoded_size = len(character.encode("utf-8"))
            except UnicodeError as error:
                raise StrictJsonError() from error
            raw_bytes += encoded_size
            if character == '"' or character == "\\":
                rendered_bytes += 2
            elif character in {"\b", "\t", "\n", "\f", "\r"}:
                rendered_bytes += 2
            elif codepoint <= 0x1F:
                rendered_bytes += 6
            else:
                rendered_bytes += encoded_size
            if raw_bytes > MAX_JSON_STRING_BYTES:
                raise StrictJsonError()
        return rendered_bytes

    def copy_value(item: object, depth: int) -> tuple[object, int]:
        nonlocal nodes
        nodes += 1
        if nodes > MAX_JSON_NODES or depth > MAX_JSON_DEPTH:
            raise StrictJsonError()
        if item is None:
            return None, 4
        if type(item) is bool:
            return item, 4 if item else 5
        if type(item) is int:
            if item.bit_length() > MAX_JSON_INTEGER_BITS:
                raise StrictJsonError()
            if item.bit_length() > 14_000:
                return item, (item.bit_length() * 30_103) // 100_000 + 2
            return item, len(str(item))
        if type(item) is float:
            if not math.isfinite(item):
                raise StrictJsonError()
            return item, len(repr(item))
        if type(item) is str:
            return item, string_size(item)
        if type(item) not in {dict, list}:
            raise StrictJsonError()
        identity = id(item)
        if identity in active:
            raise StrictJsonError()
        active.add(identity)
        try:
            total = 2
            if type(item) is list:
                copied_list: list[object] = []
                for index, child in enumerate(item):
                    if index:
                        total = add(total, 1)
                    copied_child, child_size = copy_value(child, depth + 1)
                    copied_list.append(copied_child)
                    total = add(total, child_size)
                return copied_list, total
            copied_dict: dict[str, object] = {}
            for index, (key, child) in enumerate(
                cast(dict[object, object], item).items()
            ):
                if type(key) is not str:
                    raise StrictJsonError()
                if index:
                    total = add(total, 1)
                total = add(total, string_size(key))
                total = add(total, 1)
                copied_child, child_size = copy_value(child, depth + 1)
                copied_dict[key] = copied_child
                total = add(total, child_size)
            return copied_dict, total
        finally:
            active.remove(identity)

    try:
        nodes = 1
        active.add(id(value))
        copied: dict[str, object] = {}
        seen_keys: set[str] = set()
        total = 2
        try:
            for index, (key, child) in enumerate(value.items()):
                if type(key) is not str or key in seen_keys:
                    raise StrictJsonError()
                seen_keys.add(key)
                if index:
                    total = add(total, 1)
                total = add(total, string_size(key))
                total = add(total, 1)
                copied_child, child_size = copy_value(child, 1)
                copied[key] = copied_child
                total = add(total, child_size)
        finally:
            active.remove(id(value))
        return copied, total
    except StrictJsonError:
        raise
    except Exception as error:
        raise StrictJsonError() from error


def cast_mapping(value: object) -> Mapping[object, object]:
    """Narrow the runtime-checked mapping type for strict traversal."""
    if not isinstance(value, Mapping):
        raise StrictJsonError()
    return value
