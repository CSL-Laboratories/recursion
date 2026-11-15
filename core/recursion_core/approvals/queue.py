"""The approval queue: one request, drawn after it commits, answered once, denied at its deadline.

``ApprovalQueue`` is lane APPROVALS' service (``name``, ``start``, ``stop``, ``ready``; S13 of SPINE's seams)
and the object the method handlers and the engine drivers call:

* ``request(source, effect, ...)`` binds the effect (``effects.bind``) and in ONE transaction writes the
  record, the private binding of an engine's request and ``approval.requested``. After the commit, and only
  then, the deadline worker is woken and the subscribers are told (so a surface draws it). A request that
  does not commit is never drawn. Returns the core-minted approval id; nothing after the commit can make it
  fail. ``ask(effect, binding)`` is the same for a driver's tool call (the brief's ``request(effect,
  binding)``).
* A request counts as SHOWN only once a subscriber took it without raising. One that no subscriber took
  (none was subscribed, or each one failed) is offered again by ``recover`` at the next start.
* ``wait(approval_id)`` blocks until the request is no longer pending and returns its record: approved,
  denied or expired (expired means denied). It works for a request made before a restart too.
* ``sweep()`` writes ``expired`` and ``approval.answered`` for every pending request whose deadline has come.
  The worker started by ``start`` calls it at each deadline. A failure of the worker withdraws ``ready``
  and the worker tries again; a row that cannot be read is set aside, never retried for ever.
* ``recover()`` at start: an engine's request still pending belonged to a driver that died with the daemon,
  so it ends as expired, never approved; an engine's request approved but not carried out loses its
  binding, so nothing can carry it out through the dead driver; a request never shown is offered again;
  deadlines that passed while the daemon was down are written.
* ``open_in`` / ``answer_in`` run inside the dispatcher's transaction for approval.open and approval.answer;
  ``list_page`` serves approval.list (summaries only).
* ``consume(...)`` carries an approved effect out once, against the private binding; every refusal is a
  typed ``ApprovalRefused``.

Two answers at once: with EVENTS' store (one transaction at a time) the second waits, reads the resolved
record and is answered ``conflict``/``already_resolved``. A store that lets two transactions run at once
refuses the second one's write instead; the dispatcher then answers ``unavailable``, keeps no receipt, and
the same command retried is ``already_resolved``. In both, one answer wins and none is taken twice.

Deadlines, PROVISIONAL (the brief's question to Lucas, not yet answered; built with its recommendation
(b), marked here so one line changes): 15 minutes for an engine's tool call, 300 seconds for a plugin's
effect.
"""

from __future__ import annotations

import copy
import datetime
import logging
import secrets
import sqlite3
import threading
import time
from typing import Callable

from recursion_core.approvals import model
from recursion_core.approvals import store as rows
from recursion_core.approvals.model import ApprovalRefused
from recursion_core.approvals.record import binding_problem, milliseconds

__all__ = ["DEADLINE_SECONDS", "ApprovalQueue", "timestamp_of"]

log = logging.getLogger(__name__)

# PROVISIONAL: the brief's question (b) to Lucas, not yet answered.
DEADLINE_SECONDS = {"engine_tool": 15 * 60, "plugin_effect": 300}
RETRY_SECONDS = 1.0  # the deadline worker's pause after a failure before it tries again
_CONSUME_ATTEMPTS = 12  # a consumption refused by a concurrent writer is read again this many times (about 0.4 s)


def _tell(fn, *args) -> bool:
    """A subscriber, after a commit. Its failure is logged by type only (its message could carry what it
    was given) and never reaches the code that committed. True when it took the call."""
    try:
        fn(*args)
    except Exception as exc:  # noqa: BLE001 - a subscriber cannot undo or fail a committed change
        log.error("an approval subscriber failed: %s", type(exc).__name__)
        return False
    return True


