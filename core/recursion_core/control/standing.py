"""One view of everything standing, and one Stop that ends what it can of it.

``view(control, params)`` answers ``standing.list``: every standing record the core's own stores hold, read
from those stores at the time of the call, never from a plugin's own account. ``stop_all(control)`` answers
``standing.stop``: one Stop that walks the same table, ends what each store holds that a Stop can end, says
``stopped`` only for what it saw cleaned up, and says what still stands and why. ``control`` is the daemon's
``DaemonControl`` (``daemon/control_adapter.py``); the socket (an owner) and the page (the device) reach both
through it, as they reach ``run.stop``.

THE TABLE OF STORES (``STORES``) is the one place a store of standing records is named. Each entry carries the
store's row reader and, beside it, EITHER its stop handler OR ``HOLDS_NO_WORK``, the value that says the store
holds nothing a Stop could end. The view and the Stop read the same table. Which part of the composed daemon
each store reads is named in ``HELD_BY_STORE``, and every other part that holds records says in
``HOLDS_NO_STANDING`` why it holds nothing that stands; a test walks what the daemon really composes and fails
on a part named in neither (``daemon/tests/test_standing.py``).

THE ROW KINDS are a tagged union: every row carries ``kind``, one of ``ROW_KINDS`` (the kinds of ``STORES``).
A row of any other kind is never dropped silently and never shown as if it were known: it is refused,
counted under ``refused`` with a sentence a person can read, and the rest of the view stays whole.

THE TABLE OF METHODS (``STARTS_WORK``) marks every method of the method table (``control.methods.METHODS``)
as one that starts standing work or not; ``HOST_CALLS_STARTING_WORK`` is the same mark for the calls a plugin
host may make, none on this base. A test fails on a method marked in neither way.

THE DOOR (``door``): both methods are for the principal kinds owner and device only. ``Hooks.authorize``
asks ``door`` before anything else, whether an authorizer is registered or not, and the published authorizer
(``control/scopes.py``) asks the same; every other method is not touched by it.

WHAT THE VIEW CANNOT SEE is said in its own answer and in the Stop's (``not_seen``): work an engine keeps for
later in its own files (an engine's own scheduled tasks, for one) is held by no store of the core, so the view
does not show it and Stop does not remove it; and code a loaded plugin started by itself inside the daemon's
process is not held by any store either.

Stop stays fast and whole: each store is stopped on a thread of its own, the runs one per thread, and the
answer comes after at most ``STOP_WAIT_S``, whatever a store does. A store whose handler hangs or raises is
named as not answering; it holds no other store's Stop. What still stands in a store that holds no work is read
with ``wait=False``: such a reader never waits for a lock, and a store that is busy is said as busy, so saying
what stands never makes Stop wait. Every sentence a person reads is in ``TEXT``.
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Mapping
from typing import NamedTuple

log = logging.getLogger(__name__)

VIEW_METHOD, STOP_METHOD = 'standing.list', 'standing.stop'

# ---- figures. #589's settings file is not at this base: each is ONE named value here, and becomes a row of
# the person's one settings file with this value as its shipped default.
VIEW_PAGE_ROWS = None   # rows one view answer shows before it pages; None: no limit. A call's ``limit`` wins.
VIEW_READ_WAIT_S = 5.0  # how long the view waits for its stores (read side by side) before it says which did not answer
STOP_WAIT_S = 10.0      # how long one Stop waits for seen cleanup: twice run.stop's own confirm wait (5 s)
STOP_RUN_THREADS = 16   # runs one Stop ends at the same time; None: one thread for every run

HOLDS_NO_WORK = 'holds_no_work'   # a store's stop entry when it holds nothing a Stop could end
LIVE_RUN_STATES = ('starting', 'running', 'stop_pending')   # run.record.v1 states of a run that still stands

# ---- the door: who may ask for the view and the Stop, registered authorizer or not.
DOOR_METHODS = (VIEW_METHOD, STOP_METHOD)
DOOR_KINDS = ('owner', 'device')
DOOR_REFUSED = 'standing_not_granted_to_kind'


def door(principal, method):
    """None when ``method`` is not one of the two, or the principal's kind is owner or device; else the refusal
    ``{"code": "scope_refused", "detail": "standing_not_granted_to_kind"}``. A Stop of everything would end a
    person's work, and the view shows what other plugins hold."""
    if not isinstance(method, str) or str.__str__(method) not in DOOR_METHODS:
        return None
    kind = getattr(principal, 'kind', None)
    if type(kind) is str and kind in DOOR_KINDS:
        return None
    return {'code': 'scope_refused', 'detail': DOOR_REFUSED}


