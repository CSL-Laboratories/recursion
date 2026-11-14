"""The hooks: every place where another lane's code enters the daemon, the control API and the CLI.

A HOOK is a call from one of the daemon's own files into this module. Its DEFAULT is exactly what the
daemon does today, so with nothing registered every answer, record and file is what it was. A lane that
wants behaviour registers into a hook from a file of its own, and that change is read in that lane.
The daemon's files only call this module; they hold no lane's logic.

REGISTRATION. ``compose()`` makes one ``Hooks`` object for one composition (a daemon, or one run of the
CLI). It goes through a fixed list of registrant modules, ``REGISTRANTS``: one per lane, named here and
nowhere else. Adding a name is a request to the lane that keeps this file. The imports are GUARDED STATIC
IMPORTS: each registrant has a function of its own below, with its names written out, a literal
``find_spec`` for each package above it that is not part of the core and for the module itself, and a
literal ``import`` statement. Nothing in this module imports a module by a computed name, and
``compose()`` takes no list of names: what can be loaded is what is written here. For each name:

1. ``importlib.util.find_spec`` of its parent package returns None: that lane is not installed. Skipped.
   The ancestors are looked for one at a time, outermost first, so a name whose parent's own parent is
   absent is skipped the same way and is not mistaken for a broken install.
2. ``find_spec`` of the registrant itself returns None: the package is there and has no hooks module. Skipped.
3. Otherwise the module is imported and ``module.register(hooks)`` is called. ANY exception, from the
   import or from ``register``, a ``ModuleNotFoundError`` for something the module imports included,
   fails composition (``HookError``) and the daemon does not start. So does an exception an existing
   parent package raises while step 2 imports it. There is no blanket ``except ImportError``: absent is
   decided by ``find_spec``, before the import, and a registrant that is present and broken is never
   treated as absent.

``register(hooks)`` runs once, before the socket and the page are served, and registers EVERYTHING the
lane adds: its hooks, its method handlers (S1), its command modules (S2, by importing its own
``control/commands/<family>.py``), its engine factory (S3), its start registrant (S6), its services
(S13). It only registers: it opens nothing, starts no thread and reads no state. The same function runs
in the CLI's process, where only the commands are used. After the last registrant the tables are frozen:
on the free-threaded runtime nothing mutates them while calls are served. Two registrants for one
single-valued hook, for one method, command, engine, service or launch-snapshot key, fail composition.

A HOOK THAT RAISES NEVER FAILS OPEN. The row for each hook is in its own description below; in short:
``authorize``, a start registrant, ``launch_check``, ``working_directory`` and ``credentials``: the call
or the launch is refused with a typed code, and the failure is logged. ``read_result``: logged, and
treated as no result. ``drive_owner`` raising before it accepts: logged, and the run is driven on
today's one-turn path, which is the stricter one. ``mark_changed``: swallowed; the next scan covers it.
``start_services``: composition fails.

LOCKS. No hook is called while the registry's lock, the daemon's lock or a run's lock is held, with one
exception: the change marker (S12), which does no I/O, calls no lane code and cannot wait.

THE HOOKS, with the exact arguments and return of each, the thread that calls it, and what a failure does.

S1  ``hooks.methods``: the method table (``control.methods.MethodTable``).
    ``handler(ctx, params) -> answer``. Called on the thread that serves the request (a socket worker,
    the page's request thread, or the caller's own thread for an in-process call), inside that
    request's admission. No daemon lock is held. See ``control/methods/__init__.py`` for ``ctx``, for
    who owns the transaction and for what an after-commit callback is. Raises: the command is rolled
    back, no callback runs, and the caller is answered ``unavailable``.
S2  ``hooks.commands``: the command table (``control.commands.CommandTable``).
    ``parser(subparsers)`` and ``dispatch(args, client) -> exit status``, on the CLI's main thread. A
    parser that adds a name it did not register, or changes another family's command, stops the CLI
    before any command runs.
S3  ``hooks.engines``: the engine table (``routes.engines.EngineTable``).
    ``factory(launcher, clock, credentials, environ, **options) -> adapter``, on the composing thread,
    once per composition. Raises: composition fails.
S4  ``working_directory(ctx) -> path or None``. ``ctx`` is the run's ``RunContext``; the provider reads
    ``ctx.launch.workspace`` (mode, source_path, base_ref) and makes no registry lookup of its own.
    Called by the launcher on the run's own thread, after the executable is resolved and before
    anything of the unit is prepared. None means today's folder. The launcher applies to whatever
    comes back the same unit-path check that guards today's folder. HOME does not change. A failed
    start is relaunched under its run id, so the provider can be asked twice for one run: it returns
    the folder it made before, or makes it if the first attempt never got that far, never a second.
    Default: None. Raises: the launch is refused, ``working_directory_failed``.
S5  ``authorize(principal, method, params) -> None, or a refusal code``. Called for EVERY client call
    (the socket, the page's Stop, the page's two reads, an in-process call, and each ``ctx.call``),
    on the serving thread, before the method runs. A refusal code is a word such as ``scope_refused``;
    a pair ``(code, detail)`` is taken too. Default: a principal with ``in_run_unit`` is refused
    ``scope_refused``; everything else is allowed; no scope policy is enforced. A registered
    authorizer takes over the WHOLE decision, that one included. Raises, or returns anything else:
    refused ``scope_refused``, detail ``authorizer_failed``. The daemon's own recovery Stop is not a
    client's call and never passes here.
S6  ``hooks.start_registrant(name, fields, keys, normalize)``: a lane's part of the launch snapshot.
    ``normalize(principal, request) -> {key: value}``, or a ``Refusal``. Called for a NEW start only,
    on the serving thread, outside every lock, before any record exists; it has no side effect (no
    folder, worktree, reservation or event). ``fields`` are the request fields the lane honours;
    ``keys`` the snapshot keys it sets. Registering one turns the versioned start path ON for the
    composition; with none the start path is today's, byte for byte. Raises: the start is refused
    ``unavailable``, detail ``start_registrant_failed``, and nothing is launched.
S7  ``credentials(connection_id, env_name) -> the secret``, or raises ``KeyError(connection_id)``.
    Called by an engine adapter on the run's thread. Default: today's, which always raises
    ``KeyError(connection_id)``. A source that raises anything else, or returns None, an empty string
    or anything that is not a string, is treated as having no credential: ``KeyError(connection_id)``.
S8  ``read_result(run_id) -> a typed result (a dict), or None``. Asked by ``run.status`` ONLY, and only
    when the run has no result in memory and none in ``result.json``; never by ``run.list``. On the
    serving thread. It is the RUN's result; a turn's result is in the session record. Default: None.
    Raises, or returns anything that is not a dict: logged, None.
S9  ``drive_owner(ctx, request) -> None, or an owner``. Asked at the top of the starter's drive, on the
    run's own thread. None, the default: the run is driven exactly as today. An owner accepts the run:
    ``owner.run(ctx, request, service, record_result, fault_run, settle, release)`` is then called and
    the starter returns; the run stays active until the owner calls ``release()``, exactly once. The
    owner learns the session and the turn from ``ctx.launch``. Raises before it accepts: logged, and
    the run is driven on today's path.
    ``owner.run`` is a hand-over: it returns promptly, and closing waits for it. What outlives it is a
    worker of a registered service of the owner's lane, joined by that service's ``stop`` before the
    store closes.
    The seam binds the run to the thread it calls ``owner.run`` on. An owner that calls the route
    service from another thread binds that thread itself, with ``bound_run(ctx)`` (``daemon.launcher``).
    If ``owner.run`` raises, the seam gives the run an engine-error result when it has none, settles
    what was launched unless the daemon is closing, and releases; an owner that wants another end does
    not raise.
S10``launch_check(ctx, executable) -> None, or a refusal code``. Called by the launcher on the run's
    thread, after the executable is resolved and before the run directory's FIFOs and files are made,
    so a refusal leaves nothing in the way of a relaunch. It reads the route and the containment level
    from ``ctx.launch``. Parent side only: nothing is added to the child's environment. Default: None.
    A refusal code refuses the launch ``launch_check:<code>``. Raises: refused ``launch_check_failed``.
S11 ``page_methods() -> a frozen set of method names``: the methods the page may call through
    ``POST /v1/control``. On the page's request thread. Default: the empty set, so the path answers
    404 as today. Raises: logged, the empty set.
S12 ``hooks.mark_changed(run_id)`` and ``hooks.changes``: the change marker. See ``ChangeMarker``.
S13 ``hooks.service(service)``, ``hooks.start_services(context)``, ``hooks.stop_services(context)``.
    See ``ServiceContext`` and ``Admission``. The event store itself is brought by one more
    single-valued hook, ``hooks.set("event_store", factory)``: ``factory(state_dir) -> the store's
    transaction helper``, called once by composition, on the composing thread, before any service
    starts; what the dispatcher needs of that helper is in ``control/methods/__init__.py``. Raises:
    composition fails. A service whose ``start`` raises: it and the services already started are
    stopped in reverse and composition fails.
    What is called on a run's own thread (S4, S7, S9, S10) is not covered by the admission of the start
    that made the thread, which may have answered already. Each holds a lifetime reference of its own:
    closing waits for it with the services and the store alive, and once closing has begun the run or
    the launch is refused ``launch_refused``, ``daemon_closing`` before the hook is asked.

The after-commit subscription to events is not a hook: it is the event store's own interface, which
other lanes call from their services.

BUILDING AGAINST THIS FILE. A lane needs no daemon to test what it registers: make ``Hooks()``, call the lane's own ``register(hooks)`` on it, ``freeze()``
it, and call the hook, or ``hooks.methods.dispatch(method, params, principal, call)`` for a handler.
That object IS the shared fake: every lane's tests go through these same defaults and these same checks,
not through a guess of them. The one fake of the event store's transaction helper is ``FakeStore`` in
``control/tests/test_seams.py``. The calls themselves are in ``control/api.py``, ``control/cli.py``,
``daemon/control_adapter.py``, ``daemon/launcher.py`` and ``daemon/__init__.py``, each marked with its
seam's number, and are tested in the two ``test_seams.py`` files.

Direct MethodTable.dispatch is an already-authorized, already-admitted primitive, not an entry point.
Entry points must preserve the supplied principal, authorize before receipt access, hold the one
admission through all callbacks, and release it in finally. Services, table registration and store
initialization precede serving. Lifecycle APIs are called by the one composition owner; lanes do not
invoke them from admitted handlers.
"""
from __future__ import annotations