def timestamp_of(seconds: float) -> str:
    """Epoch seconds as a record timestamp (UTC, milliseconds)."""
    moment = datetime.datetime.fromtimestamp(int(seconds * 1000) / 1000, datetime.timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + "%03d" % (moment.microsecond // 1000) + "Z"


class ApprovalQueue:
    name = "approvals"

    def __init__(self, *, clock: Callable[[], float] = time.time, deadlines: dict | None = None,
                 mint: Callable[[], str] | None = None, retry_seconds: float = RETRY_SECONDS) -> None:
        self._clock = clock
        self._deadlines = dict(DEADLINE_SECONDS if deadlines is None else deadlines)
        self._mint = mint or (lambda: "apr-" + secrets.token_hex(16))
        self._retry = retry_seconds
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

    @property
    def store(self):
        """The event store this queue writes to, or ``None`` before one is attached."""
        return self._store

    def start(self, context) -> None:
        if self._store is None:
            self.attach(context.store)
        elif self._store is not context.store:
            raise RuntimeError("the approval queue is attached to another store")
        self.recover()
        self._closing.clear()
        worker = threading.Thread(target=self._run, name="approvals-deadlines", daemon=True)
        self._worker = worker
        worker.start()
        self.ready = True

    def stop(self, context=None) -> None:
        """Signal the worker and return only once it has joined. Interrupted, the worker is kept, and a
        second ``stop`` joins it again."""
        self.ready = False
        self._closing.set()
        self._wake.set()
        worker = self._worker
        if worker is not None:
            worker.join()
            self._worker = None

    def subscribe_requested(self, fn: Callable[[dict], None]) -> Callable[[], None]:
        """``fn(record)`` after a request commits (and for each one recovery offers again)."""
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

    # -- requests ----------------------------------------------------------------------------------------

    def request(self, source: str, effect: dict, *, session_id=None, run_id=None, turn_id=None,
                engine_request_id: str | None = None, display: list = ()) -> str:
        """A new request; refused with ``ApprovalRefused`` (a typed code) and nothing written if it cannot be
        made whole. An engine's request names the driver's pending request (``engine_request_id``)."""
        if self._store is None:
            raise ApprovalRefused("store_absent")
        now = self._clock()
        approval_id = self._mint()
        if source not in self._deadlines:
            raise ApprovalRefused("source")
        record = model.new_request(approval_id=approval_id, source=source, effect=effect, session_id=session_id,
                                   run_id=run_id, turn_id=turn_id, requested_at=timestamp_of(now),
                                   expires_at=timestamp_of(now + self._deadlines[source]), display=list(display))
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
            self._done.setdefault(approval_id, threading.Event())
        try:
            with self._store.transaction() as tx:
                rows.insert(tx, record, binding)
        except BaseException:
            with self._lock:
                self._done.pop(approval_id, None)
            raise
        # Committed. Nothing below can fail this request: wake the deadline worker first, then tell.
        self._wake.set()
        self._offer(record)
        return approval_id

    def ask(self, effect: dict, binding: dict, *, turn_id=None) -> str:
        """A driver's tool call (the brief's ``request(effect, binding)``): an engine-tool effect, and the
        driver's side of the binding, ``{"engine_request_id": ...}``. Returns the approval id."""
        if type(effect) is not dict or type(binding) is not dict or set(binding) != {"engine_request_id"}:
            raise ApprovalRefused("binding_mismatch")
        return self.request("engine_tool", effect, session_id=effect.get("session_id"), run_id=effect.get("run_id"),
                            turn_id=turn_id, engine_request_id=binding["engine_request_id"])

    def _offer(self, record: dict) -> bool:
        """Tell the subscribers of a committed request; remember it as shown only if one took it."""
        with self._lock:
            listeners = [fn for _, fn in self._requested]
        taken = False
        for fn in listeners:
            taken = _tell(fn, copy.deepcopy(record)) or taken
        if taken:
            try:
                with self._store.transaction() as tx:
                    rows.mark_shown(tx, record["approval_id"])
            except Exception as exc:  # noqa: BLE001 - the request committed; it is offered again at restart
                log.error("an approval was shown but could not be marked shown: %s", type(exc).__name__)
        return taken

    def wait(self, approval_id: str, timeout: float | None = None):
        """The record once it is resolved, or ``None`` if ``timeout`` passed first or the id is unknown.

        The waiter is registered BEFORE the record is read, so a resolution that commits in between still
        wakes it. A request made before a restart can be waited for too.
        """
        with self._lock:
            done = self._done.setdefault(approval_id, threading.Event())
        with self._store.transaction() as tx:
            held = rows.load(tx, approval_id)
        if held is None:
            with self._lock:
                if self._done.get(approval_id) is done and not done.is_set():
                    self._done.pop(approval_id, None)
            return None
        if held["state"] != "pending":
            return held
        if not done.wait(timeout):
            return None
        with self._store.transaction() as tx:
            return rows.load(tx, approval_id)

    def _resolved(self, record: dict, binding) -> None:
        """After the commit that resolved a request: wake its waiters, then tell the subscribers."""
        with self._lock:
            done = self._done.pop(record["approval_id"], None)
            listeners = [fn for _, fn in self._answered]
        if done is not None:
            done.set()
        for fn in listeners:
            _tell(fn, copy.deepcopy(record), copy.deepcopy(binding))

    # -- the deadline --------------------------------------------------------------------------------------

    def sweep(self) -> list:
        """Write every expiry that is due now; return the records that expired. A row that cannot be read
        is set aside (it is not an approval any more) rather than tried again at every turn."""
        now = self.now()
        expired = []
        with self._store.transaction() as tx:
            due = rows.due(tx, milliseconds(now))
        for approval_id in due:
            try:
                with self._store.transaction() as tx:
                    held = rows.load(tx, approval_id)
                    if held is None:
                        continue
                    changed = model.expire_if_due(held, now)
                    if changed is None:
                        continue
                    rows.save(tx, changed, answered=True)
                    binding = rows.binding_of(tx, approval_id)
            except ApprovalRefused as refused:
                log.error("approval %s cannot be read (%s); set aside", approval_id, refused.code)
                with self._store.transaction() as tx:
                    rows.set_aside(tx, approval_id)
                continue
            expired.append(changed)
            self._resolved(changed, binding)
        return expired

    def _run(self) -> None:
        """The deadline worker: sweep at each deadline; on a failure, withdraw ``ready``, pause, try again."""
        while not self._closing.is_set():
            try:
                with self._store.transaction() as tx:
                    deadline = rows.next_deadline(tx)
                wait = None if deadline is None else max(0.0, deadline / 1000 - self._clock())
                self._wake.wait(wait)
                self._wake.clear()
                if self._closing.is_set():
                    break
                self.sweep()
                self.ready = True
            except Exception as exc:  # noqa: BLE001 - the worker must outlive a failed transaction
                self.ready = False
                log.error("the approval deadline worker failed (%s); trying again", type(exc).__name__)
                self._closing.wait(self._retry)

    def recover(self) -> dict:
        """At start: end engine requests whose driver died, retire the bindings of approved ones not yet
        carried out, write due expiries, offer again the requests never shown."""
        closed, retired, offered = [], [], []
        now = self.now()
        with self._store.transaction() as tx:
            engine = rows.unresolved_engine(tx)
        for approval_id in engine:
            try:
                with self._store.transaction() as tx:
                    held = rows.load(tx, approval_id)
                    binding = rows.binding_of(tx, approval_id)
                    if held is None:
                        continue
                    if held["state"] == "pending":
                        held = dict(held, state="expired")  # never approved: its engine request is gone
                        rows.save(tx, held, answered=True)
                        closed.append(approval_id)
                    elif held["state"] == "approved":
                        rows.retire_binding(tx, approval_id)  # its driver died: nothing carries it out now
                        retired.append(approval_id)
                        continue
                    else:
                        continue
            except ApprovalRefused as refused:
                log.error("approval %s cannot be read at start (%s); set aside", approval_id, refused.code)
                with self._store.transaction() as tx:
                    rows.set_aside(tx, approval_id)
                continue
            self._resolved(held, binding)
        expired = self.sweep()
        with self._store.transaction() as tx:
            never_shown = rows.unshown(tx)
        for approval_id in never_shown:
            try:
                with self._store.transaction() as tx:
                    held = rows.load(tx, approval_id)
            except ApprovalRefused:
                continue
            if held is not None and held["state"] == "pending":
                self._offer(held)
                offered.append(approval_id)
        return {"closed": closed, "retired": retired, "expired": [r["approval_id"] for r in expired],
                "told": offered, "at": now}

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
        """Carry an approved effect out once; ``ApprovalRefused`` (a typed code) and nothing changed otherwise.

        A store that runs two transactions at once refuses the second one's write; the consumption is then
        tried again from a fresh read (a short, bounded backoff), which finds it consumed and refuses
        ``already_consumed``. A store still refusing after that is ``store_busy``, never a raw store error.
        """
        for attempt in range(_CONSUME_ATTEMPTS):
            try:
                with self._store.transaction() as tx:
                    held = rows.load(tx, approval_id)
                    if held is None:
                        raise ApprovalRefused("approval_unknown")
                    kept = rows.binding_of(tx, approval_id)
                    consumed = model.consume(held, kept, revision, digest, binding)
                    rows.save(tx, consumed)
                return consumed
            except sqlite3.OperationalError:
                time.sleep(min(0.001 * 2 ** attempt, 0.05))
        raise ApprovalRefused("store_busy")
