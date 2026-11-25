"""One view of everything standing, one Stop of it (control/standing.py), through the daemon's control.

G6: every part of what the daemon composes that holds records is read by a store of the one table of stores,
or says why it holds nothing that stands, and a seeded record of each store is in the view. G10: every method of
the method table is marked as starting work or not; a run record is written only on the admission path; work a
method starts is in the view. The approvals row shows what waits now; the budgets row the spend recorded against
signed budgets. The door: both methods refuse every principal kind but owner and device, with an authorizer
registered or not. Stop: stopped only on seen cleanup; its report says what it stopped, what still stands and
what no store holds; a store that hangs, raises or is busy does not hold it. The sweep stops a stray reserved
unit on its next pass, as before. Every test uses only the temporary folders it makes; every thread it starts
is bounded in count and in time.
"""
from __future__ import annotations

import ast
import collections
import io
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from urllib.parse import urlencode

from recursion_core.control import standing
from recursion_core.control.hooks import Hooks, Principal
from recursion_core.control.methods import METHODS
from recursion_core.control.registry import EMPTY, POPULATED
from recursion_core.control.tests.fakes import FakeKillTree, make_api, started
from recursion_core.daemon import control_adapter as adapter
from recursion_core.plugin_security.approval import OPERATOR, ApprovalGate
from recursion_core.plugin_security.authority import Authority, create_ledger

CORE = Path(__file__).resolve().parents[2]          # core/recursion_core
REPO = CORE.parents[1]
UNLISTED = 'unlisted_kind'                          # a row kind no store of the table returns
POSIX = os.name == 'posix'
ANCHOR = 'ab' * 32                                  # a stand-in installer public key: only its form is checked
TTL = 300                                           # the test's own approval lifetime, in its own clock


def _effect(run_id):
    return {'schema': 'csl-effect/vNext', 'kind': 'order.submit', 'plugin_id': 'demo.plugin', 'run_id': run_id,
            'connection': 'conn-a', 'currency': 'USD', 'amount_minor': 1000, 'payload': {'note': 'stand-in'},
            'scope': {'account': 'paper-acct', 'venue': 'paper', 'contract_family': 'fake-futures',
                      'adapter': 'fake-adapter', 'environment': 'paper'}}


class _Clock:
    def __init__(self):
        self.now = 1000

    def __call__(self):
        return self.now


def _gate(clock):
    return ApprovalGate(lambda *_: None, lambda credential: OPERATOR if credential == 'op' else None, clock,
                        ttl_seconds=TTL)


def _authority(case, charges=()):
    """A real authority over a ledger the test made in its own folder, with ``charges`` seeded as
    (profile_id, idempotency_key, amount_minor)."""
    tmp = tempfile.TemporaryDirectory()
    case.addCleanup(tmp.cleanup)
    path = str(Path(tmp.name) / 'ledger.sqlite3')
    create_ledger(path, ANCHOR)
    db = sqlite3.connect(path, isolation_level=None)
    try:
        for profile_id, key, amount in charges:
            db.execute('INSERT OR IGNORE INTO profiles VALUES (?, ?)', (profile_id, 'sha256-of-the-profile'))
            db.execute('INSERT INTO charges VALUES (?, ?, ?)', (key, profile_id, amount))
    finally:
        db.close()
    authority = Authority(ANCHOR, path, lambda: 0)
    case.addCleanup(authority.close)
    return authority


class _Plugins:
    """A stand-in for the plugin host: the fields the table of stores reads, with a real gate and, when asked,
    a real authority. ``pending_approvals`` is the host's draw log, which the view must not read."""

    def __init__(self, loaded=(), stuck=None, gate=None, authority=None, drawn=()):
        self.records = [{'plugin_id': pid, 'state': 'loaded'} for pid in loaded]
        self.records.append({'plugin_id': 'refused-one', 'state': 'refused'})
        self.pending_approvals = collections.deque(drawn)
        self._stuck = stuck
        self.registries = ()
        self.gate, self.authority = gate, authority