# ---- every sentence a person reads, in one table. {placeholders} are filled from the row.
TEXT = {
    'run': 'Run {id} {doing}. Stop ends it.',
    'plugin_host': 'Plugin {id} is loaded. It runs only when the daemon asks it; Stop leaves it loaded.',
    'plugin_host_busy': 'Plugin {id} is loaded and a read of it passed its deadline and still runs. '
                        'Only a restart of the daemon ends that read.',
    'approval': 'Approval {id} waits for an answer, for {left} more seconds; then it expires by itself. Nothing '
                'it asks for runs until it is answered, and this release has no place to answer it.',
    'approval_granted': 'Approval {id} was granted and waits to be used, for {left} more seconds at most.',
    'budget': 'Budget {id} has {spent} spent against it, in the smallest unit of its currency (cents, for one). '
              'Its limit is in its signed profile; Stop does not change it.',
    'row_kind_unknown': 'The {store} store holds a kind of standing work this release does not show yet. '
                        'It is counted here and not hidden; a newer release of Recursion shows it.',
    'store_failed': 'The {store} store could not be read, so what it holds is not shown. Ask again.',
    'store_no_answer': 'The {store} store did not answer within {wait:g} seconds, so what it holds is not shown. '
                       'Ask again.',
    'store_still_reading': 'The {store} store has not answered an earlier ask yet, so what it holds is not shown. '
                           'Ask again.',
    'view_nothing': 'Nothing is standing.',
    'view_some': '{count} standing: {parts}.',
    'view_incomplete': 'Not every store answered, so this may not be everything.',
    'not_seen_engine': 'What an engine keeps for later in its own files, its own scheduled tasks for one, is held '
                       'by no store of Recursion: the list of everything standing does not show it, and Stop does '
                       'not remove it.',
    'not_seen_plugin': 'Code a loaded plugin starts by itself inside the daemon is held by no store of Recursion: '
                       'the list of everything standing does not show it, and only a restart of the daemon ends it.',
    'stop_nothing': 'Nothing was standing that Stop ends.',
    'stop_done': 'Stopped {parts}.',
    'stop_none': 'Nothing could be seen ended.',
    'stop_left': 'What Stop could not see ended: {why}. Ask Stop again; if it stays, restart the daemon.',
    'stop_stays': 'Still standing, which Stop does not end: {parts}.',
    'stop_stays_unread': 'The {store} store was busy, so what still stands in it is not said here; '
                         'look at everything standing.',
    'stop_run_pending': 'run {id}, whose processes may still be running',
    'stop_run_gone': 'run {id}, which was already gone',
    'stop_run_no_answer': 'run {id}, whose Stop the daemon did not answer',
    'stop_run_refused': 'run {id}, whose Stop was refused',
    'stop_plugin_busy': 'plugin {id}, a read of which passed its deadline and still runs inside the daemon',
    'stop_store_failed': 'the {store} store, whose Stop failed',
    'stop_store_no_answer': 'the {store} store, which did not answer within {wait:g} seconds',
    'nothing': 'nothing',
}
# How a run's record state is said in a sentence.
RUN_DOING = {'starting': 'is starting', 'running': 'is running', 'stop_pending': 'is being stopped'}
# How a count of each kind is said: (one, many).
KIND_WORDS = {'run': ('run', 'runs'), 'plugin_host': ('plugin host', 'plugin hosts'),
              'approval': ('approval', 'approvals'), 'budget': ('budget', 'budgets')}
