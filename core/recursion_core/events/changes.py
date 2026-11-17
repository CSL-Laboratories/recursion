"""The change reader: each run's state, as the daemon registry holds it, logged as ``state.changed`` events.

Lane EVENTS, slice 2. It consumes SPINE's change marker (S12, ``control.hooks.ChangeMarker``) and runs as a
service (S13). The marker's text is the protocol; this module follows it:

- An image is ``Registry.snapshot(run_id)``: the daemon's row and the stored control text of ONE run, read
  from the registry's own file under its own lock. Nothing else is read: never the control registry's
  in-memory copy, never ``all``, ``control_all`` or ``list`` in a loop, never the file itself, never a
  connection of this lane's own, and never while a transaction of the event store is open (this module
  calls ``events.db.require_no_transaction`` before every registry read: each page and each snapshot).
- The post-image is typed, ``{daemon_row, control_record}`` (either may be null); its digest is SHA-256 of
  its RFC 8785 text (``control.methods.canonical_text``), taken after the registry's lock is released.
- Dedupe is against the LAST image logged for that run, kept in this lane's own table: a run seen as A,
  then B, then A gives three events. The event and the run's new last digest commit in ONE transaction;
  there is no other checkpoint and nothing is acknowledged to the registry.
- One consumer owns the order from snapshot to commit: ``start`` claims the store for its reader until its
  ``stop``, and another reader's ``start`` or ``bind`` on that store is refused. An image whose transaction
  fails is HELD, and its transaction is retried before any later image of that run is read. At most one image
  is held: no other work is done after a failed transaction until the held image commits. There is no queue
  of old texts.
- Waiting. After a refused page or a failed transaction the reader does nothing but take marks until a
  wait has passed: the marker's poll interval at first, doubling at each further refusal of the same thing
  (the registry's pages, or the store's transactions) up to ``scan_interval_s``, and none again once that
  thing has succeeded: a page read does not end the backoff of a store that keeps failing. The poll
  interval is read through ``_poll_s``: SPINE's marker keeps it as a plain attribute, so the reader takes it
  as a number from ``MIN_POLL_S`` to ``scan_interval_s``. A run the registry refuses to read waits on a
  backoff of its own, stays pending, and is listed in ``unlogged`` as ``registry_refused`` until it is read;
  it holds up neither the scan (the cursor moves past it) nor the other runs, and no scan reads it before its
  time, nor under any reason but the one it was first read for. ``failing`` is true while anything is held or
  waited for.
- A taken batch stays pending until its runs are read and logged, or listed in ``unlogged``. A value the
  marker hands back that is not a run id (a non-empty string) is dropped, logged at ERROR, and asks for a
  scan, as a dropped mark does. Scans page with
  ``Registry.run_ids(after, page)``, one page per step, interleaved with one slice of at most
  ``slice_size`` marked runs, so new marks never put off a scan that is due. A scan begins at the first step
  after a start, and again no later than ``scan_interval_s`` after a pass completes; a dropped mark asks for
  a scan and does not send a scan in progress back to its first page. ``ready`` is true once the first pass
  of this start has completed. Every run that pass read is then logged as the pass read it, or listed in
  ``unlogged``; a change made after the pass read a run is logged by its mark or by the next scan.
- ``reason``: ``commit`` for a run read because it was marked; ``snapshot`` for a run's first record in
  this log made by the first pass ever made on this store (logging began after the run existed);
  ``reconcile`` for a run found by any other scan, and for a run this log already holds when a first pass
  was cut short by a crash. ``coalesced`` is null, always: the marker keeps which runs changed, not how
  many times.
- A run logs under the session its current control record names; until it names one, under
  ``run:<run_id>``. The run's log is kept beside its last digest and changed in the same transaction as
  the event. The first time a run is logged with a ``launch.route_decision`` that is an object (event.v1's
  payload for ``route.decision`` is one), one ``route.decision`` event is appended from it, in the same
  transaction; a value of any other type is not logged.
- A run whose stored record has no typed post-image (a control text that is not a JSON object, nested
  deeper than the JSON reader can follow included, or that has no RFC 8785 text: an integer past 2**53, a lone
  surrogate, nesting past 64), or whose event the store
  refuses, is NOT logged: event.v1 has no typed form for it (the lane's ``questions.md``, Q12). It is listed,
  with its code, in this lane's table ``events_unlogged`` (durable: a restart reads it back at its first
  scan) and in ``unlogged``, logged at ERROR, and read again at every scan. So is a run the registry refuses
  to read (``registry_refused``, logged at WARNING), until it is read. A run leaves both in the transaction
  that reads it with a typed post-image: the one that logs it, or that finds the log already holds that
  image, or that finds the registry holds nothing for a run this log never saw (nothing is missing then). A
  code is ``<kind>`` or ``<kind>:<detail>``: the kind is one of ``BadImage``'s codes, ``no_canonical_text``,
  ``store_refused`` or ``registry_refused``, and the detail is the refusal's own code when it has one
  (``registry_refused`` has none: the registry's refusal is in the WARNING line).

What this gives, and what it does not. Replay of the log reaches the registry's durable state for every
run not listed in ``unlogged``: the last image logged for a run equals what the registry holds once the
reader has caught up, with one reader per store. ``start`` holds to that: another reader's ``start`` or ``bind`` on
a started reader's store is refused, and a started reader's own ``bind`` cannot move it to another registry or store.
A start remembers the store before it claims it, and claims and binds in one step with ``bind``'s check (the
reader's own lock), so no ``bind`` lands between them and an interrupted start leaves no claim ``stop`` cannot find.
``bind`` alone takes no claim (it is the tests' stand-in for a restart), so two
readers that are only bound to one store can leave a run's last image behind the registry's until its next read.
Changes between two reads appear as the last image: the log does NOT claim every
intermediate transition (a transition-exact log would need a write inside Stop's durable path, which is
lane STOP's to propose). Its source reads take the registry's lock, so a paused reader CAN delay a Stop;
SPINE's S12 text states that cost, and nothing here adds a registry write.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import threading
import time
import weakref

from recursion_core.control.methods import canonical_text
from recursion_core.events import db as events_db

LOG = logging.getLogger(__name__)

LANE = 'events'
# Raised with every change of LANE_DDL. The helper refuses a store declared with another version or text
# (``declaration_changed``; no migration exists yet), and ``open_store`` says which version it found.
LANE_VERSION = 2
LANE_DDL = ('CREATE TABLE events_run_images (run_id TEXT PRIMARY KEY, '
            "log_id TEXT NOT NULL CHECK (log_id LIKE 'ses-%' OR log_id LIKE 'run:%'), "
            'digest TEXT NOT NULL CHECK (length(digest) = 64), '
            'route_logged INTEGER NOT NULL CHECK (route_logged IN (0, 1))); '
            'CREATE TABLE events_scan_state (key TEXT PRIMARY KEY, value INTEGER NOT NULL); '
            'CREATE TABLE events_unlogged (run_id TEXT PRIMARY KEY, code TEXT NOT NULL)')
PAGE = 64
SLICE = 64
MIN_POLL_S = 0.01
STORE_FILE = 'events.sqlite3'

_SESSION_ID = re.compile(r'ses-[A-Za-z0-9_-]{1,60}')
_TURN_ID = re.compile(r'turn-[A-Za-z0-9_-]{1,59}')
_DETAIL = re.compile(r'[A-Za-z0-9_.:-]{1,80}')
_RUN_FIELDS = ('run_id', 'unit', 'stop_requested', 'state', 'boot_id', 'detail')
_REGISTRY_REFUSED = 'registry_refused'

# The store each started reader holds, from its ``start`` to its ``stop``: one reader per store.
_CLAIMS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
_CLAIMS_LOCK = threading.Lock()

# What logging one run came to.
_DONE = 'done'            # logged, unchanged, or listed in ``unlogged``: the run leaves the pending set
_DEFERRED = 'deferred'    # the registry refused to read it, or its own wait has not passed: listed, it stays pending
_HELD = 'held'            # its transaction failed: the image is held and nothing more is done this step
_FAILED = 'failed'        # the transaction listing it in unlogged failed: it stays pending; nothing more this step


class BadImage(ValueError):
    """What the registry holds for a run has no typed post-image. The message is a code."""


def _strict_object(text: str) -> dict:
    def refuse(name):
        raise BadImage(f'control_text_not_json:{name}')
    try:
        value = json.loads(text, parse_constant=refuse)
    except BadImage:
        raise
    except RecursionError as exc:         # nested deeper than the JSON reader itself can follow
        raise BadImage('control_text_not_json:depth') from exc
    except ValueError as exc:
        raise BadImage('control_text_not_json') from exc
    if not isinstance(value, dict):
        raise BadImage('control_text_not_an_object')
    return value


def post_image(row, control_text) -> dict:
    """The typed post-image of a run, from what ``Registry.snapshot`` returned: a daemon ``Run`` or None, and
    the control text (a ``str``) or None. Anything else is ``BadImage``."""
    if control_text is not None and not isinstance(control_text, str):
        raise BadImage('control_text_not_text')
    daemon_row = None
    if row is not None:
        if not all(hasattr(row, name) for name in _RUN_FIELDS):
            raise BadImage('daemon_row_not_a_run')
        daemon_row = {'run_id': row.run_id, 'unit': row.unit, 'stop': bool(row.stop_requested), 'state': row.state,
                      'boot_id': row.boot_id, 'detail': row.detail}
    return {'daemon_row': daemon_row, 'control_record': None if control_text is None else _strict_object(control_text)}


def _checked_image(image) -> dict:
    """``image`` when it has a run's post-image shape: exactly ``daemon_row`` and ``control_record``, each an
    object or null. Anything else is ``BadImage('not_a_post_image')``."""
    if (not isinstance(image, dict) or set(image) != {'daemon_row', 'control_record'}
            or not all(half is None or isinstance(half, dict) for half in image.values())):
        raise BadImage('not_a_post_image')
    return image


def image_digest(image: dict) -> str:
    """SHA-256 of the post-image's RFC 8785 text (digest form rfc8785/v1, as event.v1 names it). A value that
    is not a run's post-image is ``BadImage``; one with no RFC 8785 text is ``NoCanonicalText`` (both are
    ``ValueError``)."""
    return hashlib.sha256(canonical_text(_checked_image(image)).encode('utf-8')).hexdigest()


def _launch(image: dict) -> dict:
    record = image.get('control_record')
    launch = record.get('launch') if isinstance(record, dict) else None
    return launch if isinstance(launch, dict) else {}


def log_id_of(run_id: str, image: dict) -> str:
    """The session the run's record names, when it names one that is a log id; otherwise ``run:<run_id>``.
    A run id that is not a non-empty string is a ``TypeError``; an image that is not a run's post-image is
    ``BadImage('not_a_post_image')``."""
    if not isinstance(run_id, str) or not run_id:
        raise TypeError('run_id')
    session = _launch(_checked_image(image)).get('session_id')
    return session if isinstance(session, str) and _SESSION_ID.fullmatch(session) else f'run:{run_id}'


def _code(exc: ValueError, kind: str) -> str:
    """A ``BadImage``'s own code; otherwise ``<kind>:<the refusal's code>``, or ``<kind>`` alone when the
    refusal's text is not a code (a sentence, say), which is then only in the ERROR line."""
    if isinstance(exc, BadImage):
        return str(exc)
    detail = str(exc)
    return f'{kind}:{detail}' if _DETAIL.fullmatch(detail) else kind


