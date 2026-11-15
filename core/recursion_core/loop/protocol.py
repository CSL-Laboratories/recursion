"""The engine protocol: JSON lines in on standard input, typed JSON lines out on standard output.

In (one object per line; the shapes of driver.v2's inputs, plus interrupt):
    {"kind": "user_message", "turn_id": "turn-...", "text": "...", "delivery_id": "dlv-..."}
    {"kind": "approval_answer", "approval_id": "apr-...", "decision": "approve" | "deny"}
    {"kind": "interrupt", "turn_id": "turn-..."}
    {"kind": "close"}
A blank line is skipped. Any other line that is not one of these gets a ``protocol_error`` line and changes
nothing, whatever it holds, and reading goes on.

Out (one object per line; standard output carries nothing else, diagnostics go to standard error):
    ready           once, at start: protocol version, provider, model, tool names
    input_accepted  {delivery_id, turn_id, how: "new_turn"}, written the moment the loop takes a message and
                    before anything is sent to the model; never for a message the loop did not take
    text            {turn_id, text}: one block of the model's text, written when its model call ends
    tool_call       {turn_id, call_id, name, input}
    tool_result     {turn_id, call_id, status, text, output_ref, truncated, spill}
    usage           {turn_id, span: "call", model, input_tokens, ...}: one per model call, a failed or
                    interrupted one included (what it had measured; unknown, never 0, where nothing was)
    limit           {turn_id, scope, kind, model, bucket, reset_at, retry_after_s}
    turn_ended      {turn_id, outcome, result, reason}
    interrupt       {turn_id, delivered}: delivered when that turn was running or waiting; such a line always
                    comes before the turn's turn_ended, which is interrupted unless the model had already
                    finished the turn (candidate_done) or the loop failed (loop_error)
    protocol_error  {code, detail}
    closed          once, when the loop exits after close or the end of its input

Where a line shares a name with an event.v1 payload, its fields are that payload's fields as far as the loop
knows them; the driver (routes/engines/recursion_loop.py, the lane's third pull request) maps the rest:
    tool_call       adds the input digest (RFC 8785) and the envelope
    tool_result     drops ``spill`` (the loop's own; the text the model saw names the saved copy)
    usage, limit    add the connection id and the bucket; limit drops ``retry_after_s`` (event.v1 has only
                    ``reset_at``, which the loop also writes)
    text            becomes engine.output, which adds the stream and the byte offsets
    turn_ended      is not a driver draft (driver.v2 has seven); the core's turn.ended is SPINE's and DRIVER-CC's
                    to settle, and this line carries what the loop knows of the outcome
    ready, interrupt, protocol_error, closed
                    are the driver's to act on, not event drafts
Values keep event.v1's limits before they are written: token counts are whole numbers, a call id is 1 to 128
characters and a tool name 1 to 256 (``providers/base.py`` refuses others).

Every string written (field names included) passes through the redactor, which removes every secret the loop
holds, before the line is serialised.
"""

from __future__ import annotations

import json
import os
import re
import select
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import IO, Any

from recursion_core.loop.providers.base import scrub_across

PROTOCOL_VERSION = 1
MAX_INPUT_LINE = 32 * 1024 * 1024

_TURN = re.compile(r"^turn-[A-Za-z0-9_-]{1,59}$")
_DELIVERY = re.compile(r"^dlv-[A-Za-z0-9_-]{1,60}$")
_APPROVAL = re.compile(r"^apr-[A-Za-z0-9_-]{1,60}$")


