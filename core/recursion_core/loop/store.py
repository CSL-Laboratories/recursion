"""The engine's own conversation store, for resume: ``<home>/sessions/<engine_session_id>.jsonl``.

The brief (step 8): "The conversation for resume is the engine's own store in its home, named by
engine_session_id (like Claude Code's), not the EVENTS database." driver.v2's ``start`` passes
``engine_session_id`` null for a new conversation and a string to resume one; the loop mints the id for a
new conversation, creates its journal, and only then names it on its ``ready`` line.

The file is an append-only journal, one JSON record per line, written as things happen and flushed to disk
at the end of each turn: each message added to the conversation; the user's text as ONE record that is both
its message and its entry in the verbatim ledger (so no crash can keep the one without the other); each tool
result as soon as its call finishes; a compaction (which replaces the conversation); and the session's usage
totals. Every string is scrubbed of the loop's secrets before it is written, and a record is checked against
the shape the reader accepts before it is written. The folder is 0700 and the file 0600, a plain file owned by
this user: both are checked on every open and again before every append, for reading and for writing. A
symbolic link is refused (opened with O_NOFOLLOW, then checked on the opened descriptor), and so is a second
name of the same file. Where the host has a file lock, a second opener is refused while this one holds the
journal, so it cannot cut a record the first is still writing.

Reading it back gives the conversation as it stood. A tool call whose result never reached the journal (the
engine stopped while it ran or before it ran) gets a result that says its outcome is unknown and that it is
to be checked before it is repeated: an effect may have happened, so it is never silently run again
(vault record 20261028-recursion-frontier-dr-completion-control-02: "unknown triggers reconciliation, not
replay"). A record is committed when its line ends: bytes after the last line end are a record cut off by a
crash (a cut UTF-8 character included); they are ignored, and removed before the next record is appended, so
the journal can be appended to and read again. Any complete line that is damaged (not UTF-8, not JSON, of the
wrong shape) refuses the resume, and so does a record holding what no request could carry (NaN, an infinity,
text that is not valid Unicode). Messages are journaled as they are added; one the engine joined to the message
before it (the roles alternate) is joined again here.
"""

from __future__ import annotations

import errno
import json
import math
import os
import re
import stat
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from recursion_core.loop.compaction import HARNESS
from recursion_core.loop.providers import base as b
from recursion_core.loop.tools.workspace import check_private_dir

_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
UNKNOWN_OUTCOME = ("outcome unknown: the engine stopped while this call was running or before it ran. Check what "
                   "it did before you repeat it.")
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_CHUNK = 1 << 16


class StoreError(Exception):
    pass


class _NotJSON(ValueError):
    """A number JSON cannot write (NaN, an infinity), which Python's decoder would take."""


def _no_constant(name: str):
    raise _NotJSON(name)