# How a count of what still stands after a Stop is said, by kind: (one, many).
STAYS_WORDS = {'plugin_host': ('plugin host stays loaded', 'plugin hosts stay loaded'),
               'approval': ('approval still waits', 'approvals still wait'),
               'budget': ('budget keeps the spend recorded against it', 'budgets keep the spend recorded against them')}


class StoreBusy(Exception):
    """A reader asked with ``wait=False`` found its store busy: it did not wait, and read nothing."""


def _text(key, **fields):
    return TEXT[key].format(**fields)


def _joined(parts):
    if not parts:
        return TEXT['nothing']
    return parts[0] if len(parts) == 1 else ', '.join(parts[:-1]) + ' and ' + parts[-1]


def _counted(counts, words=None):
    """{'run': 3, 'plugin_host': 1} -> '3 runs and 1 plugin host', in the order of the table of stores."""
    words = KIND_WORDS if words is None else words
    parts = []
    for kind in (*ROW_KINDS, *(k for k in counts if k not in ROW_KINDS)):
        n = counts.get(kind, 0)
        if n:
            one, many = words.get(kind, (kind, kind))
            parts.append(f'{n} {one if n == 1 else many}')
    return _joined(parts)


# ---- the readers and the stop handlers of today's stores -----------------------------------------------
def _read_runs(control, wait=True):
    rows = []
    for rec in control.registry.list():
        state = rec.get('state')
        if state in LIVE_RUN_STATES:
            run_id = rec.get('run_id')
            rows.append({'kind': 'run', 'id': run_id, 'state': state,
                         'text': _text('run', id=run_id, doing=RUN_DOING[state])})
    return rows


def _stop_runs(control):
    """End every run that still stands, through run.stop (the same request, confirm and attestation as any
    Stop). ``stopped`` only when run.stop answered stopped: its cleanup was seen."""
    targets = [rec.get('run_id') for rec in control.registry.list() if rec.get('state') in LIVE_RUN_STATES]
    outcomes = [None] * len(targets)
    limit = threading.BoundedSemaphore(STOP_RUN_THREADS) if STOP_RUN_THREADS else None

    def one(i, run_id):
        try:
            answer = control.call('run.stop', {'run_id': run_id})
        except Exception as exc:  # noqa: BLE001 - a failed Stop of one run is said, never taken for stopped
            answer = {'code': 'unavailable', 'detail': type(exc).__name__}
        finally:
            if limit is not None:
                limit.release()
        code = answer.get('code') if isinstance(answer, dict) else None
        if code == 'stopped':
            outcomes[i] = {'kind': 'run', 'id': run_id, 'outcome': 'stopped'}
            return
        if code == 'pending':
            reason, key = str(answer.get('reason') or answer.get('detail') or 'not_confirmed'), 'stop_run_pending'
        else:
            reason = str(code)
            key = {'run_unknown': 'stop_run_gone', 'unavailable': 'stop_run_no_answer'}.get(code, 'stop_run_refused')
        outcomes[i] = {'kind': 'run', 'id': run_id, 'outcome': 'not_stopped', 'reason': reason,
                       'text': _text(key, id=run_id)}

    threads = []
    for i, run_id in enumerate(targets):
        if limit is not None:
            limit.acquire()
        thread = threading.Thread(target=one, args=(i, run_id), name='recursion-stop-run', daemon=True)
        thread.start()
        threads.append(thread)
    for thread in threads:
        thread.join()
    return outcomes


def _plugin_host(control):
    return getattr(control, 'plugins', None)


def _loaded_plugins(control):
    plugins = _plugin_host(control)
    if plugins is None:
        return []
    return [record for record in plugins.records if record.get('state') == 'loaded']


def _plugin_busy(control, plugin_id):
    return getattr(control.plugins, '_stuck', None) == plugin_id


