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
said as an error (review 4, R3). A replay.gap line already shown is not shown again when the same gap line
comes again after the last line shown (review 3, F3).
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


def _as_shown(line: Mapping) -> dict:
    """A line as the TUI keeps it to know it again: every field but its position."""

    return {key: value for key, value in line.items() if key != "seq"}


class Transcript:
    """One session's transcript as read so far. ``cursor`` is the last position the daemon's answers
    represented, in the latest epoch one named. ``last_seq`` and ``last_line`` are the last line shown that is
    not a replay.gap line (its position, and the line without its position; ``last_line`` is None until one
    is shown), and ``after`` the gap lines shown since. ``read_params()`` asks from the position before
    ``last_seq``, for one line more than ``READ_LIMIT``, so the answer begins with that line again; ``apply``
    checks it, and returns None, with the reason in ``broken``, when the log no longer holds that line as it
    was shown. ``restart()`` begins the view again from the start of the log. Until a line is shown, a read
    asks from ``cursor``."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.cursor = first_cursor(session_id)
        self.pages = 0  # reads made in a row by the current open, the first included
        self.last_seq, self.last_line, self.after, self.broken = 0, None, [], None

    def read_cursor(self) -> dict:
        """The cursor the next read sends: the position before the last line shown, or ``cursor`` before one."""

        seq = self.last_seq - 1 if self.last_line is not None else self.cursor["seq"]
        return {"epoch": self.cursor["epoch"], "session_id": self.session_id, "seq": seq}

    def read_params(self) -> dict:
        """The next read's params: the last line shown comes again, so the page asks for one line more."""

        params = read_params(self.read_cursor())
        if self.last_line is not None:
            params["limit"] = READ_LIMIT + 1
        return params

    def restart(self) -> None:
        """The view begins again: the next read asks from the start of the log, in the epoch held."""

        self.cursor = {"epoch": self.cursor["epoch"], "session_id": self.session_id, "seq": 0}
        self.last_seq, self.last_line, self.after, self.broken = 0, None, [], None

    def apply(self, answer: Mapping, asked: int | None = None) -> list[dict] | None:
        """One ``ok`` answer that ``page_problem`` found right, to the read that asked from seq ``asked``: the
        lines to show, in order, each once. An answer whose cursor does not name this log, or one asked for
        from past the position the next read asks from (a read sent before ``restart()``), shows nothing and
        changes nothing. None when the answer shows that the log no longer holds the last line shown as it was
        shown: another line at its position, a line past it with no gap line over it, or an answer that ends
        before it; ``broken`` says which, and nothing else changes but the epoch. A gap line over that position
        is retention, which rule (7) allows: it is shown when it covers more than that position, and the lines
        after it are shown."""

        cursor = answer.get("cursor")
        if not (isinstance(cursor, Mapping) and cursor.get("session_id") == self.session_id):
            return []  # not an answer about this log
        if _is_int(asked) and asked > self.read_cursor()["seq"]:
            return []  # asked for before the view began again: the read from the start gives these lines
        epoch = self.cursor["epoch"]
        if _is_int(cursor.get("epoch")):
            epoch = max(epoch, cursor["epoch"])  # an older epoch goes on with the same log (rule (7))
        checked = self.last_line is None
        new: list[dict] = []
        lines = answer.get("lines")
        for line in lines if isinstance(lines, list) else []:
            if not (isinstance(line, Mapping) and _is_int(line.get("seq"))):
                continue
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
                if dict(line) in self.after:
                    continue  # this gap line was shown after the last line shown
                self.after = [*self.after[-READ_LIMIT:], dict(line)]
            else:
                self.last_seq, self.last_line, self.after = seq, _as_shown(line), []
            new.append(dict(line))
        if not checked and answer.get("more") is not True:
            return self._broke(epoch, f"the log ends before seq {self.last_seq}, the last line shown")
        seq = self.cursor["seq"]
        if _is_int(cursor.get("seq")):
            seq = max(seq, cursor["seq"])  # the last position represented; never back
        self.cursor = {"epoch": epoch, "session_id": self.session_id, "seq": seq}
        return new

    def _broke(self, epoch: int, why: str) -> None:
        self.cursor = {**self.cursor, "epoch": epoch}
        self.broken = f"{why} (epoch {epoch})"
        return None