import hashlib
import importlib.util
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from recursion_core.control.commands import CommandTable
from recursion_core.control.methods import MethodTable, canonical_text

log = logging.getLogger(__name__)

# One registrant module per lane. Each exposes ``register(hooks)``. A lane with no module in this list has
# nothing registered.
REGISTRANTS = (
    'recursion_core.keys.hooks',
    'recursion_core.events.hooks',
    'recursion_core.workspace.hooks',
    'recursion_core.routing.hooks',
    'recursion_core.quota.hooks',
    'recursion_core.approvals.hooks',
    'recursion_core.sessions.hooks',
    'recursion_core.launch_check.hooks',
    'recursion_core.setup.hooks',
    'recursion_core.gui.hooks',
    'recursion_core.remote.hooks',
    'recursion_core.control.mcp.hooks',
    'recursion_core.control.scopes',
    'recursion_core.routes.driver_hooks',
    'recursion_core.plugin_security.host_hooks',
    'recursion_core.plugin_security.sign_hooks',
)

SINGLE = ('authorize', 'working_directory', 'credentials', 'read_result', 'drive_owner', 'launch_check',
          'page_methods', 'event_store')
# The launch snapshot: the keys of the stored ``launch`` object, and the ones a start registrant may set.
# ``owner`` is not among them: the server derives it from the principal.
LAUNCH_KEYS = ('route_entry', 'route_decision', 'workspace', 'containment_level', 'session_id', 'turn_id',
               'owner', 'budget')
SETTABLE_KEYS = ('route_entry', 'route_decision', 'workspace', 'containment_level', 'session_id', 'turn_id',
                 'budget')
INTENT_VERSION = 1
PRINCIPAL_KINDS = ('owner', 'device', 'internal', 'plugin', 'session')


class HookError(Exception):
    """Composition cannot go on: a registrant failed, or two of them claim one thing. The daemon does not start."""


class HookRefused(Exception):
    """A hook refused a launch, or failed while deciding. ``code`` is a typed word for the refusal."""

    def __init__(self, code: str, detail: str = '') -> None:
        super().__init__(f'{code}: {detail}' if detail else code)
        self.code, self.detail = code, detail


@dataclass(frozen=True)
class Refusal:
    """What a start registrant returns to refuse a start before any record exists: the method's own code."""
    code: str
    detail: str = ''

    def answer(self) -> dict:
        return {'code': self.code, 'detail': self.detail} if self.detail else {'code': self.code}


