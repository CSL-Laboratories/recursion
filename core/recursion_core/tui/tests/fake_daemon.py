"""A fake daemon for the TUI's session and event verbs, built from SPINE's published records (lane TUI,
slice 2; TUI brief v3.3, build step 3).

Nothing here is invented: every answer is a copy of a good fixture in ``schemas/control/v1/fixtures/good``
(SPINE's, merged in from #368 at its pinned head), and the one log it serves is the transcript of the
vector ``vectors/transcript.projection/g01-a-log-and-its-lines.json``. Reading a log follows rule (7) of
``methods.v1.json`` as written there:

- a read returns only positions strictly after ``cursor.seq``, in order, at most ``limit`` of them, with
  ``more`` when there are others;
- every retained position that is gone is one ``replay.gap`` line whose ``seq`` is the gap's last
  position;
- the answer's cursor is the last position it represents, or the request's cursor when it is empty;
- a first read uses epoch 1 and seq 0; an older epoch goes on by its seq and answers the current epoch
  with ``epoch_advanced`` true; a future epoch is ``request_invalid``/``cursor_epoch_ahead``; a seq past
  the high-water mark is ``request_invalid``/``cursor_ahead``; an unknown log is ``session_unknown``;
- with no ``projection`` the answer is the transcript; ``raw`` is not served here (the TUI never asks);
- a request outside the schema's params (a ``limit`` that is not an integer from 1 to 1000, a cursor whose
  epoch, seq or log is not an integer, an integer and a string) is ``request_invalid`` and nothing else.

``methods.list`` says ``implemented`` only of what this fake serves (review 1 of #405, Sol R3): the session
and event reads, the session, queue, approval and workspace answers it holds, and ``methods.list`` itself.
The run verbs are not served (SPINE's one ``run.list`` record names a next page that no record answers), so
they are listed as not implemented and answer ``unsupported``, as the real daemon's would.

``events.follow`` answers at once (a test does not wait): new lines if there are any, else an empty page
with ``timed_out``. ``restart()`` is a daemon restart: the epoch goes up and the log stays. ``forget()``
drops positions as retention would, so the next read shows a gap. ``calls`` records every request.
"""

from __future__ import annotations

import copy
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[4]
SCHEMAS = ROOT / "schemas" / "control" / "v1"
GOOD = SCHEMAS / "fixtures" / "good"


def spine(relative: str):
    """One of SPINE's published records, by its path under schemas/control/v1."""

    return json.loads((SCHEMAS / relative).read_text(encoding="utf-8"))


def exchange(name: str) -> dict:
    """A recorded request and its answer from good/methods.v1 (``{"method", "params", "answer"}``)."""

    return spine(f"fixtures/good/methods.v1/{name}.json")


def gap_line(first: int, last: int, time_rel_ms: int) -> dict:
    """The transcript line of a replay gap, in the words of SPINE's fixture h07-replay-gap."""

    return {"seq": last, "time_rel_ms": time_rel_ms, "event_type": "replay.gap", "source": "core",
            "trust": "trusted", "state": None, "text": f"events {first} to {last} are not available (retention)",
            "speech": "some earlier events are not available", "focus": None}


