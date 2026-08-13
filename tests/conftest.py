import os
import socket

import pytest
from _pytest.fixtures import SubRequest
from _pytest.monkeypatch import MonkeyPatch


@pytest.fixture(autouse=True)
def block_network(monkeypatch: MonkeyPatch, request: SubRequest) -> None:
    if (
        request.node.get_closest_marker("live") is not None
        and os.getenv("RUN_LIVE_TESTS") == "1"
    ):
        return

    def fail_network(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("Tests must not access the network")

    monkeypatch.setattr(socket, "create_connection", fail_network)
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        fail_network,
    )