@dataclass(frozen=True)
class Principal:
    """Who is calling, made by the server from the way the request came in and never read from params.

    ``owner``: the owner-only socket. ``device``: the page, for any request that passed its token and
    origin checks. ``internal``: the daemon's own components, by name. ``plugin``: a loaded plugin, by its
    id, set by the plugin host. ``session``: a delegated session, by its id. A socket principal also
    carries today's predicate, ``in_run_unit``: true when the peer is inside a run's unit, or its cgroup
    cannot be read.
    """
    kind: str
    id: str | None = None
    in_run_unit: bool = False

    def __post_init__(self) -> None:
        if self.kind not in PRINCIPAL_KINDS:
            raise ValueError(f'principal kind {self.kind!r}')

    def public(self) -> dict:
        """The principal as a record carries it: a kind and a label, nothing else."""
        return {'kind': self.kind, 'id': self.id}


OWNER = Principal('owner', 'local')      # the owner-only socket, and an in-process call made for the owner
DEVICE = Principal('device', 'page')     # the page; a principal per paired device is a later lane's


@dataclass(frozen=True)
class LaunchContext:
    """The launch snapshot of one run at run time (S4): the fields of the stored record's ``launch``
    object, their values unchanged, and the principal, copied from the record's own ``principal``.

    The starter builds it from the stored record with ``from_record``, never from the client's params,
    and carries it as ``ctx.launch`` to the working-directory provider, the drive owner, the launch
    check and the launcher. With the versioned start path off, ``ctx.launch`` is None.

    ``launch`` and ``principal`` are stored together or not at all. A record with one and not the other
    is refused; a record with neither is a legacy record and has no launch context.
    """
    route_entry: dict | None
    route_decision: dict | None
    workspace: dict | None
    containment_level: str | None
    session_id: str | None
    turn_id: str | None
    owner: str
    budget: dict
    principal: Principal

    @classmethod
    def from_record(cls, record: dict) -> 'LaunchContext | None':
        has_launch, has_principal = 'launch' in record, 'principal' in record
        if has_launch != has_principal:
            raise ValueError('launch and principal must be stored together')
        if not has_launch:
            return None
        launch, kept = record['launch'], record['principal']
        if not isinstance(launch, dict) or set(launch) != set(LAUNCH_KEYS):
            raise ValueError('invalid launch object')
        if not isinstance(kept, dict) or set(kept) != {'kind', 'id'}:
            raise ValueError('invalid principal object')
        principal = Principal(kept['kind'], kept['id'])
        return cls(principal=principal, **{key: launch[key] for key in LAUNCH_KEYS})


def no_custody(connection_id, env_name):
    """Today's credential source: there is none, so an API-key route refuses to launch."""
    raise KeyError(connection_id)


# ------------------------------------------------------------------------------- S12, the change marker
class ChangeMarker:
    """Which runs may have changed, for the lane that logs state changes. Advisory.

    Losing a mark is allowed: a scan of the registry finds what a mark missed, and that lane scans at
    startup and periodically whatever this says. It carries run ids and nothing else.

    THE PRODUCER'S COST, exactly. The daemon registry calls ``mark(run_id)`` when ``add``,
    ``request_stop``, ``state`` or ``control_put`` returns normally, and that return path can run while
    the daemon's lock is held, before a Stop is sent. So ``mark`` never waits: ONE NON-BLOCKING attempt
    on this marker's lock. With the lock: one insertion into a set that holds at most ``CAP`` ids (past
    the cap the ``dropped`` flag is set instead), and the lock is released. Without the lock: the
    ``dropped`` flag is set with a plain attribute store. No event is set and nobody is notified, so
    there is no second lock to wait on. No I/O, no lane code, and nothing it does can raise into the
    writer. That is the whole cost: one lock attempt that cannot block and at most one set insertion.
    It is not "no cost". A writer that RAISES marks nothing: a publish that failed keeps the answer it
    has today. Nothing is written inside the registry's transaction and no table is added; the
    registry file is byte for byte what it is without the marker.

    THE CONSUMER, one thread of the lane that logs changes:

    ``take() -> (ids, dropped)``
        Swaps the set for an empty one and clears the flag under the marker's lock, O(1), then
        releases. It never iterates, reads a source or commits while holding that lock.
    ``poll_interval_s``
        How long the consumer sleeps between takes (0.1 by default). It is not a promise of freshness.

    THE SOURCE the consumer reads is the daemon registry itself, through two read-only methods on the
    registry's own connection, under its own lock and behind the same guard that refuses a read while a
    publish's directory sync is unfinished:

    ``Registry.snapshot(run_id) -> (row or None, control_text or None)``
        One run, both tables, in one hold of the lock, by two point lookups. A run with only a control
        record has row None; a run with only a daemon row has control_text None.
    ``Registry.run_ids(after, limit) -> [run_id, ...]``
        The ids present in EITHER table, in order, strictly after ``after``, at most ``limit``: a seek on
        each table's primary key for at most ``limit`` ids, merged. No OFFSET, no unbounded union, no
        walk of the whole registry. A missing control table contributes nothing.

    They are authoritative where the in-memory copies are not: a control record published by a write
    that then raised (the rename happened; the directory sync or the reopen failed) is in the file and
    in neither the daemon's rows nor the control registry's copy. Only a reader of the file's control
    table finds it. The consumer never reads the control registry's in-memory copy, never lists every
    run in a loop, never opens the file itself and keeps no connection of its own. While the registry
    refuses reads it waits and tries again. It parses, canonicalises and hashes what it read AFTER the
    registry's lock is released; no source lock spans a page and its snapshots, or a transaction of
    the event store.

    WHAT THIS COSTS A STOP. The bounds above are on rows and work, not on milliseconds or bytes: reading
    a control document costs its size, and the guard may reopen the file or retry the directory sync. A
    scan therefore contends with Stop on the registry's lock. A paused reader can delay a Stop, and an
    ordinary reentrant lock promises nobody that Stop gets it before the consumer's next read. No
    one-page, one-read or wall-clock bound on that delay is promised. The PRODUCER waits for no
    consumer lock at all.

    THE PROTOCOL the consumer follows:

    - An image is ``(row, control_text)`` as ``snapshot`` returned it; its digest is over the canonical
      JSON of the typed post-image of ``state.changed``.
    - Dedupe means "equal to the LAST image logged for this run", kept per run. It is not a unique
      (run, digest) for all time: a run OBSERVED as A, then B, then A logs all three.
    - One transaction of the event store holds the ``state.changed`` event and the run's new last
      digest. There is no separate checkpoint write and nothing to acknowledge to the source.
    - One consumer owns the order from snapshot to commit. It finishes or retries an image's
      transaction before it commits a later image of that run. There is no queue of old control texts.
    - A taken batch stays pending until its commits succeed. It is worked in bounded slices, with scan
      progress in between; new marks never put off a scan that is due. A drop asks for a scan and does
      not send a scan in progress back to its first page. Unconditional periodic scans are required:
      the ``dropped`` flag is advisory and can race with ``take`` clearing it.
    - At startup, and no later than the configured interval after a pass completed, a scan begins from
      the first id and advances by the last id returned until an empty page ends the pass. Startup
      readiness waits for one complete pass. A record inserted behind the cursor may wait for the next
      pass.
    - ``coalesced`` is null for this marker, for marked images and for scanned ones: a set keeps
      membership, not the number of calls or transitions.

    WHAT IT GIVES. Once the source can be read and the event store commits again, and scans make
    progress and complete, every retained run's latest post-image is reconciled without a restart,
    records published by a write that raised included. There is no time bound during an outage, under
    lock starvation, or for a scan that grows without end. With a scan bounded by T and an interval I
    measured from a pass's end, a lasting change a pass missed is seen within the rest of that pass,
    I, and the next pass: at most 2T + I, plus any outage or retry. Changes that come and go between
    two reads are not kept: an observed A, B, A gives three events; a B nobody observed need not give one.
    """

    CAP = 4096

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ids: set = set()
        self._dropped = False
        self.poll_interval_s = 0.1

    def mark(self, run_id) -> None:
        if not self._lock.acquire(blocking=False):
            self._dropped = True
            return
        try:
            if run_id in self._ids or len(self._ids) < self.CAP:
                self._ids.add(run_id)
            else:
                self._dropped = True
        finally:
            self._lock.release()

    def take(self) -> tuple:
        with self._lock:
            ids, self._ids = self._ids, set()
            dropped, self._dropped = self._dropped, False
        return ids, dropped


