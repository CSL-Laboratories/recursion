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
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass

FIELDS = ("seq", "time_rel_ms", "event_type", "source", "trust", "state", "text", "speech", "focus")
EVENT_TYPES = ("open", "attach", "close", "request", "answer", "condition", "conditions", "notice", "error", "output")
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
    """Append-only, bounded. Readers keep the last ``seq`` they rendered and ask for what is newer."""

    def __init__(self, cap: int = LOG_CAP) -> None:
        self._events: collections.deque[Event] = collections.deque(maxlen=cap)
        self.last_seq = 0

    def append(self, time_rel_ms: int, event_type: str, source: str, state: str, text: str, speech: str,
               focus: str) -> Event:
        self.last_seq += 1
        event = Event(self.last_seq, time_rel_ms, event_type, source,
                      "untrusted" if source == "engine" else "trusted", state, text, speech, focus)
        self._events.append(event)
        return event

    def __iter__(self) -> Iterator[Event]:
        return iter(self._events)

    def __len__(self) -> int:
        return len(self._events)

    def since(self, seq: int) -> tuple[list[Event], int]:
        """Events after ``seq``, oldest first, and how many of them already left the bounded log."""

        newer: list[Event] = []
        for event in reversed(self._events):  # O(new events), not O(log)
            if event.seq <= seq:
                break
            newer.append(event)
        newer.reverse()
        first = newer[0].seq if newer else self.last_seq + 1
        return newer, max(0, first - seq - 1)

    def latest(self, event_type: str) -> Event | None:
        for event in reversed(self._events):
            if event.event_type == event_type:
                return event
        return None


def to_jsonl(events: Iterable[Event]) -> str:
    return "".join(json.dumps(e.as_dict(), ensure_ascii=False) + "\n" for e in events)


def from_jsonl(text: str) -> list[Event]:
    return [Event(**json.loads(line)) for line in text.splitlines() if line]


def speech_lines(events: Iterable[Event]) -> list[str]:
    """The linear renderer: each event's speech, in log order, appended once and never redrawn."""

    out: list[str] = []
    for event in events:
        if event.speech:
            out.extend(event.speech.split("\n"))
    return out