class FakeDaemon:
    """``call(method, params)`` like ``SocketAPI``; answers from SPINE's fixtures."""

    def __init__(self, implemented: frozenset[str] | None = None) -> None:
        sessions = exchange("h27-session-list-ok")["answer"]
        self.session_id = sessions["sessions"][0]["session_id"]
        self.lines: list[dict] = copy.deepcopy(spine("vectors/transcript.projection/g01-a-log-and-its-lines.json")["lines"])
        self.forgotten: set[int] = set()
        self.epoch = 1
        self.calls: list[tuple[str, dict]] = []
        table = spine("methods.table.v1.json")
        self.table_version = table["table_version"]
        self.beta = {row["name"]: bool(row.get("beta")) for row in table["methods"]}
        self.table = [row["name"] for row in table["methods"]]
        self._answers = {
            "session.list": sessions,
            "approval.list": exchange("h10-approval-list-returns-summaries")["answer"],
            "approval.open": exchange("h07-approval-open")["answer"],
            "approval.answer": exchange("h31-approval-answer-ok")["answer"],
            "session.turn": exchange("h01-session-turn-ok")["answer"],
            "session.steer": exchange("h21-session-steer-ok")["answer"],
            "session.queue": exchange("h22-session-queue-ok")["answer"],
            "session.interrupt": exchange("h23-session-interrupt-ok")["answer"],
            "queue.update": exchange("h29-queue-update-ok")["answer"],
            "queue.cancel": exchange("h30-queue-cancel-ok")["answer"],
            "workspace.diff": exchange("h36-workspace-diff-ok")["answer"],
        }
        served = frozenset({"methods.list", "events.read", "events.follow", *self._answers}) & frozenset(self.table)
        # Only what is served is implemented; an override can narrow it, never widen it (Sol R3).
        self.implemented = served if implemented is None else served & frozenset(implemented)

    # -- what a test does to the daemon ----------------------------------------------------------
    @property
    def high_water(self) -> int:
        return self.lines[-1]["seq"] if self.lines else 0

    def append(self, event_type: str, text: str | None, *, source: str = "engine", state: str | None = None,
               speech: str | None = None, focus: str | None = None) -> dict:
        """One more line at the end of the log (the next seq, the same time as the last line or later)."""

        trust = "untrusted" if source in ("engine", "plugin") else "trusted"
        line = {"seq": self.high_water + 1, "time_rel_ms": self.lines[-1]["time_rel_ms"] + 1 if self.lines else 0,
                "event_type": event_type, "source": source, "trust": trust, "state": state, "text": text,
                "speech": speech, "focus": focus}
        self.lines.append(line)
        return line

    def restart(self) -> None:
        """A daemon restart: a new epoch over the same persistent log."""

        self.epoch += 1

    def forget(self, first: int, last: int) -> None:
        """Retention dropped positions ``first`` to ``last``: a read across them shows one gap."""

        self.forgotten |= set(range(first, last + 1))

    # -- the API -----------------------------------------------------------------------------------
    def call(self, method: str, params: dict | None = None) -> dict:
        params = copy.deepcopy(params or {})
        self.calls.append((method, params))
        if method not in self.table:
            return {"code": "method_unknown", "detail": method}
        if method == "methods.list":
            return {"code": "ok", "table_version": self.table_version,
                    "methods": [{"name": n, "implemented": n in self.implemented, "beta": self.beta[n]}
                                for n in self.table]}
        if method not in self.implemented:
            return {"code": "unsupported", "detail": "not_implemented"}
        if method in ("events.read", "events.follow"):
            return self._read(params, follow=method == "events.follow")
        if method in self._answers:
            return copy.deepcopy(self._answers[method])
        return {"code": "unsupported", "detail": "not_implemented"}

    def _read(self, params: dict, *, follow: bool) -> dict:
        cursor = params.get("cursor")
        if (not isinstance(cursor, dict) or not {"epoch", "session_id", "seq"} <= set(cursor)
                or type(cursor["epoch"]) is not int or cursor["epoch"] < 1 or type(cursor["seq"]) is not int
                or cursor["seq"] < 0 or not isinstance(cursor["session_id"], str)):
            return {"code": "request_invalid", "detail": "cursor"}  # DeepSeek L4: refused, never a raise
        limit = params.get("limit", 200)
        if type(limit) is not int or not 1 <= limit <= 1000:  # params.events.read: an integer, 1 to 1000
            return {"code": "request_invalid", "detail": "limit"}
        if cursor["session_id"] != self.session_id:
            return {"code": "session_unknown", "detail": str(cursor["session_id"])}
        if cursor["epoch"] > self.epoch:
            return {"code": "request_invalid", "detail": "cursor_epoch_ahead"}
        if cursor["seq"] > self.high_water:
            return {"code": "request_invalid", "detail": "cursor_ahead"}
        projection = params.get("projection", "transcript")
        if projection not in ("raw", "transcript"):
            return {"code": "request_invalid", "detail": "projection"}
        if projection != "transcript":  # raw is not served here; not_implemented is the schema's only detail
            return {"code": "unsupported", "detail": "not_implemented"}
        page: list[dict] = []
        pending_gap: list[int] = []
        for line in self.lines:
            seq = line["seq"]
            if seq <= cursor["seq"]:
                continue
            if seq in self.forgotten:
                pending_gap.append(seq)
                continue
            if pending_gap:
                page.append(gap_line(pending_gap[0], pending_gap[-1], line["time_rel_ms"]))
                pending_gap = []
            page.append(copy.deepcopy(line))
        if pending_gap:
            page.append(gap_line(pending_gap[0], pending_gap[-1], self.lines[-1]["time_rel_ms"]))
        more = len(page) > limit
        page = page[:limit]
        last = page[-1]["seq"] if page else cursor["seq"]
        answer = {"code": "ok", "projection": "transcript", "lines": page,
                  "cursor": {"epoch": self.epoch, "session_id": self.session_id, "seq": last},
                  "more": more, "epoch_advanced": cursor["epoch"] < self.epoch}
        if follow and not page:
            answer["timed_out"] = True
        return answer