def _finite(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise _NotJSON(text)
    return value


def new_id() -> str:
    return uuid.uuid4().hex


def part_to_json(part: b.Part) -> dict:
    if isinstance(part, b.TextPart):
        return {"t": "text", "text": part.text}
    if isinstance(part, b.ToolCall):
        return {"t": "call", "id": part.call_id, "name": part.name,
                "input": dict(part.input) if part.input is not None else None, "raw": part.raw_input,
                "error": part.input_error}
    if isinstance(part, b.ToolResult):
        return {"t": "result", "id": part.call_id, "text": part.text, "error": part.is_error}
    return {"t": "opaque", "vendor": part.vendor, "data": dict(part.data)}


def _text(v: Any) -> bool:
    return isinstance(v, str)


def part_from_json(doc: Any, role: str) -> b.Part:
    """One part of a message of ``role``, exactly in the shape part_to_json writes; anything else (a field of the
    wrong type, a missing or extra field, a part the role cannot carry) raises TypeError."""

    if not isinstance(doc, dict):
        raise TypeError("a part is an object")
    kind, keys = doc.get("t"), set(doc)
    if kind == "text" and keys == {"t", "text"} and _text(doc["text"]):
        return b.TextPart(doc["text"])
    if (kind == "call" and role == "assistant" and keys == {"t", "id", "name", "input", "raw", "error"}
            and _text(doc["id"]) and _text(doc["name"]) and (doc["input"] is None or isinstance(doc["input"], dict))
            and _text(doc["raw"]) and (doc["error"] is None or _text(doc["error"]))):
        return b.ToolCall(doc["id"], doc["name"], doc["input"], doc["raw"], doc["error"])
    if (kind == "result" and role == "user" and keys == {"t", "id", "text", "error"} and _text(doc["id"])
            and _text(doc["text"]) and isinstance(doc["error"], bool)):
        return b.ToolResult(doc["id"], doc["text"], doc["error"])
    if (kind == "opaque" and role == "assistant" and keys == {"t", "vendor", "data"} and _text(doc["vendor"])
            and isinstance(doc["data"], dict)):
        return b.OpaquePart(doc["vendor"], doc["data"])
    raise TypeError(f"a {kind!r} part of the wrong shape in a {role!r} message")


def _message(doc: Any, keys: set[str]) -> b.Message:
    if not isinstance(doc, dict) or set(doc) != keys or doc.get("role") not in ("user", "assistant"):
        raise TypeError("a message of the wrong shape")
    if not isinstance(doc["parts"], list) or not doc["parts"]:
        raise TypeError("a message holds a list of parts")
    return b.Message(doc["role"], tuple(part_from_json(p, doc["role"]) for p in doc["parts"]))


@dataclass
class Loaded:
    messages: list[b.Message] = field(default_factory=list)
    user_messages: list[tuple[str, str]] = field(default_factory=list)
    totals: dict | None = None
    repaired: list[str] = field(default_factory=list)  # call ids given an unknown outcome
    # The last message was put together here (the results of the last reply's calls): it is not in the journal
    # yet, and the session journals it before anything else (astra R2), whether or not an outcome is unknown.
    reconstructed: bool = False
    session_id: str | None = None


def check_file(fd: int, what: str) -> None:
    """Refuse an opened journal that is not a plain file, has another name, is not this user's, or is open to
    other users."""

    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        raise StoreError(f"{what} is not a plain file")
    # A hard link is a second name of this inode. O_NOFOLLOW refuses a symbolic link only.
    if getattr(info, "st_nlink", 1) > 1:
        raise StoreError(f"{what} has another name")
    if hasattr(os, "getuid"):
        if info.st_uid != os.getuid():
            raise StoreError(f"{what} is not owned by this user")
        if info.st_mode & 0o077:
            raise StoreError(f"{what} is open to other users (mode {stat.S_IMODE(info.st_mode):o})")


def _lock_journal(fd: int, what: str) -> None:
    """Hold this journal for as long as the descriptor is open. A host with no file lock (Windows) skips the hold.

    Success takes the lock and returns. A journal already held is a StoreError and the caller writes nothing.
    Any other error from the lock is the same refusal. ImportError means the host has no lock, and the caller
    continues. KeyboardInterrupt while locking propagates after the caller releases the descriptor; nothing was
    written. A second caller, in this process or another, gets the refusal and does not truncate or append.
    """

    try:
        import fcntl
    except ImportError:
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        raise StoreError(f"{what} is in use") from None


def _committed_end(fd: int, size: int) -> int:
    """The length of the journal up to and including its last line end (0 when it has none)."""

    end = size
    while end > 0:
        start = max(0, end - _CHUNK)
        os.lseek(fd, start, os.SEEK_SET)
        chunk = b""
        while len(chunk) < end - start:
            got = os.read(fd, end - start - len(chunk))
            if not got:
                break
            chunk += got
        at = chunk.rfind(b"\n")
        if at >= 0:
            return start + at + 1
        end = start
    return 0


class ConversationStore:
    def __init__(self, home: Path, session_id: str, scrub: Callable[[Any], Any] = lambda x: x) -> None:
        if not isinstance(session_id, str) or not _ID.fullmatch(session_id):
            raise StoreError("an engine session id is 1 to 128 letters, digits, '-' or '_'")
        self.session_id = session_id
        self.folder = Path(home) / "sessions"
        self.path = self.folder / f"{session_id}.jsonl"
        self._scrub = scrub
        self._fh = None

    def exists(self) -> bool:
        return self.path.is_file()

    def _what(self) -> str:
        return f"the stored conversation {self.session_id}"

    def _open(self):
        if self._fh is None:
            try:
                os.mkdir(self.folder, 0o700)
            except FileExistsError:
                pass
            check_private_dir(self.folder, "the session store")
            try:
                fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_APPEND | _NOFOLLOW | _CLOEXEC, 0o600)
            except OSError as exc:
                if exc.errno == errno.ELOOP:
                    raise StoreError(f"{self._what()} is a link") from None
                raise StoreError(f"{self._what()} cannot be opened ({exc.strerror})") from None
            try:
                check_file(fd, self._what())
                _lock_journal(fd, self._what())  # before any truncate, so a second opener cannot cut a live write
                size = os.fstat(fd).st_size
                end = _committed_end(fd, size)
                if end < size:
                    os.ftruncate(fd, end)  # a record cut off by a crash: removed before the next is appended
                    os.fsync(fd)
            except BaseException:
                os.close(fd)
                raise
            self._fh = os.fdopen(fd, "ab")
        return self._fh

    def create(self) -> None:
        """Create the journal of a new conversation, on disk, before its id is published (astra R11, sol R10)."""

        self._open()
        self.sync()
        if hasattr(os, "O_DIRECTORY"):
            dfd = os.open(self.folder, os.O_RDONLY | os.O_DIRECTORY | _CLOEXEC)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)

    def _write(self, record: dict) -> None:
        try:
            line = json.dumps(self._scrub(record), ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            data = (line + "\n").encode("utf-8")
            check_record(json.loads(line))  # the reader's own rules: what it would refuse is not written
        except StoreError:
            raise
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError) as exc:
            raise StoreError(f"a record the journal cannot hold ({type(exc).__name__})") from None
        fh = self._open()
        # The descriptor may have been opened on an earlier append. The folder and the file are checked again
        # here, and a refusal writes nothing. A check that raises (StoreError, ToolError, or an interrupt)
        # propagates before write. A second append on this handle, or from another caller, runs the same checks.
        check_private_dir(self.folder, "the session store")
        check_file(fh.fileno(), self._what())
        fh.write(data)
        fh.flush()

    def message(self, message: b.Message) -> None:
        self._write({"k": "message", "role": message.role, "parts": [part_to_json(p) for p in message.parts]})

    def user(self, turn_id: str, text: str) -> None:
        """The user's text: its message in the conversation and its ledger entry, as one record."""

        self._write({"k": "user", "turn": turn_id, "text": text})

    def result(self, result: b.ToolResult) -> None:
        self._write({"k": "result", "part": part_to_json(result)})

    def replace(self, messages: list[b.Message]) -> None:
        self._write({"k": "replace", "messages": [{"role": m.role, "parts": [part_to_json(p) for p in m.parts]}
                                                  for m in messages]})

    def totals(self, state: dict) -> None:
        self._write({"k": "totals", "state": state})

    def sync(self) -> None:
        if self._fh is not None:
            self._fh.flush()
            os.fsync(self._fh.fileno())

    def close(self) -> None:
        if self._fh is not None:
            self.sync()
            self._fh.close()
            self._fh = None

    def _read(self) -> bytes:
        check_private_dir(self.folder, "the session store")
        try:
            # O_NONBLOCK: a FIFO put in the journal's place does not hold the open; it is refused as not a file.
            fd = os.open(self.path, os.O_RDONLY | _NOFOLLOW | _CLOEXEC | getattr(os, "O_NONBLOCK", 0))
        except FileNotFoundError:
            raise StoreError(f"no conversation is stored as {self.session_id}") from None
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise StoreError(f"{self._what()} is a link") from None
            raise StoreError(f"{self._what()} cannot be opened ({exc.strerror})") from None
        try:
            check_file(fd, self._what())  # before fdopen, which refuses a directory with an untyped error
        except BaseException:
            os.close(fd)
            raise
        with os.fdopen(fd, "rb") as fh:
            return fh.read()

    def load(self) -> Loaded:
        if not self.folder.exists():
            raise StoreError(f"no conversation is stored as {self.session_id}")
        lines = self._read().split(b"\n")
        lines.pop()  # after the last line end: nothing, or a record cut off by a crash (ignored)
        out = Loaded(session_id=self.session_id)
        pending: dict[str, b.ToolResult] = {}  # results journaled for the last assistant message's calls
        for n, raw in enumerate(lines):
            try:
                rec = json.loads(raw.decode("utf-8"), parse_constant=_no_constant, parse_float=_finite)
            except UnicodeDecodeError:
                raise StoreError(f"line {n + 1} of the stored conversation is not valid UTF-8") from None
            except json.JSONDecodeError:
                raise StoreError(f"line {n + 1} of the stored conversation is damaged") from None
            except (ValueError, RecursionError):  # a number JSON cannot write, or nested past the decoder
                raise StoreError(f"line {n + 1} of the stored conversation holds what JSON cannot write") from None
            try:
                _read_record(out, pending, rec)
            except StoreError as exc:
                raise StoreError(f"line {n + 1}: {exc}") from None
            except (KeyError, TypeError, ValueError, AttributeError) as exc:
                raise StoreError(f"line {n + 1} of the stored conversation is damaged ({type(exc).__name__})") from None
        last = out.messages[-1] if out.messages else None
        if last is not None and last.role == "assistant":
            calls = [p for p in last.parts if isinstance(p, b.ToolCall)]
            if calls:
                results = []
                for call in calls:
                    if call.call_id in pending:
                        results.append(pending[call.call_id])
                    else:
                        results.append(b.ToolResult(call.call_id, UNKNOWN_OUTCOME, True))
                        out.repaired.append(call.call_id)
                out.messages.append(b.Message("user", tuple(results)))
                out.reconstructed = True
        _keep_unpaired(out)  # a legacy message whose ledger line was cut off still keeps the user's words
        return out


