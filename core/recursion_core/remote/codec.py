"""The relay's wire (lane REMOTE).

On both sides of the relay a frame is exactly the control socket's frame (``control_transport.framing``):
a 4-byte big-endian length, then that many bytes of UTF-8 JSON holding one object, at most 1 MiB. The
relay does not change a frame it forwards, and it adds no field to one.

The relay has a few frames of its own, told apart from the daemon's:

* ``{"op": "relay_hello", "relay_version": 1}`` from the client, answered by the relay itself with
  ``{"ok": true, "op": "relay_hello", "relay": {"version": 1, "transport": ..., ...}}``. An op that
  begins ``relay_`` never reaches the daemon.
* ``{"ok": false, "error": {"code": ..., "layer": "relay", "delivered": ...}}``: the relay did not
  deliver the request (``false``), does not know whether the daemon carried it out (``"unknown"``), or
  delivered it and could not pass the answer back (``true``).
* The socket's own two transport frames (busy, and not admitted) are passed back exactly as the socket
  sent them, so the far client reads them as the local client does: not delivered.

A reply from the daemon may not speak for the relay. An error in it that claims ``layer: "relay"`` is
rewritten to ``layer: "handler"`` with ``delivered: true`` (``daemon_reply``), as the socket server does
for ``layer: "transport"`` (``control_transport.unix._handler_reply``). Without that, a handler (or text
an engine put into an answer) could make the far client report a request it carried out as not
delivered, and invite a duplicate.
"""

from __future__ import annotations

from typing import Any, BinaryIO

from recursion_core.control_transport.errors import FrameError
from recursion_core.control_transport.framing import HEADER, MAX_FRAME_BYTES, decode_body, encode_frame
from recursion_core.control_transport.unix import BUSY_FRAME, NOT_ADMITTED_FRAME

Message = dict[str, Any]

RELAY_VERSION = 1
RELAY_LAYER = "relay"
RELAY_OP_PREFIX = "relay_"
HELLO_OP = "relay_hello"
# The word the CLI's socket client answers with when the control API cannot be reached (control.client).
UNAVAILABLE = "control_api_unavailable"
DELIVERED_VALUES = (False, "unknown", True)

# The socket's accept-path frames, as the far client must see them: byte for byte what the socket sent.
BUSY_MESSAGE: Message = decode_body(BUSY_FRAME[HEADER.size:])
NOT_ADMITTED_MESSAGE: Message = decode_body(NOT_ADMITTED_FRAME[HEADER.size:])


def read_exact(stream: BinaryIO, n: int) -> bytes | None:
    """Read exactly ``n`` bytes from a blocking byte stream; ``None`` on end of stream before the first.

    A pipe may hand back fewer bytes than asked for; this keeps reading. End of stream inside the
    ``n`` bytes is ``FrameError("frame_truncated")``.
    """
    chunks: list[bytes] = []
    got = 0
    while got < n:
        chunk = stream.read(n - got)
        if not chunk:
            if got == 0:
                return None
            raise FrameError("frame_truncated")
        chunks.append(chunk)
        got += len(chunk)
    return b"".join(chunks)


def read_message(stream: BinaryIO, *, max_bytes: int = MAX_FRAME_BYTES) -> Message | None:
    """One frame from ``stream``; ``None`` when the stream ended cleanly between frames.

    A length over ``max_bytes`` is refused before its body is read (``frame_too_large``): the stream
    cannot be trusted to resynchronise after it, so the caller ends the session.
    """
    header = read_exact(stream, HEADER.size)
    if header is None:
        return None
    (length,) = HEADER.unpack(header)
    if length > max_bytes:
        raise FrameError("frame_too_large")
    body = read_exact(stream, length)
    if body is None:
        raise FrameError("frame_truncated")
    return decode_body(body)


def write_message(stream: BinaryIO, message: Message, *, max_bytes: int = MAX_FRAME_BYTES) -> None:
    """Write one frame and flush. Encoding fails before a byte is written (``FrameError``)."""
    data = memoryview(encode_frame(message, max_bytes=max_bytes))
    while data:
        written = stream.write(data)
        if written is None or written <= 0:
            raise OSError("the stream took no bytes")
        data = data[written:]
    flush = getattr(stream, "flush", None)
    if flush is not None:
        flush()


def relay_error(code: str, *, delivered: bool | str, detail: str | None = None,
                request: Message | None = None) -> Message:
    """An error frame of the relay's own; it carries the request's ``id`` when the request had one."""
    if not _is_delivered_value(delivered):
        raise ValueError(f"delivered must be one of {DELIVERED_VALUES!r}")
    error: dict[str, Any] = {"code": code, "layer": RELAY_LAYER, "delivered": delivered}
    if detail:
        error["detail"] = detail
    reply: Message = {"ok": False, "error": error}
    if isinstance(request, dict) and "id" in request:
        reply["id"] = request["id"]
    return reply


def relay_error_of(reply: Message) -> dict[str, Any] | None:
    """The relay's own error in ``reply``, or ``None`` when the reply is not one."""
    if not isinstance(reply, dict) or reply.get("ok") is not False:
        return None
    error = reply.get("error")
    if not isinstance(error, dict) or error.get("layer") != RELAY_LAYER:
        return None
    return error if _is_delivered_value(error.get("delivered")) else None


def _is_delivered_value(value: object) -> bool:
    # bool is a subclass of int: compare by identity, so that 0 and 1 are not read as False and True.
    return value is False or value is True or value == "unknown"


def daemon_reply(reply: Message) -> Message:
    """The daemon's reply as the relay passes it on: it may not claim the relay's layer."""
    error = reply.get("error")
    if isinstance(error, dict) and error.get("layer") == RELAY_LAYER:
        return {**reply, "error": {**error, "layer": "handler", "delivered": True}}
    return reply
