"""The approval queue: one request, drawn after it commits, answered once, denied at its deadline.

``ApprovalQueue`` is lane APPROVALS' service (``name``, ``start``, ``stop``, ``ready``; S13 of SPINE's seams)
and the object the method handlers and the engine drivers call:

* ``request(source, effect, ...)`` binds the effect (``effects.bind``), and in ONE transaction writes the
  record, the private binding of an engine's request and ``approval.requested``. Only after the commit are
  subscribers told (so a surface draws it) and the deadline worker woken. A request that does not commit is
  never drawn. Returns the core-minted approval id.
* ``wait(approval_id)`` blocks until the request is no longer pending and returns its record: approved,
  denied or expired (expired means denied). It never answers anything itself.
* ``sweep()`` writes ``expired`` and ``approval.answered`` for every pending request whose deadline has come,
  each in its own transaction; the worker started by ``start`` calls it when the next deadline comes.
* ``recover()`` at start: an engine's request still pending belonged to a driver that died with the daemon,
  so it is closed as expired, never approved; any other request that committed but was never told to its
  surfaces is told now; deadlines that passed while the daemon was down are written.
* ``open_in`` / ``answer_in`` run inside the dispatcher's transaction for approval.open and approval.answer
  (``model.open_prompt`` / ``model.answer_prompt``); ``list_page`` serves approval.list (summaries only).
* ``consume(...)`` carries an approved effect out once (``model.consume``), against the private binding.

Deadlines (the brief's question to Lucas, not yet answered; built with its recommendation (b), marked
here so one line changes): 15 minutes for an engine's tool call, 300 seconds for a plugin's effect.
"""

from __future__ import annotations

import copy
import datetime
import logging
import secrets
import threading
import time
from typing import Callable

from recursion_core.approvals import model
from recursion_core.approvals import store as rows
from recursion_core.approvals.model import ApprovalRefused
from recursion_core.approvals.record import binding_problem, milliseconds

__all__ = ["DEADLINE_SECONDS", "ApprovalQueue", "timestamp_of"]

log = logging.getLogger(__name__)

DEADLINE_SECONDS = {"engine_tool": 15 * 60, "plugin_effect": 300}  # brief question (b); Lucas has not answered


def _tell(fn, *args) -> None:
    """A subscriber, after a commit: its failure is logged and never reaches the code that committed."""
    try:
        fn(*args)
    except Exception:  # noqa: BLE001 - a subscriber cannot undo or fail a committed change
        log.exception("an approval subscriber failed")


