"""The smallest handler a control API can sit behind: ping and health here, the rest delegated.

This is transport plumbing only. Stop, start and the rest of the control API belong to the
layer above (#327); ``with_ping_health`` answers liveness and hands every other op on.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

Message = dict[str, Any]
Handler = Callable[[Message], Message]

TRANSPORT_VERSION = 1


def unknown_op(message: Message) -> Message:
    return {"ok": False, "error": {"code": "unknown_op", "op": message.get("op")}}


def with_ping_health(delegate: Handler = unknown_op) -> Handler:
    def handle(message: Message) -> Message:
        op = message.get("op")
        if op == "ping":
            return {"ok": True, "op": "pong"}
        if op == "health":
            return {"ok": True, "op": "health", "transport": "unix", "version": TRANSPORT_VERSION}
        return delegate(message)

    return handle
