# SPDX-License-Identifier: MIT
# Copyright (c) 2025 Kilian A. Lieret and Carlos E. Jimenez
#
# Ported from SWE-agent/mini-swe-agent at commit
# 25941c89cfbc91eb40b3f8756348c91d9977d57e. See ../NOTICE.md.

"""Small, host-independent port of mini-SWE-agent's linear repair loop."""

from collections.abc import Awaitable, Callable
from typing import Protocol

from pydantic import JsonValue

from agentforge.models.base import ModelRequest
from agentforge.models.domain import ModelResponse
from agentforge.repair_engines.mini_native.vendor.context import compact_history


class ActionExecutor(Protocol):
    async def __call__(self, response: ModelResponse, step: int) -> bool: ...


RequestFactory = Callable[[int, list[JsonValue]], ModelRequest]
ModelGenerator = Callable[[ModelRequest], Awaitable[ModelResponse]]
Checkpointer = Callable[[int, list[JsonValue]], Awaitable[None]]


class VendorRepairLoop:
    """Run one model action and one observation at a time, as upstream does."""

    def __init__(
        self,
        *,
        generate: ModelGenerator,
        build_request: RequestFactory,
        execute: ActionExecutor,
        checkpoint: Checkpointer,
    ) -> None:
        self._generate = generate
        self._build_request = build_request
        self._execute = execute
        self._checkpoint = checkpoint

    async def run(
        self, *, max_steps: int, history: list[JsonValue]
    ) -> tuple[bool, int, list[JsonValue]]:
        for step in range(1, max_steps + 1):
            request = self._build_request(step, compact_history(history))
            response = await self._generate(request)
            submitted = await self._execute(response, step)
            compacted_history = compact_history(history)
            del history[:]
            history.extend(compacted_history)
            await self._checkpoint(step, history)
            if submitted:
                return True, step, history
        return False, max_steps, history


__all__ = ["VendorRepairLoop"]
