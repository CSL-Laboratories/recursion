"""The change reader: each run's state, as the daemon registry holds it, logged as ``state.changed`` events.

Lane EVENTS, slice 2. It consumes SPINE's change marker (S12, ``control.hooks.ChangeMarker``) and runs as a
service (S13). The marker's text is the protocol; this module follows it:

- An image is ``Registry.snapshot(run_id)``: the daemon's row and the stored control text of ONE run, read
  from the registry's own file under its own lock. Nothing else is read: never the control registry's
  in-memory copy, never ``all``, ``control_all`` or ``list`` in a loop, never the file itself, never a
  connection of this lane's own, and never while a transaction of the event store is open (this module
  calls ``events.db.require_no_transaction`` before every registry read).
- The post-image is typed, ``{daemon_row, control_record}`` (either may be null); its digest is SHA-256 of
  its RFC 8785 text (``control.methods.canonical_text``), taken after the registry's lock is released.
- Dedupe is against the LAST image logged for that run, kept in this lane's own table: a run seen as A,
  then B, then A gives three events. The event and the run's new last digest commit in ONE transaction;
  there is no other checkpoint and nothing is acknowledged to the registry.
- A taken batch stays pending until its commits succeed; a failure leaves it pending for the next step.
- Scans page with ``Registry.run_ids(after, PAGE)``, one page per step, interleaved with one slice of
  marked runs, so new marks never put off a scan that is due. A scan begins at the first step after a
  start, and again no later than ``scan_interval_s`` after a pass completes; a dropped mark asks for a
  scan and does not send a scan in progress back to its first page. ``ready`` is true once the first pass
  of this start has completed.
- ``reason``: ``commit`` for a run read because it was marked; ``snapshot`` for a run found by the first
  pass ever made on this store (logging began after the run existed); ``reconcile`` for a run found by any
  later scan. ``coalesced`` is null, always: the marker keeps which runs changed, not how many times.
- A run with no session logs under ``run:<run_id>``; one whose control record names a session logs under
  that session. The first time a run is logged with a ``launch.route_decision``, one ``route.decision``
  event is appended from it, in the same transaction.

What this gives, and what it does not. Replay of the log reaches the registry's durable state for every
run: the last image logged for a run equals what the registry holds once the reader has caught up.
Changes between two reads appear as the last image: the log does NOT claim every intermediate transition
(a transition-exact log would need a write inside Stop's durable path, which is lane STOP's to propose).
Its source reads take the registry's lock, so a paused reader CAN delay a Stop; SPINE's S12 text states
that cost, and nothing here adds a registry write.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time

from recursion_core.control.methods import canonical_text
from recursion_core.events import db as events_db

LOG = logging.getLogger(__name__)

LANE = 'events'
LANE_VERSION = 1
LANE_DDL = ('CREATE TABLE events_run_images (run_id TEXT PRIMARY KEY, log_id TEXT NOT NULL, '
            'digest TEXT NOT NULL CHECK (length(digest) = 64), route_logged INTEGER NOT NULL); '
            'CREATE TABLE events_scan_state (key TEXT PRIMARY KEY, value INTEGER NOT NULL)')
PAGE = 64
SLICE = 64
STORE_FILE = 'events.sqlite3'

_SESSION_ID = re.compile(r'ses-[A-Za-z0-9_-]{1,60}')
_TURN_ID = re.compile(r'turn-[A-Za-z0-9_-]{1,59}')


class BadImage(ValueError):
    """What the registry holds for a run has no typed post-image (a control text that is not a JSON object)."""


def _strict_object(text: str) -> dict:
    def refuse(name):
        raise BadImage(f'control_text_not_json:{name}')
    try:
        value = json.loads(text, parse_constant=refuse)
    except ValueError as exc:
        raise BadImage('control_text_not_json') from exc
    if not isinstance(value, dict):
        raise BadImage('control_text_not_an_object')
    return value


def post_image(row, control_text) -> dict:
    """The typed post-image of a run, from what ``Registry.snapshot`` returned."""
    daemon_row = None
    if row is not None:
        daemon_row = {'run_id': row.run_id, 'unit': row.unit, 'stop': bool(row.stop_requested), 'state': row.state,
                      'boot_id': row.boot_id, 'detail': row.detail}
    return {'daemon_row': daemon_row, 'control_record': None if control_text is None else _strict_object(control_text)}


def image_digest(image: dict) -> str:
    """SHA-256 of the post-image's RFC 8785 text (digest form rfc8785/v1, as event.v1 names it)."""
    return hashlib.sha256(canonical_text(image).encode('utf-8')).hexdigest()


def _launch(image: dict) -> dict:
    record = image.get('control_record')
    launch = record.get('launch') if isinstance(record, dict) else None
    return launch if isinstance(launch, dict) else {}


def log_id_of(run_id: str, image: dict) -> str:
    """The run's session, when its record names one; otherwise ``run:<run_id>``."""
    session = _launch(image).get('session_id')
    return session if isinstance(session, str) and _SESSION_ID.fullmatch(session) else f'run:{run_id}'