class InputError(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class UserMessage:
    turn_id: str
    text: str
    delivery_id: str


@dataclass(frozen=True)
class ApprovalAnswer:
    approval_id: str
    decision: str


@dataclass(frozen=True)
class Interrupt:
    turn_id: str


@dataclass(frozen=True)
class Close:
    pass


Input = UserMessage | ApprovalAnswer | Interrupt | Close

_FIELDS = {
    "user_message": {"kind", "turn_id", "text", "delivery_id"},
    "approval_answer": {"kind", "approval_id", "decision"},
    "interrupt": {"kind", "turn_id"},
    "close": {"kind"},
}


def parse_input(line: bytes | str) -> Input:
    if not isinstance(line, (bytes, str)):
        raise InputError("bad_input", "a line is bytes or text")
    if isinstance(line, bytes):
        try:
            line = line.decode("utf-8")
        except UnicodeDecodeError:
            raise InputError("not_utf8", "the line is not UTF-8") from None
    try:
        doc = json.loads(line)
    except (ValueError, RecursionError):  # RecursionError: nested deeper than the decoder goes
        raise InputError("not_json", "the line is not JSON") from None
    if not isinstance(doc, dict):
        raise InputError("not_an_object", "the line is not a JSON object")
    kind = doc.get("kind")
    if not isinstance(kind, str) or kind not in _FIELDS:
        raise InputError("unknown_kind", f"kind {kind!r} is not one of {sorted(_FIELDS)}")
    extra = sorted(set(doc) - _FIELDS[kind])
    missing = sorted(_FIELDS[kind] - set(doc))
    if extra:
        raise InputError("unknown_field", f"{kind} has unknown field(s): {', '.join(extra)}")
    if missing:
        raise InputError("missing_field", f"{kind} lacks: {', '.join(missing)}")
    if kind == "user_message":
        if not isinstance(doc["turn_id"], str) or not _TURN.fullmatch(doc["turn_id"]):
            raise InputError("bad_turn_id", "turn_id")
        if not isinstance(doc["delivery_id"], str) or not _DELIVERY.fullmatch(doc["delivery_id"]):
            raise InputError("bad_delivery_id", "delivery_id")
        if not isinstance(doc["text"], str) or not doc["text"]:
            raise InputError("bad_text", "text must be a non-empty string")
        return UserMessage(doc["turn_id"], doc["text"], doc["delivery_id"])
    if kind == "approval_answer":
        if not isinstance(doc["approval_id"], str) or not _APPROVAL.fullmatch(doc["approval_id"]):
            raise InputError("bad_approval_id", "approval_id")
        if doc["decision"] not in ("approve", "deny"):
            raise InputError("bad_decision", "decision must be approve or deny")
        return ApprovalAnswer(doc["approval_id"], doc["decision"])
    if kind == "interrupt":
        if not isinstance(doc["turn_id"], str) or not _TURN.fullmatch(doc["turn_id"]):
            raise InputError("bad_turn_id", "turn_id")
        return Interrupt(doc["turn_id"])
    return Close()


class Redactor:
    """Removes every secret the loop holds from text it writes. Secrets shorter than 8 characters are refused:
    replacing them would mangle ordinary text and they are not keys."""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        self._secrets: list[str] = []
        for s in secrets:
            self.add(s)

    def add(self, secret: str) -> None:
        if not isinstance(secret, str) or len(secret) < 8:
            raise ValueError("a secret to redact must be a string of 8 characters or more")
        if secret not in self._secrets:
            self._secrets.append(secret)
            self._secrets.sort(key=len, reverse=True)

    def scrub(self, text: str) -> str:
        for secret in self._secrets:
            if secret in text:
                text = text.replace(secret, "[redacted]")
        return text

    def holds(self, value: Any) -> bool:
        """Whether ``value`` (text, or anything JSON can write) holds a secret."""

        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
        return any(secret in text for secret in self._secrets)

    def scrub_across(self, texts: list[str]) -> list[str]:
        """``texts`` scrubbed as one text (a secret split across two of them is found in their join)."""

        return scrub_across(texts, self._secrets)

    def scrub_obj(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.scrub(value)
        if isinstance(value, Mapping):
            return {self.scrub(str(k)): self.scrub_obj(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.scrub_obj(v) for v in value]
        return value


class Writer:
    """Writes one JSON object per line, whole, under a lock (the input thread writes too)."""

    def __init__(self, stream: IO[str], redactor: Redactor) -> None:
        self._stream = stream
        self._redactor = redactor
        self._lock = threading.Lock()

    def write(self, line_type: str, /, **fields: Any) -> None:
        # Positional-only: a line may carry a field called "kind" (limit lines do).
        doc = {"type": line_type}
        doc.update(fields)
        # Every string (keys included) is scrubbed before serialising, so a secret holding a character JSON
        # escapes cannot slip through in its escaped form; numbers, booleans and null cannot carry one.
        line = json.dumps(self._redactor.scrub_obj(doc), ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            self._stream.write(line + "\n")
            self._stream.flush()


READ_CHUNK = 64 * 1024


def read_lines(stream: IO[bytes], on_line: Callable[[bytes], None], on_too_long: Callable[[], None]) -> None:
    """Read LF-terminated lines from ``stream`` until its end. A line longer than MAX_INPUT_LINE is skipped to
    its end and reported once."""

    lines_from(getattr(stream, "read1", None) or stream.read, on_line, on_too_long)


def lines_from(read: Callable[[int], bytes], on_line: Callable[[bytes], None],
               on_too_long: Callable[[], None]) -> None:
    """``read_lines`` over ``read``, a call that gives the next bytes (b"" at the end): a stream's ``read1``, or
    ``InputReader.read``. A line's trailing CRs and LF go, a blank line is skipped, and a last line with no LF is
    a line. A line longer than MAX_INPUT_LINE (its LF not counted) is reported once and skipped to its end."""

    held = bytearray()
    skipping = False  # inside a line that was too long, until its LF
    while True:
        chunk = read(READ_CHUNK)
        if not chunk:
            if held and not skipping:
                _deliver(bytes(held), on_line)
            return
        start = 0
        while True:
            end = chunk.find(b"\n", start)
            piece = chunk[start:] if end < 0 else chunk[start:end]
            if not skipping:
                if len(held) + len(piece) > MAX_INPUT_LINE:
                    on_too_long()
                    held.clear()
                    skipping = True
                else:
                    held += piece
            if end < 0:
                break
            if not skipping:
                _deliver(bytes(held), on_line)
            held.clear()
            skipping = False
            start = end + 1


def _deliver(line: bytes, on_line: Callable[[bytes], None]) -> None:
    stripped = line.rstrip(b"\r\n")
    if stripped.strip():
        on_line(stripped)


class Stopped(Exception):
    """``InputReader.stop`` was called: the reading thread ends, and a line it had begun is not a line."""


class InputReader:
    """Standard input for the input thread, read with ``os.read`` on its descriptor and never through
    ``sys.stdin``: a thread blocked inside a buffered reader holds that reader's lock, and an interpreter that
    exits while it is held aborts (SIGABRT) instead of exiting, which is what a driver that keeps the pipe open
    after ``close`` saw. On POSIX, ``stop`` (the main thread calls it once ``closed`` is written) makes a waiting
    ``read`` raise ``Stopped``, so the thread ends and is joined; elsewhere the thread stays in the system call,
    which holds no lock of Python's."""

    def __init__(self, fd: int) -> None:
        self._fd = fd
        self._wake: tuple[int, int] | None = os.pipe() if os.name == "posix" else None

    @property
    def stoppable(self) -> bool:
        return self._wake is not None

    def read(self, n: int) -> bytes:
        if self._wake is not None:
            ready, _, _ = select.select([self._fd, self._wake[0]], [], [])
            if self._wake[0] in ready:
                raise Stopped()
        return os.read(self._fd, n)

    def stop(self) -> None:
        if self._wake is not None:
            os.write(self._wake[1], b"\0")

    def close(self) -> None:
        """Close the wake pipe (once the reading thread has ended)."""

        if self._wake is not None:
            for fd in self._wake:
                os.close(fd)
            self._wake = None