def _control(case, kill=None, plugins=None):
    tmp = tempfile.TemporaryDirectory()
    case.addCleanup(tmp.cleanup)
    api, registry, kill, _ = make_api(kill=kill)
    daemon = mock.Mock()
    daemon.registry.get.side_effect = KeyError
    starter = mock.Mock(results={}, failures={})
    return adapter.DaemonControl(api, registry, daemon, starter, tmp.name,
                                 plugins=plugins), api


def _everything(case, kill=None):
    """A control with one of each: a loaded plugin, a waiting approval and a charged budget (no run yet)."""
    clock = _Clock()
    gate = _gate(clock)
    approval_id, _ = gate.request(_effect('run-a'))
    plugins = _Plugins(loaded=('plug-a',), gate=gate, authority=_authority(case, [('budget-1', 'k-1', 250)]))
    control, api = _control(case, kill=kill, plugins=plugins)
    return control, api, approval_id


CODES = ('stop_pending', 'cgroup', 'run_unknown', 'unavailable', 'plugin_call_running', 'not_confirmed',
         'everything is cleaned up')


def _texts(answer):
    found = []

    def walk(item):
        if isinstance(item, dict):
            for key, value in item.items():
                if key == 'text' and isinstance(value, str):
                    found.append(value)
                else:
                    walk(value)
        elif isinstance(item, list):
            for value in item:
                walk(value)
    walk(answer)
    return found


def _container(value):
    return isinstance(value, (dict, list, set, collections.deque, sqlite3.Connection))


def _composed_parts(case):
    """'Class.attribute' for every attribute that holds records (a dict, list, set, deque or database) of every
    object of the core's own classes reachable from what compose_control builds, with the plugin host's core
    composed as an install with an authority anchor composes it."""
    from recursion_core.daemon import Daemon, Registry
    from recursion_core.daemon import plugins as plugins_mod
    from recursion_core.daemon.launcher import EngineUnitManager
    tmp = tempfile.TemporaryDirectory()
    case.addCleanup(tmp.cleanup)
    root = Path(tmp.name)
    (root / 'run').mkdir(mode=0o700)
    environ = {'XDG_RUNTIME_DIR': str(root / 'run'), 'XDG_CONFIG_HOME': str(root / 'config'), 'HOME': str(root),
               'PATH': '/usr/bin:/bin'}
    daemon = Daemon(Registry(root / 'state'), EngineUnitManager())
    case.addCleanup(daemon.close)
    composed = adapter.compose_control(daemon, root / 'state', environ=environ, routes={})
    case.addCleanup(composed.close)
    ledger = root / 'state' / plugins_mod.STORE_DIR / plugins_mod.LEDGER_FILE
    ledger.parent.mkdir(parents=True, exist_ok=True)
    create_ledger(str(ledger), ANCHOR)
    composed.plugins._compose_core({'authority_anchor': ANCHOR}, None)
    parts, seen, todo = set(), set(), [composed, daemon]
    while todo:
        item = todo.pop()
        if id(item) in seen:
            continue
        seen.add(id(item))
        if isinstance(item, (list, tuple, set, frozenset, collections.deque)):
            todo.extend(item)
            continue
        if isinstance(item, dict):
            todo.extend(item.values())
            continue
        module = type(item).__module__
        if not module.startswith('recursion_core.') or '.tests' in module:
            continue
        try:
            fields = vars(item)
        except TypeError:
            continue
        for name, value in fields.items():
            if _container(value):
                parts.add(f'{type(item).__name__}.{name}')
            todo.append(value)
    return parts


