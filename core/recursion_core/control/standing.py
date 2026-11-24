"""One view of everything standing, and one Stop that ends it.

``view(control, params)`` answers ``standing.list``: every standing record the core's own stores hold, read
from those stores at the time of the call, never from a plugin's own account. ``stop_all(control)`` answers
``standing.stop``: one Stop that walks the same table and ends what each store holds, and says ``stopped``
only for what it saw cleaned up. ``control`` is the daemon's ``DaemonControl`` (``daemon/control_adapter.py``);
the socket (an owner) and the page (the device) reach both through it, as they reach ``run.stop``.

THE TABLE OF STORES (``STORES``) is the one place a store of standing records is named. Each entry carries the
store's row reader and, beside it, EITHER its stop handler OR ``HOLDS_NO_WORK``, the value that says the store
holds nothing a Stop could end. The view and the Stop read the same table, so a store added here is shown and
stopped, and a store added to the daemon and not here fails its test (``tests/test_standing.py``).

THE ROW KINDS are a tagged union: every row carries ``kind``, one of ``ROW_KINDS`` (the kinds of ``STORES``).
A row of any other kind is never dropped silently and never shown as if it were known: it is refused,
counted under ``refused`` with a sentence a person can read, and the rest of the view stays whole.

THE TABLE OF METHODS (``STARTS_WORK``) marks every method of the method table (``control.methods.METHODS``)
as one that starts standing work or not; ``HOST_CALLS_STARTING_WORK`` is the same mark for the calls a plugin
host may make, none on this base. A test fails on a method marked in neither way.

WHAT THE VIEW CANNOT SEE is said in its own answer (``not_seen``): work an engine keeps for later in its own
files (an engine's own scheduled tasks, for one) is held by no store of the core, so the view does not show it
and Stop does not remove it; and code a loaded plugin started by itself inside the daemon's process is not held
by any store either. The view's sentence never claims more than the stores it read.

Stop stays fast and whole: each store is stopped on a thread of its own, the runs one per thread, and the
answer comes after at most ``STOP_WAIT_S``, whatever a store does. A store whose handler hangs or raises is
named as not answering; it holds no other store's Stop. Stop never calls a view reader. Every sentence a person
reads is in ``TEXT``.
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
VIEW_READ_WAIT_S = 5.0  # how long the view waits for one store's reader before it says the store did not answer
STOP_WAIT_S = 10.0      # how long one Stop waits for seen cleanup: twice run.stop's own confirm wait (5 s)
STOP_RUN_THREADS = 16   # runs one Stop ends at the same time; None: one thread for every run
IDLE_WINDOW_S = 60.0    # the window over which a lean profile with nothing standing starts nothing (G9's test)

HOLDS_NO_WORK = 'holds_no_work'   # a store's stop entry when it holds nothing a Stop could end
LIVE_RUN_STATES = ('starting', 'running', 'stop_pending')   # run.record.v1 states of a run that still stands

# ---- every sentence a person reads, in one table. {placeholders} are filled from the row.
TEXT = {
    'run': 'Run {id} is {state}. Stop ends it.',
    'plugin_host': 'Plugin {id} is loaded. It runs only when the daemon asks it; Stop leaves it loaded.',
    'plugin_host_busy': 'Plugin {id} is loaded and a read of it passed its deadline and still runs. '
                        'Only a restart of the daemon ends that read.',
    'approval': 'Approval {id} waits for an answer. Nothing it asks for runs until a person answers.',
    'row_kind_unknown': 'The {store} store holds a kind of standing work this release does not show yet. '
                        'It is counted here and not hidden.',
    'store_failed': 'The {store} store could not be read, so what it holds is not shown. Ask again.',
    'store_no_answer': 'The {store} store did not answer within {wait:g} s, so what it holds is not shown. Ask again.',
    'view_nothing': 'Nothing is standing.',
    'view_some': '{count} standing: {parts}.',
    'view_incomplete': 'Not every store answered, so this may not be everything.',
    'not_seen_engine': 'What an engine keeps for later in its own files, its own scheduled tasks for one, is held by '
                       'no store of Recursion: it is not shown here, and Stop does not remove it.',
    'not_seen_plugin': 'Code a loaded plugin starts by itself inside the daemon is held by no store of Recursion: '
                       'it is not shown here.',
    'stop_nothing': 'Nothing was standing, so nothing needed to stop.',
    'stop_all_clean': 'Stopped {parts}; everything is cleaned up.',
    'stop_some_left': 'Stopped {parts}. {left} could not be seen cleaned up: {why}. Ask Stop again; '
                      'if it stays, restart the daemon.',
    'stop_none_clean': 'Nothing could be seen cleaned up: {why}. Ask Stop again; if it stays, restart the daemon.',
    'stop_run_pending': 'run {id} did not empty yet ({reason})',
    'stop_run_failed': 'run {id} answered {reason}',
    'stop_plugin_busy': 'a read of plugin {id} still runs inside the daemon',
    'stop_store_failed': 'the {store} store failed',
    'stop_store_no_answer': 'the {store} store did not answer within {wait:g} s',
    'nothing': 'nothing',
}
# How a count of each kind is said: (one, many).
KIND_WORDS = {'run': ('run', 'runs'), 'plugin_host': ('plugin host', 'plugin hosts'),
              'approval': ('approval', 'approvals')}


def _text(key, **fields):
    return TEXT[key].format(**fields)


def _counted(counts):
    """{'run': 3, 'plugin_host': 1} -> '3 runs and 1 plugin host', in the order of the table of stores."""
    parts = []
    for kind in (*ROW_KINDS, *(k for k in counts if k not in ROW_KINDS)):
        n = counts.get(kind, 0)
        if n:
            one, many = KIND_WORDS.get(kind, (kind, kind))
            parts.append(f'{n} {one if n == 1 else many}')
    if not parts:
        return TEXT['nothing']
    return parts[0] if len(parts) == 1 else ', '.join(parts[:-1]) + ' and ' + parts[-1]


# ---- the readers and the stop handlers of today's stores -----------------------------------------------
def _read_runs(control):
    rows = []
    for rec in control.registry.list():
        state = rec.get('state')
        if state in LIVE_RUN_STATES:
            run_id = rec.get('run_id')
            rows.append({'kind': 'run', 'id': run_id, 'state': state, 'text': _text('run', id=run_id, state=state)})
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
        elif code == 'pending':
            reason = str(answer.get('reason') or answer.get('detail') or 'not confirmed')
            outcomes[i] = {'kind': 'run', 'id': run_id, 'outcome': 'not_stopped', 'reason': reason,
                           'text': _text('stop_run_pending', id=run_id, reason=reason)}
        else:
            reason = str(code)
            outcomes[i] = {'kind': 'run', 'id': run_id, 'outcome': 'not_stopped', 'reason': reason,
                           'text': _text('stop_run_failed', id=run_id, reason=reason)}

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


def _loaded_plugins(control):
    plugins = getattr(control, 'plugins', None)
    if plugins is None:
        return []
    return [record for record in plugins.records if record.get('state') == 'loaded']


def _plugin_busy(control, plugin_id):
    return getattr(control.plugins, '_stuck', None) == plugin_id


def _read_plugin_hosts(control):
    rows = []
    for record in _loaded_plugins(control):
        plugin_id = record.get('plugin_id')
        busy = _plugin_busy(control, plugin_id)
        rows.append({'kind': 'plugin_host', 'id': plugin_id, 'state': 'busy' if busy else 'loaded',
                     'text': _text('plugin_host_busy' if busy else 'plugin_host', id=plugin_id)})
    return rows


def _stop_plugin_hosts(control):
    """A loaded plugin runs only when the daemon calls it, and every call has its deadline: there is nothing of
    it a Stop can end. A call that passed its deadline and has not returned is said, never counted stopped."""
    return [{'kind': 'plugin_host', 'id': record.get('plugin_id'), 'outcome': 'not_stopped',
             'reason': 'plugin_call_running', 'text': _text('stop_plugin_busy', id=record.get('plugin_id'))}
            for record in _loaded_plugins(control) if _plugin_busy(control, record.get('plugin_id'))]


def _read_approvals(control):
    plugins = getattr(control, 'plugins', None)
    if plugins is None:
        return []
    rows = []
    for approval_id, digest in tuple(plugins.pending_approvals):
        rows.append({'kind': 'approval', 'id': approval_id, 'digest': digest,
                     'text': _text('approval', id=approval_id)})
    return rows


class Store(NamedTuple):
    name: str                       # how the store is named in the view and in its sentences
    kind: str                       # the row kind its reader returns
    read: Callable                  # read(control) -> rows
    stop: object                    # stop(control) -> outcomes, or HOLDS_NO_WORK


# THE TABLE OF STORES: every store of the core that holds standing records on this base. A routine's row joins
# when the core holds routine records; budgets are not held by any store on this base.
STORES = (
    Store('runs', 'run', _read_runs, _stop_runs),
    Store('plugin hosts', 'plugin_host', _read_plugin_hosts, _stop_plugin_hosts),
    Store('approvals', 'approval', _read_approvals, HOLDS_NO_WORK),
)
ROW_KINDS = tuple(store.kind for store in STORES)

# What each attribute of a DaemonControl holds: the stores the table reads it through, or why it holds no
# standing record. A test holds the attributes of a composed control equal to these two lists.
CONTROL_SOURCES = {'registry': ('runs',), 'plugins': ('plugin hosts', 'approvals')}
CONTROL_NOT_STANDING = {
    'api': 'the control API over the run registry: its runs are the runs store',
    'daemon': "the daemon's unit registry: the units of the runs the runs store lists",
    'starter': 'the route starter: every run it launches is a record of the runs store first',
    '_list_reply_bytes': 'a figure of the reply frame',
    '_state': 'the state folder',
    '_routes': 'the route file: what can be started, not what stands',
    '_breaker': 'limits on connections, not work',
    '_breaker_store': 'a label for the breaker store',
    '_stop_retry': 'when a Stop the sweep could not send is sent again: runs of the runs store',
    '_starts_lock': 'a lock',
    '_starts_in_flight': 'a count of starts not answered yet: their runs are records of the runs store',
}
# What each attribute of a Composition (daemon/control_adapter.py) holds.
COMPOSITION_NOT_STANDING = {
    'control': 'the DaemonControl: its attributes are listed above',
    'server': 'the control socket',
    'door': "the page's device door",
    'client': "the page's client of the control",
    'plugins': 'the plugin host: read through the control',
    'breaker': 'limits on connections, not work',
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
def _within(function, wait_s):
    """``function()`` on a thread of its own: ('ok', value), ('failed', exc) or ('no_answer', None)."""
    box = {}
    done = threading.Event()

    def run():
        try:
            box['value'] = function()
        except Exception as exc:  # noqa: BLE001 - a store that fails is said, never guessed around
            box['error'] = exc
        finally:
            done.set()

    threading.Thread(target=run, name='recursion-standing', daemon=True).start()
    if not done.wait(wait_s):
        return 'no_answer', None
    if 'error' in box:
        return 'failed', box['error']
    return 'ok', box['value']


def view(control, params=None, *, stores=STORES):
    """The answer of ``standing.list``. ``params``: ``limit`` (rows on this page; wins over VIEW_PAGE_ROWS) and
    ``after`` (the ``key`` of the last row of the page before)."""
    params = params if isinstance(params, Mapping) else {}
    limit, after = params.get('limit', VIEW_PAGE_ROWS), params.get('after')
    if limit is not None and (type(limit) is not int or limit < 1):
        return {'code': 'request_invalid', 'detail': 'limit'}
    if after is not None and (not isinstance(after, str) or not after):
        return {'code': 'request_invalid', 'detail': 'after'}
    known = tuple(store.kind for store in stores)
    rows, refused, read = [], [], []
    complete = True
    for store in stores:
        state, value = _within(lambda store=store: store.read(control), VIEW_READ_WAIT_S)
        if state == 'ok' and not isinstance(value, list):
            state, value = 'failed', TypeError('reader returned no list')
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
def stop_all(control, *, stores=STORES, wait_s=None):
    """The answer of ``standing.stop``: ``stopped`` when everything that stood was seen cleaned up, else
    ``pending`` with what was not and why. It answers after at most ``wait_s`` (STOP_WAIT_S) whatever a store
    does, and it reads no view reader."""
    wait_s = STOP_WAIT_S if wait_s is None else wait_s
    deadline = time.monotonic() + wait_s
    boxes = []
    for store in stores:
        if store.stop == HOLDS_NO_WORK:
            continue
        box, done = {}, threading.Event()

        def run(store=store, box=box, done=done):
            try:
                box['outcomes'] = list(store.stop(control))
            except Exception as exc:  # noqa: BLE001 - a store whose Stop fails is said, never taken for stopped
                box['error'] = exc
            finally:
                done.set()

        threading.Thread(target=run, name='recursion-stop-store', daemon=True).start()
        boxes.append((store, box, done))
    stopped, left = {}, []
    for store, box, done in boxes:
        if not done.wait(max(0.0, deadline - time.monotonic())):
            left.append({'store': store.name, 'outcome': 'no_answer',
                         'text': _text('stop_store_no_answer', store=store.name, wait=wait_s)})
            continue
        if 'error' in box:
            log.error('standing stop: the %s store failed (%s)', store.name, type(box['error']).__name__)
            left.append({'store': store.name, 'outcome': 'failed',
                         'text': _text('stop_store_failed', store=store.name)})
            continue
        for outcome in box['outcomes']:
            if outcome.get('outcome') == 'stopped':
                stopped[outcome['kind']] = stopped.get(outcome['kind'], 0) + 1
            else:
                left.append(dict(outcome, store=store.name))
    parts = _counted(stopped)
    if not left:
        text = _text('stop_all_clean', parts=parts) if stopped else TEXT['stop_nothing']
    else:
        why = '; '.join(item['text'] for item in left)
        text = (_text('stop_some_left', parts=parts, left=len(left), why=why) if stopped
                else _text('stop_none_clean', why=why))
    return {'code': 'pending' if left else 'stopped', 'stopped': stopped, 'not_stopped': left, 'text': text}
