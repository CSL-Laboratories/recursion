"""Typed failures for the AT1 route adapter. Messages stay free of secrets."""

from __future__ import annotations

from scripts.v3.route_adapter.session_key import SessionKey


class RouteAdapterError(Exception):
    """A refused adapter operation. ``str(error)`` is safe to log."""

    code = "route-adapter-error"


class Busy(RouteAdapterError):
    """The session already has an owner. AT1 rejects; it does not enqueue."""

    code = "busy"

    def __init__(self, key: SessionKey) -> None:
        self.key = key
        super().__init__(f"busy {key.tenant} {key.route} {key.session}")


class StaleGeneration(RouteAdapterError):
    """A completion or send named a generation that is not the active owner."""

    code = "stale-generation"

    def __init__(self, op_id: str, generation: int) -> None:
        self.op_id = op_id
        self.generation = generation
        super().__init__(f"stale generation {generation}")


class CustodyDenied(RouteAdapterError):
    """Credential custody refused the call. ``code`` is the whole message."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)