def _read_plugin_hosts(control, wait=True):
    rows = []
    for record in _loaded_plugins(control):
        plugin_id = record.get('plugin_id')
        busy = _plugin_busy(control, plugin_id)
        rows.append({'kind': 'plugin_host', 'id': plugin_id, 'state': 'busy' if busy else 'loaded',
                     'text': _text('plugin_host_busy' if busy else 'plugin_host', id=plugin_id)})
    return rows


def _stop_plugin_hosts(control):
    """A loaded plugin runs only when the daemon calls it, and every call has its deadline: Stop ends nothing of
    it. A loaded plugin stays (and is said so); a call that passed its deadline and has not returned is said as
    not ended, never counted stopped."""
    outcomes = []
    for record in _loaded_plugins(control):
        plugin_id = record.get('plugin_id')
        if _plugin_busy(control, plugin_id):
            outcomes.append({'kind': 'plugin_host', 'id': plugin_id, 'outcome': 'not_stopped',
                             'reason': 'plugin_call_running', 'text': _text('stop_plugin_busy', id=plugin_id)})
        else:
            outcomes.append({'kind': 'plugin_host', 'id': plugin_id, 'outcome': 'stays'})
    return outcomes


def _read_approvals(control, wait=True):
    """The approvals the plugin host's gate holds NOW: waiting for an answer or granted and not used, and not
    past their expiry. An expired, used or refused approval is not shown."""
    gate = getattr(_plugin_host(control), 'gate', None)
    if gate is None:
        return []
    live = gate.waiting(wait=wait)
    if live is None:
        raise StoreBusy('approvals')
    rows = []
    for item in live:
        key = 'approval' if item['state'] == 'pending' else 'approval_granted'
        rows.append({'kind': 'approval', 'id': item['approval_id'], 'digest': item['digest'], 'state': item['state'],
                     'text': _text(key, id=item['approval_id'], left=item['left_s'])})
    return rows


def _read_budgets(control, wait=True):
    """The spend recorded against each signed budget in the plugin host's authority ledger. The limit of each
    budget is in its signed profile, which the ledger does not keep."""
    authority = getattr(_plugin_host(control), 'authority', None)
    if authority is None:
        return []
    spent = authority.spent(wait=wait)
    if spent is None:
        raise StoreBusy('budgets')
    return [{'kind': 'budget', 'id': profile_id, 'spent_minor': amount,
             'text': _text('budget', id=profile_id, spent=amount)} for profile_id, amount in spent]


class Store(NamedTuple):
    name: str                       # how the store is named in the view and in its sentences
    kind: str                       # the row kind its reader returns
    read: Callable                  # read(control, wait=True) -> rows; with wait=False it never waits for a lock
    stop: object                    # stop(control) -> outcomes, or HOLDS_NO_WORK


# THE TABLE OF STORES: every store of the core that holds standing records on this base. A routine's row joins
# when the core holds routine records.
STORES = (
    Store('runs', 'run', _read_runs, _stop_runs),
    Store('plugin hosts', 'plugin_host', _read_plugin_hosts, _stop_plugin_hosts),
    Store('approvals', 'approval', _read_approvals, HOLDS_NO_WORK),
    Store('budgets', 'budget', _read_budgets, HOLDS_NO_WORK),
)
ROW_KINDS = tuple(store.kind for store in STORES)

