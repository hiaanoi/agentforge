from agentforge.context.builder import ContextBuilder
from agentforge.context.models import ContextItem, ContextItemKind, ContextPolicy


def call(call_id: str, value: str) -> ContextItem:
    return ContextItem(
        kind=ContextItemKind.TOOL_CALL,
        call_id=call_id,
        payload={"tool": "read_file", "arguments": {"path": value}},
    )


def result(call_id: str, value: str) -> ContextItem:
    return ContextItem(
        kind=ContextItemKind.TOOL_RESULT,
        call_id=call_id,
        payload={"content": value},
    )


def test_context_builder_preserves_system_task_and_complete_recent_pair() -> None:
    builder = ContextBuilder(
        ContextPolicy(max_items=5, max_characters=500, max_utf8_bytes=1000)
    )
    items = [
        call("old", "old.py"),
        result("old", "x" * 300),
        call("new", "new.py"),
        result("new", "new result"),
    ]

    built = builder.build(task="inspect repository", items=items)

    assert built.request.instructions
    assert built.request.task == "inspect repository"
    kept_ids = [item.call_id for item in built.items if item.call_id]
    assert kept_ids[-2:] == ["new", "new"]
    assert built.state.item_count <= 5


def test_context_builder_is_deterministic_and_keeps_protected_items() -> None:
    policy = ContextPolicy(max_items=4, max_characters=300, max_utf8_bytes=600)
    builder = ContextBuilder(policy)
    items = [
        ContextItem(kind=ContextItemKind.APPROVAL_RESULT, payload={"decision": "REJECTED"}),
        ContextItem(kind=ContextItemKind.RUNTIME_ERROR, payload={"code": "MODEL_TIMEOUT"}),
        call("1", "a.py"),
        result("1", "x" * 400),
    ]

    first = builder.build(task="task", items=items)
    second = builder.build(task="task", items=items)

    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    assert ContextItemKind.APPROVAL_RESULT in {item.kind for item in first.items}
    assert ContextItemKind.RUNTIME_ERROR in {item.kind for item in first.items}


def test_context_builder_preserves_multi_tool_normalization_feedback() -> None:
    builder = ContextBuilder(ContextPolicy(max_items=2))
    items = [
        call("old", "old.py"),
        result("old", "old result"),
        ContextItem(
            kind=ContextItemKind.MULTI_TOOL_NORMALIZATION,
            payload={
                "message": "Other calls were not executed.",
                "discarded_call_count": 2,
            },
        ),
    ]

    built = builder.build(task="preserve normalization", items=items)

    assert [item.kind for item in built.items] == [
        ContextItemKind.MULTI_TOOL_NORMALIZATION
    ]


def test_context_builder_preserves_repair_runtime_state() -> None:
    builder = ContextBuilder(ContextPolicy(max_items=2))
    items = [
        call("old", "old.py"),
        result("old", "old result"),
        ContextItem(
            kind=ContextItemKind.REPAIR_RUNTIME_STATE,
            payload={"remaining_model_calls": 3, "latest_source_verified": False},
        ),
    ]

    built = builder.build(task="preserve repair state", items=items)

    assert [item.kind for item in built.items] == [
        ContextItemKind.REPAIR_RUNTIME_STATE
    ]
