"""Length-prefixed JSON frames.

One frame is a 4-byte big-endian unsigned length followed by that many bytes of UTF-8
JSON holding one object. The wire format is language-neutral so a later Rust core can
speak it unchanged (O73).
"""

from __future__ import annotations

import json
import math
import re
import socket
import struct
import time
from typing import Any

from .errors import FrameError

HEADER = struct.Struct(">I")
MAX_FRAME_BYTES = 1 << 20


def _no_constant(name: str) -> Any:
    # NaN, Infinity and -Infinity are not JSON. Accepting them let a request reach its handler and then
    # crash the worker when the reply (echoing the id) could not be encoded (Nov 12 review, finding 8).
    raise ValueError(f"non-finite constant {name}")


def _finite_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):  # 1e999 parses to inf: refused like the constants
        raise ValueError("non-finite number")
    return value


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


# A JSON escape in the range \uD800-\uDFFF: the only way a lone surrogate gets into a decoded frame, since a
# strict UTF-8 decode refuses an encoded one.
_SURROGATE_ESCAPE = re.compile(rb"\\u[dD][89a-fA-F]")


def decode_body(body: bytes) -> dict[str, Any]:
    """One frame body as a dict. A body that is not strict JSON (a non-finite number included) is
    ``frame_not_json``, refused before anything sees it. So is a string holding a lone surrogate (I-JSON,
    RFC 7493 section 2.1): no UTF-8 frame can carry it back, so a reply echoing it could not be written
    (the reads of #377 and #385 by DeepSeek V4.1 Flash, F8 and F1)."""
    try:
        message = json.loads(body.decode("utf-8"), parse_constant=_no_constant, parse_float=_finite_float)
        if _SURROGATE_ESCAPE.search(body):
            json.dumps(message, ensure_ascii=False).encode("utf-8")   # a lone surrogate raises here
    except RecursionError:
        # A 1 MiB frame can nest ~500k deep; json.loads would raise RecursionError, which is
        # not a ValueError and used to escape the server worker. Refuse it as a frame error.
        raise FrameError("frame_too_deep") from None
    except (UnicodeDecodeError, ValueError) as exc:
        raise FrameError("frame_not_json") from exc
    if not isinstance(message, dict):
        raise FrameError("frame_not_object")
    return message


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
        raise FrameError("frame_too_large")
    body = _recv_exact(sock, length, deadline)
    if body is None:
        raise FrameError("frame_truncated")
    return decode_body(body)


def write_frame(sock: socket.socket, message: dict[str, Any], *, max_bytes: int = MAX_FRAME_BYTES) -> None:
    sock.sendall(encode_frame(message, max_bytes=max_bytes))


# Names the remote lane's product code imports from this module.
__all__ = ["HEADER", "MAX_FRAME_BYTES", "decode_body", "encode_frame"]
