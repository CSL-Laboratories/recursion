"""Sessions and their transcripts (lane TUI, slice 2, part 2; TUI brief v3.3, build step 4).

The TUI reads ``session.list``, and ``events.read`` with ``projection: "transcript"``, and prints the lines
the daemon's projection gives, as given: it never projects or parses events itself, and a ``replay.gap``
line is a line like any other. Every line's text goes through the same sanitizer as engine output (engine
text is ``trust: untrusted``). Toolkit-neutral: answers in, rows out; nothing here sends a request.

The request frames have the shape of SPINE's recorded ones (``schemas/control/v1/fixtures/good/methods.v1``):
``h27-session-list-ok`` for the list, its values as recorded, and ``h11-events-read-as-a-transcript`` for a
read, whose cursor is the one the read goes on from (a first read sends epoch 1 and seq 0, as rule (7)
says; h11 itself reads on from epoch 3, seq 41). The CLI twins of these verbs belong to the verb lanes;
until they exist the TUI's parity tests compare against those frames.

An ``ok`` answer is checked before anything of it is taken (``page_problem``): a page it refuses changes
nothing (review 1 of #405, GPT-6.1 Sol R2 and DeepSeek H1, L1, L2).

A transcript never loses a line in silence and never shows one twice in silence: the user sees every line
once, or a marker line that says the view was rebuilt (the Liaison's ruling on review 5 of #405). What the
TUI relies on from the daemon is written in one place, the README's "What a transcript relies on": a line
at a position never changes, and the log never loses a position except to retention, which a replay.gap
line says, in an epoch and across the epochs of one store. The one thing a reader can check is checked on
every read after a line was shown: the read asks from the position before the last line shown, so the
answer gives that line again, and it must be the line shown. When the answer has another line there or
ends before it, or the daemon has no position there any more (``cursor_ahead``), the daemon broke that
promise: a marker line says so, and the log is shown again from its start below it. ``cursor_epoch_ahead``
sets the epoch back to 1 and the check decides (review 2 of #405, DeepSeek L1). A ``cursor_ahead`` when no
line has been shown (only gap lines) sends the next read back to the start. A refusal of the start itself is
said as an error (review 4, R3). A replay.gap line already shown is not shown again, nor one over positions
already shown or said since the last line shown (review 3, F3; review 6, F5). A transcript gap line does not say
where its gap starts (SPINE's transcript line leaves out the raw event's ``from``), so every gap line shown is
taken to cover everything from the last line shown up to it: ``GAP_MARK`` is shown after it, and the text
before and after a gap is never joined as if nothing lay between (the Liaison's ruling on review 6 of #405, F1).
"""

from __future__ import annotations

import threading
from collections.abc import Mapping

from recursion_core.tui.sanitize import OutputSanitizer, field

LIST, READ, FOLLOW = "session.list", "events.read", "events.follow"
SESSION_METHODS = frozenset({LIST, READ, FOLLOW})  # reads only; the session verbs come with the composer
LIST_PARAMS = {"state": "live", "limit": 20}  # h27-session-list-ok
READ_LIMIT = 200  # h11-events-read-as-a-transcript
MAX_PAGES = 50  # reads one open makes in a row, the first included; then it stops and says how to read on
# Shown after every replay.gap line: a transcript gap line says where its gap ends, not where it starts (review 6
# of #405, F1; the Liaison's ruling).
GAP_MARK = "-- lines may be missing before this point: a gap line says where its gap ends, not where it starts --"


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
    ``projection`` and nothing else of it is looked at. ``lines`` is not a list, or holds more lines than the
    read's ``limit`` (``lines``; Sol's round 2 of #405, 3), or a line is not
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
    if _is_int(asked.get("limit")) and len(lines) > asked["limit"]:
        return "lines"  # more lines than the read asked for: refused before anything of it is taken
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


def _as_shown(line: Mapping) -> dict:
    """A line as the TUI keeps it to know it again: every field but its position."""

    return {key: value for key, value in line.items() if key != "seq"}


