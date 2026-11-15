"""Sessions and their transcripts (lane TUI, slice 2, part 2; TUI brief v3.3, build step 4).

The TUI reads ``session.list``, and ``events.read`` with ``projection: "transcript"``, and prints the lines
the daemon's projection gives, as given: it never projects or parses events itself. Each line is shown
once, in ``seq`` order; a ``replay.gap`` line is a line like any other. Every line's text goes through the
same sanitizer as engine output (engine text is ``trust: untrusted``). Toolkit-neutral: answers in, rows
out; nothing here sends a request.

The request frames have the shape of SPINE's recorded ones (``schemas/control/v1/fixtures/good/methods.v1``):
``h27-session-list-ok`` for the list, its values as recorded, and ``h11-events-read-as-a-transcript`` for a
read, whose cursor is the one the read goes on from (a first read sends epoch 1 and seq 0, as rule (7)
says; h11 itself reads on from epoch 3, seq 41). The CLI twins of these verbs belong to the verb lanes;
until they exist the TUI's parity tests compare against those frames.

An ``ok`` answer is checked before anything of it is taken (``page_problem``): a page it refuses changes
nothing (review 1 of #405, GPT-6.1 Sol R2 and DeepSeek H1, L1, L2). A refused answer is said and leaves the
cursor as it is, but for two: ``cursor_epoch_ahead`` sets its epoch back to 1, and ``cursor_ahead`` its seq
back to the last line shown, so the next read can be served (review 2 of #405, DeepSeek M1, L4).
"""

from __future__ import annotations

from collections.abc import Mapping

from recursion_core.tui.sanitize import OutputSanitizer, field

LIST, READ, FOLLOW = "session.list", "events.read", "events.follow"
SESSION_METHODS = frozenset({LIST, READ, FOLLOW})  # reads only; the session verbs come with the composer
LIST_PARAMS = {"state": "live", "limit": 20}  # h27-session-list-ok
READ_LIMIT = 200  # h11-events-read-as-a-transcript
MAX_PAGES = 50  # reads one open makes in a row, the first included; then it stops and says how to read on


def first_cursor(session_id: str) -> dict:
    """A first read: epoch 1, seq 0 (rule (7) of methods.v1.json)."""

    return {"epoch": 1, "session_id": session_id, "seq": 0}


def read_params(cursor: Mapping) -> dict:
    return {"cursor": {"epoch": cursor["epoch"], "session_id": cursor["session_id"], "seq": cursor["seq"]},
            "limit": READ_LIMIT, "projection": "transcript"}


def _is_int(value) -> bool:
    return type(value) is int  # True and 1.0 are not positions


def page_problem(asked: Mapping, answer: Mapping) -> str | None:
    """Why an ``ok`` answer is not the answer to the read ``asked`` (that read's params), or None.

    Rule (7) as SPINE's ``conformance/rules.py`` (``page_problems``) states it for the side that reads, after
    the shapes ``transcript.line.v1`` gives. A page in another projection than the one asked for is
    ``projection`` and nothing else of it is looked at. ``lines`` is not a list (``lines``), or a line is not
    an object with an integer ``seq``, a text ``event_type`` and a ``text`` and ``speech`` that are each text
    or null (``line``). Each line is the next position after the one before (the asked ``seq`` first), or a
    ``replay.gap`` whose ``seq`` is the last of the positions it covers (``order``). The answer's cursor
    names the log asked for, at the last position represented, or the asked one for an empty page
    (``cursor``); its epoch is the asked one or later, and ``epoch_advanced`` says exactly whether it is later
    (``epoch``), an empty page included; ``more`` is true or false (``more``). A call whose ``asked`` is not a
    read's params is ``asked``, and an ``answer`` that is not a mapping is ``answer``: refused, never raised."""

    cursor = asked.get("cursor") if isinstance(asked, Mapping) else None
    if not (isinstance(cursor, Mapping) and _is_int(cursor.get("seq")) and _is_int(cursor.get("epoch"))
            and isinstance(cursor.get("session_id"), str)):
        return "asked"  # not a read's params (review 2 of #405, DeepSeek L2)
    if not isinstance(answer, Mapping):
        return "answer"
    if answer.get("projection") != asked.get("projection", "transcript"):
        return "projection"
    lines = answer.get("lines")
    if not isinstance(lines, list):
        return "lines"
    position = cursor["seq"]
    for line in lines:
        if not (isinstance(line, Mapping) and _is_int(line.get("seq")) and isinstance(line.get("event_type"), str)
                and all(line.get(k) is None or isinstance(line.get(k), str) for k in ("text", "speech"))):
            return "line"
        if not (line["seq"] >= position + 1 if line["event_type"] == "replay.gap" else line["seq"] == position + 1):
            return "order"
        position = line["seq"]
    answered = answer.get("cursor")
    if not (isinstance(answered, Mapping) and answered.get("session_id") == cursor["session_id"]
            and _is_int(answered.get("seq")) and answered["seq"] == position):
        return "cursor"
    if not (_is_int(answered.get("epoch")) and answered["epoch"] >= cursor["epoch"]
            and answer.get("epoch_advanced") is (answered["epoch"] > cursor["epoch"])):
        return "epoch"
    if not isinstance(answer.get("more"), bool):
        return "more"
    return None


def rows(line: Mapping) -> list[str]:
    """The rows one transcript line prints: its ``text`` (with none, its ``speech``; with neither, its
    ``event_type``), sanitized as engine output is, one row per line of it."""

    text = next((v for v in (line.get("text"), line.get("speech"), line.get("event_type")) if isinstance(v, str)),
                "(a line with no text)")
    sanitizer = OutputSanitizer()
    clean = sanitizer.feed(text) + sanitizer.flush()
    out = clean.split("\n")
    if len(out) > 1 and out[-1] == "":
        out.pop()  # a line that ends in a newline is one row, not two
    return out


def session_row(session: Mapping) -> str:
    """One session in the list: its ``session_id``, ``state``, ``route.engine``, ``route.model``,
    ``workspace.path`` and ``active_turn_id`` as the record gives them, and how many ``turns`` it lists."""

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
        self.pages = 0  # reads made in a row by the current open, the first included
        self.printed = 0  # the last position whose line was shown (a gap line shows no content of the log)

    def apply(self, answer: Mapping) -> list[dict]:
        """One ``ok`` answer that ``page_problem`` found right: the lines after the cursor, each once, in
        order; the cursor moves to the last position the answer represents (its own cursor), never back."""

        last = self.cursor["seq"]
        new: list[dict] = []
        lines = answer.get("lines")
        for line in lines if isinstance(lines, list) else []:
            if isinstance(line, Mapping) and _is_int(line.get("seq")) and line["seq"] > last:
                new.append(dict(line))
                last = line["seq"]
                if line.get("event_type") != "replay.gap":
                    self.printed = last
        cursor = answer.get("cursor")
        epoch = self.cursor["epoch"]
        if (isinstance(cursor, Mapping) and cursor.get("session_id") == self.session_id
                and _is_int(cursor.get("epoch")) and _is_int(cursor.get("seq"))):
            epoch, last = max(epoch, cursor["epoch"]), max(last, cursor["seq"])
        self.cursor = {"epoch": epoch, "session_id": self.session_id, "seq": last}
        return new
