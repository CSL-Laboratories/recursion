"""A Server-Sent Events reader for streamed provider replies (standard library only).

It follows the WHATWG event-stream rules that matter for model APIs: lines end in LF, CRLF or CR (and
nothing else: U+2028 inside JSON data is data, not a line end); a line starting with ':' is a comment;
``event:`` sets the event type, ``data:`` lines are joined with LF, a blank line dispatches; one leading space
after the colon is dropped; ``id`` and ``retry`` are read and ignored. An event with no data is not
dispatched, and neither is an event the stream ends in before its blank line. Bytes are decoded as UTF-8,
strictly: a vendor that sends something else is a protocol fault, not text to guess at.
"""

from __future__ import annotations

import codecs
from collections.abc import Callable, Iterator
from dataclasses import dataclass

MAX_EVENT_CHARS = 64 * 1024 * 1024  # one event larger than this is refused rather than buffered without end


class SSEError(Exception):
    pass


@dataclass(frozen=True)
class SSEEvent:
    event: str
    data: str


def _split_lines(text: str) -> tuple[list[str], str]:
    """Split on CR, LF and CRLF only. Returns the complete lines and the unfinished rest."""

    lines: list[str] = []
    start = 0
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\n":
            lines.append(text[start:i])
            i += 1
            start = i
        elif ch == "\r":
            if i + 1 == n:
                break  # a CR at the very end may be the first half of a CRLF: wait for the next byte
            lines.append(text[start:i])
            i += 2 if text[i + 1] == "\n" else 1
            start = i
        else:
            i += 1
    return lines, text[start:]


def iter_events(read: Callable[[int], bytes], chunk_size: int = 8192) -> Iterator[SSEEvent]:
    """Yield events from ``read(n)``, which returns b"" at the end of the stream."""

    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    rest = ""
    event_type = ""
    data_lines: list[str] = []
    size = 0
    while True:
        chunk = read(chunk_size)
        final = not chunk
        try:
            text = decoder.decode(chunk, final=final)
        except UnicodeDecodeError as exc:
            raise SSEError(f"stream is not UTF-8: {exc.reason}") from None
        lines, rest = _split_lines(rest + text)
        if final and rest:
            lines.append(rest.rstrip("\r"))
            rest = ""
        if len(rest) > MAX_EVENT_CHARS:
            raise SSEError("one line is longer than the reader accepts")
        for line in lines:
            if line == "":
                if data_lines:
                    yield SSEEvent(event_type or "message", "\n".join(data_lines))
                event_type, data_lines, size = "", [], 0
                continue
            if line.startswith(":"):
                continue
            name, sep, value = line.partition(":")
            if sep and value.startswith(" "):
                value = value[1:]
            if name == "event":
                event_type = value
            elif name == "data":
                size += len(value) + 1
                if size > MAX_EVENT_CHARS:
                    raise SSEError("one event is larger than the reader accepts")
                data_lines.append(value)
            # "id", "retry" and unknown fields are ignored, as the format says
        if final:
            return