class Transcript:
    """One session's transcript as read so far. ``cursor`` is the last position the daemon's answers
    represented, in the latest epoch one named. ``last_seq`` and ``last_line`` are the last line shown that is
    not a replay.gap line (its position, and the line without its position; ``last_line`` is None until one
    is shown), and ``gap_top`` the last position the gap lines shown since then cover (0 for none): every
    position from the last line shown to it has been shown or said. ``read_params()``, an open's first read,
    asks from the position before ``last_seq``, for one line more than ``READ_LIMIT``, so the answer begins
    with that line again; ``apply`` checks it. ``read_params(continuing=True)``, the next page of the same
    open, does the same when the last position represented is that line, and reads on from ``cursor`` when
    gap lines came after it, which carry nothing to compare: so gap-only pages never ask for the same page
    twice, and what comes after them is reached (Sol's round 2 of #405, 2). When the log no longer holds the
    last line shown as it was shown, ``apply`` returns None and says why in ``broken``: a caller must not
    iterate None, but rebuild the view (``restart()``) and say so, as the TUI's ``_rebuild`` does (review 6,
    F8). ``owed`` is ``"pending"`` while a rebuilt view's read from the start is not sent (no read left in the
    open) and ``"read_failed"`` when it could not be sent; it is None again once a page is taken (Sol's round
    2, 4). A caller shows ``GAP_MARK`` after every gap line ``apply`` returns (F1). Until a line is shown, a
    read asks from ``cursor``. ``apply`` copies the page before it takes the transcript's own lock, and
    ``read_cursor``, ``read_params`` and ``restart`` take it too, so two threads may drive one transcript and
    each line is still shown once (review 6, F2)."""

    __slots__ = ("_lock", "__dict__", "__weakref__")  # the lock is not state: ``__dict__`` holds the state only

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.cursor = first_cursor(session_id)
        self.pages = 0  # reads made in a row by the current open, the first included
        self.last_seq, self.last_line, self.gap_top, self.broken, self.owed = 0, None, 0, None, None
        self._lock = threading.RLock()

    def read_cursor(self, continuing: bool = False) -> dict:
        """The cursor the next read sends: from the position before the last line shown (the check), or, before a
        line is shown or on a page of the same open that gap lines ended, from ``cursor``."""

        with self._lock:
            if self.last_line is None or (continuing and self.cursor["seq"] > self.last_seq):
                seq = self.cursor["seq"]
            else:
                seq = self.last_seq - 1
            return {"epoch": self.cursor["epoch"], "session_id": self.session_id, "seq": seq}

    def furthest_ask(self) -> int:
        """The furthest position a read of this transcript asks from: an answer, or a refusal, to a read asked from
        past it was sent before the view began again (``restart()``), and is not taken."""

        with self._lock:
            return max(self.cursor["seq"], self.read_cursor()["seq"])

    def read_params(self, continuing: bool = False) -> dict:
        """The next read's params: a read that checks the last line shown asks for one line more."""

        with self._lock:
            cursor = self.read_cursor(continuing)
            params = read_params(cursor)
            if self.last_line is not None and cursor["seq"] < self.last_seq:
                params["limit"] = READ_LIMIT + 1
            return params

    def restart(self) -> None:
        """The view begins again: the next read asks from the start of the log, in the epoch held."""

        with self._lock:
            self.cursor = {"epoch": self.cursor["epoch"], "session_id": self.session_id, "seq": 0}
            self.last_seq, self.last_line, self.gap_top, self.broken = 0, None, 0, None

    def apply(self, answer: Mapping, asked: int | None = None) -> list[dict] | None:
        """One ``ok`` answer that ``page_problem`` found right, to the read that asked from seq ``asked``: the
        lines to show, in order, each once. An answer whose cursor does not name this log, or one asked for
        from past every position represented (a read sent before ``restart()``), shows nothing and changes
        nothing; so does a line at a position below 1 (review 6 of #405, F11). A page asked from the last line
        shown or after it has nothing to check. None when the answer shows that the log no longer holds the
        last line shown as it was shown: another line at its position, a line past it with no gap line over
        it, or an answer that ends before it; ``broken`` says which, and nothing else changes but the epoch. A
        gap line over that position is retention, which rule (7) allows: it is shown when it covers more than
        that position, and the lines after it are shown. The page is copied before the lock is taken, so a
        caller's slow object holds no other caller up (review 6, F2)."""

        cursor, lines = answer.get("cursor"), answer.get("lines")
        page = {"cursor": dict(cursor) if isinstance(cursor, Mapping) else cursor, "more": answer.get("more"),
                "lines": [dict(line.items()) if isinstance(line, Mapping) else line for line in lines]
                if isinstance(lines, list) else lines}
        with self._lock:
            return self._apply(page, asked)

    def _apply(self, answer: dict, asked: int | None) -> list[dict] | None:
        cursor = answer.get("cursor")
        if not (isinstance(cursor, Mapping) and cursor.get("session_id") == self.session_id):
            return []  # not an answer about this log
        if _is_int(asked) and asked > self.furthest_ask():
            return []  # asked for before the view began again: the read from the start gives these lines
        epoch = self.cursor["epoch"]
        if _is_int(cursor.get("epoch")):
            epoch = max(epoch, cursor["epoch"])  # an older epoch goes on with the same log (rule (7))
        checked = self.last_line is None or (_is_int(asked) and asked >= self.last_seq)
        new: list[dict] = []
        lines = answer.get("lines")
        for line in lines if isinstance(lines, list) else []:
            if not (isinstance(line, Mapping) and _is_int(line.get("seq")) and line["seq"] >= 1):
                continue  # a position is 1 or more
            seq, gap = line["seq"], line.get("event_type") == "replay.gap"
            if not checked:
                if seq < self.last_seq:
                    continue  # before the last line shown: shown, if that line is still there
                if seq == self.last_seq and not gap:
                    if _as_shown(line) != self.last_line:
                        return self._broke(epoch, f"seq {seq} no longer holds the line shown there")
                    checked = True
                    continue  # the last line shown, still there as it was shown
                if not gap:
                    return self._broke(epoch, f"the answer goes past seq {self.last_seq}, the last line shown, "
                                              f"with no line there")
                checked = True  # a gap line over the last line shown: retention
                if seq == self.last_seq:
                    continue  # it covers only the last line shown, which the user has seen
            if gap:
                if seq <= self.gap_top:
                    continue  # every position it covers was shown or said since the last line shown
                self.gap_top = seq
            else:
                self.last_seq, self.last_line, self.gap_top = seq, _as_shown(line), 0
            new.append(dict(line))
        if not checked and answer.get("more") is not True:
            return self._broke(epoch, f"the log ends before seq {self.last_seq}, the last line shown")
        seq = self.cursor["seq"]
        if _is_int(cursor.get("seq")):
            seq = max(seq, cursor["seq"])  # the last position represented; never back
        self.cursor = {"epoch": epoch, "session_id": self.session_id, "seq": seq}
        self.owed = None  # a page is taken: nothing of a rebuilt view's read is owed any more
        return new

    def _broke(self, epoch: int, why: str) -> None:
        self.cursor = {**self.cursor, "epoch": epoch}
        self.broken = f"{why} (epoch {epoch})"
        return None