def _check_options(*, scan_interval_s: float = 60.0, page: int = PAGE, slice_size: int = SLICE,
                   monotonic=time.monotonic) -> tuple:
    """The reader's options, checked: ``(scan_interval_s, page, slice_size, monotonic)``; a refused one is a
    ``ValueError`` naming it (an option this does not know is the language's own ``TypeError``). ``register``
    calls it before it sets any hook, so a refused option leaves the hooks as they were."""
    for field, value in (('page', page), ('slice_size', slice_size)):
        if type(value) is not int or value < 1:
            raise ValueError(field)
    if (isinstance(scan_interval_s, bool) or not isinstance(scan_interval_s, (int, float))
            or not math.isfinite(scan_interval_s) or scan_interval_s <= 0):
        raise ValueError('scan_interval_s')
    if not callable(monotonic):
        raise ValueError('monotonic')
    return float(scan_interval_s), page, slice_size, monotonic


def _claim(store, reader, *, take: bool) -> None:
    """Refuses ``store`` when another reader's ``start`` holds it; with ``take``, ``reader`` then holds it."""
    if store is None:
        return
    with _CLAIMS_LOCK:
        holder = _CLAIMS.get(store)
        if holder is not None and holder is not reader:
            raise RuntimeError('the event store has another change reader')
        if take:
            _CLAIMS[store] = reader


