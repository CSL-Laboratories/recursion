"""Typed Control API failures. ``str(error)`` is a fixed token, never request text.

JSON-RPC 2.0 reserves -32768..-32000. The standard codes are used for protocol
faults and -32001..-32009 for application faults.
"""

from __future__ import annotations

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
UNAUTHENTICATED = -32001
FORBIDDEN = -32002
BUSY = -32003
REFUSED = -32004
UNKNOWN_SESSION = -32005
SESSION_CLOSED = -32006
UNSUPPORTED = -32007
TOO_LARGE = -32008


class ControlError(Exception):
    """A refused call. ``name`` is a fixed token and ``data`` is public."""

    code = INTERNAL_ERROR
    name = "internal_error"

    def __init__(self, name: str | None = None, *, data: dict[str, object] | None = None) -> None:
        if name is not None:
            self.name = name
        self.data = data
        super().__init__(self.name)

    def to_error(self) -> dict[str, object]:
        body: dict[str, object] = {"code": self.code, "message": self.name}
        if self.data:
            body["data"] = dict(self.data)
        return body


class ParseFailure(ControlError):
    code = PARSE_ERROR
    name = "parse_error"


class InvalidRequest(ControlError):
    code = INVALID_REQUEST
    name = "invalid_request"


class MethodNotFound(ControlError):
    code = METHOD_NOT_FOUND
    name = "method_not_found"


class InvalidParams(ControlError):
    code = INVALID_PARAMS
    name = "invalid_params"


class Unauthenticated(ControlError):
    code = UNAUTHENTICATED
    name = "unauthenticated"


class Forbidden(ControlError):
    code = FORBIDDEN
    name = "forbidden"


class SessionBusy(ControlError):
    code = BUSY
    name = "busy"


class RunRefused(ControlError):
    code = REFUSED
    name = "refused"


class UnknownSession(ControlError):
    code = UNKNOWN_SESSION
    name = "unknown_session"


class SessionClosed(ControlError):
    code = SESSION_CLOSED
    name = "session_closed"


class Unsupported(ControlError):
    code = UNSUPPORTED
    name = "unsupported"


class TooLarge(ControlError):
    code = TOO_LARGE
    name = "too_large"