class TheTableOfStores(unittest.TestCase):
    @unittest.skipUnless(POSIX, 'the daemon composes on a POSIX system only')
    def test_every_part_the_daemon_composes_that_holds_records_is_read_by_a_store_or_says_why(self):
        parts = _composed_parts(self)
        named = set(standing.HELD_BY_STORE) | set(standing.HOLDS_NO_STANDING)
        self.assertEqual(sorted(parts - named), [], 'a part of the composed daemon that holds records and that the '
                         'table of stores does not name: add its store to STORES, or say why it holds nothing that '
                         'stands')
        self.assertEqual(sorted(named - parts), [], 'a name of the table that the composed daemon does not hold')
        self.assertEqual(set(standing.HELD_BY_STORE) & set(standing.HOLDS_NO_STANDING), set())
        names = {store.name for store in standing.STORES}
        self.assertEqual(set(standing.HELD_BY_STORE.values()), names, 'a store that reads no part of the daemon')

    def test_every_store_carries_a_reader_and_a_stop_handler_or_says_it_holds_no_work(self):
        for store in standing.STORES:
            with self.subTest(store=store.name):
                self.assertTrue(callable(store.read))
                self.assertTrue(callable(store.stop) or store.stop == standing.HOLDS_NO_WORK)
        self.assertEqual(len(set(standing.ROW_KINDS)), len(standing.STORES))

    def test_a_seeded_record_of_every_store_is_in_the_view(self):
        control, api, approval_id = _everything(self)
        run_id = started(api)
        seeded = {('run', run_id), ('plugin_host', 'plug-a'), ('approval', approval_id), ('budget', 'budget-1')}
        self.assertEqual({kind for kind, _ in seeded}, set(standing.ROW_KINDS), 'a store with no seed here')
        answer = control.call('standing.list')
        self.assertEqual(answer['code'], 'ok')
        self.assertEqual({(row['kind'], row['id']) for row in answer['rows']}, seeded)
        self.assertTrue(answer['complete'])
        self.assertEqual(answer['text'], '4 standing: 1 run, 1 plugin host, 1 approval and 1 budget.')
        budget = next(row for row in answer['rows'] if row['kind'] == 'budget')
        self.assertEqual(budget['spent_minor'], 250)
        self.assertEqual(budget['text'], standing._text('budget', id='budget-1', spent=250))
        for text in _texts(answer):
            self.assertTrue(text.endswith('.'), text)
            for code in CODES:
                self.assertNotIn(code, text)
        self.assertEqual(answer['not_seen'], [standing.TEXT['not_seen_engine'], standing.TEXT['not_seen_plugin']])

    def test_a_row_of_a_kind_outside_the_known_set_is_refused_and_the_view_stays_whole(self):
        control, api = _control(self, plugins=_Plugins())
        run_id = started(api)
        real = standing.STORES[0]

        def runs_and_more(c, wait=True):
            return [*real.read(c), {'kind': UNLISTED, 'id': 'x'}]

        stores = (standing.Store('runs', 'run', runs_and_more, real.stop), *standing.STORES[1:])
        answer = standing.view(control, {}, stores=stores)
        self.assertEqual([(row['kind'], row['id']) for row in answer['rows']], [('run', run_id)])
        self.assertEqual(answer['refused'], [{'store': 'runs', 'code': 'row_kind_unknown',
                                              'text': standing._text('row_kind_unknown', store='runs')}])
        self.assertFalse(answer['complete'])
        self.assertNotIn(UNLISTED, json.dumps(answer['rows']))

    def test_stores_are_read_side_by_side_and_a_hung_store_gets_no_second_reader(self):
        control, api = _control(self, plugins=_Plugins())
        started(api)
        gate = threading.Event()
        self.addCleanup(gate.set)
        asked = []

        def hang(c, wait=True):
            asked.append(1)
            gate.wait(10)
            return []

        def fail(c, wait=True):
            raise OSError('unreadable')

        stores = (*standing.STORES, *(standing.Store(f'hanging {n}', 'run', hang, standing.HOLDS_NO_WORK)
                                      for n in range(3)), standing.Store('failing', 'run', fail, standing.HOLDS_NO_WORK))
        with mock.patch.object(standing, 'VIEW_READ_WAIT_S', 0.3):
            begun = time.monotonic()
            answer = standing.view(control, {}, stores=stores)
            self.assertLess(time.monotonic() - begun, 1.0, 'three hung stores were waited for one after another')
            again = standing.view(control, {}, stores=stores)
        states = {item['store']: item['state'] for item in answer['stores']}
        self.assertEqual(states, {'runs': 'read', 'plugin hosts': 'read', 'approvals': 'read', 'budgets': 'read',
                                  'hanging 0': 'no_answer', 'hanging 1': 'no_answer', 'hanging 2': 'no_answer',
                                  'failing': 'failed'})
        self.assertEqual(len(answer['rows']), 1)
        self.assertFalse(answer['complete'])
        self.assertIn(standing.TEXT['view_incomplete'], answer['text'])
        self.assertEqual(len(asked), 3, 'a second reader was started for a store whose reader still runs')
        still = [item for item in again['stores'] if item['store'].startswith('hanging')]
        self.assertEqual({item['text'] for item in still},
                         {standing._text('store_still_reading', store=item['store']) for item in still})

    def test_the_view_pages_by_a_limit_and_the_key_of_the_last_row(self):
        control, api = _control(self, plugins=_Plugins())
        ids = [started(api) for _ in range(3)]
        first = control.call('standing.list', {'limit': 2})
        self.assertEqual([row['id'] for row in first['rows']], ids[:2])
        rest = control.call('standing.list', {'after': first['next_after']})
        self.assertEqual([row['id'] for row in rest['rows']], ids[2:])
        self.assertNotIn('next_after', rest)
        self.assertEqual(control.call('standing.list', {'limit': 0})['code'], 'request_invalid')
        self.assertEqual(standing.VIEW_PAGE_ROWS, None)

    def test_a_run_is_said_starting_running_or_being_stopped_never_by_its_record_state(self):
        records = [{'run_id': f'r-{state}', 'state': state} for state in standing.LIVE_RUN_STATES]
        control = mock.Mock()
        control.registry.list.return_value = records
        texts = [row['text'] for row in standing.STORES[0].read(control)]
        self.assertEqual(texts, ['Run r-starting is starting. Stop ends it.', 'Run r-running is running. Stop ends it.',
                                 'Run r-stop_pending is being stopped. Stop ends it.'])