def _release(store, reader) -> None:
    if store is None:
        return
    with _CLAIMS_LOCK:
        if _CLAIMS.get(store) is reader:
            del _CLAIMS[store]


class ChangeReader:
    """The S13 service that logs runs' state changes. One thread; ``step()`` is one turn of its loop."""

    name = LANE

    def __init__(self, marker, **options) -> None:
        """``options``: ``scan_interval_s`` (60.0), ``page`` and ``slice_size`` (64 each), ``monotonic``."""
        if not callable(getattr(marker, 'take', None)):
            raise TypeError('marker')
        self._interval, self._page, self._slice, self._monotonic = _check_options(**options)
        self._marker = marker
        self._pending: set = set()
        self._held = None                 # (run_id, image, digest, reason): read, its transaction not yet done
        self._deferred: dict = {}         # run_id -> (refusals, the time before which it is not read again, reason)
        self._unlogged: dict = {}         # run_id -> code, as events_unlogged holds it
        self._registry = self._store = None
        self._stopping = threading.Event()   # the current start's stop signal; each start makes its own
        self._thread = None                   # the current start's thread, from before it is started to its join
        self._claimed = None                  # the store the current start claimed, until its stop: the started mark
        self._binding = threading.Lock()      # bind's check and change, and start's claim and binding: one step each
        self._reset()

    def _reset(self) -> None:
        """What every start begins with."""
        self._scan_wanted = True          # a start scans
        self._scan_cursor = None
        self._scanning = False
        self._next_scan_at = float('inf')   # the clock schedules a scan only once a pass has completed
        self._first_pass_ever = None      # read from the store at the first scan of this start
        self._ready = False
        self._backoff = 0.0
        self._waiting_on = None           # what refused last, 'registry' or 'store', until it succeeds
        self._wait_until = None           # after a refused page or a failed transaction: no work before this

    # -- the service

    @property
    def ready(self) -> bool:
        """True once the first pass of this start has completed: every run it read is then logged as it read it,
        or listed in ``unlogged``."""
        return self._ready

    @property
    def failing(self) -> bool:
        """True while an image is held, a refused page or failed transaction is waited out, or a run the
        registry refused to read is waiting for its next read."""
        return self._held is not None or self._wait_until is not None or bool(self._deferred)

    @property
    def unlogged(self) -> dict:
        """``{run_id: code}``: the runs this reader cannot log as the registry holds them, or cannot read
        (``registry_refused``), as a copy. The lane's table ``events_unlogged`` holds the same; a start reads it
        back at its first scan."""
        return dict(self._unlogged)

    def bind(self, registry, store) -> None:
        """The two things the reader reads and writes. ``start`` binds them from the service context. A store
        another reader's ``start`` holds (until its ``stop``) is refused. ``bind`` alone takes no claim: the tests
        bind a second reader on one store to stand for a restart. A started reader (from its ``start`` until its
        ``stop``, a start that raised after it took the claim included) cannot be bound to another registry or
        store: that is refused, and binding the same two changes nothing. The check and the change are one step with
        ``start``'s claim and binding (the reader's own lock), so a ``bind`` on another thread cannot land between
        them. So its thread reads and writes only what it was started with, on the store its claim holds."""
        with self._binding:
            if self._claimed is not None and (registry is not self._registry or store is not self._store):
                raise RuntimeError('the change reader is started: bind cannot change its registry or store until stop')
            self._bind(registry, store)

    def _bind(self, registry, store) -> None:
        _claim(store, self, take=False)
        self._registry, self._store = registry, store

    def start(self, context) -> None:
        """Claims the context's store for this reader (another reader's start or bind on it is refused until
        this reader's ``stop``), binds, and starts the thread. The store is remembered before it is claimed, and
        the claim and the binding are one step with ``bind``'s check. A start that raises before it took the claim,
        or while it binds, gives the claim back at once. One that raises after that (its thread is owned, or about
        to be) holds the claim until ``stop``."""
        if self._thread is not None:
            raise RuntimeError('the change reader is already started')
        if context.store is None:
            raise RuntimeError('the change reader needs the event store')
        with self._binding:
            if self._claimed is not None:     # a start that raised after its claim, with no stop since
                raise RuntimeError('the change reader is already started')
            self._claimed = context.store     # remembered first: an interruption after the claim leaves it findable
            try:
                _claim(context.store, self, take=True)
                self._bind(context.registry, context.store)
            except BaseException:
                _release(context.store, self)  # gives up only a claim this reader holds
                self._claimed = None
                raise
        self._reset()
        stopping = self._stopping = threading.Event()
        thread = threading.Thread(target=self._run, args=(stopping,), name='events-change-reader', daemon=True)
        # Owned before it is started: a start that raises may have launched the thread all the same (an
        # interruption while ``Thread.start`` waits for it), and then only ``stop`` ends and joins it.
        self._thread = thread
        thread.start()

    def stop(self, context) -> None:
        """Ends the current start's thread, joins it, and gives up the claim that start took, on the store it was
        started with (while it is started, ``bind`` cannot move it to another; an interrupted start's claim
        included). A thread whose start raised before it began is not joined: when it runs, it reads its own stop
        signal, set here, before anything else, and does nothing."""
        self._stopping.set()
        thread = self._thread
        if thread is not None:
            if thread.is_alive():
                thread.join()
            self._thread = None
        with self._binding:
            _release(self._claimed, self)     # given up before the mark is cleared: never a claim with no mark
            self._claimed = None

    def _run(self, stopping=None) -> None:
        stopping = self._stopping if stopping is None else stopping
        while not stopping.is_set():
            try:
                busy = self.step()
            except Exception:                 # noqa: BLE001 - the loop goes on; what failed stays pending
                LOG.exception('the change reader failed a step')
                busy = False
            if not busy:
                stopping.wait(self._poll_s())

    # -- one turn of the loop

    def step(self) -> bool:
        """Take the marks; then, unless a wait is running: the held image's transaction, one page of a scan
        when one is due or running, and one slice of marked runs. True only when a scan is in progress and
        this step advanced it (the loop does not sleep then); False after any refusal, so the loop waits."""
        if self._registry is None or self._store is None:
            raise RuntimeError('the change reader is not bound')
        ids, dropped = self._marker.take()
        ids = list(ids)
        runs = [run_id for run_id in ids if isinstance(run_id, str) and run_id]
        if len(runs) != len(ids):              # not run ids: dropped, and a scan asked for, as for a dropped mark
            LOG.error('the change marker handed back %d values that are not run ids; they are dropped and a scan '
                      'is asked for', len(ids) - len(runs))
            dropped = True
        self._pending.update(runs)
        if dropped:
            self._scan_wanted = True
        now = self._monotonic()
        if self._wait_until is not None and now < self._wait_until:
            return False
        if self._held is not None and not self._commit_held():
            return False
        if not self._scanning and (self._scan_wanted or now >= self._next_scan_at):
            self._scanning, self._scan_cursor, self._scan_wanted = True, None, False
        advanced = False
        if self._scanning:
            advanced = self._scan_page()
            if self._held is not None or self._wait_until is not None:
                return False
        due = [run_id for run_id in sorted(self._pending) if self._deferred.get(run_id, (0, now, None))[1] <= now]
        for run_id in due[:self._slice]:
            outcome = self._log_run(run_id, 'commit')
            if outcome is _HELD or outcome is _FAILED:
                return False
            if outcome is _DONE:
                self._pending.discard(run_id)
        return self._scanning and advanced

    def _scan_page(self) -> bool:
        """One page of the scan. True when the cursor advanced or the pass completed."""
        if self._first_pass_ever is None:
            try:
                done, unlogged = self._scan_state()
            except Exception:                 # noqa: BLE001 - the store failed: wait, as after any failed transaction
                LOG.error('the change reader could not read its scan state; it waits and tries again', exc_info=True)
                self._wait('store')
                return False
            self._first_pass_ever, self._unlogged = not done, unlogged
        events_db.require_no_transaction('daemon.registry')
        try:
            ids = self._registry.run_ids(self._scan_cursor, self._page)
        except Exception:                     # noqa: BLE001 - the registry refuses reads for now; the same page later
            LOG.warning('the change reader could not page the registry; it waits and tries again', exc_info=True)
            self._wait('registry')
            return False
        self._waited('registry')
        if not ids:
            try:
                self._finish_pass()
            except Exception:                 # noqa: BLE001 - the store failed: the pass completes at a later step
                LOG.error('the change reader could not record its completed pass; it waits and tries again',
                          exc_info=True)
                self._wait('store')
                return False
            return True
        reason = 'snapshot' if self._first_pass_ever else 'reconcile'
        for run_id in ids:
            outcome = self._log_run(run_id, reason)
            if outcome is _HELD or outcome is _FAILED:
                return False                   # the same run again, from the same cursor, once the wait has passed
            self._scan_cursor = run_id        # a run the registry refused is pending and deferred; the scan goes on
        return True

    def _scan_state(self) -> tuple:
        """``(first pass done, {run_id: code} of events_unlogged)``, read in one transaction."""
        with self._store.transaction() as tx:
            done = bool(tx.execute("SELECT 1 FROM events_scan_state WHERE key = 'first_pass_done'"))
            unlogged = dict(tx.execute('SELECT run_id, code FROM events_unlogged'))
        return done, unlogged

    def _finish_pass(self) -> None:
        if self._first_pass_ever:
            with self._store.transaction() as tx:
                tx.execute("INSERT OR REPLACE INTO events_scan_state (key, value) VALUES ('first_pass_done', 1)")
            self._waited('store')
            self._first_pass_ever = False
        self._scanning, self._scan_cursor = False, None
        self._next_scan_at = self._monotonic() + self._interval
        self._ready = True

    # -- waiting

    def _poll_s(self) -> float:
        """The marker's poll interval as the reader waits on it: from ``MIN_POLL_S`` to the scan interval. A value
        that is not a number (or is a bool, NaN or missing) is read as ``MIN_POLL_S``, and so is one of 0 or less;
        an int too large for a float is read as infinity is, by its sign."""
        value = getattr(self._marker, 'poll_interval_s', None)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return MIN_POLL_S
        try:
            number = float(value)
        except OverflowError:                 # an int past a float's range
            number = math.inf if value > 0 else -math.inf
        if math.isnan(number):
            return MIN_POLL_S
        return min(max(number, MIN_POLL_S), self._interval)

    def _wait(self, what: str) -> None:
        """``what`` ('registry' or 'store') refused: no work until the wait has passed. The wait is the poll
        interval, or twice the last one when the last refusal was of the same thing and it has not succeeded since."""
        again = self._waiting_on == what
        self._backoff = min(self._interval, self._backoff * 2 if again else self._poll_s())
        self._waiting_on, self._wait_until = what, self._monotonic() + self._backoff

    def _waited(self, what: str) -> None:
        """``what`` succeeded: the wait is over, and its backoff too when ``what`` is what refused."""
        self._wait_until = None
        if self._waiting_on == what:
            self._backoff, self._waiting_on = 0.0, None

    def _defer(self, run_id: str, reason: str) -> None:
        refusals = min(self._deferred.get(run_id, (0, 0.0, reason))[0] + 1, 32)
        delay = min(self._interval, self._poll_s() * 2 ** (refusals - 1))
        self._deferred[run_id] = (refusals, self._monotonic() + delay, reason)
        self._pending.add(run_id)

    # -- one run

    def _log_run(self, run_id: str, reason: str) -> str:
        """Read one image and log it if it differs from the last one logged. A run the registry refused to read
        is listed as ``registry_refused``, read again only once its own wait has passed, and then for the reason
        it was first read for."""
        deferred = self._deferred.get(run_id)
        if deferred is not None:
            if deferred[1] > self._monotonic():
                return self._refused(run_id)
            reason = deferred[2]
        events_db.require_no_transaction('daemon.registry')
        try:
            row, text = self._registry.snapshot(run_id)
        except Exception:                     # noqa: BLE001 - e.g. a publish whose directory sync is unfinished
            LOG.warning('the change reader could not read run %s; it tries again later', run_id, exc_info=True)
            self._defer(run_id, reason)
            return self._refused(run_id)
        self._deferred.pop(run_id, None)
        # The registry's lock is released: parse, canonicalise and hash now.
        try:
            image = post_image(row, text)
            digest = image_digest(image)
        except ValueError as exc:
            return self._unlog(run_id, _code(exc, 'no_canonical_text'))
        return self._commit_or_hold(run_id, image, digest, reason)

    def _commit_held(self) -> bool:
        run_id, image, digest, reason = self._held
        self._held = None
        return self._commit_or_hold(run_id, image, digest, reason) is _DONE

    def _commit_or_hold(self, run_id: str, image: dict, digest: str, reason: str) -> str:
        try:
            self._commit(run_id, image, digest, reason)
        except ValueError as exc:             # the store refuses this event itself: no retry can log it as it is
            return self._unlog(run_id, _code(exc, 'store_refused'))
        except Exception:                     # noqa: BLE001 - the store failed: the image is held and retried first
            LOG.error('the change reader could not log run %s; it holds the image and tries again', run_id,
                      exc_info=True)
            self._held = (run_id, image, digest, reason)
            self._wait('store')
            return _HELD
        self._waited('store')
        self._unlogged.pop(run_id, None)
        return _DONE

    def _refused(self, run_id: str) -> str:
        """A deferred run, listed as ``registry_refused``: ``_DEFERRED``, or ``_FAILED`` when listing it failed."""
        return _FAILED if self._list(run_id, _REGISTRY_REFUSED) is _FAILED else _DEFERRED

    def _unlog(self, run_id: str, code: str) -> str:
        """A run that cannot be logged: logged at ERROR, and listed."""
        LOG.error('run %s cannot be logged as the registry holds it (%s); every scan reads it again', run_id, code)
        return self._list(run_id, code)

    def _list(self, run_id: str, code: str) -> str:
        """List a run in ``events_unlogged`` and in ``unlogged``. The row is written only when the run's code is
        new; a transaction that fails leaves the run pending, and the reader waits."""
        if self._unlogged.get(run_id) == code:
            return _DONE
        try:
            with self._store.transaction() as tx:
                tx.execute('INSERT INTO events_unlogged (run_id, code) VALUES (?, ?) '
                           'ON CONFLICT (run_id) DO UPDATE SET code = excluded.code', (run_id, code))
        except Exception:                     # noqa: BLE001 - the store failed: the run is listed at a later step
            LOG.error('the change reader could not list run %s as unlogged; it waits and tries again', run_id,
                      exc_info=True)
            self._wait('store')
            return _FAILED
        self._waited('store')
        self._unlogged[run_id] = code
        return _DONE

    def _commit(self, run_id: str, image: dict, digest: str, reason: str) -> None:
        both_null = image['daemon_row'] is None and image['control_record'] is None
        with self._store.transaction() as tx:
            # Read with a typed image: it leaves the list, whether that image is new to the log or not.
            tx.execute('DELETE FROM events_unlogged WHERE run_id = ?', (run_id,))
            last = tx.execute('SELECT log_id, digest, route_logged FROM events_run_images WHERE run_id = ?', (run_id,))
            if last and last[0][1] == digest:
                return
            if not last and both_null:
                return                         # a run the registry does not hold and this log never saw
            if last and reason == 'snapshot':
                reason = 'reconcile'           # a first pass cut short by a crash: this log already holds the run
            log_id = log_id_of(run_id, image)
            if last and log_id == f'run:{run_id}':
                log_id = last[0][0]            # a record that names no session keeps the run's log
            route_logged = bool(last and last[0][2])
            tx.append({'session_id': log_id, 'run_id': run_id, 'turn_id': None, 'type': 'state.changed',
                       'source': 'core', 'trust': 'trusted',
                       'payload': {'entity': 'run', 'entity_id': run_id, 'post_image': image,
                                   'post_image_sha256': digest, 'reason': reason, 'coalesced': None}})
            decision = _launch(image).get('route_decision')
            if not route_logged and isinstance(decision, dict):
                turn = _launch(image).get('turn_id')
                tx.append({'session_id': log_id, 'run_id': run_id,
                           'turn_id': turn if isinstance(turn, str) and _TURN_ID.fullmatch(turn) else None,
                           'type': 'route.decision', 'source': 'core', 'trust': 'trusted', 'payload': decision})
                route_logged = True
            tx.execute('INSERT INTO events_run_images (run_id, log_id, digest, route_logged) VALUES (?, ?, ?, ?) '
                       'ON CONFLICT (run_id) DO UPDATE SET log_id = excluded.log_id, digest = excluded.digest, '
                       'route_logged = excluded.route_logged',
                       (run_id, log_id, digest, int(route_logged)))