# ------------------------------------------------------------------------- S13, services and admission
@dataclass
class ServiceContext:
    """What a service is started and stopped with (S13). A service uses these declared fields, not
    attributes it discovers on them.

    ``daemon``: the daemon. ``registry``: the daemon's registry (the file). ``control_registry``: the
    control API's run records. ``control``: the object every surface calls. ``store``: the event
    store's transaction helper, or None when no event store is composed; its tables are registered
    and it is initialised before any handler can use it, and its first reconciliation may still be
    running. ``state_dir``: the daemon's state directory. ``clock``: the composition's clock.
    ``closing``: a ``threading.Event`` set when the daemon begins to close.

    A SERVICE is an object with a ``name``, ``start(context)``, ``stop(context)`` and ``ready``.
    ``start`` returns promptly, after starting its thread; ``ready`` becomes true only when the
    initialisation the service needs has succeeded. ``stop`` joins its workers, leaves pending durable
    work recoverable and leaves ambiguous attempts marked for reconciliation.

    ``stop`` RETURNS only when every worker of the service has joined: its return is what tells the
    teardown that nothing of the service still uses the store. It is safe after a ``start`` that raised
    part way (the service is tracked before its start is attempted, and is stopped if the start
    fails), and it is idempotent: after a ``stop`` that raised, the teardown keeps the service and
    everything it may use alive and calls ``stop`` again.
    """
    daemon: object = None
    registry: object = None
    control_registry: object = None
    control: object = None
    store: object = None
    state_dir: Path | None = None
    clock: Callable[[], float] = time.monotonic
    closing: threading.Event | None = None


def own_teardown(lock, errors: list) -> None:
    """Take the ownership of a teardown and return holding it. ``lock`` is a ``threading.RLock``, re-entrant only
    so that its owner can be told. An interruption while it is waited for is kept in ``errors`` and the wait goes
    on: a caller that left here would run its own clean-up while the owner is still tearing down. The thread
    that already owns it is refused: a teardown does not wait for itself. The caller releases the lock."""
    if lock._is_owned():
        raise HookError('a lifecycle method was called from inside another one; only the composition owner calls them')
    while True:
        try:
            lock.acquire()
            return
        except BaseException as exc:  # noqa: BLE001 - kept; ownership is still needed
            errors.append(exc)
            if lock._is_owned():     # taken, and interrupted on the way back from taking it
                return


def raise_kept(errors: list) -> None:
    """What a teardown kept, raised once it is over: an interruption before an ordinary failure, the first of each."""
    for exc in errors:
        if not isinstance(exc, Exception):
            raise exc
    if errors:
        raise errors[0]


class Admission:
    """The composition-wide gate every client call passes: the socket, the page and an in-process call.

    ``enter()`` admits a call, or returns False once the gate is closed. An admitted call holds one
    reference until ``leave()``: through its handler, its transaction and ALL its after-commit
    callbacks. A ``ctx.call`` made inside a handler belongs to that admitted call and takes no new
    admission. ``close()`` stops new admissions; ``drain()`` waits until every admitted call has left.

    ``drain`` has no timeout, on purpose. The daemon's main closes its registry after the composition
    closes, even when closing raises, so closing must not return while a handler, a callback or a
    worker can still use a service or a store: a stalled drain is logged and the wait goes on with
    their resources alive. A timeout is never taken as permission to tear them down. Neither is an
    interruption of the wait: it is kept, the wait goes on, and it is raised once every call has left.

    The gate does nothing until it is ``enabled``, which composition does when a lane has registered a
    handler, a service or an event store. With nothing registered a call is never refused and closing
    waits for nothing, as today.
    """

    def __init__(self) -> None:
        self._changed = threading.Condition()
        self._admitted = 0
        self._closed = False
        self.enabled = False
        self.stall_log_s = 30.0

    def enter(self) -> bool:
        if not self.enabled:
            return True
        with self._changed:
            if self._closed:
                return False
            self._admitted += 1
            return True

    def leave(self) -> None:
        if not self.enabled:
            return
        with self._changed:
            self._admitted -= 1
            if self._admitted <= 0:
                self._changed.notify_all()

    def close(self) -> None:
        with self._changed:
            self._closed = True

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def admitted(self) -> int:
        return self._admitted

    def drain(self) -> None:
        """Return when no admitted call is left, and not before. An interruption while the condition is being
        taken, or while it is waited on, is kept and the drain goes on; the first one is raised once every call
        has left. So an exception from this method is raised only AFTER the last call left, and a caller that
        wants to be sure calls it until it returns."""
        if not self.enabled:
            return
        interrupted = None
        while True:
            try:
                with self._changed:
                    while self._admitted > 0:
                        if not self._changed.wait(self.stall_log_s):
                            log.warning('closing waits for %d admitted call(s); services and stores stay alive',
                                        self._admitted)
                break
            except BaseException as exc:  # noqa: BLE001 - calls may still be inside: the wait is not over
                interrupted = interrupted or exc
                log.warning('the wait for admitted calls was interrupted (%s); it goes on', type(exc).__name__)
        if interrupted is not None:
            raise interrupted