class TheApprovalsRow(unittest.TestCase):
    def test_only_what_waits_now_is_shown_never_an_expired_or_decided_approval(self):
        clock = _Clock()
        gate = _gate(clock)
        waiting, _ = gate.request(_effect('run-1'))
        refused, refused_digest = gate.request(_effect('run-2'))
        granted, granted_digest = gate.request(_effect('run-3'))
        used, used_digest = gate.request(_effect('run-4'))
        gate.decide(refused, refused_digest, 'op', False)
        gate.decide(granted, granted_digest, 'op', True)
        gate.decide(used, used_digest, 'op', True)
        gate.take(used, _effect('run-4'))
        drawn = [(approval_id, 'digest') for approval_id in (waiting, refused, granted, used)]
        control, _ = _control(self, plugins=_Plugins(gate=gate, drawn=drawn))
        rows = {row['id']: row for row in control.call('standing.list')['rows']}
        self.assertEqual(sorted(rows), sorted([waiting, granted]), 'a refused or used approval shown as standing')
        self.assertEqual(rows[waiting]['text'], standing._text('approval', id=waiting, left=TTL))
        self.assertEqual(rows[granted]['text'], standing._text('approval_granted', id=granted, left=TTL))
        clock.now += TTL
        answer = control.call('standing.list')
        self.assertEqual((answer['rows'], answer['text']), ([], standing.TEXT['view_nothing']),
                         'an expired approval shown as waiting')

    def test_the_readers_of_a_store_that_holds_no_work_never_wait_for_its_lock(self):
        gate = _gate(_Clock())
        gate.request(_effect('run-1'))
        authority = _authority(self, [('budget-1', 'k-1', 5)])
        with gate._lock, authority._lock:
            self.assertIsNone(gate.waiting(wait=False))
            self.assertIsNone(authority.spent(wait=False))
        self.assertEqual(len(gate.waiting(wait=False)), 1)
        self.assertEqual(authority.spent(wait=False), [('budget-1', 5)])


