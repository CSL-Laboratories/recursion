"""The daemon's route breaker with its trips, run bindings and faults in a durable store (CSL-738).

``routes.ConnectionBreaker`` decides admission (cooldown, the one leased probe, short faults) and
lives in memory. ``plugin_security.ConnectionBreaker`` is the durable register the approval path
reads (PRODUCT.md d84cdd26, CSLR-D-012, section 4.2 "Two breakers": an intent from a run on a
tripped route, or from a run that ended in a fault, is refused; section 3.1 lines 459-461: runs
outlive the daemon, so losing this state at a restart would remove a refusal). ``DurableRouteBreaker``
is the route breaker ``RouteService`` calls, and writes what must survive into that store. The
store's module is only called here, never changed.

* A connection-scoped typed limit that opens the route breaker is reported to the store
  (``report(connection, "plan_limit")``). A model-bucket limit is not: the store is keyed by
  connection only, and tripping it would fence every model on that login.
* After a restart the in-memory breaker is empty. The first request on a connection the store
  still calls tripped reopens it in memory with the breaker's own cooldown (the vendor's reset
  time is not stored), so that request is refused ``breaker_open``. A request whose store read
  fails is refused ``breaker_open`` too, and the next request reads the store again. After the cooldown one
  leased probe is admitted; the trip clears in the store only through that probe's lease
  (``begin_probe`` / ``reset_after_probe``), and only when the probe run succeeded; never when this
  daemon reported a newer trip of that connection after the lease was taken (E01). The store's
  lease is as long as the longest route deadline plus the route breaker's grace; a trip the store
  still holds after a successful probe is logged and fenced again, never shown closed.
* Every run the daemon launches is bound in the store to its connection, engine and model, owned
  by ``host``: a reserved plugin id, so no plugin's effect can cite an operator's run. A run whose
  route result is an error is recorded as faulted. A trip or a fault the store does not take is
  kept in ``breaker-pending.json`` beside the store and applied on the next open (and at shutdown
  when the store accepts it). An error ``result.json`` with no fault row is faulted on that open,
  so a stop between the result and ``fault_run`` does not drop the fault.

The store is ``<state dir>/plugin-security/breaker.sqlite3`` (0600 in a 0700 directory; the file, its
WAL and shm files and the directory must not be symlinks: ``breaker_store_untrusted``). There is
no installer yet, so the daemon creates it on first start; a store deleted later is created empty
again and that is logged, not hidden. A store that exists and cannot be read is not replaced: the
daemon keeps serving reads and Stop, and refuses every start (``breaker_store_unavailable``).
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import sqlite3
import stat
import threading
import weakref

from recursion_core.plugin_security import ConnectionBreaker as StoredBreaker
from recursion_core.plugin_security import Refused, create_breaker_store
from recursion_core.routes import contract as c

from .launcher import _current, private_dir, require_launcher_platform

log = logging.getLogger(__name__)

STORE_DIR = 'plugin-security'
STORE_FILE = 'breaker.sqlite3'
PENDING_NAME = 'breaker-pending.json'  # trips and faults the sqlite store did not take
_PENDING_LIMIT = 1024 * 1024
HOST = 'host'  # plugin_security.protocol.HOST: reserved, so no plugin manifest can carry it
RESTORED_SOURCE = 'recursion.breaker_store'
STORE_PROBE_LEASE_S = 300.0  # plugin_security.ConnectionBreaker's default probe_lease_seconds
# The path open_breaker_store used, so a trip the store does not take can be written beside that
# file. The frozen store does not publish its path, and this module only calls it. A stand-in that
# was not opened here has no entry and writes no pending file.
_OPENED = weakref.WeakKeyDictionary()


def store_path(state_dir):
    return Path(state_dir) / STORE_DIR / STORE_FILE


def store_probe_lease_s(routes, route_breaker):
    """The store's probe lease: at least as long as the longest probe the route breaker can lease.

    The route breaker leases a probe for the run's deadline plus its grace; the store's lease is one
    length per store, so it is the longest of those over every configured route (ADV2-2), and never
    shorter than the store's own default.
    """
    deadlines = [entry['deadline_s'] for entry in (routes or {}).values()]
    return max([STORE_PROBE_LEASE_S, route_breaker.probe_lease_s()]
               + [route_breaker.probe_lease_s(deadline) for deadline in deadlines])


def open_breaker_store(state_dir, *, clock, probe_lease_seconds=None):
    """Open the daemon's breaker store, creating it when there is none. Returns ``(store, created)``.

    A new store is built under a temporary name and renamed, so a daemon killed part-way never
    leaves a half-made file that a later start would have to refuse. ``probe_lease_seconds`` is
    ``store_probe_lease_s`` of the loaded routes (the store's default when None).
    """
    require_launcher_platform()  # Windows: typed PlatformUnsupported before any directory or file
    path = store_path(state_dir)
    private_dir(path.parent)
    created = False
    if not os.path.lexists(path):
        tmp = path.with_name(f'{STORE_FILE}.{os.getpid()}.tmp')
        for leftover in (tmp, Path(f'{tmp}-wal'), Path(f'{tmp}-shm')):
            try:
                os.unlink(leftover)
            except FileNotFoundError:
                pass
        create_breaker_store(str(tmp))
        os.replace(tmp, path)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        created = True
    for item in (path, Path(f'{path}-wal'), Path(f'{path}-shm')):
        _require_own_file(item, required=item == path)
    lease_s =STORE_PROBE_LEASE_S if probe_lease_seconds is None else float(probe_lease_seconds)
    stored = StoredBreaker(str(path), clock=clock, probe_lease_seconds=lease_s)
    _OPENED[stored] = path
    try:
        _apply_pending(path, stored)
        _fault_error_results(state_dir, stored)
    except BaseException:
        stored.close()
        _OPENED.pop(stored, None)
        raise
    return stored, created


def _require_own_file(path, *, required):
    """Refuse a store file that is a symlink, not a regular file, or not ours (ADV2-8).

    SQLite opens by path, so a symlink here would silently read another store. ``private_dir``
    already refuses a symlinked directory. Same-uid code can still swap the file after this check
    (B17).
    """
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        if required:
            raise Refused('breaker_store_missing') from None
        return
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
        raise Refused('breaker_store_untrusted', f'{path.name} must be an operator-owned regular file, not a symlink')


def _read_private(path, limit):
    """Bytes of an owned regular file, not a symlink and not group- or other-writable."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o022 or info.st_size > limit):
            raise Refused('breaker_store_untrusted', Path(path).name)
        with os.fdopen(os.dup(fd), 'rb') as handle:
            return handle.read(limit + 1)
    finally:
        os.close(fd)


def _write_all(fd, raw):
    view = memoryview(raw)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError('short write')
        view = view[written:]


def _write_private(path, raw):
    path = Path(path)
    tmp = path.with_name(f'.{path.name}.{os.getpid()}.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        _write_all(fd, raw)
        os.fchmod(fd, 0o600)
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.close(fd)
    os.replace(tmp, path)
    dirfd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(dirfd)
    finally:
        os.close(dirfd)


def _pending_path(store_file):
    return Path(store_file).with_name(PENDING_NAME)


def _load_pending(path):
    if not os.path.lexists(path):
        return [], []
    data = json.loads(_read_private(path, _PENDING_LIMIT).decode('utf-8'))
    if not isinstance(data, dict):
        raise Refused('breaker_store_untrusted', Path(path).name)
    trips, faults = data.get('trips', []), data.get('faults', [])
    if (not isinstance(trips, list) or not isinstance(faults, list)
            or not all(type(item) is str for item in trips)
            or not all(type(item) is str for item in faults)):
        raise Refused('breaker_store_untrusted', Path(path).name)
    return list(dict.fromkeys(trips)), list(dict.fromkeys(faults))


def _store_pending(path, trips, faults):
    if not trips and not faults:
        if os.path.lexists(path):
            os.unlink(path)
        return
    raw = (json.dumps({'faults': faults, 'trips': trips}, sort_keys=True) + '\n').encode('utf-8')
    _write_private(path, raw)


def _add_pending(store_file, *, trip=None, fault=None):
    path = _pending_path(store_file)
    trips, faults = _load_pending(path)
    if trip is not None and trip not in trips:
        trips.append(trip)
    if fault is not None and fault not in faults:
        faults.append(fault)
    _store_pending(path, trips, faults)


def _apply_pending(store_file, stored):
    """Put trips and faults the previous process could not write into the store. Idempotent."""
    path = _pending_path(store_file)
    if not os.path.lexists(path):
        return
    trips, faults = _load_pending(path)
    for connection in trips:
        stored.report(connection, c.PLAN_LIMIT)
    for run_id in faults:
        stored.fault_run(run_id)
    os.unlink(path)


def _fault_error_results(state_dir, stored):
    """A durable error result with no fault row is faulted. A run still going has no result file."""
    runs = Path(state_dir) / 'runs'
    try:
        info = os.lstat(runs)
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(info.st_mode):
        return
    for entry in os.scandir(runs):
        if not entry.is_dir(follow_symlinks=False):
            continue
        result = Path(entry.path) / 'result.json'
        if not os.path.lexists(result):
            continue
        try:
            payload = json.loads(_read_private(result, _PENDING_LIMIT).decode('utf-8'))
        except (OSError, Refused, ValueError, UnicodeError):
            continue
        if not isinstance(payload, dict) or payload.get('code') is None:
            continue
        try:
            stored.fault_run(entry.name)
        except Refused:
            log.error('an error result was not a fault the store can record')


class DurableRouteBreaker:
    """``admit`` / ``record`` for ``RouteService``, over the in-memory breaker and the durable store."""

    def __init__(self, inner, stored):
        self._inner, self._stored = inner, stored
        self._lock = threading.Lock()
        self._restored = set()  # connections whose stored trip this process has already taken over
        self._leases = {}       # route probe token -> (the store's probe lease, the trip generation it was taken at)
        # E01: every connection trip this daemon reports to the store counts one generation of that
        # connection. A store lease is taken, a trip reported and a trip cleared under a fresh lease
        # only while holding ``_trip_lock``, so "no newer trip since the lease" is checked and acted
        # on as one step. Never taken while ``_lock`` is held.
        self._trip_lock = threading.Lock()
        self._trips = {}        # connection -> how many trips this process has reported for it
        self._store_file = _OPENED.get(stored)

    # -- the store, with its refusals and errors kept out of a route request -------------------
    def tripped(self, connection):
        """True, False, or None when the store could not be read."""
        try:
            return self._stored.tripped(connection)
        except (Refused, sqlite3.Error) as exc:
            log.error('breaker store could not be read for a connection: %s', _code(exc))
            return None

    def representable(self, connection):
        """Whether the store can key this connection at all. An id it refuses could never be durable.

        ``core_timeout`` is an event the store checks the connection id for and then ignores by
        contract (the core's own timeouts never trip), so this asks without writing anything.
        """
        try:
            self._stored.report(connection, 'core_timeout')
        except Refused:
            return False
        except sqlite3.Error:
            pass
        return True

    def model_representable(self, model):
        """Whether the store can record this model with a run's binding (``bind_run`` would take it).

        The rule is the store's own (``ConnectionBreaker.model_recordable``); nothing is read or written.
        """
        return StoredBreaker.model_recordable(model)

    def state(self, connection):
        return self._inner.state(connection)

    # -- runs -----------------------------------------------------------------------------------
    def bind_run(self, run_id, connection, provider, model_version):
        """Record the run's connection, engine and model for the approval path. Returns a refusal code or None."""
        try:
            self._stored.bind_run(run_id, connection, HOST, provider, model_version)
        except (Refused, sqlite3.Error) as exc:
            # Not bound: an effect citing this run is refused ``run_unknown``, which fails closed.
            log.error('run %s was not bound in the breaker store: %s', run_id, _code(exc))
            return _code(exc)
        return None

    def fault_run(self, run_id):
        try:
            self._stored.fault_run(run_id)
        except (Refused, sqlite3.Error) as exc:
            log.error('fault of run %s was not recorded in the breaker store: %s', run_id, _code(exc))
            if not isinstance(exc, Refused) and self._store_file is not None and type(run_id) is str:
                self._keep_pending(fault=run_id)

    # -- routes.ConnectionBreaker interface ---------------------------------------------------------
    def admit(self, connection, model, tenant, *, deadline_s=None):
        if not self._restore(connection, model, tenant):
            # The store could not say whether an earlier daemon tripped this connection. Fail closed
            # as tripped; the next request reads the store again (ADV2-1).
            return {'admitted': False, 'code': c.BREAKER_OPEN, 'probe': None, 'retry_at': None, 'key': connection}
        answer = self._inner.admit(connection, model, tenant, deadline_s=deadline_s)
        probe = answer.get('probe')
        # A store that cannot say whether it holds a trip (None) goes the leased way too, and fails there.
        if answer.get('admitted') and probe is not None and self.tripped(connection) is not False:
            try:
                with self._trip_lock:
                    generation = self._trips.get(connection, 0)
                    lease = self._stored.begin_probe(connection)
            except (Refused, sqlite3.Error) as exc:
                # E24: no store lease (another holder's lease is live, or the store failed). A probe
                # admitted without one could never clear the stored trip, and two probes would run on
                # one trip: the route's probe is given back unused and this request is refused.
                log.error('no probe lease from the breaker store (%s): the route probe is refused', _code(exc))
                self._inner.record(connection, model, tenant, {'code': c.BREAKER_OPEN}, probe)
                return {'admitted': False, 'code': c.BREAKER_OPEN, 'probe': None, 'retry_at': None,
                        'key': connection}
            with self._lock:
                self._leases[probe] = (lease, generation)
        if answer.get('admitted'):
            ctx = getattr(_current, 'ctx', None)
            if ctx is not None:
                ctx.admitted = (connection, str(model))
        return answer

    def record(self, connection, model, tenant, outcome, probe=None):
        effect = self._inner.record(connection, model, tenant, outcome, probe)
        with self._lock:
            leased = self._leases.pop(probe, None) if probe is not None else None
        lease = None if leased is None else leased[0]
        try:
            if effect == 'opened':
                limit = outcome.get('limit')
                if isinstance(limit, dict) and limit.get('scope') == 'connection':
                    with self._trip_lock:  # a newer trip: no older probe may clear it under a fresh lease
                        self._trips[connection] = self._trips.get(connection, 0) + 1
                        self._stored.report(connection, c.PLAN_LIMIT)  # also drops any lease the store held
            elif effect == 'closed':
                if self._stored.tripped(connection) and not self._clear_stored_trip(connection, leased):
                    # The store still calls the connection tripped: do not show it closed (ADV2-2).
                    log.error('a successful probe could not clear the trip in the breaker store; the '
                              'connection is fenced again for the cooldown')
                    self._inner.record(connection, model, tenant, {
                        'code': c.PLAN_LIMIT, 'limit': c.limit_signal('connection', RESTORED_SOURCE)})
                    return 'none'
            elif lease is not None:
                self._stored.reset_after_probe(connection, lease, False)  # the probe proved nothing: spend it
        except (Refused, sqlite3.Error) as exc:
            # The in-memory breaker already holds the outcome; what did not reach the store is said.
            log.error('breaker store was not updated (%s after %s): the change is not durable', _code(exc), effect)
            limit = outcome.get('limit') if isinstance(outcome, dict) else None
            if (effect == 'opened' and not isinstance(exc, Refused) and self._store_file is not None
                    and isinstance(limit, dict) and limit.get('scope') == 'connection'):
                self._keep_pending(trip=connection)
            if effect == 'closed':
                with self._lock:
                    self._restored.discard(connection)  # the next request reads the store again first
        return effect

    def _clear_stored_trip(self, connection, leased):
        """Clear the stored trip after the route breaker closed on a successful probe. True when cleared.

        ``leased`` is the probe's store lease with the trip generation it was taken at, or None. The
        route breaker closes only for its own live probe (leased for the run's deadline plus grace).
        The store's lease is one length for every probe; when it ran out first, the trip is cleared
        under a fresh store lease, and that is logged. Never when this daemon reported a newer trip of
        the connection after the probe's lease was taken (a newer typed limit also drops that lease):
        the probe's success says nothing about it, so it stays and the caller fences the connection
        again (E01). A probe that took no lease cleared nothing then and clears nothing now.
        """
        if leased is None:
            log.error('a successful probe held no breaker store lease; the stored trip stays')
            return False
        lease, generation = leased
        try:
            if self._stored.reset_after_probe(connection, lease, True):
                return True
            with self._trip_lock:
                if self._trips.get(connection, 0) != generation:
                    log.error('a newer trip of the connection was reported after the probe was leased; '
                              'the older probe does not clear it')
                    return False
                log.warning("the breaker store's probe lease ended before the probe did; leasing again to clear the trip")
                return self._stored.reset_after_probe(connection, self._stored.begin_probe(connection), True)
        except (Refused, sqlite3.Error) as exc:  # e.g. probe_lease_held: the trip stays in the store
            log.error('breaker store trip not cleared: %s', _code(exc))
            return False

    def _restore(self, connection, model, tenant):
        """Take over a trip an earlier daemon stored. False when the store could not be read.

        The connection counts as taken over only once the store was read: a failed read is
        retried at the next request, never treated as "not tripped" (ADV2-1).
        """
        with self._lock:
            if connection in self._restored:
                return True
            # Under the lock: a second request on this connection must not be admitted in between.
            tripped = self.tripped(connection)
            if tripped is None:
                log.error('breaker store unreadable at the first request on a connection: refused as '
                          'tripped (breaker_open); the store is read again at the next request')
                return False
            self._restored.add(connection)
            if tripped and self._inner.state(connection) == 'closed':
                log.warning('connection is tripped in the breaker store from an earlier daemon; it stays '
                            'fenced for the cooldown, then one probe is admitted')
                self._inner.record(connection, model, tenant, {
                    'code': c.PLAN_LIMIT, 'limit': c.limit_signal('connection', RESTORED_SOURCE)})
            return True

    def _keep_pending(self, *, trip=None, fault=None):
        """Remember one trip or fault the store did not take, so the next open can apply it."""
        try:
            with self._lock:
                _add_pending(self._store_file, trip=trip, fault=fault)
        except (OSError, Refused, ValueError, UnicodeError) as exc:
            log.error('breaker state the store refused was not kept for the next open: %s', type(exc).__name__)

    def close(self):
        if self._store_file is not None and os.path.lexists(_pending_path(self._store_file)):
            try:
                _apply_pending(self._store_file, self._stored)
            except (OSError, Refused, sqlite3.Error, ValueError, UnicodeError) as exc:
                log.error('pending breaker state was not applied at shutdown: %s', type(exc).__name__)
        self._stored.close()


def _code(exc):
    return exc.code if isinstance(exc, Refused) else f'store_error:{type(exc).__name__}'