def _declared_version(store: events_db.EventStore):
    """The version of this lane's declaration that the store holds, or None when it cannot be read."""
    try:
        rows = store._query('SELECT version FROM lane_tables WHERE lane = ?', (LANE,))
    except Exception:                         # noqa: BLE001 - only the refusal's text depends on it
        return None
    return rows[0][0] if rows else None


def open_store(state_dir) -> events_db.EventStore:
    """The ``event_store`` factory: the store in the daemon's state directory, with this lane's tables. A state
    directory that is not a folder's path is ``StoreError('state_dir_not_a_folder')``; a store whose tables of
    this lane were declared by another version or text says which version it holds."""
    try:
        folder = os.fspath(state_dir)
    except TypeError:
        raise events_db.StoreError('state_dir_not_a_folder') from None
    if not isinstance(folder, str) or not os.path.isdir(folder):
        raise events_db.StoreError('state_dir_not_a_folder')
    store = events_db.EventStore(os.path.join(folder, STORE_FILE))
    try:
        store.register_tables(LANE, LANE_VERSION, LANE_DDL)
    except events_db.StoreError as exc:
        found = _declared_version(store) if str(exc) == 'declaration_changed' else None
        store.close()
        if str(exc) == 'declaration_changed':
            raise events_db.StoreError(
                f'declaration_changed: this store holds lane {LANE} tables of version {found}; this code declares '
                f'version {LANE_VERSION} with another text, and no migration exists yet') from exc
        raise
    except BaseException:
        store.close()
        raise
    return store


def register(hooks, **reader_options) -> ChangeReader:
    """Lane EVENTS' registration: the event store's factory, the change marker's consumer, the service. The
    options are checked before any hook is set. Not yet called by any composition: ``events/hooks.py`` (the
    registrant SPINE imports) is not added until the Liaison says registration may go live."""
    if not callable(getattr(hooks, 'set', None)):
        raise TypeError('hooks')
    _check_options(**reader_options)
    hooks.set('event_store', open_store)
    reader = ChangeReader(hooks.enable_changes(), **reader_options)
    hooks.service(reader)
    return reader