# What the composed daemon holds, part by part ('Class.attribute', for every attribute that holds records: a
# dict, a list, a set, a deque or a database). A part a store reads names that store; every other part says why
# what it holds does not stand. The test walks what compose_control really builds and fails on a part in neither.
HELD_BY_STORE = {
    'DurableRunRegistry._runs': 'runs',
    'Registry.db': 'runs',
    'PluginHost.records': 'plugin hosts',
    'ApprovalGate._approvals': 'approvals',
    'Authority._db': 'budgets',
}
HOLDS_NO_STANDING = {
    'DurableRunRegistry._starts': 'starts in flight: each is a run record of the runs store first',
    'Daemon._stop_resend': 'when a Stop the sweep could not send is sent again: runs of the runs store',
    'DaemonControl._stop_retry': 'when a Stop is sent again: runs of the runs store',
    'DaemonControl._routes': 'the route file: what can be started, not what stands',
    'ControlAPI._failed': 'runs of the runs store whose start failed',
    'ControlAPI._launched': 'runs of the runs store this daemon launched',
    'ControlAPI._stale_stops': 'Stops of runs of the runs store',
    'ControlAPI._stop_wanted': 'Stops asked for runs of the runs store',
    'ControlAPI._start_locks': 'locks of starts',
    'OwnerLockTable._count': 'locks',
    'OwnerLockTable._owner': 'locks',
    'RouteStarter._active': 'runs of the runs store being launched',
    'RouteStarter.results': 'answers of starts of runs of the runs store',
    'RouteStarter.failures': 'failures of starts of runs of the runs store',
    'RouteStarter._routes': 'the route file: what can be started, not what stands',
    'RouteService._adapters': 'the engine adapters: what can be started, not what stands',
    'RouteService._limits': 'limits on starts, not work',
    'ClaudeCodeAdapter._parent_env': "the daemon's environment a start is built from, not work",
    'CodexAdapter._parent_env': "the daemon's environment a start is built from, not work",
    'EngineUnitManager._pending': 'unit settings of a start under way: its run is a record of the runs store',
    'ConnectionBreaker._db': 'the breaker store: limits on connections, not work',
    'ConnectionBreaker._keys': 'limits on connections, not work',
    'ConnectionBreaker._leases': 'limits on connections, not work',
    'ConnectionBreaker._short': 'limits on connections, not work',
    'DurableRouteBreaker._leases': 'limits on connections, not work',
    'DurableRouteBreaker._restored': 'limits on connections, not work',
    'UnixControlServer._conns': 'connections to the control socket, not work',
    'UnixControlServer._workers': 'threads answering the control socket, not work',
    'UnixControlServer.refused': 'connections the socket refused',
    'PluginHost._operators': 'what loaded plugins answer: part of the plugin hosts store',
    'PluginHost._panels': 'panels of loaded plugins: part of the plugin hosts store',
    'PluginHost.degraded': 'calls of loaded plugins that passed their deadline: the plugin hosts store says them',
    'PluginHost.pending_approvals': 'a log of the approvals drawn, for the inventory count: what waits now is '
                                    'read from the gate',
    'ApprovalGate._tombstones': 'approvals that ended: used, refused or expired',
    'ApprovalGate._expiry': 'when each approval of the gate expires',
    'ApprovalGate._vetoed': 'refusals a person made: decided, not waiting',
    'Authority._verified': 'signatures already checked',
}

# THE TABLE OF METHODS: does a call of the method start standing work? Every row of the method table is
# written here by hand, so a row added to the table and not here fails its test.
_YES, _NO = True, False
STARTS_WORK = {
    'run.start': _YES, 'run.status': _NO, 'run.list': _NO, 'run.stop': _NO, 'route.list': _NO, 'plugin.list': _NO,
    'session.start': _YES, 'session.turn': _YES, 'session.steer': _NO, 'session.queue': _YES,
    'session.interrupt': _NO, 'session.resume': _YES, 'session.fork': _YES, 'session.export': _NO,
    'session.list': _NO, 'session.status': _NO, 'session.stop': _NO, 'queue.update': _NO, 'queue.cancel': _NO,
    'approval.list': _NO, 'approval.open': _NO, 'approval.answer': _YES, 'events.read': _NO, 'events.follow': _NO,
    'route.explain': _NO, 'operator.read': _NO, 'budget.show': _NO, 'workspace.diff': _NO,
    'workspace.checkpoint': _NO, 'workspace.restore': _NO, 'workspace.publish': _NO, 'plugin.status': _NO,
    'plugin.call': _YES, 'key.list': _NO, 'surface.prefs.get': _NO, 'surface.prefs.set': _NO, 'doctor': _NO,
    'methods.list': _NO, 'standing.list': _NO, 'standing.stop': _NO,
}
# The same mark for the calls a plugin host may make into the core: none on this base.
HOST_CALLS_STARTING_WORK: dict = {}