class TheDoor(unittest.TestCase):
    def test_both_methods_refuse_every_kind_but_owner_and_device_with_no_authorizer_registered(self):
        hooks = Hooks()
        refusal = {'code': 'scope_refused', 'detail': 'standing_not_granted_to_kind'}
        for method in standing.DOOR_METHODS:
            for kind in ('plugin', 'session', 'internal'):
                with self.subTest(method=method, kind=kind):
                    self.assertEqual(hooks.authorize(Principal(kind, 'x'), method, {}), refusal)
            for kind in standing.DOOR_KINDS:
                with self.subTest(method=method, kind=kind):
                    self.assertIsNone(hooks.authorize(Principal(kind, 'x'), method, {}))

    def test_every_other_method_is_answered_at_the_door_as_before(self):
        hooks = Hooks()
        for method in ('run.list', 'run.stop', 'plugin.list'):
            for kind in ('plugin', 'session', 'internal', 'owner', 'device'):
                with self.subTest(method=method, kind=kind):
                    self.assertIsNone(hooks.authorize(Principal(kind, 'x'), method, {}))
        self.assertEqual(hooks.authorize(Principal('owner', 'x', in_run_unit=True), 'run.list', {})['code'],
                         'scope_refused')


class TheMethods(unittest.TestCase):
    def test_every_method_of_the_table_is_marked_as_starting_work_or_not(self):
        self.assertEqual(sorted(set(METHODS) ^ set(standing.STARTS_WORK)), [],
                         'a method marked in neither way, or a mark for a method the table does not have')
        for name, mark in (*standing.STARTS_WORK.items(), *standing.HOST_CALLS_STARTING_WORK.items()):
            self.assertIs(type(mark), bool, name)
        self.assertEqual(standing.HOST_CALLS_STARTING_WORK, {})
        self.assertIs(standing.STARTS_WORK['run.start'], True)

    def test_a_run_record_is_written_only_by_the_admission_path_and_the_records_own_upkeep(self):
        writers = {'put', 'compare_and_swap', 'join_member', 'control_put', 'reap_orphans'}
        allowed = {('control/api.py', 'ControlAPI'), ('control/registry.py', None),
                   ('daemon/control_adapter.py', 'DurableRunRegistry'),
                   ('daemon/control_adapter.py', 'DaemonControl._reconcile_one'),
                   ('daemon/__init__.py', 'Registry')}
        found = []
        for path in sorted(CORE.rglob('*.py')):
            rel = path.relative_to(CORE).as_posix()
            if '/tests/' in '/' + rel:
                continue
            tree = ast.parse(path.read_text(encoding='utf-8'))
            for scope, node in _calls(tree):
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else None
                receiver = ast.unparse(func.value) if isinstance(func, ast.Attribute) else ''
                via_reg = (name == '_reg' and node.args and isinstance(node.args[0], ast.Constant)
                           and node.args[0].value in writers)
                if not via_reg and not (name in writers and 'registry' in receiver.lower()):
                    continue
                if not any(rel == file and (owner is None or scope == owner or scope.startswith(owner + '.'))
                           for file, owner in allowed):
                    found.append(f'{rel}:{node.lineno} {scope}: {ast.unparse(node)[:80]}')
        self.assertEqual(found, [], 'a run record written past the one admission path')

    def test_work_a_method_starts_is_in_the_view(self):
        control, _ = _control(self, plugins=_Plugins())
        answered = [name for name, mark in standing.STARTS_WORK.items() if mark and name == 'run.start']
        self.assertEqual(answered, ['run.start'])
        answer = control.call('run.start', {'tenant': 't1', 'engine': 'codex', 'prompt': 'p'})
        run_id = answer['run']['run_id']
        self.assertIn(('run', run_id), {(row['kind'], row['id']) for row in control.call('standing.list')['rows']})

    def test_the_view_and_the_stop_are_rows_of_the_published_table_and_no_engine_tool(self):
        table = json.loads((REPO / 'schemas/control/v1/methods.table.v1.json').read_text(encoding='utf-8'))
        rows = {row['name']: row for row in table['methods']}
        for name in (standing.VIEW_METHOD, standing.STOP_METHOD):
            self.assertIn(name, METHODS)
            self.assertIn(name, rows)
            self.assertIs(rows[name]['mcp']['exposed'], False, name)
        self.assertTrue(adapter.is_stop_request({'op': 'control', 'method': standing.STOP_METHOD}))
        self.assertTrue(adapter.is_stop_request({'op': 'control', 'method': 'run.stop'}))
        self.assertFalse(adapter.is_stop_request({'op': 'control', 'method': standing.VIEW_METHOD}))


