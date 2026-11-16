"""The approval queue: one request, drawn after it commits, answered once, denied at its deadline.

``ApprovalQueue`` is lane APPROVALS' service (``name``, ``start``, ``stop``, ``ready``; S13 of SPINE's seams)
and the object the method handlers and the engine drivers call:

* ``request(source, effect, ...)`` binds the effect (``effects.bind``) and in ONE transaction writes the
  record, the private binding of an engine's request and ``approval.requested``. After the commit, and only
  then, the deadline worker is woken and the subscribers are told (so a surface draws it). A request that
  does not commit is never drawn. Returns the core-minted approval id; nothing after the commit can make it
  fail. A request that cannot be made whole is refused with ``ApprovalRefused`` and nothing is written; any
  other ``Exception`` of the insert's transaction is ``store_unavailable`` with its cause chained, a store
  that fails and a caller's own misuse alike (a request made inside another transaction); an interruption
  passes as it is. ``ask(effect, binding)`` is the same for a driver's tool call (the brief's
  ``request(effect, binding)``).
* A request counts as SHOWN only once a subscriber took it without raising. One that no subscriber took
  (none was subscribed, or each one failed) is offered again by the next start, once its worker runs.
* ``wait(approval_id)`` blocks until the request is no longer pending and returns its record: approved,
  denied or expired (expired means denied). It works for a request made before a restart too. The expiry is
  written by the deadline worker of a started queue (or an answer resolves it); on a queue that is attached
  but not started a pending request stays pending, so a caller that must not wait past a moment passes
  ``timeout`` (seconds, at most ``threading.TIMEOUT_MAX``; anything else is refused ``timeout``).
* ``sweep()`` writes ``expired`` and ``approval.answered`` for every pending request whose deadline has come.
  The worker started by ``start`` calls it at each deadline. A failure of the worker withdraws ``ready``
  and the worker tries again; a row that cannot be read is set aside, never retried for ever.
* ``recover()`` at start: an engine's request still pending belonged to a driver that died with the daemon,
  so it ends as expired, never approved; an engine's request approved but not carried out loses its
  binding, so nothing can carry it out through the dead driver; deadlines that passed while the daemon was
  down are written. While a start runs no surface is offered anything: the start offers what no surface
  took to every surface subscribed then, once its worker runs, and nothing at all if it fails. A start that
  raises before it lets the surfaces through (a failure, or an interruption anywhere from the moment it holds
  the surfaces back) lets them through all the same, so each new request is offered to every surface at once.
  If it had published its own worker, that worker is withdrawn (``ready`` too) and kept for ``stop`` to join,
  and the queue is left not started; if not, a worker an earlier start left running is left as it was, and so
  is ``ready``. One interrupted while it offers the backlog leaves the queue started; what it had not offered
  stays unshown. (A second interruption that lands inside that withdrawal itself is not covered.)
* ``stop`` wakes and joins every worker a start launched and no stop has joined yet: the current one, one a
  later start replaced, one an interrupted launch kept. Those workers share one wake signal, and a worker that
  wakes to find the queue closing, or its generation gone, sets it again before it returns, so none takes the
  signal another is waiting on.
* ``start`` and ``stop`` run one at a time, whoever calls them: a start that comes while a stop runs waits until
  that stop has joined its workers, and a stop waits for a start in progress. So no worker is published or
  launched while a stop withdraws and joins, none a stop has read is forgotten, and no start clears the wake
  a stop's worker is still waiting on. The lock is re-entrant: a subscriber that the start's offering calls
  may call ``stop`` on its own thread.
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
import math
import secrets
import sqlite3
import threading
import time
from typing import Callable

from recursion_core.approvals import model
from recursion_core.approvals import store as rows
from recursion_core.approvals.model import ApprovalRefused
from recursion_core.approvals.record import SURFACES, binding_problem, milliseconds

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
        self._workers: list = []       # every worker a start launched and no stop has joined yet
        self._lifecycle = threading.RLock()  # held across all of start and all of stop: one at a time
        self._generation = None        # the running worker's token; a worker whose token is gone returns
        self._running = False          # started and not stopped: a surface that subscribes now is offered
        self._late = None              # while a start runs: (token, fn) of surfaces that subscribed during it
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
        """Recover, launch the deadline worker and let the surfaces through (the module text says how a start
        that fails or is interrupted leaves the queue). One at a time with ``stop``: under ``_lifecycle``."""
        with self._lifecycle:
            self._start(context)

    def _start(self, context) -> None:
        if self._store is None:
            self.attach(context.store)
        elif self._store is not context.store:
            raise RuntimeError("the approval queue is attached to another store")
        generation = object()
        try:
            with self._lock:
                # Held back until the worker runs: every surface subscribed now, and each one that subscribes while
                # this start runs (subscribe_requested adds it), so no surface is offered anything during the
                # start. Inside the guarded region, so a start interrupted even as this is taken lets them go.
                self._late = list(self._requested)
            self.recover()
            worker = threading.Thread(target=self._run, args=(generation,), name="approvals-deadlines",
                                      daemon=True)
            with self._lock:
                # Ready before the worker runs, in one step with its generation: from here only this worker
                # withdraws ready (on a failure) and restores it (after a sweep), so start never overwrites a
                # failure the worker has already reported, and a worker whose generation has ended changes
                # nothing.
                self._closing.clear()
                self._generation = generation
                self._worker = worker
                self._workers.append(worker)  # stop joins it, whatever start comes after
                self.ready = True
            worker.start()
            # Recovery has retired what a dead driver left, and the worker runs: the surfaces are let through here,
            # INSIDE the guarded region, so a start interrupted before or as they are is withdrawn below, never
            # left running with every surface held back.
            with self._lock:
                self._late = None
                self._running = True
        except BaseException:
            # Failed or interrupted anywhere above: the surfaces are let through, so a request after this is
            # offered to every surface at once. If this start published its worker (the thread may have launched
            # all the same, by an interruption that lands once it has), that worker is withdrawn as stop withdraws
            # it, in the same step: it can never publish ready, and it is kept for stop to join, and the queue is
            # left not started. If it had not, a worker of an earlier start that still runs is not this start's
            # to withdraw: it and ``ready`` are left as they were.
            with self._lock:
                self._late = None
                if self._generation is generation:
                    self.ready = False
                    self._running = False
                    self._closing.set()
                    self._generation = None
            self._wake.set()
            raise
        # Every surface subscribed now (before the start or during it, unless it unsubscribed) is offered what no
        # surface took, once, here; from here a surface that subscribes is offered it when it subscribes. The queue
        # has started: an interruption while this offers leaves it started, and what was not offered stays unshown.
        self._offer_backlog()

    def stop(self, context=None) -> None:
        """Signal every worker and return only once each has joined: the current one, one a later start replaced
        and one an interrupted launch kept (a start never joins one: it only publishes its own). Interrupted,
        the workers not yet joined are kept, and a second ``stop`` joins them. A worker whose thread never
        launched (its start failed) has nothing to join and is let go; were it to launch late, its generation is
        gone and it returns at once. One at a time with ``start`` (under ``_lifecycle``), so no start publishes
        or launches a worker while this withdraws and joins."""
        with self._lifecycle:
            self._stop()

    def _stop(self) -> None:
        self._withdraw()
        while True:
            with self._lock:
                pending = list(self._workers)
                if self._worker is not None and self._worker not in pending:
                    pending.append(self._worker)
            if not pending:
                return
            worker = pending[0]
            if worker.ident is not None or worker.is_alive():
                worker.join()
            with self._lock:
                self._workers = [kept for kept in self._workers if kept is not worker]
                if self._worker is worker:
                    self._worker = None

    def _withdraw(self) -> None:
        """Withdraw ``ready`` and signal the worker to return. ``ready`` and the closing signal change together
        under the lock, so a sweep that ends now cannot publish ready again (publishing needs the signal clear)
        and its worker returns at its loop head; then it is woken. The generation is cleared in the same step
        for tidiness only: nothing tells it apart, since a failing sweep of that worker can only write
        ``ready = False`` over a ``False``, and the next start replaces the generation as it clears the signal."""
        with self._lock:
            self.ready = False
            self._running = False
            self._closing.set()
            self._generation = None
        self._wake.set()

    def subscribe_requested(self, fn: Callable[[dict], None]) -> Callable[[], None]:
        """``fn(record)`` after a request commits. A start offers every pending request no surface took to each
        surface subscribed before it or during it, once its worker has launched (nothing before, and nothing at
        all if the start fails); a surface that subscribes once the queue has started is offered those at once.
        So a surface can be offered one request more than once, always under the same approval id."""
        token = object()
        with self._lock:
            self._requested.append((token, fn))
            running = self._running
            if self._late is not None:
                self._late.append((token, fn))
        if running:
            self._offer_backlog(fn)
        return self._unsubscriber(self._requested, token)

    def _offer_backlog(self, only=None) -> None:
        """Offer what no surface took, to every subscriber or only to ``only``. A failure is logged and changes
        no subscription; the next start offers it again."""
        try:
            self._offer_unshown(only=only)
        except Exception as exc:  # noqa: BLE001 - the subscriptions stand; the next start offers them
            log.error("pending approvals could not be offered: %s", type(exc).__name__)

    def subscribe_answered(self, fn: Callable[[dict, object], None]) -> Callable[[], None]:
        """``fn(record, binding)`` after a request is resolved: a driver answers its engine from this."""
        return self._subscribe(self._answered, fn)

    def _subscribe(self, where: list, fn) -> Callable[[], None]:
        token = object()
        with self._lock:
            where.append((token, fn))
        return self._unsubscriber(where, token)

    def _unsubscriber(self, where: list, token) -> Callable[[], None]:
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
        if type(display) not in (list, tuple) or any(type(item) is not dict for item in display):
            raise ApprovalRefused("display")
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
        except BaseException as exc:
            with self._lock:
                self._done.pop(approval_id, None)
            if isinstance(exc, Exception) and not isinstance(exc, ApprovalRefused):
                raise ApprovalRefused("store_unavailable") from exc  # nothing was written
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

    def _offer(self, record: dict, *, only=None) -> bool:
        """Tell the subscribers of a committed request (or only ``only``); remember it as shown only if one
        took it. While a start runs every surface is held back (those subscribed when it began and each one
        that subscribes during it): recovery's own offering, and a request committed meanwhile, reach none of
        them, and ``start`` offers them what no surface took once its worker runs."""
        with self._lock:
            if only is None:
                waiting = {token for token, _ in self._late or ()}
                listeners = [fn for token, fn in self._requested if token not in waiting]
            else:
                listeners = [only]
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
        wakes it. A request made before a restart can be waited for too. The deadline is the worker's: a
        pending request is resolved by an answer or by the deadline worker of a started queue, so with none
        running only ``timeout`` ends the wait: ``None``, or a number of seconds ``threading.Event.wait`` takes
        (finite and at most ``threading.TIMEOUT_MAX``; zero or less returns at once). Anything else is refused
        ``timeout`` before anything is read.
        """
        if timeout is not None and (type(timeout) not in (int, float)
                                    or not -math.inf < timeout <= threading.TIMEOUT_MAX):
            raise ApprovalRefused("timeout")  # compared, never converted: a huge int is refused, not overflowed
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
            # Already resolved: this registration will never be resolved again, so it goes now; a waiter
            # sharing it is woken and reads the same resolved record.
            with self._lock:
                if self._done.get(approval_id) is done:
                    del self._done[approval_id]
            done.set()
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
        is set aside (it is not an approval any more) rather than tried again at every turn. Any other
        failure of one row holds back no other row: the rest are expired, then the first failure is raised
        (the worker withdraws ``ready``, pauses and tries again)."""
        now = self.now()
        expired = []
        failure = None
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
                try:
                    with self._store.transaction() as tx:
                        rows.set_aside(tx, approval_id)
                except Exception as exc:  # noqa: BLE001 - tried again at the next turn; the others go on
                    log.error("approval %s could not be set aside (%s)", approval_id, type(exc).__name__)
                    failure = failure or exc
                continue
            except Exception as exc:  # noqa: BLE001 - one row's failure holds back no other deadline
                log.error("approval %s could not be expired (%s)", approval_id, type(exc).__name__)
                failure = failure or exc
                continue
            expired.append(changed)
            self._resolved(changed, binding)
        if failure is not None:
            raise failure
        return expired

    def _run(self, generation=None) -> None:
        """The deadline worker: sweep at each deadline; on a failure, withdraw ``ready``, pause, try again.
        It returns once the queue closes or its generation is no longer the queue's; a sweep it is in when that
        happens is finished, and from then on it changes ``ready`` no more. The wake signal is shared with any
        other worker still alive (one a later start replaced, or one an interrupted launch kept): a worker that
        wakes to find the queue closing, or its generation gone, sets it again before it returns, so its own
        clear never takes the wake another worker waits on, a stop's above all."""
        while not self._closing.is_set() and self._generation is generation:
            try:
                with self._store.transaction() as tx:
                    deadline = rows.next_deadline(tx)
                wait = None if deadline is None else max(0.0, deadline / 1000 - self._clock())
                self._wake.wait(wait)
                self._wake.clear()
                if self._closing.is_set() or self._generation is not generation:
                    self._wake.set()  # passed on: the clear above may have taken another worker's wake
                    break
                self.sweep()
                with self._lock:
                    if not self._closing.is_set() and self._generation is generation:
                        self.ready = True
            except Exception as exc:  # noqa: BLE001 - the worker must outlive a failed transaction
                with self._lock:
                    if self._generation is generation:  # a worker whose generation has ended changes nothing
                        self.ready = False
                log.error("the approval deadline worker failed (%s); trying again", type(exc).__name__)
                self._closing.wait(self._retry)

    def _offer_unshown(self, *, only=None) -> list:
        """Offer again each pending request no surface has taken, to every subscriber or only to ``only``."""
        offered = []
        with self._store.transaction() as tx:
            never_shown = rows.unshown(tx)
        for approval_id in never_shown:
            try:
                with self._store.transaction() as tx:
                    held = rows.load(tx, approval_id)
            except ApprovalRefused:
                continue
            if held is not None and held["state"] == "pending":
                self._offer(held, only=only)
                offered.append(approval_id)
        return offered

    def recover(self) -> dict:
        """At start: end engine requests whose driver died, retire the bindings of approved ones not yet
        carried out, write due expiries, offer again the requests never shown (during a start that offering
        reaches no surface: the start makes it once its worker runs)."""
        closed, retired = [], []
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
        offered = self._offer_unshown()
        return {"closed": closed, "retired": retired, "expired": [r["approval_id"] for r in expired],
                "told": offered, "at": now}

    # -- the methods ---------------------------------------------------------------------------------------

    def open_in(self, tx, principal: dict, params: dict) -> dict:
        """approval.open inside the dispatcher's transaction. Whoever calls it, what it would write is held to
        the record's own shape first: a principal that is not an approver exactly as a record keeps one is
        refused (``scope_refused``), and a surface that is not one of ``SURFACES`` is ``request_invalid``."""
        refusal = model.principal_refusal(principal)
        if refusal is not None:
            return refusal
        if type(params) is not dict or params.get("surface") not in SURFACES:
            return {"code": "request_invalid", "detail": "surface"}
        held = rows.load(tx, params["approval_id"])
        if held is None:
            return {"code": "approval_unknown", "approval_id": params["approval_id"]}
        changed, answer = model.open_prompt(held, principal, params, self.now())
        if changed["opened_by"] != held["opened_by"]:
            rows.save(tx, changed)
        return answer

    def answer_in(self, tx, principal: dict, params: dict, after_commit: Callable) -> dict:
        """approval.answer inside the dispatcher's transaction; waiters and drivers are told after commit. A
        principal that is not an approver exactly as a record keeps one is refused here too (``scope_refused``),
        whoever calls it."""
        refusal = model.principal_refusal(principal)
        if refusal is not None:
            return refusal
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
