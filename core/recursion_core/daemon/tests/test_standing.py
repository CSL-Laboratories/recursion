"""One view of everything standing, one Stop of it (control/standing.py), through the daemon's control.

G6: every store of standing records is in the one table of stores, and a seeded record of each is in the view.
G10: every method of the method table is marked as starting work or not; a run record is written only on the
admission path; work a method starts is in the view. G9: a lean profile with nothing standing starts no process
and makes no call off the machine over the idle window. Stop: stopped only on seen cleanup, and a store that
hangs or raises does not hold it. Every test uses only the temporary folders it makes; every thread it starts
is bounded in count and in time.
"""
from __future__ import annotations

import ast
import collections
import io
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from urllib.parse import urlencode

from recursion_core.control import standing
from recursion_core.control.methods import METHODS
from recursion_core.control.registry import EMPTY, POPULATED
from recursion_core.control.tests.fakes import FakeKillTree, make_api, started
from recursion_core.daemon import control_adapter as adapter

CORE = Path(__file__).resolve().parents[2]          # core/recursion_core
REPO = CORE.parents[1]
UNLISTED = 'unlisted_kind'                          # a row kind no store of the table returns


class _Plugins:
    """A stand-in for the plugin host: the fields the table of stores reads, nothing else."""

    def __init__(self, loaded=(), approvals=(), stuck=None):
        self.records = [{'plugin_id': pid, 'state': 'loaded'} for pid in loaded]
        self.records.append({'plugin_id': 'refused-one', 'state': 'refused'})
        self.pending_approvals = collections.deque(approvals)
        self._stuck = stuck
        self.registries = ()


def _control(case, kill=None, plugins=None):
    tmp = tempfile.TemporaryDirectory()
    case.addCleanup(tmp.cleanup)
    api, registry, kill, _ = make_api(kill=kill)
    daemon = mock.Mock()
    daemon.registry.get.side_effect = KeyError
    starter = mock.Mock(results={}, failures={})
    return adapter.DaemonControl(api, registry, daemon, starter, tmp.name,
                                 plugins=plugins), api


