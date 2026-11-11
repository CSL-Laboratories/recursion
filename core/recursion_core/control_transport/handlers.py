"""The smallest handler a control API can sit behind: ping and health here, the rest delegated.

This is transport plumbing only. Stop, start and the rest of the control API belong to the
layer above (#327); ``with_ping_health`` answers liveness and hands every other op on.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .peercred import PeerCred

Message = dict[str, Any]

TRANSPORT_VERSION = 1


@dataclass
class Connection:
    """Who is behind a frame: handed to the handler with every request on one connection.

    The transport authenticates the peer's uid and nothing more. Every same-uid process,
    including engine CLIs the daemon starts, passes that check, so the uid grants no scope:
    observe, control and approval belong to the layer above (D3 scopes, I-42), which can
    tell peers apart by ``peer`` (SO_PEERCRED pid/uid/gid at connect time), keep per-
    connection session state in ``state``, and key it by ``conn_id`` (unique per server).

    ``cancelled`` is set when this request is cancelled or the server's close deadline
    has passed. The server binds a fresh event for each request. A handler may wait on
    it. A result returned after it is set is not written, including from a handler that
    never looks at ``cancelled``: close still drops that result.
    """

    conn_id: int
    peer: PeerCred
    state: dict[str, Any] = field(default_factory=dict)
    cancelled: threading.Event = field(default_factory=threading.Event)


Handler = Callable[[Message, Connection], Message]


def unknown_op(message: Message, conn: Connection | None = None) -> Message:
    return {"ok": False, "error": {"code": "unknown_op", "op": message.get("op")}}


def with_ping_health(delegate: Handler = unknown_op) -> Handler:
    def handle(message: Message, conn: Connection) -> Message:
        op = message.get("op")
        if op == "ping":
            return {"ok": True, "op": "pong"}
        if op == "health":
            return {"ok": True, "op": "health", "transport": "unix", "version": TRANSPORT_VERSION}
        return delegate(message, conn)

    return handle
