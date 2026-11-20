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

import json
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
    cannot be trusted to resynchronise after it, so the caller ends the session. So does a body that is
    not JSON or not one object (``frame_not_json``, ``frame_not_object``), and a truncated one.

    A whole body that is one JSON object but holds a value the control socket refuses on read (NaN,
    Infinity, a number too large for a float, a lone surrogate written as an escape) leaves the boundary
    sound. It is returned as ``json.loads`` reads it, so that the relay refuses that one frame
    (``frame_refusal``: ``bad_frame``, not delivered, its id kept when it can be framed again), sends
    nothing, and reads the next frame.
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
    try:
        return decode_body(body)
    except FrameError as exc:
        if str(exc) != "frame_not_json":
            raise
        try:
            message = json.loads(body.decode("utf-8"))
        except (RecursionError, UnicodeDecodeError, ValueError):
            raise exc from None
        if not isinstance(message, dict):
            raise exc from None
        return message


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


def frame_refusal(message: Message, *, max_bytes: int = MAX_FRAME_BYTES) -> str | None:
    """Why ``message`` cannot be framed again (too large, too deep, a lone surrogate, NaN), or ``None``.

    ``json.loads`` accepts some text ``encode_frame`` refuses: a lone surrogate written as an escape, and NaN
    or Infinity. Such a frame was readable and cannot be forwarded: it is refused before anything is sent.
    """
    try:
        encode_frame(message, max_bytes=max_bytes)
    except FrameError as exc:
        return str(exc)
    except (ValueError, TypeError) as exc:
        return f"frame_not_encodable:{type(exc).__name__}"
    return None


def copyable_id(request: object) -> tuple[bool, Any]:
    """``(True, id)`` when the request carries an ``id`` that can be framed again, else ``(False, None)``."""
    if not isinstance(request, dict) or "id" not in request:
        return False, None
    if frame_refusal({"id": request["id"]}) is not None:
        return False, None
    return True, request["id"]


def ascii_text(text: str, limit: int = 200) -> str:
    """Text the relay writes into its own frames: ASCII, other characters escaped, at most ``limit`` long."""
    return text.encode("ascii", "backslashreplace").decode("ascii")[:limit]


def relay_error(code: str, *, delivered: bool | str, detail: str | None = None,
                request: Message | None = None) -> Message:
    """An error frame of the relay's own; it carries the request's ``id`` when the request had one.

    The detail is made ASCII and an ``id`` that cannot be framed again is left out, so that the relay's own
    answer can always be written. An error with no ``id`` answers a frame the relay could not read or copy.
    """
    if not _is_delivered_value(delivered):
        raise ValueError(f"delivered must be one of {DELIVERED_VALUES!r}")
    error: dict[str, Any] = {"code": code, "layer": RELAY_LAYER, "delivered": delivered}
    if detail:
        error["detail"] = ascii_text(detail)
    reply: Message = {"ok": False, "error": error}
    has_id, value = copyable_id(request)
    if has_id:
        reply["id"] = value
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