class TheTableOfStores(unittest.TestCase):
    def test_every_attribute_of_the_control_is_a_store_of_the_table_or_says_why_it_holds_none(self):
        control, _ = _control(self, plugins=_Plugins())
        named = set(standing.CONTROL_SOURCES) | set(standing.CONTROL_NOT_STANDING)
        self.assertEqual(sorted(set(vars(control)) - named), [], 'a field of the control that the table of '
                         'stores does not name: add its store to STORES, or say why it holds no standing record')
        self.assertEqual(sorted(named - set(vars(control))), [])
        names = {store.name for store in standing.STORES}
        for field, stores in standing.CONTROL_SOURCES.items():
            self.assertLessEqual(set(stores), names, field)
        self.assertEqual(names, {name for stores in standing.CONTROL_SOURCES.values() for name in stores})
        composition = adapter.Composition(control, object(), object(), control.plugins, None)
        self.assertEqual(sorted(vars(composition)), sorted(standing.COMPOSITION_NOT_STANDING))

    def test_every_store_carries_a_reader_and_a_stop_handler_or_says_it_holds_no_work(self):
        for store in standing.STORES:
            with self.subTest(store=store.name):
                self.assertTrue(callable(store.read))
                self.assertTrue(callable(store.stop) or store.stop == standing.HOLDS_NO_WORK)
        self.assertEqual(len(set(standing.ROW_KINDS)), len(standing.STORES))

    def test_a_seeded_record_of_every_store_is_in_the_view(self):
        control, api = _control(self, plugins=_Plugins(loaded=('plug-a',), approvals=(('ap-1', 'sha256:aa'),)))
        run_id = started(api)
        seeded = {('run', run_id), ('plugin_host', 'plug-a'), ('approval', 'ap-1')}
        self.assertEqual({kind for kind, _ in seeded}, set(standing.ROW_KINDS), 'a store with no seed here')
        answer = control.call('standing.list')
        self.assertEqual(answer['code'], 'ok')
        self.assertEqual({(row['kind'], row['id']) for row in answer['rows']}, seeded)
        self.assertTrue(answer['complete'])
        self.assertEqual(answer['text'], '3 standing: 1 run, 1 plugin host and 1 approval.')
        for row in answer['rows']:
            self.assertTrue(row['text'].endswith('.'), row)
        self.assertEqual(answer['not_seen'], [standing.TEXT['not_seen_engine'], standing.TEXT['not_seen_plugin']])

    def test_a_row_of_a_kind_outside_the_known_set_is_refused_and_the_view_stays_whole(self):
        control, api = _control(self, plugins=_Plugins(approvals=(('ap-1', 'd'),)))
        run_id = started(api)
        real = standing.STORES[0]

        def runs_and_more(c):
            return [*real.read(c), {'kind': UNLISTED, 'id': 'x'}]

        stores = (standing.Store('runs', 'run', runs_and_more, real.stop), *standing.STORES[1:])
        answer = standing.view(control, {}, stores=stores)
        self.assertEqual([(row['kind'], row['id']) for row in answer['rows']], [('run', run_id), ('approval', 'ap-1')])
        self.assertEqual(answer['refused'], [{'store': 'runs', 'code': 'row_kind_unknown',
                                              'text': standing._text('row_kind_unknown', store='runs')}])
        self.assertFalse(answer['complete'])
        self.assertNotIn(UNLISTED, json.dumps(answer['rows']))

    def test_a_store_that_raises_or_hangs_is_said_and_the_rest_of_the_view_stays(self):
        control, api = _control(self, plugins=_Plugins(approvals=(('ap-1', 'd'),)))
        started(api)
        gate = threading.Event()
        self.addCleanup(gate.set)

        def hang(c):
            gate.wait(5)
            return []

        def fail(c):
            raise OSError('unreadable')

        stores = (*standing.STORES, standing.Store('hanging', 'run', hang, standing.HOLDS_NO_WORK),
                  standing.Store('failing', 'run', fail, standing.HOLDS_NO_WORK))
        with mock.patch.object(standing, 'VIEW_READ_WAIT_S', 0.3):
            begun = time.monotonic()
            answer = standing.view(control, {}, stores=stores)
            self.assertLess(time.monotonic() - begun, 3.0)
        states = {item['store']: item['state'] for item in answer['stores']}
        self.assertEqual(states, {'runs': 'read', 'plugin hosts': 'read', 'approvals': 'read',
                                  'hanging': 'no_answer', 'failing': 'failed'})
        self.assertEqual(len(answer['rows']), 2)
        self.assertFalse(answer['complete'])
        self.assertIn(standing.TEXT['view_incomplete'], answer['text'])

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


class TheTableOfMethods(unittest.TestCase):
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


class _Popen:
    """Counts every process a run would start and starts none: an empty, successful answer."""
    count = 0

    def __init__(self, args, *a, **k):
        type(self).count += 1
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


