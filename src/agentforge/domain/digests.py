import hashlib
import json
from uuid import UUID

from pydantic import JsonValue


def compute_tool_call_digest(
    *,
    tool_name: str,
    validated_arguments: dict[str, JsonValue],
    checkpoint_id: UUID,
    step_number: int,
) -> str:
    payload = {
        "tool_name": tool_name,
        "validated_arguments": validated_arguments,
        "checkpoint_id": str(checkpoint_id),
        "step_number": step_number,
    }
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()