def check_totals(state: Any) -> None:
    """The shape UsageTotal.to_state writes; anything else is a damaged record (TypeError)."""

    def count(v: Any) -> bool:
        return isinstance(v, int) and not isinstance(v, bool) and v >= 0

    def amount(v: Any) -> bool:
        return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v >= 0

    if (not isinstance(state, dict) or not set(state) <= {"calls", "models", "null_model", "sum", "known", "exact",
                                                           "source"}
            or not count(state.get("calls")) or not isinstance(state.get("null_model"), bool)):
        raise TypeError("a totals record holds the totals' shape")
    models = state.get("models")
    if not isinstance(models, list) or not all(isinstance(m, str) for m in models):
        raise TypeError("a totals record names its models as text")
    sums = state.get("sum")
    if (not isinstance(sums, dict) or set(sums) != set(b.USAGE_FIELDS)
            or not all(count(sums[k]) for k in b.TOKEN_FIELDS) or not amount(sums["cost_usd"])):
        raise TypeError("a totals record's sum is a whole number of tokens per field and an amount of money")
    for key, ok in (("known", count), ("exact", lambda v: isinstance(v, bool))):
        table = state.get(key)
        if not isinstance(table, dict) or set(table) != set(b.USAGE_FIELDS) or not all(ok(v) for v in table.values()):
            raise TypeError(f"a totals record's {key} has one value per usage field")
    source = state.get("source")  # absent in a journal written before #417's round 1
    if source is not None and (not isinstance(source, dict) or set(source) != set(b.USAGE_FIELDS)
                               or not all(v in (b.OBSERVED, b.MODELLED, b.INFERRED) for v in source.values())):
        raise TypeError("a totals record's source has one provenance per usage field")


