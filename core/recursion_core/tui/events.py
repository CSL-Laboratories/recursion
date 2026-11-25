"""The TUI's one ordered event log (CSL-799, candidate). Toolkit-neutral: plain data, no terminal.

Every request, answer, state change, condition edge, engine output chunk and error the TUI shows is
an ``Event`` appended here first. The full-screen view and the linear (screen-reader) renderer both
read this log; neither renders from the other's frame or strings. ``to_jsonl`` writes it as JSON Lines,
one event per line, which is the CI form of O7's TUI transcript evidence.

Fields: ``seq`` (1, 2, ...), ``time_rel_ms`` (from the app's injected clock, so fake-clock goldens are
stable), ``event_type``, ``source`` (``operator``, ``recursion`` or ``engine``), ``trust``
(``trusted``, or ``untrusted`` for engine text), ``state`` (``live``, ``watch_only`` or ``closed``),
``text`` (the full transcript line; engine text is already sanitized), ``speech`` (what the linear
renderer says; empty means it says nothing) and ``focus`` (where keyboard focus is).
"""

from __future__ import annotations

import collections
import json
import threading
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass

FIELDS = ("seq", "time_rel_ms", "event_type", "source", "trust", "state", "text", "speech", "focus")
EVENT_TYPES = ("open", "attach", "close", "request", "answer", "condition", "conditions", "notice", "error", "output",
               "help", "sessions", "transcript")
SOURCES = ("operator", "recursion", "engine")
LOG_CAP = 20_000  # events kept for renderers that fall behind; seq keeps counting past it


@dataclass(frozen=True, slots=True)
class Event:
    seq: int
    time_rel_ms: int
    event_type: str
    source: str
    trust: str
    state: str
    text: str
    speech: str
    focus: str

    def as_dict(self) -> dict:
        return asdict(self)


class EventLog:
    """Append-only, bounded. Readers keep the last ``seq`` they rendered and ask for what is newer.

    ``append_last`` appends the final event and seals the log in one step: any append after it, even one
    that a busy thread had already started, is refused (it returns None), so the final event stays last."""

    def __init__(self, cap: int = LOG_CAP) -> None:
        self._events: collections.deque[Event] = collections.deque(maxlen=cap)
        self.last_seq = 0
        self.sealed = False
        self._lock = threading.Lock()

    def append(self, time_rel_ms: int, event_type: str, source: str, state: str, text: str, speech: str,
               focus: str) -> Event | None:
        with self._lock:
            return self._append(time_rel_ms, event_type, source, state, text, speech, focus)

    def append_last(self, time_rel_ms: int, event_type: str, source: str, state: str, text: str, speech: str,
                    focus: str) -> Event | None:
        with self._lock:
            event = self._append(time_rel_ms, event_type, source, state, text, speech, focus)
            self.sealed = True
            return event

    def _append(self, time_rel_ms, event_type, source, state, text, speech, focus) -> Event | None:
        if self.sealed:
            return None
        self.last_seq += 1
        event = Event(self.last_seq, time_rel_ms, event_type, source,
                      "untrusted" if source == "engine" else "trusted", state, text, speech, focus)
        self._events.append(event)
        return event

    def __iter__(self) -> Iterator[Event]:
        # A tuple taken under the writer's lock: a later append cannot raise from this iterator,
        # and the reader keeps the events that were retained when it began.
        with self._lock:
            return iter(tuple(self._events))

    def __len__(self) -> int:
        return len(self._events)

    def since(self, seq: int) -> tuple[list[Event], int]:
        """Events after ``seq``, oldest first, and how many of them already left the bounded log.

        The walk and the eviction count share the writer's lock, so an append cannot change the deque
        under the walk, and a retained event is not counted as already gone."""

        with self._lock:
            newer: list[Event] = []
            for event in reversed(self._events):  # O(new events), not O(log)
                if event.seq <= seq:
                    break
                newer.append(event)
            newer.reverse()
            first = newer[0].seq if newer else self.last_seq + 1
            return newer, max(0, first - seq - 1)

    def latest(self, event_type: str) -> Event | None:
        with self._lock:
            for event in reversed(self._events):
                if event.event_type == event_type:
                    return event
            return None


# U+2028 and U+2029 end a line for str.splitlines and for JavaScript, so a record that held one raw
# would read as two (R2 finding 11). The writer escapes them; JSON allows the escape inside a string,
# which is the only place a non-ASCII character can be.
_SEPARATOR_ESCAPES = {0x2028: "\\" + "u2028", 0x2029: "\\" + "u2029"}


def to_jsonl(events: Iterable[Event]) -> str:
    return "".join(json.dumps(e.as_dict(), ensure_ascii=False).translate(_SEPARATOR_ESCAPES) + "\n"
                   for e in events)


class EventInvalid(ValueError):
    """One JSONL line is not an event this log can keep. ``line_no`` is one-based."""

    code = "event_invalid"

    def __init__(self, line_no: int) -> None:
        self.line_no = line_no
        super().__init__(f"event_invalid line {line_no}")


_STATES = ("live", "watch_only", "closed")


def _event_from_record(raw: object, line_no: int) -> Event:
    if not isinstance(raw, dict) or set(raw) != set(FIELDS):
        raise EventInvalid(line_no)
    seq, ms = raw.get("seq"), raw.get("time_rel_ms")
    if type(seq) is not int or type(ms) is not int:  # bool is an int, and it is not a sequence number
        raise EventInvalid(line_no)
    event_type, source, trust = raw.get("event_type"), raw.get("source"), raw.get("trust")
    state, text, speech, focus = raw.get("state"), raw.get("text"), raw.get("speech"), raw.get("focus")
    if (not isinstance(event_type, str) or event_type not in EVENT_TYPES
            or not isinstance(source, str) or source not in SOURCES
            or not isinstance(state, str) or state not in _STATES
            or not isinstance(text, str) or not isinstance(speech, str) or not isinstance(focus, str)):
        raise EventInvalid(line_no)
    # Trust is the log's own mark, not a field a record may choose: engine text is untrusted.
    if trust != ("untrusted" if source == "engine" else "trusted"):
        raise EventInvalid(line_no)
    return Event(seq, ms, event_type, source, trust, state, text, speech, focus)


def from_jsonl(text: str) -> list[Event]:
    """One event per LF-ended line. Only LF ends a record, so a log written before the writer escaped
    U+2028 and U+2029 still reads back whole. A line that is not one event raises ``EventInvalid``
    and nothing of that line is returned."""

    out: list[Event] = []
    for line_no, line in enumerate(text.split("\n"), 1):
        if not line:
            continue
        try:
            raw = json.loads(line)
        except ValueError:
            raise EventInvalid(line_no) from None
        out.append(_event_from_record(raw, line_no))
    return out


def speech_lines(events: Iterable[Event]) -> list[str]:
    """The linear renderer: each event's speech, in log order, appended once and never redrawn."""

    out: list[str] = []
    for event in events:
        if event.speech:
            out.extend(event.speech.split("\n"))
    return out