class IdleProfile(unittest.TestCase):
    def test_a_lean_profile_with_nothing_standing_starts_nothing_over_the_idle_window(self):
        from recursion_core import daemon as daemon_mod
        from recursion_core.daemon import __main__ as main_mod
        calls = []

        def counted(what):
            def refuse(*a, **k):
                calls.append(what)
                raise OSError(f'{what} refused by the test')
            return refuse

        with tempfile.TemporaryDirectory() as tmp:
            _Popen.count = 0
            patches = [mock.patch.object(subprocess, 'Popen', _Popen),
                       mock.patch.object(socket.socket, 'connect', counted('connect')),
                       mock.patch.object(socket, 'create_connection', counted('create_connection'))]
            for name in ('fork', 'posix_spawn', 'posix_spawnp', 'system'):
                if hasattr(__import__('os'), name):
                    patches.append(mock.patch(f'os.{name}', counted(name)))
            for p in patches:
                p.start()
                self.addCleanup(p.stop)
            daemon = daemon_mod.Daemon(daemon_mod.Registry(Path(tmp) / 'state'), daemon_mod.SystemdUserManager())
            self.addCleanup(daemon.close)
            daemon.sweep()                          # the startup sweep lists the reserved units once
            after_start = _Popen.count
            control, _ = _control(self, plugins=None)
            passes = int(standing.IDLE_WINDOW_S // 5)   # the main loop's sweep, every 5 s by default
            with mock.patch.object(main_mod, 'require_free_threaded', lambda: None):
                for _ in range(passes):
                    main_mod.main_pass(daemon, control)
            self.assertEqual((_Popen.count - after_start, calls), (0, []),
                             'an idle pass started a process or made a call off the machine')
            self.assertEqual(control.call('standing.list')['text'], standing.TEXT['view_nothing'])


class OneStop(unittest.TestCase):
    def test_stop_answers_stopped_only_when_every_cleanup_was_seen(self):
        control, api = _control(self, kill=FakeKillTree(answers=(EMPTY,)), plugins=_Plugins(approvals=(('ap', 'd'),)))
        started(api)
        started(api)
        answer = control.call('standing.stop')
        self.assertEqual((answer['code'], answer['stopped'], answer['not_stopped']), ('stopped', {'run': 2}, []))
        self.assertEqual(answer['text'], 'Stopped 2 runs; everything is cleaned up.')
        self.assertEqual(control.call('standing.list')['counts'], {'approval': 1})
        held, api = _control(self, kill=FakeKillTree(answers=(POPULATED,)), plugins=_Plugins())
        run_id = started(api)
        answer = held.call('standing.stop')
        self.assertEqual((answer['code'], answer['stopped']), ('pending', {}))
        self.assertEqual([(item['id'], item['outcome']) for item in answer['not_stopped']], [(run_id, 'not_stopped')])
        self.assertTrue(answer['text'].startswith('Nothing could be seen cleaned up'), answer['text'])
        self.assertEqual(_control(self, plugins=_Plugins())[0].call('standing.stop')['text'], standing.TEXT['stop_nothing'])

    def test_a_running_plugin_read_is_said_never_counted_stopped(self):
        control, _ = _control(self, plugins=_Plugins(loaded=('plug-a',), stuck='plug-a'))
        answer = control.call('standing.stop')
        self.assertEqual(answer['code'], 'pending')
        self.assertEqual([(i['kind'], i['reason']) for i in answer['not_stopped']], [('plugin_host', 'plugin_call_running')])

    def test_a_store_whose_stop_or_reader_hangs_or_raises_does_not_hold_stop(self):
        control, api = _control(self, kill=FakeKillTree(answers=(EMPTY,)), plugins=_Plugins())
        started(api)
        gate = threading.Event()
        self.addCleanup(gate.set)
        read_asked = []

        def hang(c):
            gate.wait(10)
            return []

        def fail(c):
            raise OSError('store down')

        def read_hangs(c):
            read_asked.append(1)
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
        self.assertEqual(read_asked, [], 'Stop asked a view reader')


class Surfaces(unittest.TestCase):
    def test_the_terminal_and_the_page_show_the_view_whole_and_reach_the_one_stop(self):
        from recursion_core.control import cli
        from recursion_core.gui.client import ControlClient
        from recursion_core.gui.surface import Application
        control, api = _control(self, kill=FakeKillTree(answers=(EMPTY,)), plugins=_Plugins(approvals=(('ap-1', 'd'),)))
        run_id = started(api)
        out = io.StringIO()
        self.assertEqual(cli.main(['standing'], control, out=out), 0)
        self.assertEqual(json.loads(out.getvalue()), control.call('standing.list'))
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
        self.assertIn('Stopped 1 run; everything is cleaned up.', page)
        self.assertNotIn(run_id, {row['id'] for row in control.call('standing.list')['rows']})
        out = io.StringIO()
        self.assertEqual(cli.main(['stop-all'], control, out=out), 0)
        self.assertEqual(json.loads(out.getvalue())['text'], standing.TEXT['stop_nothing'])


if __name__ == '__main__':
    unittest.main()