# ---- the view ------------------------------------------------------------------------------------------
# The readers still running, by (control, store name): a store whose reader has not returned gets no second one.
_reading: dict = {}
_reading_lock = threading.Lock()


def _start_read(control, store):
    """Start ``store.read(control)`` on a thread of its own: (box, done), or None when a reader of this store
    for this control has not returned yet."""
    key = (id(control), store.name)
    box, done = {}, threading.Event()
    with _reading_lock:
        if key in _reading:
            return None
        _reading[key] = done

    def run():
        try:
            box['value'] = store.read(control)
        except Exception as exc:  # noqa: BLE001 - a store that fails is said, never guessed around
            box['error'] = exc
        finally:
            with _reading_lock:
                _reading.pop(key, None)
            done.set()

    threading.Thread(target=run, name='recursion-standing', daemon=True).start()
    return box, done


def view(control, params=None, *, stores=STORES):
    """The answer of ``standing.list``. ``params``: ``limit`` (rows on this page; wins over VIEW_PAGE_ROWS) and
    ``after`` (the ``key`` of the last row of the page before). The stores are read side by side under one
    wait, VIEW_READ_WAIT_S."""
    params = params if isinstance(params, Mapping) else {}
    limit, after = params.get('limit', VIEW_PAGE_ROWS), params.get('after')
    if limit is not None and (type(limit) is not int or limit < 1):
        return {'code': 'request_invalid', 'detail': 'limit'}
    if after is not None and (not isinstance(after, str) or not after):
        return {'code': 'request_invalid', 'detail': 'after'}
    known = tuple(store.kind for store in stores)
    started = [(store, _start_read(control, store)) for store in stores]
    deadline = time.monotonic() + VIEW_READ_WAIT_S
    rows, refused, read = [], [], []
    complete = True
    for store, reading in started:
        if reading is None:
            complete = False
            read.append({'store': store.name, 'state': 'no_answer',
                         'text': _text('store_still_reading', store=store.name)})
            continue
        box, done = reading
        if not done.wait(max(0.0, deadline - time.monotonic())):
            state, value = 'no_answer', None
        elif 'error' in box:
            state, value = 'failed', box['error']
        elif not isinstance(box.get('value'), list):
            state, value = 'failed', TypeError('reader returned no list')
        else:
            state, value = 'ok', box['value']
        if state != 'ok':
            complete = False
            if state == 'failed':
                log.error('standing view: the %s store could not be read (%s)', store.name, type(value).__name__)
            sentence = (_text('store_failed', store=store.name) if state == 'failed'
                        else _text('store_no_answer', store=store.name, wait=VIEW_READ_WAIT_S))
            read.append({'store': store.name, 'state': state, 'text': sentence})
            continue
        count = 0
        for row in value:
            kind = row.get('kind') if isinstance(row, Mapping) else None
            if kind != store.kind or kind not in known:
                refused.append({'store': store.name, 'code': 'row_kind_unknown',
                                'text': _text('row_kind_unknown', store=store.name)})
                continue
            row = dict(row)
            row['store'], row['key'] = store.name, f'{kind}:{row.get("id")}'
            rows.append(row)
            count += 1
        read.append({'store': store.name, 'state': 'read', 'count': count})
    counts = {}
    for row in rows:
        counts[row['kind']] = counts.get(row['kind'], 0) + 1
    answer = {'code': 'ok', 'complete': complete and not refused, 'stores': read, 'refused': refused,
              'counts': counts, 'not_seen': [TEXT['not_seen_engine'], TEXT['not_seen_plugin']]}
    text = (_text('view_some', count=len(rows), parts=_counted(counts)) if rows else TEXT['view_nothing'])
    if not complete:
        text += ' ' + TEXT['view_incomplete']
    answer['text'] = text
    if after is not None:
        at = next((i for i, row in enumerate(rows) if row['key'] == after), None)
        if at is None:
            return {'code': 'request_invalid', 'detail': 'after_unknown'}
        rows = rows[at + 1:]
    if limit is not None and len(rows) > limit:
        rows = rows[:limit]
        answer['next_after'] = rows[-1]['key']
    answer['rows'] = rows
    return answer