# ------------------------------------------------------------------------------------------ the hooks
class Hooks:
    """Every hook of one composition, with its default. Made by ``compose()``; see the module's text."""

    def __init__(self) -> None:
        self.methods = MethodTable()
        self.commands = CommandTable()
        self._engines = None
        self._single: dict[str, Callable] = {}
        self._start: dict[str, tuple] = {}       # lane name -> (fields, keys, normalize, chooses_engine)
        self._services: list = []
        self._started: list = []
        self._frozen = False
        self.changes: ChangeMarker | None = None
        self.closing = self.methods.closing
        self.gate = Admission()
        self.store = None
        # The lifecycle has one owner at a time: starting, stopping and closing are serialised by this lock.
        self._lifecycle = threading.RLock()   # re-entrant only so that its owner can be told: see own_teardown
        self._closed = False
        self.stop_retry_s = 1.0      # between two tries of a service's stop that raised

    # -- registration -----------------------------------------------------------------------------
    def _open(self, what: str) -> None:
        if self._frozen:
            raise HookError(f'{what}: the hooks are frozen; register from register(hooks), at composition')

    @property
    def engines(self):
        """The engine table (S3). Made on first use, so a process that builds no adapter imports none."""
        if self._engines is None:
            from recursion_core.routes.engines import EngineTable
            self._engines = EngineTable()
            if self._frozen:
                self._engines.freeze()
        return self._engines

    def set(self, name: str, function: Callable) -> None:
        """Register a single-valued hook: one of ``SINGLE``. A second registration fails composition.

        ``event_store`` is the event store's factory, ``factory(state_dir) -> transaction helper``:
        composition calls it once, before any service is started.
        """
        self._open(name)
        if name not in SINGLE:
            raise HookError(f'no hook named {name!r}')
        if name in self._single:
            raise HookError(f'hook {name!r} already has a registrant')
        if not callable(function):
            raise HookError(f'hook {name!r}: not callable')
        self._single[name] = function

    def start_registrant(self, name: str, fields, keys, normalize: Callable, *, chooses_engine: bool = False) -> None:
        """Register a lane's part of a start (S6), and so turn the versioned start path on.

        ``fields``: the request fields the lane honours. A field no registrant declared is ignored, as
        today, and is not part of the request's identity. ``keys``: the launch-snapshot keys it sets,
        from ``SETTABLE_KEYS``; two registrants declaring one key fail composition. ``chooses_engine``:
        the lane (routing) also honours a request that names NO engine and chooses one.
        """
        self._open(f'start registrant {name!r}')
        fields, keys = tuple(fields), tuple(keys)
        if not isinstance(name, str) or not name or name in self._start:
            raise HookError(f'start registrant {name!r}: a name, used once')
        if not callable(normalize):
            raise HookError(f'start registrant {name!r}: not callable')
        if not all(isinstance(field, str) and field for field in fields):
            raise HookError(f'start registrant {name!r}: fields are names')
        reserved = {'tenant', 'engine', 'prompt', 'prompt_sha256', 'client_key',
                    'run_id', 'principal', 'owner', 'intent_version', 'intent_sha256'}
        if reserved & set(fields):
            raise HookError(f'start registrant {name!r}: {sorted(reserved & set(fields))!r} are not extension fields')
        for key in keys:
            if key not in SETTABLE_KEYS:
                raise HookError(f'start registrant {name!r}: {key!r} is not a key a registrant sets')
            taken = next((other for other, entry in self._start.items() if key in entry[1]), None)
            if taken is not None:
                raise HookError(f'start registrant {name!r}: {taken!r} already sets {key!r}')
        if chooses_engine and any(entry[3] for entry in self._start.values()):
            raise HookError(f'start registrant {name!r}: another already chooses the engine')
        self._start[name] = (fields, keys, normalize, bool(chooses_engine))

    def service(self, service) -> None:
        """Register a service (S13). Services start in registration order and stop in reverse."""
        self._open('service')
        name = getattr(service, 'name', None)
        if not isinstance(name, str) or not name:
            raise HookError('a service has a name')
        if any(other.name == name for other in self._services):
            raise HookError(f'service {name!r} is already registered')
        for part in ('start', 'stop'):
            if not callable(getattr(service, part, None)):
                raise HookError(f'service {name!r} has no {part}(context)')
        if not hasattr(service, 'ready'):
            raise HookError(f'service {name!r} has no ready')
        self._services.append(service)

    def enable_changes(self) -> ChangeMarker:
        """Turn the change marker on (S12) and return it. For the one lane that consumes it."""
        self._open('the change marker')
        if self.changes is not None:
            raise HookError('the change marker already has its consumer')
        self.changes = ChangeMarker()
        return self.changes

    def freeze(self) -> None:
        """Close every table. Called by ``compose()`` after the last registrant."""
        if self._frozen:
            return
        names = {service.name for service in self._services}
        for method, needs in self.methods.services_needed().items():
            if needs not in names:
                raise HookError(f'method {method!r} needs the service {needs!r}, which nobody registered')
        self._frozen = True
        self.methods.freeze()
        self.commands.freeze()
        if self._engines is not None:
            self._engines.freeze()
        self.methods.service_ready = self.service_ready
        # The gate guards what a lane brought: a handler, a service, a store. With none of them it stays off.
        self.gate.enabled = bool(self._services or self.methods.registered() or 'event_store' in self._single)

    # -- S5 ---------------------------------------------------------------------------------------
    def authorize(self, principal: Principal, method, params) -> dict | None:
        """None when the call may go on, else the refusal: ``{"code", "detail"}``."""
        authorizer = self._single.get('authorize')
        if authorizer is None:
            if principal.in_run_unit:
                return {'code': 'scope_refused', 'detail': 'peer is inside a run unit'}
            return None
        try:
            verdict = authorizer(principal, method, params)
        except Exception:  # noqa: BLE001 - an authorizer that fails refuses
            log.exception('the authorizer failed on %s; the call is refused', method)
            return {'code': 'scope_refused', 'detail': 'authorizer_failed'}
        if verdict is None:
            return None
        if isinstance(verdict, str) and verdict:
            return {'code': verdict, 'detail': ''}
        if (isinstance(verdict, tuple) and len(verdict) == 2 and isinstance(verdict[0], str) and verdict[0]
                and isinstance(verdict[1], str)):
            return {'code': verdict[0], 'detail': verdict[1]}
        log.error('the authorizer answered %r on %s; the call is refused', type(verdict).__name__, method)
        return {'code': 'scope_refused', 'detail': 'authorizer_failed'}

    # -- S4, S10 ----------------------------------------------------------------------------------
    def working_directory(self, ctx):
        """The folder the run's unit works in, or None for today's. Raises ``HookRefused``."""
        provider = self._single.get('working_directory')
        if provider is None:
            return None
        try:
            folder = provider(ctx)
        except Exception:  # noqa: BLE001
            log.exception('the working-directory provider failed for %s; the launch is refused', getattr(ctx, 'run_id', '?'))
            raise HookRefused('working_directory_failed') from None
        if folder is None:
            return None
        if not isinstance(folder, (str, Path)) or not str(folder):
            log.error('the working-directory provider answered %r; the launch is refused', type(folder).__name__)
            raise HookRefused('working_directory_failed')
        return Path(folder)

    def launch_check(self, ctx, executable) -> None:
        """Returns when the launch may go on. Raises ``HookRefused`` when the check refuses it, or fails."""
        check = self._single.get('launch_check')
        if check is None:
            return
        try:
            verdict = check(ctx, executable)
        except Exception:  # noqa: BLE001
            log.exception('the launch check failed for %s; the launch is refused', getattr(ctx, 'run_id', '?'))
            raise HookRefused('launch_check_failed') from None
        if verdict is None:
            return
        if isinstance(verdict, str) and verdict:
            raise HookRefused('launch_check', verdict)
        log.error('the launch check answered %r; the launch is refused', type(verdict).__name__)
        raise HookRefused('launch_check_failed')

    # -- S7 ---------------------------------------------------------------------------------------
    def credentials(self, connection_id, env_name):
        """The secret for a connection, or ``KeyError(connection_id)``. Never None and never empty."""
        source = self._single.get('credentials')
        if source is None:
            return no_custody(connection_id, env_name)
        try:
            secret = source(connection_id, env_name)
        except KeyError:
            raise KeyError(connection_id) from None
        except Exception:  # noqa: BLE001 - a source that fails has no credential to give
            log.exception('the credential source failed for a connection; it is treated as having none')
            raise KeyError(connection_id) from None
        if not isinstance(secret, str) or not secret:
            raise KeyError(connection_id)
        return secret

    # -- S8, S9, S11 ------------------------------------------------------------------------------
    def read_result(self, run_id):
        reader = self._single.get('read_result')
        if reader is None:
            return None
        try:
            result = reader(run_id)
        except Exception:  # noqa: BLE001
            log.exception('the result reader failed for %s; treated as no result', run_id)
            return None
        return result if isinstance(result, dict) else None

    def drive_owner(self, ctx, request):
        chooser = self._single.get('drive_owner')
        if chooser is None:
            return None
        try:
            owner = chooser(ctx, request)
        except Exception:  # noqa: BLE001
            log.exception('the drive owner failed before accepting %s; today\'s path drives it', getattr(ctx, 'run_id', '?'))
            return None
        if owner is not None and not callable(getattr(owner, 'run', None)):
            log.error('the drive owner answered something with no run(); today\'s path drives %s', getattr(ctx, 'run_id', '?'))
            return None
        return owner

    def page_methods(self) -> frozenset:
        names = self._single.get('page_methods')
        if names is None:
            return frozenset()
        try:
            listed = names()
        except Exception:  # noqa: BLE001
            log.exception('page_methods failed; the page may call nothing')
            return frozenset()
        if not isinstance(listed, (set, frozenset)) or not all(isinstance(name, str) for name in listed):
            log.error('page_methods answered %r; the page may call nothing', type(listed).__name__)
            return frozenset()
        return frozenset(listed)

    # -- S12 --------------------------------------------------------------------------------------
    def mark_changed(self, run_id) -> None:
        """What the daemon registry calls after a write returned normally. Nothing, unless a lane turned
        the marker on. It cannot raise into the writer and it never waits."""
        marker = self.changes
        if marker is not None:
            try:
                marker.mark(run_id)
            except Exception:  # noqa: BLE001 - a mark that is lost is found by the next scan
                pass

    # -- S6 ---------------------------------------------------------------------------------------
    @property
    def start_on(self) -> bool:
        """Whether the versioned start path runs: true once any lane registered a start registrant."""
        return bool(self._start)

    def start_order(self) -> tuple:
        """The registrants' names in the order they run: routing, workspace, then the others by name."""
        first = [name for name in ('routing', 'workspace') if name in self._start]
        return tuple(first + sorted(name for name in self._start if name not in first))

    def declared_fields(self) -> frozenset:
        return frozenset(field for fields, _, _, _ in self._start.values() for field in fields)

    @property
    def engine_may_be_chosen(self) -> bool:
        return any(entry[3] for entry in self._start.values())

    def declared_present(self, params: dict) -> dict:
        """The declared extension fields that the request carries with a value. A missing field, a null
        one and one no registrant declared are the same: not here."""
        return {field: params[field] for field in sorted(self.declared_fields()) if params.get(field) is not None}

    def start_intent(self, params: dict, prompt_sha256: str) -> str:
        """The identity of a keyed start request, version 1: the SHA-256 of the UTF-8 bytes of the canonical
        JSON (RFC 8785: keys sorted, no insignificant space) of one object: ``engine`` as supplied, or
        null when the request names none; ``prompt_sha256``; and each DECLARED extension that is present
        and not null in the validated request. Undeclared, absent and null extensions are omitted. It is
        computed by the daemon from what the client sent, BEFORE any registrant normalizes or routes the
        request, and never from a registrant's output. ``ValueError`` when a declared field has no such
        text. With no registrant (the versioned path off) there is no intent at all: the identity is the
        legacy three members, exactly."""
        return hashlib.sha256(self.start_intent_text(params, prompt_sha256).encode('utf-8')).hexdigest()

    def start_intent_text(self, params: dict, prompt_sha256: str) -> str:
        """The exact preimage of ``start_intent``."""
        intent = {**self.declared_present(params),
                  'engine': params.get('engine'), 'prompt_sha256': prompt_sha256}
        return canonical_text(intent)

    def launch_snapshot(self, principal: Principal, request: dict):
        """Run the start registrants for a NEW start: ``(launch, None)``, or ``(None, refusal answer)``.

        ``request`` is the client's params, which a registrant validates and never copies through. The
        snapshot starts from what the server alone knows: no route entry, decision, workspace, level,
        session or turn; ``owner`` is ``host``, or the plugin's id only when the principal IS that
        plugin; ``budget`` is a cap of 0. Each registrant may set the keys it declared and no other.
        """
        launch = {'route_entry': None, 'route_decision': None, 'workspace': None, 'containment_level': None,
                  'session_id': None, 'turn_id': None,
                  'owner': principal.id if principal.kind == 'plugin' and principal.id else 'host',
                  'budget': {'cap_usd': 0}}
        for name in self.start_order():
            _, keys, normalize, _ = self._start[name]
            try:
                part = normalize(principal, dict(request))
            except Exception:  # noqa: BLE001 - a registrant that fails refuses the start
                log.exception('start registrant %s failed; the start is refused', name)
                return None, {'code': 'unavailable', 'detail': 'start_registrant_failed'}
            if isinstance(part, Refusal):
                return None, part.answer()
            if not isinstance(part, dict) or not set(part) <= set(keys):
                log.error('start registrant %s answered keys it did not declare; the start is refused', name)
                return None, {'code': 'unavailable', 'detail': 'start_registrant_failed'}
            launch.update(part)
        return launch, None

    # -- S13 --------------------------------------------------------------------------------------
    # Lifecycle operations have one owner. A concurrent or repeated close waits for the owner's completed
    # teardown; it never independently closes resources. Once closing begins, no store or service can be
    # opened or started. A service is tracked before its start is attempted. Its stop is safe after partial
    # startup and is idempotent. A stop failure is retained as an error, not interpreted as proof that its
    # workers joined. Keep the service and all resources it may use alive until stop confirms quiescence. Log
    # a stalled cleanup and keep waiting. Propagate retained teardown errors only after all resource users
    # have left.
    #
    # Interruption while acquiring lifecycle ownership, closing admission, acquiring the drain condition, or
    # waiting for admitted calls is retained. No close caller returns or raises until the teardown owner has
    # finished and every resource user has left. An exception from a drain attempt is never itself evidence
    # that admission is empty.
    #
    # Lifecycle methods are called only by the composition owner, never from an admitted handler or callback,
    # a service's start/stop, or a worker that teardown must join. A shutdown request is handed to that owner;
    # it does not recursively execute teardown. (A lifecycle method entered from inside another one, on the
    # same thread, raises HookError.)
    def open_store(self, state_dir):
        """Call the event store's factory, once, before any service starts. None when no lane registered one."""
        with self._lifecycle:
            if self.closing.is_set() or self._closed:
                raise HookError('closing has begun: no store is opened')
            factory = self._single.get('event_store')
            if factory is not None:
                self.store = factory(state_dir)
                self.methods.store = self.store
            return self.store

    def start_services(self, context: ServiceContext) -> None:
        """Start the registered services in order. Each is tracked BEFORE its start is attempted, so one that
        started a worker and then raised is stopped like the others: it and the ones already started are
        stopped in reverse, and the start's own error is raised. No thread is left with a closed store."""
        with self._lifecycle:
            for service in self._services:
                if self.closing.is_set() or self._closed:
                    log.error('closing began while services were starting; stopping the ones that did')
                    self._stop_all(context, [])
                    raise HookError('closing has begun: no service is started')
                self._started.append(service)
                try:
                    service.start(context)
                except BaseException:
                    log.exception('service %s did not start; stopping it and the ones that did', service.name)
                    self._stop_all(context, [])
                    raise

    def stop_services(self, context: ServiceContext) -> None:
        """Stop every started service, in reverse order, each until its stop has returned. The first failure
        is raised after the last service has stopped."""
        errors: list = []
        own_teardown(self._lifecycle, errors)
        try:
            self._stop_all(context, errors)
        finally:
            self._lifecycle.release()
        raise_kept(errors)

    def _stop_all(self, context, errors: list) -> None:
        """Stop every tracked service and return only when none is left. An interruption, wherever in the
        stopping it lands (inside a service's ``stop``, in the wait before it is tried again, or between two
        steps), is kept and the stopping goes on at once with the services still tracked. This is the one place
        that keeps it, so none is lost behind an ordinary failure."""
        while self._started:
            try:
                self._stop_started(context, errors)
            except BaseException as exc:  # noqa: BLE001 - services are still tracked: the teardown is not over
                errors.append(exc)

    def _stop_started(self, context, errors: list) -> None:
        """Stop the tracked services, newest first. A service leaves the list only when its ``stop`` has
        RETURNED, which is what says its workers have joined. A ``stop`` that raises is kept as an error (the
        first one of a service) and tried again after ``stop_retry_s``, for as long as it takes: nothing the
        service may use is torn down on the strength of a failure. An interruption is not caught here: it goes
        to ``_stop_all``, which keeps it and comes back. The caller holds the lifecycle lock."""
        while self._started:
            service = self._started[-1]
            failed = False
            while True:
                try:
                    service.stop(context)       # a normal return confirms all of its resource users joined
                    break
                except Exception as exc:        # noqa: BLE001 - not proof that anything stopped
                    if not failed:
                        failed = True
                        errors.append(exc)
                        log.exception('service %s did not stop; it and what it uses stay alive, and its stop '
                                      'is tried again', service.name)
                    else:
                        log.warning('service %s has still not stopped (%s); waiting', service.name, type(exc).__name__)
                time.sleep(self.stop_retry_s)
            self._started.pop()                 # only after that confirmation

    def service_ready(self, name: str) -> bool:
        return any(service.name == name and bool(service.ready) for service in self._started)

    def begin_close(self) -> None:
        """First step of closing: no new client call is admitted, and everyone who waits is told. From here on
        no store is opened and no service is started."""
        self.gate.close()
        self.closing.set()

    def finish_close(self, context: ServiceContext) -> None:
        """After the transports stopped admitting: wait for every admitted call to leave, with its services
        and stores alive; then stop the services in reverse, each until its stop has returned; then close the
        store. A teardown error, or an interruption, is raised only after all of that.

        One caller owns the teardown. Another that calls while it runs, or after it, waits for the owner to
        finish and then returns: it closes nothing itself. A caller that is interrupted while it waits for the
        owner goes on waiting and raises its interruption only once the owner has finished."""
        errors: list = []
        own_teardown(self._lifecycle, errors)
        try:
            # The owned teardown. Whatever interrupts a step of it is kept and the teardown goes on from that
            # step: closing admission again changes nothing, the drain is asked again until it RETURNS, the
            # services still tracked are stopped, and the store is closed while it is still the store.
            while not self._closed:
                try:
                    self.begin_close()
                    self.gate.drain()               # returns only when every admitted call has left
                    self._stop_all(context, errors)
                    store = self.store
                    close = getattr(store, 'close', None)
                    if callable(close):
                        try:
                            close()
                        except Exception as exc:  # noqa: BLE001
                            log.exception('the event store did not close cleanly')
                            errors.append(exc)
                    self.store = None
                    self.methods.store = None
                    self._closed = True
                except BaseException as exc:  # noqa: BLE001 - proves no quiescence: the owned teardown goes on
                    errors.append(exc)
        finally:
            self._lifecycle.release()
        raise_kept(errors)