def _calls(tree):
    """(dotted scope, call node) for every call, scope being Class.method names around it."""
    def walk(node, scope):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                yield from walk(child, f'{scope}.{child.name}' if scope else child.name)
            else:
                if isinstance(child, ast.Call):
                    yield scope, child
                yield from walk(child, scope)
    yield from walk(tree, '')


@unittest.skipUnless(POSIX, 'the daemon runs on a POSIX system only')
class TheSweep(unittest.TestCase):
    def test_a_stray_reserved_unit_is_stopped_on_the_next_pass_while_nothing_stands(self):
        from recursion_core.daemon import Daemon, Registry
        from recursion_core.daemon.tests.test_daemon import Boot, FakeManager
        with tempfile.TemporaryDirectory() as tmp:
            manager = FakeManager()
            daemon = Daemon(Registry(Path(tmp) / 'state'), manager, Boot())
            self.addCleanup(daemon.close)
            daemon.sweep()                              # the startup sweep: nothing stands, no unit
            daemon.sweep()                              # an idle pass: nothing changed
            manager.units.update({'recursion-run-stray1.service': 'running', 'someone-else.service': 'running'})
            daemon.sweep()                              # the next pass
            self.assertEqual(manager.stops, ['recursion-run-stray1.service'])
            self.assertEqual(sorted(manager.units), ['someone-else.service'])

    def test_an_idle_pass_makes_no_call_off_this_machine(self):
        from recursion_core import daemon as daemon_mod
        from recursion_core.daemon import __main__ as main_mod
        calls = []

        def counted(what):
            def refuse(*a, **k):
                calls.append(what)
                raise OSError(f'{what} refused by the test')
            return refuse

        class Popen:                                    # starts nothing: an empty, successful answer
            def __init__(self, args, *a, **k):
                self.args, self.returncode, self.pid = args, 0, None

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def communicate(self, input=None, timeout=None):
                return '', ''

            def poll(self):
                return 0

            def wait(self, timeout=None):
                return 0

            def kill(self):
                pass

        with tempfile.TemporaryDirectory() as tmp:
            for p in (mock.patch.object(subprocess, 'Popen', Popen),
                      mock.patch.object(socket.socket, 'connect', counted('connect')),
                      mock.patch.object(socket, 'create_connection', counted('create_connection')),
                      mock.patch.object(main_mod, 'require_free_threaded', lambda: None)):
                p.start()
                self.addCleanup(p.stop)
            daemon = daemon_mod.Daemon(daemon_mod.Registry(Path(tmp) / 'state'), daemon_mod.SystemdUserManager())
            self.addCleanup(daemon.close)
            control, _ = _control(self, plugins=None)
            for _ in range(12):
                main_mod.main_pass(daemon, control)
            self.assertEqual(calls, [], 'an idle pass made a call off this machine')
            self.assertEqual(control.call('standing.list')['text'], standing.TEXT['view_nothing'])