# ---- the one Stop --------------------------------------------------------------------------------------
def _stays_of_store(store, control):
    """What still stands in a store that holds no work, read without waiting for a lock: outcomes 'stays', or
    StoreBusy."""
    rows = store.read(control, wait=False)
    return [{'kind': row.get('kind'), 'id': row.get('id'), 'outcome': 'stays'} for row in rows
            if isinstance(row, Mapping)]


def stop_all(control, *, stores=STORES, wait_s=None):
    """The answer of ``standing.stop``: ``stopped`` when everything Stop ends was seen cleaned up, else
    ``pending`` with what was not and why; ``still_standing`` says what stays (by store, with its count) and
    ``not_seen`` what no store holds. It answers after at most ``wait_s`` (STOP_WAIT_S) whatever a store does:
    every store's part runs on a thread of its own, and a store that holds no work is read with wait=False."""
    wait_s = STOP_WAIT_S if wait_s is None else wait_s
    deadline = time.monotonic() + wait_s
    boxes = []
    for store in stores:
        box, done = {}, threading.Event()
        part = ((lambda store=store: _stays_of_store(store, control)) if store.stop == HOLDS_NO_WORK
                else (lambda store=store: store.stop(control)))

        def run(part=part, box=box, done=done):
            try:
                box['outcomes'] = list(part())
            except Exception as exc:  # noqa: BLE001 - a store whose Stop fails is said, never taken for stopped
                box['error'] = exc
            finally:
                done.set()

        threading.Thread(target=run, name='recursion-stop-store', daemon=True).start()
        boxes.append((store, box, done))
    stopped, left, stays, unread = {}, [], [], []
    for store, box, done in boxes:
        holds_work = store.stop != HOLDS_NO_WORK
        if not done.wait(max(0.0, deadline - time.monotonic())):
            if holds_work:
                left.append({'store': store.name, 'outcome': 'no_answer',
                             'text': _text('stop_store_no_answer', store=store.name, wait=wait_s)})
            else:
                unread.append({'store': store.name, 'text': _text('stop_stays_unread', store=store.name)})
            continue
        if 'error' in box:
            if not holds_work:
                unread.append({'store': store.name, 'text': _text('stop_stays_unread', store=store.name)})
                continue
            log.error('standing stop: the %s store failed (%s)', store.name, type(box['error']).__name__)
            left.append({'store': store.name, 'outcome': 'failed',
                         'text': _text('stop_store_failed', store=store.name)})
            continue
        counted = {}
        for outcome in box['outcomes']:
            if outcome.get('outcome') == 'stopped':
                stopped[outcome['kind']] = stopped.get(outcome['kind'], 0) + 1
            elif outcome.get('outcome') == 'stays':
                counted[outcome['kind']] = counted.get(outcome['kind'], 0) + 1
            else:
                left.append(dict(outcome, store=store.name))
        for kind, count in counted.items():
            one, many = STAYS_WORDS.get(kind, (kind, kind))
            stays.append({'store': store.name, 'kind': kind, 'count': count,
                          'text': f'{count} {one if count == 1 else many}'})
    sentences = [_text('stop_done', parts=_counted(stopped)) if stopped
                 else TEXT['stop_none'] if left else TEXT['stop_nothing']]
    if left:
        sentences.append(_text('stop_left', why='; '.join(item['text'] for item in left)))
    if stays:
        sentences.append(_text('stop_stays', parts=_joined([item['text'] for item in stays])))
    sentences.extend(item['text'] for item in unread)
    sentences.append(TEXT['not_seen_engine'])
    return {'code': 'pending' if left else 'stopped', 'stopped': stopped, 'not_stopped': left,
            'still_standing': [*stays, *unread], 'not_seen': [TEXT['not_seen_engine'], TEXT['not_seen_plugin']],
            'text': ' '.join(sentences)}