# ---- the registrants, one guarded static import each -----------------------------------------------------------
# Each function looks for its registrant with literal names and imports it with a literal statement. None when
# the lane is not installed, or its package has no such module. An installed package that fails while it is
# imported raises here: it is never read as absence. The order is REGISTRANTS'.
def _load_keys_hooks():
    if (importlib.util.find_spec('recursion_core.keys') is None
            or importlib.util.find_spec('recursion_core.keys.hooks') is None):
        return None
    import recursion_core.keys.hooks as module
    return module


def _load_events_hooks():
    if (importlib.util.find_spec('recursion_core.events') is None
            or importlib.util.find_spec('recursion_core.events.hooks') is None):
        return None
    import recursion_core.events.hooks as module
    return module


def _load_workspace_hooks():
    if (importlib.util.find_spec('recursion_core.workspace') is None
            or importlib.util.find_spec('recursion_core.workspace.hooks') is None):
        return None
    import recursion_core.workspace.hooks as module
    return module


def _load_routing_hooks():
    if (importlib.util.find_spec('recursion_core.routing') is None
            or importlib.util.find_spec('recursion_core.routing.hooks') is None):
        return None
    import recursion_core.routing.hooks as module
    return module


def _load_quota_hooks():
    if (importlib.util.find_spec('recursion_core.quota') is None
            or importlib.util.find_spec('recursion_core.quota.hooks') is None):
        return None
    import recursion_core.quota.hooks as module
    return module