class OneStop(unittest.TestCase):
    def test_the_report_says_what_was_stopped_what_still_stands_and_what_no_store_holds(self):
        control, api, approval_id = _everything(self, kill=FakeKillTree(answers=(EMPTY,)))
        started(api)
        started(api)
        answer = control.call('standing.stop')
        self.assertEqual((answer['code'], answer['stopped'], answer['not_stopped']), ('stopped', {'run': 2}, []))
        self.assertEqual(answer['text'], 'Stopped 2 runs. Still standing, which Stop does not end: 1 plugin host '
                         'stays loaded, 1 approval still waits and 1 budget keeps the spend recorded against it. '
                         + standing.TEXT['not_seen_engine'])
        self.assertEqual({(item['store'], item['kind'], item['count']) for item in answer['still_standing']},
                         {('plugin hosts', 'plugin_host', 1), ('approvals', 'approval', 1), ('budgets', 'budget', 1)})
        after = control.call('standing.list')
        self.assertEqual(after['counts'], {item['kind']: item['count'] for item in answer['still_standing']},
                         'the Stop said less than the view still lists')
        self.assertEqual(answer['not_seen'], after['not_seen'])
        for text in _texts(answer) + [answer['text']]:
            for code in CODES:
                self.assertNotIn(code, text)

    def test_a_stop_that_could_not_see_a_run_ended_says_which_and_never_stopped(self):
        held, api = _control(self, kill=FakeKillTree(answers=(POPULATED,)), plugins=_Plugins())
        run_id = started(api)
        answer = held.call('standing.stop')
        self.assertEqual((answer['code'], answer['stopped']), ('pending', {}))
        self.assertEqual([(item['id'], item['outcome']) for item in answer['not_stopped']], [(run_id, 'not_stopped')])
        self.assertTrue(answer['text'].startswith('Nothing could be seen ended. What Stop could not see ended: run '
                                                  f'{run_id}, whose processes may still be running.'), answer['text'])
        self.assertEqual(_control(self, plugins=_Plugins())[0].call('standing.stop')['text'],
                         standing.TEXT['stop_nothing'] + ' ' + standing.TEXT['not_seen_engine'])

    def test_a_running_plugin_read_is_said_never_counted_stopped(self):
        control, _ = _control(self, plugins=_Plugins(loaded=('plug-a',), stuck='plug-a'))
        answer = control.call('standing.stop')
        self.assertEqual(answer['code'], 'pending')
        self.assertEqual([(i['kind'], i['reason']) for i in answer['not_stopped']], [('plugin_host', 'plugin_call_running')])
        self.assertIn(standing._text('stop_plugin_busy', id='plug-a'), answer['text'])

    def test_a_store_whose_stop_or_reader_hangs_or_raises_does_not_hold_stop(self):
        control, api = _control(self, kill=FakeKillTree(answers=(EMPTY,)), plugins=_Plugins())
        started(api)
        gate = threading.Event()
        self.addCleanup(gate.set)
        waits = []

        def hang(c):
            gate.wait(10)
            return []

        def fail(c):
            raise OSError('store down')

        def read_hangs(c, wait=True):
            waits.append(wait)
            gate.wait(10)
            return []

        stores = (*standing.STORES, standing.Store('hanging', 'run', read_hangs, hang),
                  standing.Store('failing', 'run', read_hangs, fail),
                  standing.Store('quiet', 'run', read_hangs, standing.HOLDS_NO_WORK))
        begun = time.monotonic()
        answer = standing.stop_all(control, stores=stores, wait_s=1.0)
        self.assertLess(time.monotonic() - begun, 3.0)
        self.assertEqual(answer['stopped'], {'run': 1})
        self.assertEqual(sorted((i['store'], i['outcome']) for i in answer['not_stopped']),
                         [('failing', 'failed'), ('hanging', 'no_answer')])
        self.assertEqual(answer['code'], 'pending')
        self.assertEqual(waits, [False], 'Stop asked a reader that may wait')
        self.assertIn(standing._text('stop_stays_unread', store='quiet'), answer['text'])

    def test_stop_never_waits_for_a_busy_store_to_say_what_still_stands(self):
        control, api, _ = _everything(self, kill=FakeKillTree(answers=(EMPTY,)))
        started(api)
        plugins = control.plugins
        with plugins.gate._lock, plugins.authority._lock:
            begun = time.monotonic()
            answer = control.call('standing.stop')
            spent = time.monotonic() - begun
        self.assertLess(spent, 1.0, 'Stop waited on a busy store')
        self.assertEqual((answer['code'], answer['stopped']), ('stopped', {'run': 1}))
        self.assertIn({'store': 'approvals', 'text': standing._text('stop_stays_unread', store='approvals')},
                      answer['still_standing'])
        self.assertIn({'store': 'budgets', 'text': standing._text('stop_stays_unread', store='budgets')},
                      answer['still_standing'])


