"""The engine's own conversation store, for resume: ``<home>/sessions/<engine_session_id>.jsonl``.

The brief (step 8): "The conversation for resume is the engine's own store in its home, named by
engine_session_id (like Claude Code's), not the EVENTS database." driver.v2's ``start`` passes
``engine_session_id`` null for a new conversation and a string to resume one; the loop mints the id for a
new conversation and names it on its ``ready`` line.

The file is an append-only journal, one JSON record per line, written as things happen and flushed to disk
at the end of each turn: each message added to the conversation, each user message for the verbatim ledger,
each tool result as soon as its call finishes, a compaction (which replaces the conversation), and the
session's usage totals. Every string is scrubbed of the loop's secrets before it is written. The folder is
0700 and the file 0600.

Reading it back gives the conversation as it stood. A tool call whose result never reached the journal (the
engine stopped while it ran or before it ran) gets a result that says its outcome is unknown and that it is
to be checked before it is repeated: an effect may have happened, so it is never silently run again
(vault record 20261028-recursion-frontier-dr-completion-control-02: "unknown triggers reconciliation, not
replay"). A line cut off by a crash at the very end is ignored; any other damaged line refuses the resume, and so
does a record holding what no request could carry (NaN, an infinity, text that is not valid Unicode). Messages
are journaled as they are added; one the engine joined to the message before it (the roles alternate) is joined
again here.
"""

from __future__ import annotations

import json
import math
import os
import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from recursion_core.loop.providers import base as b
from recursion_core.loop.tools.workspace import check_private_dir

_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
UNKNOWN_OUTCOME = ("outcome unknown: the engine stopped while this call was running or before it ran. Check what "
                   "it did before you repeat it.")


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
        return {"t": "call", "id": part.call_id, "name": part.name, "input": part.input, "raw": part.raw_input,
                "error": part.input_error}
    if isinstance(part, b.ToolResult):
        return {"t": "result", "id": part.call_id, "text": part.text, "error": part.is_error}
    return {"t": "opaque", "vendor": part.vendor, "data": dict(part.data)}


def part_from_json(doc: Mapping[str, Any]) -> b.Part:
    kind = doc["t"]
    if kind == "text":
        return b.TextPart(doc["text"])
    if kind == "call":
        return b.ToolCall(doc["id"], doc["name"], doc["input"], doc.get("raw", ""), doc.get("error"))
    if kind == "result":
        return b.ToolResult(doc["id"], doc["text"], bool(doc["error"]))
    if kind == "opaque":
        return b.OpaquePart(doc["vendor"], doc["data"])
    raise StoreError(f"unknown part {kind!r}")


@dataclass
class Loaded:
    messages: list[b.Message] = field(default_factory=list)
    user_messages: list[tuple[str, str]] = field(default_factory=list)
    totals: dict | None = None
    repaired: list[str] = field(default_factory=list)  # call ids given an unknown outcome


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

    def _open(self):
        if self._fh is None:
            try:
                os.mkdir(self.folder, 0o700)
            except FileExistsError:
                pass
            check_private_dir(self.folder, "the session store")
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0), 0o600)
            self._fh = os.fdopen(fd, "a", encoding="utf-8", newline="\n")
        return self._fh

    def _write(self, record: dict) -> None:
        fh = self._open()
        fh.write(json.dumps(self._scrub(record), ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n")
        fh.flush()

    def message(self, message: b.Message) -> None:
        self._write({"k": "message", "role": message.role, "parts": [part_to_json(p) for p in message.parts]})

    def user(self, turn_id: str, text: str) -> None:
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

    def load(self) -> Loaded:
        if not self.exists():
            raise StoreError(f"no conversation is stored as {self.session_id}")
        check_private_dir(self.folder, "the session store")
        raw = self.path.read_text(encoding="utf-8").split("\n")
        if raw and raw[-1] == "":
            raw.pop()
        out = Loaded()
        pending: dict[str, b.ToolResult] = {}  # results journaled for the last assistant message's calls
        for n, line in enumerate(raw):
            try:
                rec = json.loads(line, parse_constant=_no_constant, parse_float=_finite)
            except json.JSONDecodeError:
                if n == len(raw) - 1:
                    break  # cut off by a crash while it was being written
                raise StoreError(f"line {n + 1} of the stored conversation is damaged") from None
            except (ValueError, RecursionError):  # a number JSON cannot write, or nested past the decoder
                raise StoreError(f"line {n + 1} of the stored conversation holds what JSON cannot write") from None
            if b.holds_unencodable(rec):
                raise StoreError(f"line {n + 1} of the stored conversation holds text that is not valid Unicode")
            try:
                _read_record(out, pending, rec, n)
            except StoreError:
                raise
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
        return out


def _check_totals(state: Any) -> None:
    """The shape UsageTotal.to_state writes; anything else is a damaged record (TypeError)."""

    def count(v: Any) -> bool:
        return isinstance(v, int) and not isinstance(v, bool) and v >= 0

    if not isinstance(state, dict) or not count(state.get("calls")) or not isinstance(state.get("null_model"), bool):
        raise TypeError("a totals record holds the totals' shape")
    models = state.get("models")
    if not isinstance(models, list) or not all(isinstance(m, str) for m in models):
        raise TypeError("a totals record names its models as text")
    for key, ok in (("sum", lambda v: isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0),
                    ("known", count), ("exact", lambda v: isinstance(v, bool))):
        table = state.get(key)
        if not isinstance(table, dict) or set(table) != set(b.USAGE_FIELDS) or not all(ok(v) for v in table.values()):
            raise TypeError(f"a totals record's {key} has one value per usage field")


def _read_record(out: Loaded, pending: dict[str, b.ToolResult], rec: Any, n: int) -> None:
    """One journal record into what is being loaded. Raises KeyError, TypeError, ValueError or AttributeError for
    a record of the wrong shape (the caller makes them a StoreError)."""

    if not isinstance(rec, dict):
        raise TypeError("a record is an object")
    kind = rec.get("k")
    if kind == "message":
        msg = b.Message(rec["role"], tuple(part_from_json(p) for p in rec["parts"]))
        if msg.role == "user" and any(isinstance(p, b.ToolResult) for p in msg.parts):
            pending.clear()
        if out.messages and out.messages[-1].role == msg.role:
            msg = b.Message(msg.role, out.messages.pop().parts + msg.parts)  # joined, as the engine joined it
        out.messages.append(msg)
    elif kind == "user":
        if not isinstance(rec["turn"], str) or not isinstance(rec["text"], str):
            raise TypeError("a user record holds text")
        out.user_messages.append((rec["turn"], rec["text"]))
    elif kind == "result":
        part = part_from_json(rec["part"])
        if not isinstance(part, b.ToolResult):
            raise TypeError("a result record holds a tool result")
        pending[part.call_id] = part
    elif kind == "replace":
        out.messages = [b.Message(m["role"], tuple(part_from_json(p) for p in m["parts"])) for m in rec["messages"]]
        pending.clear()
    elif kind == "totals":
        _check_totals(rec["state"])
        out.totals = rec["state"]
    else:
        raise StoreError(f"line {n + 1} has an unknown record {kind!r}")
