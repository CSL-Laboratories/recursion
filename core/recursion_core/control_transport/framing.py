"""Length-prefixed JSON frames.

One frame is a 4-byte big-endian unsigned length followed by that many bytes of UTF-8
JSON holding one object. The wire format is language-neutral so a later Rust core can
speak it unchanged (O73).

A bad frame echoes a top-level id when that id can be read and written back, so a
client that sent one can match the reply. A frame that is not JSON, whose object
has no readable id, or whose id cannot be re-encoded (a non-finite number, a lone
surrogate) stays id-less: the server does not invent an id and does not put an
unwritable one on the reply. That id-less reply is the case where a client that
attached an id still sees ``reply_id_mismatch``. A body too deep to parse, or larger
than the scan window, still yields an id when the id is a shallow value at the start
of the object. The scan that looks for that id has one deadline; a byte does not
restart it.
"""

from __future__ import annotations

import json
import re
import socket
import struct
import time
from typing import Any

from .errors import FrameError

HEADER = struct.Struct(">I")
MAX_FRAME_BYTES = 1 << 20
# How much of an unreadable body is inspected for a leading id. The declared length
# beyond this is not read: a hostile length must not stall the worker.
_ID_SCAN_BYTES = 4096
_READY_TIMEOUT_S = 0.2
_JSON_STRING = rb'"(?:[^"\\]|\\.)*"'
_JSON_NUMBER = rb"-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?"
_LEADING_ID = re.compile(
    rb'\A\{\s*"id"\s*:\s*(?P<value>null|true|false|' + _JSON_NUMBER + rb"|" + _JSON_STRING + rb")"
)


def encode_frame(message: dict[str, Any], *, max_bytes: int = MAX_FRAME_BYTES) -> bytes:
    if not isinstance(message, dict):
        raise FrameError("frame_not_object")
    try:
        body = json.dumps(message, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    except RecursionError:
        # A deeply nested message must refuse as a frame error, not kill the worker.
        raise FrameError("frame_too_deep") from None
    if len(body) > max_bytes:
        raise FrameError("frame_too_large")
    return HEADER.pack(len(body)) + body


def decode_body(body: bytes) -> dict[str, Any]:
    try:
        message = json.loads(body.decode("utf-8"))
    except RecursionError:
        # A 1 MiB frame can nest ~500k deep; json.loads would raise RecursionError, which is
        # not a ValueError and used to escape the server worker. Refuse it as a frame error.
        raise FrameError("frame_too_deep") from None
    except (UnicodeDecodeError, ValueError) as exc:
        raise FrameError("frame_not_json") from exc
    if not isinstance(message, dict):
        raise FrameError("frame_not_object")
    return message


def parseable_request_id(body: bytes) -> tuple[bool, object]:
    """``(True, id)`` when a top-level id can be read, else ``(False, None)``.

    A complete object at or under the scan window yields its id wherever the field
    sits, including null. A body that is not JSON, or an object with no id, has
    nothing to echo. A body that cannot be parsed (too deep, or larger than the
    scan window) still yields an id when that id is a shallow value at the start
    of the object. An id this cannot read, or cannot be written back out, is left
    unknown.
    """
    if len(body) <= _ID_SCAN_BYTES:
        try:
            message = json.loads(body)
        except RecursionError:
            pass
        except (UnicodeDecodeError, ValueError):
            pass
        else:
            if isinstance(message, dict) and "id" in message and id_reencodes(message["id"]):
                return True, message["id"]
            return False, None
    match = _LEADING_ID.match(body[:_ID_SCAN_BYTES])
    if match is None:
        return False, None
    try:
        ident = json.loads(match.group("value"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return False, None
    if id_reencodes(ident):
        return True, ident
    return False, None


def id_reencodes(ident: object) -> bool:
    """True when ``ident`` survives ``json.dumps(..., allow_nan=False)`` and UTF-8.

    ``1e9999`` loads as infinity and a lone surrogate loads as a string, but neither
    can be put on a reply. Callers omit such an id instead of failing the write.
    """
    try:
        # Same arguments as encode_frame. ensure_ascii=False is what rejects a
        # lone surrogate; the default would escape it and hide the failure.
        json.dumps(ident, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, TypeError):
        return False
    return True


def _stamp_id(exc: FrameError, body: bytes) -> FrameError:
    known, ident = parseable_request_id(body)
    if known:
        exc.id_known = True
        exc.request_id = ident
    return exc


def _recv_exact(sock: socket.socket, n: int, deadline: float | None = None) -> bytes | None:
    """Read exactly ``n`` bytes; ``None`` on a clean close before the first byte.

    With ``deadline`` (a ``time.monotonic()`` value) the whole read must finish by then:
    a peer sending one byte at a time cannot reset the clock on every ``recv``.
    """
    chunks: list[bytes] = []
    got = 0
    while got < n:
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("frame_deadline")
            sock.settimeout(remaining)
        chunk = sock.recv(n - got)
        if not chunk:
            if got == 0:
                return None
            raise FrameError("frame_truncated")
        chunks.append(chunk)
        got += len(chunk)
    return b"".join(chunks)


def read_frame(
    sock: socket.socket, *, max_bytes: int = MAX_FRAME_BYTES, deadline: float | None = None
) -> dict[str, Any] | None:
    """Read one frame; ``None`` when the peer closed cleanly between frames.

    ``deadline`` bounds the whole frame, header and body together (``TimeoutError``).
    """
    header = _recv_exact(sock, HEADER.size, deadline)
    if header is None:
        return None
    (length,) = HEADER.unpack(header)
    if length > max_bytes:
        # Read only enough to see a leading id. The rest of a declared huge length
        # stays unread, and a frame whose id is not in that prefix stays id-less.
        raise _stamp_id(FrameError("frame_too_large"), _scan_oversize(sock, length, deadline))
    body = _recv_exact(sock, length, deadline)
    if body is None:
        raise FrameError("frame_truncated")
    try:
        return decode_body(body)
    except FrameError as exc:
        raise _stamp_id(exc, body) from None


def _scan_oversize(sock: socket.socket, length: int, deadline: float | None) -> bytes:
    """Bytes of an oversized body that may hold an id. Never the declared huge length.

    The wait is one deadline for the whole scan, the same rule as ``_recv_exact``.
    A byte that arrives does not restart it, so a peer trickling bytes cannot hold
    the worker for the scan window times the per-read timeout. A frame already on
    the wire still yields its id. A declared length that never arrives does not
    hold the worker for the idle timeout, and the reply stays id-less.
    """
    scan_deadline = time.monotonic() + _READY_TIMEOUT_S
    if deadline is not None:
        scan_deadline = min(scan_deadline, deadline)
    return _read_buffered(sock, min(length, _ID_SCAN_BYTES), scan_deadline)


def _read_buffered(sock: socket.socket, limit: int, deadline: float) -> bytes:
    """Read what is already buffered. Stop when ``deadline`` (monotonic) is reached."""
    previous = sock.gettimeout()
    chunks: list[bytes] = []
    got = 0
    try:
        while got < limit:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            sock.settimeout(remaining)
            try:
                chunk = sock.recv(limit - got)
            except OSError:
                break
            if not chunk:
                break
            chunks.append(chunk)
            got += len(chunk)
    finally:
        try:
            sock.settimeout(previous)
        except OSError:
            pass
    return b"".join(chunks)


def write_frame(sock: socket.socket, message: dict[str, Any], *, max_bytes: int = MAX_FRAME_BYTES) -> None:
    sock.sendall(encode_frame(message, max_bytes=max_bytes))