def _legacy_text(msg: b.Message) -> str | None:
    """The user's own words in a legacy message record: one text part, not a tool result and not a harness line.

    Anything else is not a pair a following user record can complete, and it is not copied into the ledger from
    a result or a harness line.
    """

    if msg.role != "user" or len(msg.parts) != 1 or not isinstance(msg.parts[0], b.TextPart):
        return None
    text = msg.parts[0].text
    if text.startswith(HARNESS):
        return None
    return text


def _keep_unpaired(out: Loaded) -> None:
    """A legacy user message with no following user record keeps its words in the ledger, once."""

    pending = getattr(out, "_legacy_pending", None)
    if not isinstance(pending, str):
        return
    n = getattr(out, "_legacy_n", 0) + 1
    out._legacy_n = n
    out.user_messages.append((f"legacy-{n}", pending))
    out._legacy_pending = None


def _add(out: Loaded, msg: b.Message) -> None:
    if out.messages and out.messages[-1].role == msg.role:
        msg = b.Message(msg.role, out.messages.pop().parts + msg.parts)  # joined, as the engine joined it
    out.messages.append(msg)


def check_record(rec: Any) -> None:
    """Whether ``rec`` is a record the reader accepts (StoreError, TypeError, KeyError ... when it is not)."""

    _read_record(Loaded(), {}, rec)


