"""Sessions and their transcripts (lane TUI, slice 2, part 2; TUI brief v3.3, build step 4).

The TUI reads ``session.list``, and ``events.read`` with ``projection: "transcript"``, and prints the lines
the daemon's projection gives, as given: it never projects or parses events itself. Each line is shown
once, in ``seq`` order; a ``replay.gap`` line is a line like any other. Every line's text goes through the
same sanitizer as engine output (engine text is ``trust: untrusted``). Toolkit-neutral: answers in, rows
out; nothing here sends a request.

The request frames are SPINE's recorded ones (``schemas/control/v1/fixtures/good/methods.v1``):
``h27-session-list-ok`` for the list and ``h11-events-read-as-a-transcript`` for a read. The CLI twins of
these verbs belong to the verb lanes; until they exist the TUI's parity tests compare against those frames.
"""

from __future__ import annotations

from collections.abc import Mapping

from recursion_core.tui.sanitize import OutputSanitizer, field

LIST, READ, FOLLOW = "session.list", "events.read", "events.follow"
SESSION_METHODS = frozenset({LIST, READ, FOLLOW})  # reads only; the session verbs come with the composer
LIST_PARAMS = {"state": "live", "limit": 20}  # h27-session-list-ok
READ_LIMIT = 200  # h11-events-read-as-a-transcript
MAX_PAGES = 50  # pages one open reads in a row; then it stops and says how to read on


def first_cursor(session_id: str) -> dict:
    """A first read: epoch 1, seq 0 (rule (7) of methods.v1.json)."""

    return {"epoch": 1, "session_id": session_id, "seq": 0}


def read_params(cursor: Mapping) -> dict:
    return {"cursor": {"epoch": cursor["epoch"], "session_id": cursor["session_id"], "seq": cursor["seq"]},
            "limit": READ_LIMIT, "projection": "transcript"}


def rows(line: Mapping) -> list[str]:
    """The rows one transcript line prints: its ``text`` (with none, its ``speech``; with neither, its
    ``event_type``), sanitized as engine output is, one row per line of it."""

    text = next((v for v in (line.get("text"), line.get("speech")) if isinstance(v, str)), None)
    if text is None:
        text = str(line.get("event_type"))
    sanitizer = OutputSanitizer()
    clean = sanitizer.feed(text) + sanitizer.flush()
    out = clean.split("\n")
    if len(out) > 1 and out[-1] == "":
        out.pop()  # a line that ends in a newline is one row, not two
    return out


def session_row(session: Mapping) -> str:
    """One session in the list, in the record's own words (no field is interpreted)."""

    route = session.get("route") if isinstance(session.get("route"), Mapping) else {}
    workspace = session.get("workspace") if isinstance(session.get("workspace"), Mapping) else {}
    turns = session.get("turns") if isinstance(session.get("turns"), list) else []
    return (f"session={field(session.get('session_id'))} state={field(session.get('state'))} "
            f"engine={field(route.get('engine'))} model={field(route.get('model'))} "
            f"workspace={field(workspace.get('path'))} turns={len(turns)} "
            f"active_turn={field(session.get('active_turn_id'))}")


class Transcript:
    """One session's transcript as read so far, and the cursor to read on from."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.cursor = first_cursor(session_id)
        self.pages = 0  # pages read in a row by the current open

    def apply(self, answer: Mapping) -> list[dict]:
        """One ``ok`` answer: the lines after the cursor, each once, in order; the cursor moves to the last
        position the answer represents (its own cursor), never back."""

        last = self.cursor["seq"]
        new: list[dict] = []
        for line in answer.get("lines") or []:
            if isinstance(line, Mapping) and type(line.get("seq")) is int and line["seq"] > last:
                new.append(dict(line))
                last = line["seq"]
        cursor = answer.get("cursor")
        epoch = self.cursor["epoch"]
        if (isinstance(cursor, Mapping) and cursor.get("session_id") == self.session_id
                and type(cursor.get("epoch")) is int and type(cursor.get("seq")) is int):
            epoch, last = max(epoch, cursor["epoch"]), max(last, cursor["seq"])
        self.cursor = {"epoch": epoch, "session_id": self.session_id, "seq": last}
        return new