def _load_approvals_hooks():
    if (importlib.util.find_spec('recursion_core.approvals') is None
            or importlib.util.find_spec('recursion_core.approvals.hooks') is None):
        return None
    import recursion_core.approvals.hooks as module
    return module


def _load_sessions_hooks():
    if (importlib.util.find_spec('recursion_core.sessions') is None
            or importlib.util.find_spec('recursion_core.sessions.hooks') is None):
        return None
    import recursion_core.sessions.hooks as module
    return module


def _load_launch_check_hooks():
    if (importlib.util.find_spec('recursion_core.launch_check') is None
            or importlib.util.find_spec('recursion_core.launch_check.hooks') is None):
        return None
    import recursion_core.launch_check.hooks as module
    return module


def _load_setup_hooks():
    if (importlib.util.find_spec('recursion_core.setup') is None
            or importlib.util.find_spec('recursion_core.setup.hooks') is None):
        return None
    import recursion_core.setup.hooks as module
    return module


def _load_gui_hooks():
    if (importlib.util.find_spec('recursion_core.gui') is None
            or importlib.util.find_spec('recursion_core.gui.hooks') is None):
        return None
    import recursion_core.gui.hooks as module
    return module


def _load_remote_hooks():
    if (importlib.util.find_spec('recursion_core.remote') is None
            or importlib.util.find_spec('recursion_core.remote.hooks') is None):
        return None
    import recursion_core.remote.hooks as module
    return module


