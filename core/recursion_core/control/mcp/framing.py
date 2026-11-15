"""Newline-delimited JSON-RPC framing for the MCP stdio transport (protocol revision 2025-06-18).

The stdio transport of 2025-06-18 (basic/transports, "stdio"): the client writes JSON-RPC messages to
the server's stdin and reads them from its stdout, one message per line, each line ending in a newline;
a message never contains a raw newline. stdout carries nothing but protocol messages; diagnostics go to
stderr.

Reading (``read_frames``):

* a line is everything up to and including ``\\n``; one ``\\r`` before it is dropped (CRLF tolerated);
* a line holding only whitespace is skipped;
* a line longer than ``MAX_LINE_BYTES`` (terminator included) is drained to its newline without being
  kept in memory and yields ``Frame(error=TOO_LARGE)``; the next line is read normally;
* bytes that are not strict UTF-8 yield ``NOT_UTF8``; text that is not one JSON value yields ``NOT_JSON``.
  ``NaN``, ``Infinity`` and ``-Infinity`` are not JSON and are refused, and so is nesting deep enough to
  exhaust the parser;
* a last line with no newline before end of input is still read as a message.

Writing (``encode``, ``FrameWriter``): compact JSON, ASCII only (every non-ASCII character escaped), so a
message can never carry a raw newline or depend on the console's encoding; one ``write`` and one
``flush`` per message under one lock, so concurrent answers never interleave. A write that fails (the
client stopped reading) marks the writer broken; later messages are dropped and reading goes on until
end of input.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, BinaryIO

# Two control-socket frames (control_transport.framing.MAX_FRAME_BYTES is 1 MiB): an argument larger than
# that could never be forwarded, and a line this long is refused without being parsed.
MAX_LINE_BYTES = 2 * 1024 * 1024
_DRAIN_CHUNK = 64 * 1024

TOO_LARGE, NOT_UTF8, NOT_JSON = "too_large", "not_utf8", "not_json"


@dataclass(frozen=True, slots=True)
class Frame:
    """One line of input: ``message`` (any JSON value) when ``error`` is None, else why it was refused."""

    message: Any = None
    error: str | None = None


def _refuse_constant(name: str) -> Any:
    raise ValueError(f"{name} is not JSON")


def decode_line(line: bytes) -> Frame | None:
    """One line (terminator included or not) as a ``Frame``; ``None`` for a blank line."""

    if line.endswith(b"\n"):
        line = line[:-1]
        if line.endswith(b"\r"):
            line = line[:-1]
    try:
        text = line.decode("utf-8")
    except UnicodeDecodeError:
        return Frame(error=NOT_UTF8)
    if not text.strip():
        return None
    try:
        return Frame(message=json.loads(text, parse_constant=_refuse_constant))
    except (ValueError, RecursionError):
        return Frame(error=NOT_JSON)


def read_frames(stream: BinaryIO, *, max_bytes: int = MAX_LINE_BYTES) -> Iterator[Frame]:
    """Yield one ``Frame`` per non-blank input line until end of input."""

    if max_bytes < 2:
        raise ValueError("max_bytes must leave room for a message and its newline")
    while True:
        line = stream.readline(max_bytes + 1)
        if not line:
            return
        if len(line) > max_bytes:
            # Drain the rest of this line without keeping it; the next line is a new message.
            tail = line
            while not tail.endswith(b"\n"):
                tail = stream.readline(_DRAIN_CHUNK)
                if not tail:
                    break
            yield Frame(error=TOO_LARGE)
            continue
        frame = decode_line(line)
        if frame is not None:
            yield frame


def encode(message: dict) -> bytes:
    """One message as one ASCII line ending in ``\\n``. Raises ``ValueError`` for a value JSON cannot hold."""

    text = json.dumps(message, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":"))
    return (text + "\n").encode("ascii")


class FrameWriter:
    """Writes whole messages to a binary stream, one at a time."""

    def __init__(self, stream: BinaryIO) -> None:
        self._stream = stream
        self._lock = threading.Lock()
        self.broken = False

    def write(self, message: dict) -> bool:
        """Write one message; ``False`` when it was not written (the stream failed now or earlier)."""

        data = encode(message)
        with self._lock:
            if self.broken:
                return False
            try:
                self._stream.write(data)
                self._stream.flush()
            except (OSError, ValueError):  # a pipe the client stopped reading, or a stream already shut
                self.broken = True
                return False
        return True