class Surfaces(unittest.TestCase):
    def test_the_terminal_and_the_page_show_the_view_whole_and_reach_the_one_stop(self):
        from recursion_core.control import cli
        from recursion_core.gui.client import ControlClient
        from recursion_core.gui.surface import Application
        control, api, _ = _everything(self, kill=FakeKillTree(answers=(EMPTY,)))
        run_id = started(api)
        out = io.StringIO()
        self.assertEqual(cli.main(['standing'], control, out=out), 0)
        self.assertEqual(json.loads(out.getvalue())['rows'], control.call('standing.list')['rows'])
        host, origin = '127.0.0.1:1', 'http://127.0.0.1:1'
        app = Application(ControlClient(control.gui_transport), lambda env: True, frozenset({host}),
                          frozenset({origin}))

        def ask(method, path, form=None):
            body = urlencode(form).encode() if form else b''
            env = {'REQUEST_METHOD': method, 'PATH_INFO': path, 'QUERY_STRING': '', 'HTTP_HOST': host,
                   'CONTENT_LENGTH': str(len(body)), 'CONTENT_TYPE': 'application/x-www-form-urlencoded',
                   'wsgi.input': io.BytesIO(body), 'wsgi.errors': sys.stderr}
            if method == 'POST':
                env.update(HTTP_ORIGIN=origin, HTTP_SEC_FETCH_SITE='same-origin')
            status = []
            page = b''.join(app(env, lambda s, h: status.append(s))).decode()
            return status[0], page

        status, page = ask('GET', '/')
        self.assertEqual(status, '200 OK')
        for row in control.call('standing.list')['rows']:
            self.assertIn(row['text'], page)
        self.assertIn('action="/stop-all"', page)
        status, page = ask('POST', '/stop-all', {'confirm': 'explicit'})
        self.assertEqual(status, '200 OK')
        self.assertIn('Stopped 1 run. Still standing, which Stop does not end: 1 plugin host stays loaded, '
                      '1 approval still waits and 1 budget keeps the spend recorded against it.', page)
        self.assertIn('<li>1 approval still waits</li>', page)
        self.assertIn('<li>%s</li>' % standing.TEXT['not_seen_plugin'], page)
        self.assertNotIn('everything is cleaned up', page)
        self.assertNotIn(run_id, {row['id'] for row in control.call('standing.list')['rows']})
        out = io.StringIO()
        self.assertEqual(cli.main(['stop-all'], control, out=out), 0)
        self.assertTrue(json.loads(out.getvalue())['text'].startswith(standing.TEXT['stop_nothing']))


if __name__ == '__main__':
    unittest.main()