def _read_record(out: Loaded, pending: dict[str, b.ToolResult], rec: Any) -> None:
    """One journal record into what is being loaded. Raises KeyError, TypeError, ValueError or AttributeError for
    a record of the wrong shape (the caller makes them a StoreError)."""

    if b.holds_unencodable(rec):
        raise StoreError("the record holds text that is not valid Unicode")
    if not isinstance(rec, dict):
        raise TypeError("a record is an object")
    kind, keys = rec.get("k"), set(rec)
    if kind == "message":
        msg = _message(rec, {"k", "role", "parts"})
        if msg.role == "user" and any(isinstance(p, b.ToolResult) for p in msg.parts):
            pending.clear()
        _keep_unpaired(out)
        _add(out, msg)
        own = _legacy_text(msg)
        if own is not None:
            out._legacy_pending = own  # the following user record, if it is this text, is the same words
    elif kind == "user":
        if keys != {"k", "turn", "text"} or not _text(rec["turn"]) or not _text(rec["text"]):
            raise TypeError("a user record holds a turn id and text")
        out.user_messages.append((rec["turn"], rec["text"]))
        if getattr(out, "_legacy_pending", None) == rec["text"]:
            out._legacy_pending = None  # the message record already holds these words: do not add them again
        else:
            _keep_unpaired(out)
            _add(out, b.Message("user", (b.TextPart(rec["text"]),)))
    elif kind == "result":
        _keep_unpaired(out)
        if keys != {"k", "part"}:
            raise TypeError("a result record holds one part")
        part = part_from_json(rec["part"], "user")
        if not isinstance(part, b.ToolResult):
            raise TypeError("a result record holds a tool result")
        pending[part.call_id] = part
    elif kind == "replace":
        if keys != {"k", "messages"} or not isinstance(rec["messages"], list):
            raise TypeError("a replace record holds a list of messages")
        _keep_unpaired(out)  # the words are about to leave the message list; the ledger keeps them
        out.messages = [_message(m, {"role", "parts"}) for m in rec["messages"]]
        pending.clear()
    elif kind == "totals":
        _keep_unpaired(out)
        if keys != {"k", "state"}:
            raise TypeError("a totals record holds one state")
        check_totals(rec["state"])
        out.totals = rec["state"]
    else:
        raise StoreError(f"an unknown record {kind!r}")