def timestamp_of(seconds: float) -> str:
    """Epoch seconds as a record timestamp (UTC, milliseconds)."""
    moment = datetime.datetime.fromtimestamp(int(seconds * 1000) / 1000, datetime.timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + "%03d" % (moment.microsecond // 1000) + "Z"


class ApprovalQueue:
    name = "approvals"

    def __init__(self, *, clock: Callable[[], float] = time.time, deadlines: dict | None = None,
                 mint: Callable[[], str] | None = None) -> None:
        self._clock = clock
        self._deadlines = dict(DEADLINE_SECONDS if deadlines is None else deadlines)
        self._mint = mint or (lambda: "apr-" + secrets.token_hex(16))
        self._store = None
        self._lock = threading.Lock()  # guards the fields below; never held across a store transaction
        self._done: dict = {}          # approval id -> Event set once its request is resolved
        self._requested: list = []     # subscribers told after a request commits
        self._answered: list = []      # subscribers told after a request is resolved
        self._wake = threading.Event()
        self._closing = threading.Event()
        self._worker: threading.Thread | None = None
        self.ready = False

    # -- composition -------------------------------------------------------------------------------------

    def attach(self, store, *, declare_tables: bool = True) -> None:
        """Use this event store. Its tables are declared once per process (``register_tables``); a second
        queue on the same store in one process (a restart in a test) passes ``declare_tables=False``."""
        if declare_tables:
            store.register_tables(rows.LANE, rows.VERSION, rows.DDL)
        self._store = store

    def start(self, context) -> None:
        self.attach(context.store)
        self.recover()
        self._closing.clear()
        self._worker = threading.Thread(target=self._run, name="approvals-deadlines", daemon=True)
        self._worker.start()
        self.ready = True

    def stop(self, context=None) -> None:
        self.ready = False
        self._closing.set()
        self._wake.set()
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.join()

    def subscribe_requested(self, fn: Callable[[dict], None]) -> Callable[[], None]:
        """``fn(record)`` after a request commits (and for each one recovery tells again)."""
        return self._subscribe(self._requested, fn)

    def subscribe_answered(self, fn: Callable[[dict, object], None]) -> Callable[[], None]:
        """``fn(record, binding)`` after a request is resolved: a driver answers its engine from this."""
        return self._subscribe(self._answered, fn)

    def _subscribe(self, where: list, fn) -> Callable[[], None]:
        token = object()
        with self._lock:
            where.append((token, fn))

        def unsubscribe() -> None:
            with self._lock:
                where[:] = [item for item in where if item[0] is not token]
        return unsubscribe

    def now(self) -> str:
        return timestamp_of(self._clock())

    @property
    def store(self):
        """The event store this queue writes to, or ``None`` before one is attached."""
        return self._store

    # -- requests ----------------------------------------------------------------------------------------

    def request(self, source: str, effect: dict, *, session_id=None, run_id=None, turn_id=None,
                engine_request_id: str | None = None, display: list = ()) -> str:
        """A new request; refused with ``ApprovalRefused`` (a typed code) and nothing written if it cannot be
        made whole. An engine's request names the driver's pending request (``engine_request_id``)."""
        if self._store is None:
            raise ApprovalRefused("store_absent")
        now = self._clock()
        approval_id = self._mint()
        record = model.new_request(approval_id=approval_id, source=source, effect=effect, session_id=session_id,
                                   run_id=run_id, turn_id=turn_id, requested_at=timestamp_of(now),
                                   expires_at=timestamp_of(now + self._deadlines[source]) if source in self._deadlines
                                   else timestamp_of(now), display=list(display))
        binding = None
        if source == "engine_tool":
            binding = {"approval_id": approval_id, "run_id": record["effect"]["run_id"],
                       "engine": record["effect"]["engine"], "engine_request_id": engine_request_id,
                       "bound_at": record["requested_at"]}
            if binding_problem(binding) is not None:
                raise ApprovalRefused("binding_mismatch")
        elif engine_request_id is not None:
            raise ApprovalRefused("binding_mismatch")  # a plugin's effect has no engine request to bind
        with self._lock:
            self._done[approval_id] = threading.Event()
        try:
            with self._store.transaction() as tx:
                rows.insert(tx, record, binding)
        except BaseException:
            with self._lock:
                self._done.pop(approval_id, None)
            raise
        self._told(record)  # after the commit, and only then
        self._wake.set()
        return approval_id

    def _told(self, record: dict) -> None:
        """Tell the surfaces of a committed request, then remember that they were told."""
        with self._lock:
            listeners = [fn for _, fn in self._requested]
        for fn in listeners:
            _tell(fn, copy.deepcopy(record))
        with self._store.transaction() as tx:
            rows.mark_shown(tx, record["approval_id"])

    def wait(self, approval_id: str, timeout: float | None = None):
        """The record once it is resolved, or ``None`` if ``timeout`` passed first or the id is unknown."""
        with self._lock:
            done = self._done.get(approval_id)
        if done is None:
            with self._store.transaction() as tx:
                held = rows.load(tx, approval_id)
            return held if held is not None and held["state"] != "pending" else None
        if not done.wait(timeout):
            return None
        with self._store.transaction() as tx:
            return rows.load(tx, approval_id)

    def _resolved(self, record: dict, binding) -> None:
        """After the commit that resolved a request: wake its waiter, then tell the subscribers."""
        with self._lock:
            done = self._done.pop(record["approval_id"], None)
            listeners = [fn for _, fn in self._answered]
        if done is not None:
            done.set()
        for fn in listeners:
            _tell(fn, copy.deepcopy(record), copy.deepcopy(binding))

    # -- the deadline --------------------------------------------------------------------------------------

    def sweep(self) -> list:
        """Write every expiry that is due now; return the records that expired."""
        now = self.now()
        expired = []
        with self._store.transaction() as tx:
            due = rows.due(tx, milliseconds(now))
        for approval_id in due:
            with self._store.transaction() as tx:
                held = rows.load(tx, approval_id)
                if held is None:
                    continue
                changed = model.expire_if_due(held, now)
                if changed is None:
                    continue
                rows.save(tx, changed, answered=True)
                binding = rows.binding_of(tx, approval_id)
            expired.append(changed)
            self._resolved(changed, binding)
        return expired

    def _run(self) -> None:
        while not self._closing.is_set():
            with self._store.transaction() as tx:
                deadline = rows.next_deadline(tx)
            wait = None if deadline is None else max(0.0, deadline / 1000 - self._clock())
            self._wake.wait(wait)
            self._wake.clear()
            if not self._closing.is_set():
                self.sweep()

    def recover(self) -> dict:
        """At start: close engine requests whose driver died, tell untold requests, write due expiries."""
        closed, told = [], []
        now = self.now()
        with self._store.transaction() as tx:
            waiting = rows.pending(tx)
        for approval_id in waiting:
            with self._store.transaction() as tx:
                held = rows.load(tx, approval_id)
                if held is None or held["state"] != "pending" or held["source"] != "engine_tool":
                    continue
                held = dict(held, state="expired")  # never approved: its engine request is gone
                rows.save(tx, held, answered=True)
                binding = rows.binding_of(tx, approval_id)
            closed.append(held["approval_id"])
            self._resolved(held, binding)
        expired = self.sweep()
        with self._store.transaction() as tx:
            untold = [rows.load(tx, approval_id) for approval_id in rows.unshown(tx)]
        for held in untold:
            if held is not None:
                self._told(held)
                told.append(held["approval_id"])
        return {"closed": closed, "expired": [r["approval_id"] for r in expired], "told": told, "at": now}

    # -- the methods ---------------------------------------------------------------------------------------

    def open_in(self, tx, principal: dict, params: dict) -> dict:
        """approval.open inside the dispatcher's transaction."""
        held = rows.load(tx, params["approval_id"])
        if held is None:
            return {"code": "approval_unknown", "approval_id": params["approval_id"]}
        changed, answer = model.open_prompt(held, principal, params, self.now())
        if changed["opened_by"] != held["opened_by"]:
            rows.save(tx, changed)
        return answer

    def answer_in(self, tx, principal: dict, params: dict, after_commit: Callable) -> dict:
        """approval.answer inside the dispatcher's transaction; waiters and drivers are told after commit."""
        held = rows.load(tx, params["approval_id"])
        if held is None:
            return {"code": "approval_unknown", "approval_id": params["approval_id"]}
        changed, answer = model.answer_prompt(held, principal, params, self.now())
        if changed["state"] != held["state"]:
            rows.save(tx, changed, answered=True)
            binding = rows.binding_of(tx, changed["approval_id"])
            after_commit(lambda: self._resolved(changed, binding))
        return answer

    def list_page(self, params: dict) -> dict:
        """approval.list: summaries only; it counts as showing nothing."""
        with self._store.transaction() as tx:
            summaries, next_after = rows.list_summaries(
                tx, session_id=params.get("session_id"), state=params.get("state"),
                limit=params.get("limit", 100), after=params.get("after"))
        answer = {"code": "ok", "approvals": summaries}
        if next_after is not None:
            answer["next_after"] = next_after
        return answer

    def consume(self, approval_id: str, revision: int, digest: str, binding) -> dict:
        """Carry an approved effect out once; ``ApprovalRefused`` (a typed code) and nothing changed otherwise."""
        with self._store.transaction() as tx:
            held = rows.load(tx, approval_id)
            if held is None:
                raise ApprovalRefused("approval_unknown")
            kept = rows.binding_of(tx, approval_id)
            consumed = model.consume(held, kept, revision, digest, binding)
            rows.save(tx, consumed)
        return consumed