def _load_control_mcp_hooks():
    if (importlib.util.find_spec('recursion_core.control.mcp') is None
            or importlib.util.find_spec('recursion_core.control.mcp.hooks') is None):
        return None
    import recursion_core.control.mcp.hooks as module
    return module


def _load_control_scopes():
    if importlib.util.find_spec('recursion_core.control.scopes') is None:
        return None
    import recursion_core.control.scopes as module
    return module


def _load_routes_driver_hooks():
    if importlib.util.find_spec('recursion_core.routes.driver_hooks') is None:
        return None
    import recursion_core.routes.driver_hooks as module
    return module


def _load_plugin_security_host_hooks():
    if importlib.util.find_spec('recursion_core.plugin_security.host_hooks') is None:
        return None
    import recursion_core.plugin_security.host_hooks as module
    return module


def _load_plugin_security_sign_hooks():
    if importlib.util.find_spec('recursion_core.plugin_security.sign_hooks') is None:
        return None
    import recursion_core.plugin_security.sign_hooks as module
    return module


_LOADERS = (
    ('recursion_core.keys.hooks', _load_keys_hooks),
    ('recursion_core.events.hooks', _load_events_hooks),
    ('recursion_core.workspace.hooks', _load_workspace_hooks),
    ('recursion_core.routing.hooks', _load_routing_hooks),
    ('recursion_core.quota.hooks', _load_quota_hooks),
    ('recursion_core.approvals.hooks', _load_approvals_hooks),
    ('recursion_core.sessions.hooks', _load_sessions_hooks),
    ('recursion_core.launch_check.hooks', _load_launch_check_hooks),
    ('recursion_core.setup.hooks', _load_setup_hooks),
    ('recursion_core.gui.hooks', _load_gui_hooks),
    ('recursion_core.remote.hooks', _load_remote_hooks),
    ('recursion_core.control.mcp.hooks', _load_control_mcp_hooks),
    ('recursion_core.control.scopes', _load_control_scopes),
    ('recursion_core.routes.driver_hooks', _load_routes_driver_hooks),
    ('recursion_core.plugin_security.host_hooks', _load_plugin_security_host_hooks),
    ('recursion_core.plugin_security.sign_hooks', _load_plugin_security_sign_hooks),
)


def bare() -> Hooks:
    """The hooks with nothing registered: every default, and the tables closed. What a composition that is
    given no hooks uses, and what a test builds on."""
    hooks = Hooks()
    hooks.freeze()
    return hooks


def compose() -> Hooks:
    """The hooks of one composition: every registrant that is installed has registered, and the tables are frozen."""
    hooks = Hooks()
    for name, load in _LOADERS:
        try:
            # Absence is decided by find_spec before the import. An installed ancestor that fails to import
            # raises inside the loader and fails composition, and so does anything the registrant raises.
            module = load()
            if module is None:
                continue        # the lane is not installed, or its package has no such module
            module.register(hooks)
        except HookError:
            raise
        except Exception as exc:
            raise HookError(f'registrant {name}: {type(exc).__name__}: {exc}') from exc
    hooks.freeze()
    return hooks
