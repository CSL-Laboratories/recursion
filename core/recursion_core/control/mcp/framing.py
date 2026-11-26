"""Newline-delimited JSON-RPC framing for the MCP stdio transport (protocol revision 2025-06-18).

The stdio transport of 2025-06-18 (basic/transports, "stdio"): the client writes JSON-RPC messages to
the server's stdin and reads them from its stdout, one message per line, each line ending in a newline;
a message never contains a raw newline. stdout carries nothing but protocol messages; diagnostics go to
stderr.

Reading (``read_frames``):

* a line is everything up to and including ``\\n``; a ``\\r`` before it is JSON whitespace, so CRLF lines
  are read as they are;
* a line holding only whitespace is skipped;
* a line longer than ``MAX_LINE_BYTES`` (terminator included) yields ``Frame(error=TOO_LARGE)``: its first
  ``MAX_LINE_BYTES + 1`` bytes are read, the rest is drained to the newline without being kept, and the
  next line is read normally;
* bytes that are not strict UTF-8 yield ``NOT_UTF8``; text that is not one JSON value yields ``NOT_JSON``.
  ``NaN``, ``Infinity`` and ``-Infinity`` are not JSON and are refused, and so is nesting deep enough to
  exhaust the parser;
* a JSON integer longer than ``MAX_INT_DIGITS`` digits is valid JSON that this server does not read (it is
  CPython's own limit for turning digits into an int): it yields ``NUMBER_TOO_LONG``, not ``NOT_JSON``;
* a last line with no newline before end of input is still read as a message.

Writing (``encode``, ``FrameWriter``): compact JSON, ASCII only (every non-ASCII character escaped), so a
message can never carry a raw newline or depend on the console's encoding; each message is written whole
(a short write is given the rest) and flushed once, under one lock, so concurrent answers never
interleave. A write that fails (the client stopped reading) marks the writer broken and calls
``on_broken`` once; later messages are dropped, and the server stops forwarding (``server.py``, OUTPUT
FAILURE).
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any, BinaryIO

# Twice the control socket's frame (control_transport.framing.MAX_FRAME_BYTES, 1 MiB): a longer line is
# refused without being parsed. A shorter one is read; if its call's frame would be over 1 MiB, the call
# answers delivered: false before anything is written (socket_check.CheckedSocketAPI).
MAX_LINE_BYTES = 2 * 1024 * 1024
_DRAIN_CHUNK = 64 * 1024

MAX_INT_DIGITS = 4300

TOO_LARGE, NOT_UTF8, NOT_JSON, NUMBER_TOO_LONG = "too_large", "not_utf8", "not_json", "number_too_long"


@dataclass(frozen=True, slots=True)
class Frame:
    """One line of input: ``message`` (any JSON value) when ``error`` is None, else why it was refused."""

    message: Any = None
    error: str | None = None


def _refuse_constant(name: str) -> Any:
    raise ValueError(f"{name} is not JSON")


class _NumberTooLong(Exception):
    pass


def _parse_int(digits: str) -> int:
    if len(digits.lstrip("-")) > MAX_INT_DIGITS:
        raise _NumberTooLong()
    return int(digits)


def decode_line(line: bytes) -> Frame | None:
    """One line (terminator included or not) as a ``Frame``; ``None`` for a blank line."""

    if line.endswith(b"\n"):
        line = line[:-1]
    try:
        text = line.decode("utf-8")
    except UnicodeDecodeError:
        return Frame(error=NOT_UTF8)
    if not text.strip():
        return None
    try:
        return Frame(message=json.loads(text, parse_constant=_refuse_constant, parse_int=_parse_int))
    except _NumberTooLong:
        return Frame(error=NUMBER_TOO_LONG)
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
    """Writes whole messages to a binary stream, one at a time.

    ``on_broken``, when set, is called once, outside the writer's lock, by the write that finds the stream
    failed; ``broken`` is already true when it is called.
    """

    def __init__(self, stream: BinaryIO, on_broken: Callable[[], None] | None = None) -> None:
        self._stream = stream
        self._lock = threading.Lock()
        self.broken = False
        self.on_broken = on_broken

    def write(self, message: dict) -> bool:
        """Write one message; ``False`` when it was not written (the stream failed now or earlier).

        A stream that takes part of a message is given the rest. One that takes nothing (``0``, ``None``,
        which a raw stream returns when it would block, or a negative count) has failed, and so has one that raises any
        ``Exception``: a pipe the client stopped reading, a stream already shut, a text stream given bytes.
        One that raises an exit or an interrupt has failed too (``broken``, ``on_broken``), and that exception
        then goes on to the caller. A message ``encode`` refuses raises before anything is written."""

        data = encode(message)
        raised: BaseException | None = None
        with self._lock:
            if self.broken:
                return False
            try:
                if self._write_all(data):
                    self._stream.flush()
                    return True
            except Exception:  # noqa: BLE001 - whatever the stream raises, it took no whole message
                pass
            except BaseException as exc:  # an exit or an interrupt: the stream failed, and the exception goes on
                raised = exc
            self.broken = True
        if self.on_broken is not None:
            self.on_broken()
        if raised is not None:
            raise raised
        return False

    def _write_all(self, data: bytes) -> bool:
        """Under the lock: every byte of ``data``, in as many writes as the stream needs; ``False`` when a
        write takes nothing or reports a negative count."""

        while data:
            taken = self._stream.write(data)
            if not taken or taken < 0:  # 0, None (a raw stream that would block) or a negative count: no progress
                return False
            data = data[taken:]
        return True