class ChangeReader:
    """The S13 service that logs runs' state changes. One thread; ``step()`` is one turn of its loop."""

    name = LANE

    def __init__(self, marker, *, scan_interval_s: float = 60.0, page: int = PAGE, slice_size: int = SLICE,
                 monotonic=time.monotonic) -> None:
        self._marker = marker
        self._interval = scan_interval_s
        self._page, self._slice = page, slice_size
        self._monotonic = monotonic
        self._pending: set = set()
        self._scan_wanted = True          # a start scans
        self._scan_cursor = None
        self._scanning = False
        self._next_scan_at = 0.0
        self._first_pass_ever = None      # read from the store at the first scan of this start
        self._ready = False
        self._registry = self._store = None
        self._stopping = threading.Event()
        self._thread = None

    # -- the service

    @property
    def ready(self) -> bool:
        return self._ready

    def bind(self, registry, store) -> None:
        """The two things the reader reads and writes. ``start`` binds them from the service context."""
        self._registry, self._store = registry, store

    def start(self, context) -> None:
        self.bind(context.registry, context.store)
        if self._store is None:
            raise RuntimeError('the change reader needs the event store')
        self._stopping.clear()
        self._thread = threading.Thread(target=self._run, name='events-change-reader', daemon=True)
        self._thread.start()

    def stop(self, context) -> None:
        self._stopping.set()
        thread = self._thread
        if thread is not None:
            thread.join()
            self._thread = None

    def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                busy = self.step()
            except Exception:                 # noqa: BLE001 - the loop goes on; what failed stays pending
                LOG.exception('the change reader failed a step')
                busy = False
            if not busy:
                self._stopping.wait(self._marker.poll_interval_s)

    # -- one turn of the loop

    def step(self) -> bool:
        """Take the marks, do one page of a scan when one is due or running, and one slice of marked runs.
        True when a scan is still in progress (the loop does not sleep then)."""
        ids, dropped = self._marker.take()
        self._pending |= set(ids)
        if dropped:
            self._scan_wanted = True
        if not self._scanning and (self._scan_wanted or self._monotonic() >= self._next_scan_at):
            self._scanning, self._scan_cursor, self._scan_wanted = True, None, False
        if self._scanning:
            self._scan_page()
        for run_id in sorted(self._pending)[:self._slice]:
            if not self._log_run(run_id, 'commit'):
                break                          # the registry or the store refused: the rest waits for the next step
            self._pending.discard(run_id)
        return self._scanning

    def _scan_page(self) -> None:
        if self._first_pass_ever is None:
            self._first_pass_ever = not self._first_pass_done()
        events_db.require_no_transaction('daemon.registry')
        try:
            ids = self._registry.run_ids(self._scan_cursor, self._page)
        except Exception:                     # noqa: BLE001 - the registry refuses reads for now; the same page later
            LOG.warning('the change reader could not page the registry; it tries again', exc_info=True)
            return
        if not ids:
            self._finish_pass()
            return
        reason = 'snapshot' if self._first_pass_ever else 'reconcile'
        for run_id in ids:
            if not self._log_run(run_id, reason):
                return                         # the same page again at the next step, from the same cursor
            self._scan_cursor = run_id

    def _first_pass_done(self) -> bool:
        with self._store.transaction() as tx:
            return bool(tx.execute("SELECT 1 FROM events_scan_state WHERE key = 'first_pass_done'"))

    def _finish_pass(self) -> None:
        if self._first_pass_ever:
            with self._store.transaction() as tx:
                tx.execute("INSERT OR REPLACE INTO events_scan_state (key, value) VALUES ('first_pass_done', 1)")
            self._first_pass_ever = False
        self._scanning, self._scan_cursor = False, None
        self._next_scan_at = self._monotonic() + self._interval
        self._ready = True

    def _log_run(self, run_id: str, reason: str) -> bool:
        """Read one image and log it if it differs from the last one logged. False when the registry or the
        store refused (the run stays pending); True when it is logged, unchanged, or has no typed image."""
        events_db.require_no_transaction('daemon.registry')
        try:
            row, text = self._registry.snapshot(run_id)
        except Exception:                     # noqa: BLE001 - e.g. a publish whose directory sync is unfinished
            LOG.warning('the change reader could not read run %s; it tries again', run_id, exc_info=True)
            return False
        # The registry's lock is released: parse, canonicalise and hash now.
        try:
            image = post_image(row, text)
            digest = image_digest(image)
        except ValueError:
            LOG.error('run %s has no typed post-image in the registry; a later scan reads it again', run_id, exc_info=True)
            return True
        try:
            self._commit(run_id, image, digest, reason)
        except Exception:                     # noqa: BLE001 - the store failed or refused: the run stays pending
            LOG.error('the change reader could not log run %s; it tries again', run_id, exc_info=True)
            return False
        return True

    def _commit(self, run_id: str, image: dict, digest: str, reason: str) -> None:
        both_null = image['daemon_row'] is None and image['control_record'] is None
        with self._store.transaction() as tx:
            last = tx.execute('SELECT log_id, digest, route_logged FROM events_run_images WHERE run_id = ?', (run_id,))
            if last and last[0][1] == digest:
                return
            if not last and both_null:
                return                         # a run the registry does not hold and this log never saw
            log_id = last[0][0] if last else log_id_of(run_id, image)
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
                       'ON CONFLICT (run_id) DO UPDATE SET digest = excluded.digest, route_logged = excluded.route_logged',
                       (run_id, log_id, digest, int(route_logged)))


def open_store(state_dir) -> events_db.EventStore:
    """The ``event_store`` factory: the store in the daemon's state directory, with this lane's tables."""
    store = events_db.EventStore(os.path.join(os.fspath(state_dir), STORE_FILE))
    try:
        store.register_tables(LANE, LANE_VERSION, LANE_DDL)
    except BaseException:
        store.close()
        raise
    return store


def register(hooks, **reader_options) -> ChangeReader:
    """Lane EVENTS' registration: the event store's factory, the change marker's consumer, the service.
    Not yet called by any composition: ``events/hooks.py`` (the registrant SPINE imports) is not added until
    the Liaison says registration may go live."""
    hooks.set('event_store', open_store)
    reader = ChangeReader(hooks.enable_changes(), **reader_options)
    hooks.service(reader)
    return reader
