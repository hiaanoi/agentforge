import json

from agentforge.repair_engines.mini_native.vendor.context import (
    MAX_OBSERVATION_CHARS,
    compact_history,
)


def test_vendor_context_redacts_and_bounds_tool_call_payloads() -> None:
    oversized_content = "x" * (MAX_OBSERVATION_CHARS * 2)

    compacted = compact_history(
        [
            {
                "kind": "TOOL_CALL",
                "payload": {
                    "arguments": {
                        "api_key": "sk-live-secret-value",
                        "authorization": "Bearer bearer-secret-value",
                        "content": oversized_content,
                    }
                },
            }
        ]
    )

    encoded = json.dumps(compacted)
    assert "sk-live-secret-value" not in encoded
    assert "bearer-secret-value" not in encoded
    assert len(json.dumps(compacted[0]["payload"])) <= MAX_OBSERVATION_CHARS
