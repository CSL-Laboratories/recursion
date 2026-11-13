"""The seams' own modules: the hooks and their defaults, the method, command and engine tables.

Every hook has a test that registers a fake and sees it called with the documented arguments, a test of
its default (which is what the daemon does today), and a test that a hook that fails does not fail open.
The calls from the daemon's own files into these hooks are tested where those files are.

``FakeStore`` is the event store's transaction helper as the dispatcher needs it, and nothing more. It is
the one fake of that helper: a lane that builds a handler before the event store exists uses this shape.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib
import io
import json
import logging
import pathlib
import sys
import tempfile
import threading
import time
import unittest

from recursion_core.control import commands, hooks as H, methods
from recursion_core.routes import ClaudeCodeAdapter, CodexAdapter, engines

REPO = pathlib.Path(__file__).resolve().parents[4]


class FakeTx:
    """One open transaction: the receipts and the rows as they will be if it commits."""

    def __init__(self, receipts: dict, rows: dict) -> None:
        self.receipts, self.rows = receipts, rows

    def receipt(self, tenant, method, command_id):
        return self.receipts.get((tenant, method, command_id))

    def put_receipt(self, tenant, method, command_id, digest, answer) -> None:
        self.receipts[(tenant, method, command_id)] = (digest, dict(answer))

    def put(self, table, key, value) -> None:
        """A lane's own table, written in this transaction: its work intents, say."""
        self.rows[(table, key)] = value


class FakeStore:
    """``with store.transaction() as tx``: commits on a normal exit, rolls back when the block raises.

    By default one transaction is open at a time, as with a store that takes its write lock at the start.
    ``serial=False`` is a store that does NOT do that: two transactions can be open at once, each over the
    state as it was when it began. The dispatcher must not depend on which kind it is given.
    """

    def __init__(self, log: list | None = None, serial: bool = True) -> None:
        self._lock = threading.Lock() if serial else contextlib.nullcontext()
        self.receipts: dict = {}
        self.rows: dict = {}
        self.commits = self.rollbacks = 0
        self.fail_commit = False
        self.closed = False
        self._log = log

    @contextlib.contextmanager
    def transaction(self):
        with self._lock:
            tx = FakeTx(dict(self.receipts), dict(self.rows))
            try:
                yield tx
            except BaseException:
                self.rollbacks += 1
                raise
            if self.fail_commit:
                self.rollbacks += 1
                raise OSError('the commit failed')
            self.receipts, self.rows = tx.receipts, tx.rows
            self.commits += 1

    def close(self) -> None:
        self.closed = True
        if self._log is not None:
            self._log.append(('close', 'store', None))


class Cut(BaseException):
    """Stands for the process ending at this point: nothing after it runs."""


def quiet(test):
    """No log output from a test that makes a hook fail on purpose."""
    def wrapped(self):
        logging.disable(logging.CRITICAL)
        try:
            return test(self)
        finally:
            logging.disable(logging.NOTSET)
    wrapped.__name__, wrapped.__doc__ = test.__name__, test.__doc__
    return wrapped


class FakePackages:
    """Packages written to a temporary folder on ``sys.path``, with names no other test uses."""

    count = 0

    def __init__(self, case: unittest.TestCase) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._dir.name)
        FakePackages.count += 1
        self.prefix = f'seamfake{FakePackages.count}'
        sys.path.insert(0, str(self.root))
        case.addCleanup(self._clean)
        importlib.invalidate_caches()

    def _clean(self) -> None:
        sys.path.remove(str(self.root))
        for name in [name for name in sys.modules if name.split('.')[0].startswith(self.prefix)]:
            del sys.modules[name]
        self._dir.cleanup()
        importlib.invalidate_caches()

    def package(self, name: str, init: str = '', **modules: str) -> str:
        full = f'{self.prefix}_{name}'
        folder = self.root / full
        folder.mkdir()
        (folder / '__init__.py').write_text(init, encoding='utf-8')
        for module, text in modules.items():
            (folder / f'{module}.py').write_text(text, encoding='utf-8')
        importlib.invalidate_caches()
        return full


class RegistrationTest(unittest.TestCase):
    """Absent is not broken, and absent is decided before the import."""

    def setUp(self) -> None:
        self.fake = FakePackages(self)

    def test_nothing_is_installed_today_and_the_hooks_compose_with_their_defaults(self) -> None:
        composed = H.compose()
        self.assertFalse(composed.start_on)
        self.assertFalse(composed.gate.enabled)
        self.assertIsNone(composed.changes)
        self.assertEqual(composed.methods.registered(), ())
        self.assertEqual(composed.commands.names(), ())
        self.assertEqual(composed.engines.names(), ('claude_code', 'codex'))

    def test_a_registrant_whose_parent_package_is_absent_is_skipped(self) -> None:
        composed = H.compose((f'{self.fake.prefix}_nolane.hooks',))
        self.assertEqual(composed.methods.registered(), ())

    def test_a_package_with_no_hooks_module_is_skipped(self) -> None:
        package = self.fake.package('bare')
        composed = H.compose((f'{package}.hooks',))
        self.assertEqual(composed.methods.registered(), ())
        self.assertIn(package, sys.modules, 'the parent package is imported to look for its hooks module')

    def test_a_hooks_module_with_a_missing_dependency_fails_composition(self) -> None:
        package = self.fake.package('needs', hooks='import a_module_that_does_not_exist_anywhere\n\ndef register(hooks):\n    pass\n')
        with self.assertRaises(H.HookError) as caught:
            H.compose((f'{package}.hooks',))
        self.assertIn('ModuleNotFoundError', str(caught.exception))
        self.assertIsInstance(caught.exception.__cause__, ModuleNotFoundError)

    def test_a_hooks_module_that_raises_on_import_fails_composition(self) -> None:
        package = self.fake.package('boom', hooks='raise RuntimeError("at import")\n')
        with self.assertRaises(H.HookError):
            H.compose((f'{package}.hooks',))

    def test_a_register_that_raises_fails_composition(self) -> None:
        package = self.fake.package('late', hooks='def register(hooks):\n    raise RuntimeError("in register")\n')
        with self.assertRaises(H.HookError):
            H.compose((f'{package}.hooks',))

    def test_a_hooks_module_with_no_register_fails_composition(self) -> None:
        package = self.fake.package('empty', hooks='VALUE = 1\n')
        with self.assertRaises(H.HookError):
            H.compose((f'{package}.hooks',))

    def test_a_parent_package_that_raises_while_it_is_imported_fails_composition(self) -> None:
        package = self.fake.package('badparent', init='raise RuntimeError("the package itself")\n',
                                    hooks='def register(hooks):\n    pass\n')
        with self.assertRaises(H.HookError):
            H.compose((f'{package}.hooks',))

    def test_register_runs_once_and_registers_everything_the_lane_adds(self) -> None:
        package = self.fake.package('lane', hooks=(
            'CALLS = []\n'
            'def register(hooks):\n'
            '    CALLS.append(hooks)\n'
            '    hooks.set("read_result", lambda run_id: {"status": "ok", "code": None, "detail": run_id})\n'
            '    hooks.methods.register("session.list", lambda ctx, params: {"code": "ok", "sessions": []})\n'
            '    hooks.commands.register(("session",), lambda sub: sub.add_parser("session"), lambda args, client: 0)\n'
            '    hooks.engines.register("pi", lambda *a, **k: None)\n'))
        composed = H.compose((f'{package}.hooks',))
        module = sys.modules[f'{package}.hooks']
        self.assertEqual(module.CALLS, [composed])
        self.assertEqual(composed.read_result('run-1'), {'status': 'ok', 'code': None, 'detail': 'run-1'})
        self.assertEqual(composed.methods.registered(), ('session.list',))
        self.assertEqual(composed.commands.names(), ('session',))
        self.assertEqual(composed.engines.names(), ('claude_code', 'codex', 'pi'))

    def test_two_registrants_for_one_hook_fail_composition(self) -> None:
        text = 'def register(hooks):\n    hooks.set("authorize", lambda principal, method, params: None)\n'
        first, second = self.fake.package('one', hooks=text), self.fake.package('two', hooks=text)
        with self.assertRaises(H.HookError) as caught:
            H.compose((f'{first}.hooks', f'{second}.hooks'))
        self.assertIn('authorize', str(caught.exception))

    def test_two_registrants_for_one_method_fail_composition(self) -> None:
        text = 'def register(hooks):\n    hooks.methods.register("session.list", lambda ctx, params: {"code": "ok"})\n'
        first, second = self.fake.package('m1', hooks=text), self.fake.package('m2', hooks=text)
        with self.assertRaises(H.HookError):
            H.compose((f'{first}.hooks', f'{second}.hooks'))

    def test_after_composition_nothing_can_be_registered(self) -> None:
        composed = H.compose(())
        with self.assertRaises(H.HookError):
            composed.set('authorize', lambda principal, method, params: None)
        with self.assertRaises(H.HookError):
            composed.start_registrant('routing', (), (), lambda principal, request: {})
        with self.assertRaises(H.HookError):
            composed.service(Service('late'))
        with self.assertRaises(H.HookError):
            composed.enable_changes()
        with self.assertRaises(methods.MethodTableError):
            composed.methods.register('session.list', lambda ctx, params: {'code': 'ok'})
        with self.assertRaises(commands.CommandTableError):
            composed.commands.register(('x',), lambda sub: None, lambda args, client: 0)
        with self.assertRaises(engines.EngineTableError):
            composed.engines.register('pi', lambda *a, **k: None)

    def test_a_hook_nobody_defined_cannot_be_registered(self) -> None:
        with self.assertRaises(H.HookError):
            H.Hooks().set('approve_everything', lambda: True)

    def test_the_registrant_list_names_each_module_once(self) -> None:
        self.assertEqual(len(set(H.REGISTRANTS)), len(H.REGISTRANTS))
        self.assertTrue(all(name.startswith('recursion_core.') for name in H.REGISTRANTS))


class DefaultsTest(unittest.TestCase):
    """With nothing registered each hook does what the daemon does today."""

    def setUp(self) -> None:
        self.hooks = H.compose(())

    def test_the_default_authorizer_refuses_only_a_peer_inside_a_run_unit(self) -> None:
        self.assertIsNone(self.hooks.authorize(H.OWNER, 'run.stop', {'run_id': 'run-1'}))
        self.assertIsNone(self.hooks.authorize(H.DEVICE, 'run.stop', {'run_id': 'run-1'}))
        self.assertIsNone(self.hooks.authorize(H.Principal('session', 'ses-1'), 'run.start', {}))
        self.assertEqual(self.hooks.authorize(H.Principal('owner', 'local', in_run_unit=True), 'run.list', {}),
                         {'code': 'scope_refused', 'detail': 'peer is inside a run unit'})

    def test_the_default_credential_source_has_none(self) -> None:
        with self.assertRaises(KeyError) as caught:
            self.hooks.credentials('api_key:work-key', 'ANTHROPIC_API_KEY')
        self.assertEqual(caught.exception.args, ('api_key:work-key',))

    def test_the_other_defaults_add_nothing(self) -> None:
        ctx = object()
        self.assertIsNone(self.hooks.working_directory(ctx))
        self.assertIsNone(self.hooks.launch_check(ctx, '/usr/bin/engine'))
        self.assertIsNone(self.hooks.read_result('run-1'))
        self.assertIsNone(self.hooks.drive_owner(ctx, {'request_id': 'run-1'}))
        self.assertEqual(self.hooks.page_methods(), frozenset())
        self.assertIsNone(self.hooks.mark_changed('run-1'))
        self.assertIsNone(self.hooks.open_store('/nowhere'))
        self.assertIsNone(self.hooks.methods.store)

    def test_a_principal_is_one_of_five_kinds_and_shows_only_its_kind_and_label(self) -> None:
        self.assertEqual(H.Principal('plugin', 'notes.search').public(), {'kind': 'plugin', 'id': 'notes.search'})
        self.assertEqual(H.Principal('owner', 'local', in_run_unit=True).public(), {'kind': 'owner', 'id': 'local'})
        with self.assertRaises(ValueError):
            H.Principal('root')


class HookCallTest(unittest.TestCase):
    """Each single-valued hook: the fake is called with the documented arguments, and a failure does not fail open."""

    def hooks_with(self, name, function):
        made = H.Hooks()
        made.set(name, function)
        made.freeze()
        return made

    def test_authorize_is_asked_with_the_principal_the_method_and_the_params(self) -> None:
        seen = []
        made = self.hooks_with('authorize', lambda principal, method, params: seen.append((principal, method, params)))
        peer = H.Principal('owner', 'local', in_run_unit=True)
        self.assertIsNone(made.authorize(peer, 'run.list', {'limit': 5}))
        self.assertEqual(seen, [(peer, 'run.list', {'limit': 5})])
        # The registered authorizer took over the whole decision, the run-unit one included.

    def test_an_authorizer_refuses_with_a_code_or_a_code_and_a_detail(self) -> None:
        self.assertEqual(self.hooks_with('authorize', lambda *a: 'scope_refused').authorize(H.OWNER, 'run.stop', {}),
                         {'code': 'scope_refused', 'detail': ''})
        self.assertEqual(self.hooks_with('authorize', lambda *a: ('scope_refused', 'run')).authorize(H.DEVICE, 'run.stop', {}),
                         {'code': 'scope_refused', 'detail': 'run'})

    @quiet
    def test_an_authorizer_that_raises_refuses_the_call(self) -> None:
        def broken(principal, method, params):
            raise RuntimeError('policy store unreadable')
        self.assertEqual(self.hooks_with('authorize', broken).authorize(H.OWNER, 'run.stop', {}),
                         {'code': 'scope_refused', 'detail': 'authorizer_failed'})

    @quiet
    def test_an_authorizer_that_answers_nonsense_refuses_the_call(self) -> None:
        for nonsense in (True, 0, '', {'code': 'ok'}, ('scope_refused',)):
            with self.subTest(answer=nonsense):
                self.assertEqual(self.hooks_with('authorize', lambda *a, n=nonsense: n).authorize(H.OWNER, 'run.stop', {}),
                                 {'code': 'scope_refused', 'detail': 'authorizer_failed'})

    def test_credentials_are_asked_by_connection_and_variable_name(self) -> None:
        seen = []

        def source(connection_id, env_name):
            seen.append((connection_id, env_name))
            return 'not-a-real-secret'
        self.assertEqual(self.hooks_with('credentials', source).credentials('api_key:work-key', 'ANTHROPIC_API_KEY'),
                         'not-a-real-secret')
        self.assertEqual(seen, [('api_key:work-key', 'ANTHROPIC_API_KEY')])

    @quiet
    def test_a_credential_source_never_gives_none_or_nothing(self) -> None:
        def raises_other(connection_id, env_name):
            raise OSError('keyring locked')

        def raises_key(connection_id, env_name):
            raise KeyError('something else')
        for source in (lambda c, e: None, lambda c, e: '', lambda c, e: b'bytes', lambda c, e: 7, raises_other, raises_key):
            with self.subTest(source=source):
                with self.assertRaises(KeyError) as caught:
                    self.hooks_with('credentials', source).credentials('api_key:work-key', 'ANTHROPIC_API_KEY')
                self.assertEqual(caught.exception.args, ('api_key:work-key',))

    def test_the_working_directory_provider_is_asked_with_the_run_context(self) -> None:
        seen = []
        ctx = object()

        def provider(given):
            seen.append(given)
            return '/home/dev/.local/state/recursion/worktrees/ses-1'
        made = self.hooks_with('working_directory', provider)
        self.assertEqual(made.working_directory(ctx), pathlib.Path('/home/dev/.local/state/recursion/worktrees/ses-1'))
        self.assertEqual(seen, [ctx])
        self.assertIsNone(self.hooks_with('working_directory', lambda given: None).working_directory(ctx))

    @quiet
    def test_a_working_directory_provider_that_fails_refuses_the_launch(self) -> None:
        def broken(ctx):
            raise OSError('disk full')
        for provider in (broken, lambda ctx: 7, lambda ctx: ''):
            with self.subTest(provider=provider):
                with self.assertRaises(H.HookRefused) as caught:
                    self.hooks_with('working_directory', provider).working_directory(object())
                self.assertEqual(caught.exception.code, 'working_directory_failed')

    def test_the_launch_check_is_asked_with_the_run_context_and_the_resolved_executable(self) -> None:
        seen = []
        ctx = object()
        made = self.hooks_with('launch_check', lambda given, executable: seen.append((given, executable)))
        self.assertIsNone(made.launch_check(ctx, '/usr/local/bin/claude'))
        self.assertEqual(seen, [(ctx, '/usr/local/bin/claude')])

    @quiet
    def test_a_launch_check_refuses_with_its_code_and_a_failing_one_refuses_too(self) -> None:
        with self.assertRaises(H.HookRefused) as caught:
            self.hooks_with('launch_check', lambda ctx, executable: 'binary_changed').launch_check(object(), '/bin/x')
        self.assertEqual((caught.exception.code, caught.exception.detail), ('launch_check', 'binary_changed'))

        def broken(ctx, executable):
            raise RuntimeError('cannot read the binary')
        for check in (broken, lambda ctx, executable: False, lambda ctx, executable: ''):
            with self.subTest(check=check):
                with self.assertRaises(H.HookRefused) as caught:
                    self.hooks_with('launch_check', check).launch_check(object(), '/bin/x')
                self.assertEqual(caught.exception.code, 'launch_check_failed')

    @quiet
    def test_the_result_reader_is_asked_by_run_id_and_a_failing_one_is_no_result(self) -> None:
        seen = []

        def reader(run_id):
            seen.append(run_id)
            return {'status': 'ok', 'code': None, 'detail': ''}
        self.assertEqual(self.hooks_with('read_result', reader).read_result('run-7'), {'status': 'ok', 'code': None, 'detail': ''})
        self.assertEqual(seen, ['run-7'])

        def broken(run_id):
            raise OSError('event store closed')
        self.assertIsNone(self.hooks_with('read_result', broken).read_result('run-7'))
        self.assertIsNone(self.hooks_with('read_result', lambda run_id: 'ok').read_result('run-7'))

    @quiet
    def test_the_drive_owner_is_asked_with_the_context_and_the_request(self) -> None:
        seen = []
        ctx, request = object(), {'request_id': 'run-7'}

        class Owner:
            def run(self, *args):
                pass
        owner = Owner()

        def chooser(given, asked):
            seen.append((given, asked))
            return owner
        self.assertIs(self.hooks_with('drive_owner', chooser).drive_owner(ctx, request), owner)
        self.assertEqual(seen, [(ctx, request)])
        self.assertIsNone(self.hooks_with('drive_owner', lambda given, asked: None).drive_owner(ctx, request))

        def broken(given, asked):
            raise RuntimeError('before accepting')
        self.assertIsNone(self.hooks_with('drive_owner', broken).drive_owner(ctx, request), 'today\'s path drives the run')
        self.assertIsNone(self.hooks_with('drive_owner', lambda given, asked: object()).drive_owner(ctx, request))

    @quiet
    def test_page_methods_is_a_frozen_set_and_a_failing_one_is_empty(self) -> None:
        made = self.hooks_with('page_methods', lambda: {'session.turn', 'session.status'})
        self.assertEqual(made.page_methods(), frozenset({'session.turn', 'session.status'}))
        self.assertIsInstance(made.page_methods(), frozenset)

        def broken():
            raise RuntimeError('no')
        for names in (broken, lambda: ['session.turn'], lambda: {1, 2}):
            with self.subTest(names=names):
                self.assertEqual(self.hooks_with('page_methods', names).page_methods(), frozenset())

    def test_the_event_store_factory_is_called_once_with_the_state_directory(self) -> None:
        seen, store = [], FakeStore()

        def factory(state_dir):
            seen.append(state_dir)
            return store
        made = self.hooks_with('event_store', factory)
        self.assertIs(made.open_store('/state'), store)
        self.assertEqual(seen, ['/state'])
        self.assertIs(made.methods.store, store)
        self.assertTrue(made.gate.enabled)


class Handlers:
    """Handlers for the dispatcher's tests: they count their calls and can be made to wait."""

    def __init__(self) -> None:
        self.calls: list = []
        self.callbacks: list = []
        self.entered, self.release = threading.Event(), threading.Event()
        self.wait = False

    def turn(self, ctx, params):
        self.calls.append((ctx.principal, ctx.tx is not None, dict(params)))
        if self.wait:
            self.entered.set()
            self.release.wait()
        ctx.tx.put('work', params['command_id'], {'operation_id': 'op-' + params['command_id'], 'state': 'pending'})
        ctx.after_commit(lambda: self.callbacks.append(('wake', params['command_id'])))
        return {'code': 'ok', 'turn_id': 'turn-' + params['command_id']}


def table_with(store=None, **handlers):
    table = methods.MethodTable()
    for name, handler in handlers.items():
        table.register(name.replace('_', '.', 1) if '.' not in name else name, handler)
    table.store = store
    return table


TURN = {'command_id': 'cmd-1', 'session_id': 'ses-1', 'expected_revision': 3, 'text': 'go on'}


class MethodTableTest(unittest.TestCase):
    def test_the_table_lists_the_six_built_in_methods_and_every_other_one_once(self) -> None:
        self.assertEqual(tuple(methods.METHODS)[:6], methods.BUILT_IN)
        self.assertEqual(len(methods.METHODS), 38)
        self.assertEqual(methods.METHODS['run.start'].idempotency, 'client_key')
        self.assertEqual(methods.METHODS['run.stop'], methods.Row('run', True, 'none'))
        keyed = [name for name, row in methods.METHODS.items() if row.idempotency == 'command_id']
        self.assertTrue(all(methods.METHODS[name].mutating for name in keyed))
        self.assertEqual(sorted(name for name, row in methods.METHODS.items() if row.mutating and name not in keyed),
                         ['run.start', 'run.stop'], 'every mutating method but the two run ones takes a command id')
        scopes = {name: row.scope for name, row in methods.METHODS.items()}
        self.assertEqual({name for name, scope in scopes.items() if scope == 'approve'}, {'approval.open', 'approval.answer'})
        self.assertEqual({name for name, scope in scopes.items() if scope == 'manage'}, {'workspace.publish'})
        self.assertEqual(set(scopes.values()), {'read', 'run', 'approve', 'manage'})
        self.assertTrue(all((row.scope == 'read') == (not row.mutating) for row in methods.METHODS.values()),
                        'a method that changes nothing needs read, and only such a method does')

    def test_the_table_is_the_published_method_table(self) -> None:
        published = REPO / 'schemas' / 'control' / 'v1' / 'methods.table.v1.json'
        if not published.is_file():
            self.skipTest('schemas/control/v1/methods.table.v1.json is not in this checkout yet')
        rows = json.loads(published.read_text(encoding='utf-8'))['methods']
        self.assertEqual({row['name']: methods.Row(row['scope'], row['mutating'], row['idempotency']) for row in rows},
                         methods.METHODS)
        self.assertEqual([row['name'] for row in rows], list(methods.METHODS))

    def test_a_name_that_is_not_in_the_table_is_method_unknown_and_an_unbuilt_one_is_unsupported(self) -> None:
        table = methods.MethodTable()
        self.assertEqual(table.dispatch('session.kill', {}, H.OWNER, None), {'code': 'method_unknown', 'detail': 'session.kill'})
        self.assertEqual(table.dispatch('device.pair_begin', {}, H.OWNER, None),
                         {'code': 'method_unknown', 'detail': 'device.pair_begin'}, 'a reserved name has no row')
        self.assertEqual(table.dispatch(None, {}, H.OWNER, None), {'code': 'method_unknown', 'detail': 'None'})
        self.assertEqual(table.dispatch('session.turn', dict(TURN), H.OWNER, None),
                         {'code': 'unsupported', 'detail': 'not_implemented'})
        self.assertEqual(table.implemented(), methods.BUILT_IN)

    def test_a_handler_cannot_replace_a_built_in_method_or_take_a_name_outside_the_table(self) -> None:
        table = methods.MethodTable()
        for name in (*methods.BUILT_IN, 'run.resume', 'session.kill', 'device.list'):
            with self.subTest(name=name):
                with self.assertRaises(methods.MethodTableError):
                    table.register(name, lambda ctx, params: {'code': 'ok'})
        table.register('session.list', lambda ctx, params: {'code': 'ok', 'sessions': []})
        with self.assertRaises(methods.MethodTableError):
            table.register('session.list', lambda ctx, params: {'code': 'ok', 'sessions': []})
        with self.assertRaises(methods.MethodTableError):
            table.register('session.status', 'not a function')
        self.assertEqual(table.implemented(), (*methods.BUILT_IN, 'session.list'))

    def test_a_read_only_handler_gets_the_principal_and_no_transaction(self) -> None:
        seen = []

        def listing(ctx, params):
            seen.append((ctx.principal, ctx.tx, params))
            return {'code': 'ok', 'sessions': []}
        table = table_with(FakeStore(), **{'session.list': listing})
        self.assertEqual(table.dispatch('session.list', {'limit': 5}, H.DEVICE, None), {'code': 'ok', 'sessions': []})
        self.assertEqual(seen, [(H.DEVICE, None, {'limit': 5})])
        self.assertEqual(table.store.commits, 0, 'the dispatcher opens no transaction for a method with no command id')

    def test_with_no_event_store_a_command_is_unsupported_and_the_handler_is_not_called(self) -> None:
        handlers = Handlers()
        table = table_with(None, **{'session.turn': handlers.turn})
        self.assertEqual(table.dispatch('session.turn', dict(TURN), H.OWNER, None), {'code': 'unsupported', 'detail': 'store_absent'})
        self.assertEqual(handlers.calls, [])

    def test_a_command_runs_in_the_dispatchers_transaction_and_leaves_a_receipt(self) -> None:
        handlers, store = Handlers(), FakeStore()
        table = table_with(store, **{'session.turn': handlers.turn})
        answer = table.dispatch('session.turn', dict(TURN), H.OWNER, None)
        self.assertEqual(answer, {'code': 'ok', 'turn_id': 'turn-cmd-1'})
        self.assertEqual(handlers.calls, [(H.OWNER, True, TURN)])
        self.assertEqual((store.commits, store.rollbacks), (1, 0))
        digest = hashlib.sha256(b'{"expected_revision":3,"session_id":"ses-1","text":"go on"}').hexdigest()
        self.assertEqual(store.receipts, {('', 'session.turn', 'cmd-1'): (digest, answer)})
        self.assertEqual(store.rows, {('work', 'cmd-1'): {'operation_id': 'op-cmd-1', 'state': 'pending'}},
                         'the work intent is in the same commit as the receipt')
        self.assertEqual(handlers.callbacks, [('wake', 'cmd-1')])

    def test_the_same_command_again_returns_the_first_answer_and_runs_nothing(self) -> None:
        handlers, store = Handlers(), FakeStore()
        table = table_with(store, **{'session.turn': handlers.turn})
        first = table.dispatch('session.turn', dict(TURN), H.OWNER, None)
        again = table.dispatch('session.turn', dict(TURN), H.OWNER, None)
        self.assertEqual(again, first)
        self.assertEqual(len(handlers.calls), 1)
        self.assertEqual(handlers.callbacks, [('wake', 'cmd-1')], 'a replay stages no callback')
        self.assertEqual(len(store.rows), 1, 'and no second work intent')

    def test_the_same_id_with_another_payload_is_a_conflict(self) -> None:
        handlers, store = Handlers(), FakeStore()
        table = table_with(store, **{'session.turn': handlers.turn})
        table.dispatch('session.turn', dict(TURN), H.OWNER, None)
        self.assertEqual(table.dispatch('session.turn', dict(TURN, text='something else'), H.OWNER, None),
                         {'code': 'conflict', 'detail': 'command_id_reused'})
        self.assertEqual(len(handlers.calls), 1)

    def test_the_identity_is_the_tenant_the_method_and_the_id(self) -> None:
        handlers, store = Handlers(), FakeStore()
        table = table_with(store, **{'session.turn': handlers.turn, 'session.queue': handlers.turn})
        table.dispatch('session.turn', dict(TURN), H.OWNER, None)
        table.dispatch('session.queue', dict(TURN), H.OWNER, None)
        table.dispatch('session.turn', dict(TURN, tenant='t2'), H.OWNER, None)
        self.assertEqual(sorted(store.receipts), [('', 'session.queue', 'cmd-1'), ('', 'session.turn', 'cmd-1'),
                                                  ('t2', 'session.turn', 'cmd-1')])
        self.assertEqual(len(handlers.calls), 3)

    def test_a_command_with_no_id_or_with_params_that_have_no_text_is_refused_before_the_handler(self) -> None:
        handlers = Handlers()
        table = table_with(FakeStore(), **{'session.turn': handlers.turn})
        for params, detail in (({'session_id': 'ses-1'}, 'command_id'), (dict(TURN, command_id=''), 'command_id'),
                               (dict(TURN, command_id=7), 'command_id'), (dict(TURN, budget=2**60), 'params'),
                               (dict(TURN, weight=float('nan')), 'params'), (dict(TURN, text='\ud800'), 'params')):
            with self.subTest(params=params):
                self.assertEqual(table.dispatch('session.turn', params, H.OWNER, None), {'code': 'request_invalid', 'detail': detail})
        self.assertEqual(handlers.calls, [])
        self.assertEqual(table.store.commits, 0)

    def test_two_identical_commands_at_once_run_the_handler_once(self) -> None:
        # The store here lets two transactions be open at once, so nothing but the dispatcher's own slot
        # keeps the second command out of the handler while the first is in it.
        handlers, store = Handlers(), FakeStore(serial=False)
        handlers.wait = True
        self.addCleanup(handlers.release.set)
        table = table_with(store, **{'session.turn': handlers.turn})
        answers = []
        threads = [threading.Thread(target=lambda: answers.append(table.dispatch('session.turn', dict(TURN), H.OWNER, None)),
                                    daemon=True) for _ in range(2)]
        threads[0].start()
        handlers.entered.wait()
        threads[1].start()
        key = ('', 'session.turn', 'cmd-1')
        while table._slots[key][1] < 2:   # the second command has arrived and waits for the first
            time.sleep(0.001)
        self.assertEqual(len(handlers.calls), 1, 'the second command did not enter the handler beside the first')
        handlers.release.set()
        for thread in threads:
            thread.join()
        self.assertEqual(len(handlers.calls), 1)
        self.assertEqual(answers, [{'code': 'ok', 'turn_id': 'turn-cmd-1'}] * 2)
        self.assertEqual(handlers.callbacks, [('wake', 'cmd-1')])
        self.assertEqual(store.commits, 2, 'the second commit is the replay\'s own, with nothing in it')
        self.assertEqual(table._slots, {}, 'the slot goes when nobody waits on it')

    @quiet
    def test_a_handler_that_raises_is_rolled_back_and_no_callback_runs(self) -> None:
        ran, store = [], FakeStore()

        def broken(ctx, params):
            ctx.tx.put('work', 'x', 'half done')
            ctx.after_commit(lambda: ran.append('callback'))
            raise RuntimeError('half way')
        table = table_with(store, **{'session.turn': broken})
        self.assertEqual(table.dispatch('session.turn', dict(TURN), H.OWNER, None), {'code': 'unavailable', 'detail': 'handler:RuntimeError'})
        self.assertEqual((store.commits, store.rollbacks, store.receipts, store.rows), (0, 1, {}, {}))
        self.assertEqual(ran, [])
        self.assertEqual(table._slots, {})

    def test_a_handler_that_does_not_accept_the_command_leaves_no_receipt(self) -> None:
        ran, store, busy = [], FakeStore(), [True]

        def turn(ctx, params):
            ctx.after_commit(lambda: ran.append('callback'))
            if busy[0]:
                raise methods.NotAccepted({'code': 'busy', 'session_id': params['session_id']})
            return {'code': 'ok'}
        table = table_with(store, **{'session.turn': turn})
        self.assertEqual(table.dispatch('session.turn', dict(TURN), H.OWNER, None), {'code': 'busy', 'session_id': 'ses-1'})
        self.assertEqual((store.receipts, ran), ({}, []))
        busy[0] = False
        self.assertEqual(table.dispatch('session.turn', dict(TURN), H.OWNER, None), {'code': 'ok'}, 'the same id may be sent again')
        self.assertEqual(ran, ['callback'])

    @quiet
    def test_a_commit_that_fails_answers_unavailable_and_runs_no_callback(self) -> None:
        handlers, store = Handlers(), FakeStore()
        store.fail_commit = True
        table = table_with(store, **{'session.turn': handlers.turn})
        self.assertEqual(table.dispatch('session.turn', dict(TURN), H.OWNER, None), {'code': 'unavailable', 'detail': 'store:OSError'})
        self.assertEqual((store.receipts, store.rows, handlers.callbacks), ({}, {}, []))

    @quiet
    def test_a_handler_that_returns_no_answer_is_rolled_back(self) -> None:
        store = FakeStore()
        table = table_with(store, **{'session.turn': lambda ctx, params: None, 'session.list': lambda ctx, params: 'ok'})
        self.assertEqual(table.dispatch('session.turn', dict(TURN), H.OWNER, None), {'code': 'unavailable', 'detail': 'handler:TypeError'})
        self.assertEqual(table.dispatch('session.list', {}, H.OWNER, None), {'code': 'unavailable', 'detail': 'handler:TypeError'})
        self.assertEqual(store.receipts, {})

    @quiet
    def test_a_callback_that_fails_leaves_the_receipt_and_the_later_callbacks_run(self) -> None:
        ran, store = [], FakeStore()

        def turn(ctx, params):
            ctx.after_commit(lambda: ran.append('first'))
            ctx.after_commit(lambda: 1 / 0)
            ctx.after_commit(lambda: ran.append('third'))
            return {'code': 'ok'}
        table = table_with(store, **{'session.turn': turn})
        self.assertEqual(table.dispatch('session.turn', dict(TURN), H.OWNER, None), {'code': 'ok'})
        self.assertEqual(ran, ['first', 'third'])
        self.assertEqual(list(store.receipts), [('', 'session.turn', 'cmd-1')])

    def test_callbacks_run_after_the_commit_and_outside_the_commands_slot(self) -> None:
        seen, store = [], FakeStore()

        def turn(ctx, params):
            seen.append(('handler', store.commits, len(table._slots), store._lock.locked()))
            ctx.after_commit(lambda: seen.append(('callback', store.commits, len(table._slots), store._lock.locked())))
            return {'code': 'ok'}
        table = table_with(store, **{'session.turn': turn})
        table.dispatch('session.turn', dict(TURN), H.OWNER, None)
        self.assertEqual(seen, [('handler', 0, 1, True), ('callback', 1, 0, False)],
                         'the handler runs inside the slot and the transaction; the callback after both')

    def test_a_committed_intent_is_still_there_when_the_process_is_cut_before_the_callback(self) -> None:
        handlers, store = Handlers(), FakeStore()
        woken = []

        def turn(ctx, params):
            ctx.tx.put('work', params['command_id'], {'operation_id': 'op-1', 'state': 'pending'})
            ctx.after_commit(lambda: (_ for _ in ()).throw(Cut()))
            ctx.after_commit(lambda: woken.append('worker'))
            handlers.calls.append(params['command_id'])
            return {'code': 'ok', 'accepted': True}
        table = table_with(store, **{'session.turn': turn})
        with self.assertRaises(Cut):
            table.dispatch('session.turn', dict(TURN), H.OWNER, None)
        self.assertEqual(woken, [], 'the callback that would have woken the worker never ran')
        self.assertEqual(store.rows, {('work', 'cmd-1'): {'operation_id': 'op-1', 'state': 'pending'}},
                         'the intent is committed: a service that scans for pending work finds it')
        self.assertIn(('', 'session.turn', 'cmd-1'), store.receipts)
        # The client retries after the restart: the receipt answers, and the work is not staged again.
        self.assertEqual(table.dispatch('session.turn', dict(TURN), H.OWNER, None), {'code': 'ok', 'accepted': True})
        self.assertEqual(handlers.calls, ['cmd-1'])
        self.assertEqual(woken, [])
        self.assertEqual(len(store.rows), 1)

    def test_a_nested_call_reaches_the_other_methods_and_never_another_command(self) -> None:
        asked, store = [], FakeStore()

        def call(method, params):
            asked.append((method, params))
            if method != 'run.stop':
                return {'code': 'method_unknown', 'detail': method}
            return {'code': 'stopped', 'run_id': params['run_id']}

        def stop(ctx, params):
            return {'code': 'ok', 'run': ctx.call('run.stop', {'run_id': 'run-1'}),
                    'nested': ctx.call('session.turn', dict(TURN)), 'unknown': ctx.call('session.kill')}
        table = table_with(store, **{'session.stop': stop})
        answer = table.dispatch('session.stop', {'command_id': 'cmd-9', 'session_id': 'ses-1'}, H.OWNER, call)
        self.assertEqual(answer['run'], {'code': 'stopped', 'run_id': 'run-1'})
        self.assertEqual(answer['nested'], {'code': 'request_invalid', 'detail': 'nested_command'})
        self.assertEqual(asked[0], ('run.stop', {'run_id': 'run-1'}))
        self.assertEqual(asked[1], ('session.kill', None), 'a name with no row goes to the caller, which answers as today')
        self.assertEqual(answer['unknown'], {'code': 'method_unknown', 'detail': 'session.kill'})
        self.assertEqual(len(asked), 2, 'the nested command never reached the caller')

    def test_a_method_waits_for_its_service_before_any_transaction_is_opened(self) -> None:
        handlers, store, ready = Handlers(), FakeStore(), [False]
        table = methods.MethodTable()
        table.register('session.turn', handlers.turn, service='sessions')
        table.register('session.list', lambda ctx, params: {'code': 'ok', 'sessions': []})
        table.store, table.service_ready = store, lambda name: name == 'sessions' and ready[0]
        self.assertEqual(table.dispatch('session.turn', dict(TURN), H.OWNER, None), {'code': 'unavailable', 'detail': 'service_starting'})
        self.assertEqual((handlers.calls, store.commits, store.rollbacks), ([], 0, 0))
        self.assertEqual(table.dispatch('session.list', {}, H.OWNER, None)['code'], 'ok', 'a method that needs no service does not wait')
        ready[0] = True
        self.assertEqual(table.dispatch('session.turn', dict(TURN), H.OWNER, None)['code'], 'ok')
        self.assertEqual(table.services_needed(), {'session.turn': 'sessions'})

    def test_a_handler_is_given_the_closing_signal(self) -> None:
        seen = []
        table = table_with(None, **{'events.follow': lambda ctx, params: seen.append(ctx.closing) or {'code': 'ok'}})
        table.dispatch('events.follow', {}, H.OWNER, None)
        self.assertIs(seen[0], table.closing)
        self.assertFalse(seen[0].is_set())

    def test_the_payload_digest_is_over_one_text_whatever_the_spelling(self) -> None:
        text = methods.canonical_text
        self.assertEqual(text({'b': 1.0, 'a': [True, None, 'x']}), '{"a":[true,null,"x"],"b":1}')
        self.assertEqual(text([1e-7, 1E3, -0.0, 0.1, 1e21, 1.5e300, 100.50, 2**53 - 1]),
                         '[1e-7,1000,0,0.1,1e+21,1.5e+300,100.5,9007199254740991]')
        self.assertEqual(text({'': 1, '\U00010000': 2}), '{"\U00010000":2,"":1}', 'keys in UTF-16 order')
        self.assertEqual(text('café — "q"\n\x01'), '"café — \\"q\\"\\n\\u0001"')
        for value in (2**53, -2**53, float('inf'), '\udc00', {1: 'x'}, object()):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    text(value)
        one = methods.payload_digest({'command_id': 'a', 'n': 1.0, 'text': 'x'})
        self.assertEqual(one, methods.payload_digest({'text': 'x', 'n': 1, 'command_id': 'another id'}))
        self.assertNotEqual(one, methods.payload_digest({'text': 'x', 'n': 2}))


class CommandTableTest(unittest.TestCase):
    def parser(self):
        parser = argparse.ArgumentParser(prog='recursion-control')
        sub = parser.add_subparsers(dest='command', required=True)
        sub.add_parser('list')
        start = sub.add_parser('start')
        start.add_argument('--tenant', required=True)
        return parser, sub

    def test_a_family_adds_its_commands_and_is_dispatched_with_the_parsed_arguments_and_the_client(self) -> None:
        seen = []

        def add(sub):
            sub.add_parser('session').add_argument('session_id')
            sub.add_parser('sessions')

        def dispatch(args, client):
            seen.append((args.command, getattr(args, 'session_id', None), client))
            return 3
        table = commands.CommandTable()
        table.register(('session', 'sessions'), add, dispatch)
        table.freeze()
        parser, sub = self.parser()
        table.add_parsers(sub)
        client = object()
        self.assertEqual(table.dispatch(parser.parse_args(['session', 'ses-1']), client), 3)
        self.assertEqual(seen, [('session', 'ses-1', client)])
        self.assertIn('sessions', table)
        self.assertNotIn('start', table, 'the CLI\'s own commands are not in the table')
        self.assertEqual(table.names(), ('session', 'sessions'))

    def test_the_clis_own_names_and_a_name_already_taken_cannot_be_registered(self) -> None:
        table = commands.CommandTable()
        for name in commands.BUILT_IN:
            with self.subTest(name=name):
                with self.assertRaises(commands.CommandTableError):
                    table.register((name,), lambda sub: None, lambda args, client: 0)
        table.register(('session',), lambda sub: sub.add_parser('session'), lambda args, client: 0)
        for names in (('session',), ('queue', 'session'), ('a', 'a'), (), 'session', ('',)):
            with self.subTest(names=names):
                with self.assertRaises(commands.CommandTableError):
                    table.register(names, lambda sub: None, lambda args, client: 0)
        with self.assertRaises(commands.CommandTableError):
            table.register(('queue',), 'not a function', lambda args, client: 0)

    def test_a_parser_that_adds_another_name_or_leaves_one_out_is_refused(self) -> None:
        for names, add in ((('session',), lambda sub: (sub.add_parser('session'), sub.add_parser('extra'))),
                           (('session', 'queue'), lambda sub: sub.add_parser('session')),
                           (('session',), lambda sub: None)):
            with self.subTest(names=names):
                table = commands.CommandTable()
                table.register(names, add, lambda args, client: 0)
                with self.assertRaises(commands.CommandTableError):
                    table.add_parsers(self.parser()[1])

    def test_a_parser_cannot_add_an_option_to_a_command_another_keeps(self) -> None:
        def add(sub):
            sub.add_parser('workspace')
            sub.choices['start'].add_argument('--workspace')
        table = commands.CommandTable()
        table.register(('workspace',), add, lambda args, client: 0)
        with self.assertRaises(commands.CommandTableError) as caught:
            table.add_parsers(self.parser()[1])
        self.assertIn('start', str(caught.exception))

    def test_a_dispatch_returns_an_exit_status(self) -> None:
        table = commands.CommandTable()
        table.register(('session',), lambda sub: sub.add_parser('session'), lambda args, client: 'ok')
        with self.assertRaises(commands.CommandTableError):
            table.dispatch(argparse.Namespace(command='session'), None)

    def test_an_unregistered_name_is_still_a_usage_error_of_the_parser(self) -> None:
        table = commands.CommandTable()
        table.register(('session',), lambda sub: sub.add_parser('session'), lambda args, client: 0)
        parser, sub = self.parser()
        table.add_parsers(sub)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            parser.parse_args(['sesion'])
        self.assertEqual(caught.exception.code, 2)


class EngineTableTest(unittest.TestCase):
    def test_a_new_table_holds_todays_two_adapters_in_todays_order(self) -> None:
        table = engines.EngineTable()
        self.assertEqual(list(table.items()), [('claude_code', ClaudeCodeAdapter), ('codex', CodexAdapter)])
        self.assertEqual(table.names(), engines.SEEDED)
        self.assertIn('codex', table)
        self.assertNotIn('pi', table)

    def test_the_catalogue_has_ten_names_and_a_name_alone_adds_nothing(self) -> None:
        self.assertEqual(len(engines.CATALOGUE), 10)
        self.assertEqual(len(set(engines.CATALOGUE)), 10)
        self.assertEqual(engines.CATALOGUE[:2], engines.SEEDED)
        self.assertEqual(engines.EngineTable().names(), ('claude_code', 'codex'))

    def test_an_engine_lane_registers_its_factory_after_todays_two(self) -> None:
        table, factory = engines.EngineTable(), lambda launcher, clock, credentials, environ, **options: None
        table.register('pi', factory)
        self.assertEqual(table.names(), ('claude_code', 'codex', 'pi'))
        self.assertEqual(list(table.items())[2], ('pi', factory))

    def test_a_second_factory_for_a_name_is_refused_unless_it_replaces_a_seeded_one_on_purpose(self) -> None:
        table, factory = engines.EngineTable(), lambda *a, **k: None
        table.register('pi', factory)
        for name, replace in (('pi', False), ('pi', True), ('codex', False), ('not_an_engine', False), ('hermes', True)):
            with self.subTest(name=name, replace=replace):
                with self.assertRaises(engines.EngineTableError):
                    table.register(name, factory, replace=replace)
        table.register('claude_code', factory, replace=True)
        self.assertEqual(list(table.items())[0], ('claude_code', factory), 'a replaced engine keeps its place')
        self.assertEqual(table.names(), ('claude_code', 'codex', 'pi'))
        with self.assertRaises(engines.EngineTableError):
            table.register('claude_code', factory, replace=True)
        with self.assertRaises(engines.EngineTableError):
            table.register('hermes', 'not a function')

    def test_two_compositions_do_not_share_a_table(self) -> None:
        one, two = H.compose(()), H.compose(())
        self.assertIsNot(one.engines, two.engines)
        self.assertEqual(two.engines.names(), ('claude_code', 'codex'))


class ChangeMarkerTest(unittest.TestCase):
    def test_marks_are_taken_once_as_a_set(self) -> None:
        marker = H.ChangeMarker()
        for run_id in ('run-a', 'run-b', 'run-a'):
            marker.mark(run_id)
        self.assertEqual(marker.take(), ({'run-a', 'run-b'}, False))
        self.assertEqual(marker.take(), (set(), False))
        self.assertEqual(marker.poll_interval_s, 0.1)

    def test_past_the_cap_a_mark_is_dropped_and_said_so(self) -> None:
        marker = H.ChangeMarker()
        for number in range(H.ChangeMarker.CAP):
            marker.mark(f'run-{number}')
        marker.mark('run-0')
        self.assertFalse(marker._dropped, 'an id that is already there is not a drop')
        marker.mark('run-one-more')
        ids, dropped = marker.take()
        self.assertEqual((len(ids), dropped, 'run-one-more' in ids), (4096, True, False))
        self.assertEqual(marker.take(), (set(), False), 'the flag is cleared with the set')

    def test_a_mark_never_waits_for_a_consumer_that_holds_the_markers_lock(self) -> None:
        marker = H.ChangeMarker()
        marker.mark('run-before')
        marker._lock.acquire()          # the consumer is paused inside take()
        try:
            marker.mark('run-during')   # returns at once: this line would never return if it waited
        finally:
            marker._lock.release()
        self.assertEqual(marker.take(), ({'run-before'}, True), 'the mark is dropped, and the flag asks for a scan')

    def test_the_hook_marks_nothing_until_a_lane_turns_the_marker_on_and_never_raises(self) -> None:
        made = H.Hooks()
        self.assertIsNone(made.mark_changed('run-1'))
        marker = made.enable_changes()
        with self.assertRaises(H.HookError):
            made.enable_changes()
        made.freeze()
        made.mark_changed('run-1')
        self.assertIsNone(made.mark_changed(['not', 'hashable']), 'a mark that cannot be kept is swallowed')
        self.assertEqual(marker.take(), ({'run-1'}, False))
        self.assertIs(made.changes, marker)


class Service:
    def __init__(self, name, log=None, fail_start=False, fail_stop=False) -> None:
        self.name, self.ready = name, False
        self._log = log if log is not None else []
        self._fail_start, self._fail_stop = fail_start, fail_stop

    def start(self, context) -> None:
        self._log.append(('start', self.name, context))
        if self._fail_start:
            raise RuntimeError(f'{self.name} cannot start')

    def stop(self, context) -> None:
        self._log.append(('stop', self.name, context))
        if self._fail_stop:
            raise RuntimeError(f'{self.name} cannot stop')


class ServicesTest(unittest.TestCase):
    def hooks_with(self, *services, **methods_needing):
        made = H.Hooks()
        for service in services:
            made.service(service)
        for method, needs in methods_needing.items():
            made.methods.register(method.replace('_', '.'), lambda ctx, params: {'code': 'ok'}, service=needs)
        made.freeze()
        return made

    def test_services_start_in_order_with_the_context_and_stop_in_reverse(self) -> None:
        log, context = [], H.ServiceContext(state_dir=pathlib.Path('/state'))
        made = self.hooks_with(Service('events', log), Service('sessions', log), Service('approvals', log))
        made.start_services(context)
        made.stop_services(context)
        self.assertEqual(log, [('start', 'events', context), ('start', 'sessions', context), ('start', 'approvals', context),
                               ('stop', 'approvals', context), ('stop', 'sessions', context), ('stop', 'events', context)])
        made.stop_services(context)
        self.assertEqual(len(log), 6, 'a service is stopped once')

    @quiet
    def test_a_service_that_does_not_start_stops_the_ones_before_it_and_fails_composition(self) -> None:
        log, context = [], H.ServiceContext()
        made = self.hooks_with(Service('events', log), Service('sessions', log, fail_start=True), Service('approvals', log))
        with self.assertRaises(RuntimeError):
            made.start_services(context)
        self.assertEqual([(what, name) for what, name, _ in log], [('start', 'events'), ('start', 'sessions'), ('stop', 'events')])
        self.assertFalse(made.service_ready('events'))

    @quiet
    def test_every_service_is_stopped_even_when_one_fails_and_the_failure_is_raised_last(self) -> None:
        log, context = [], H.ServiceContext()
        made = self.hooks_with(Service('events', log), Service('sessions', log, fail_stop=True), Service('approvals', log))
        made.start_services(context)
        with self.assertRaises(RuntimeError) as caught:
            made.stop_services(context)
        self.assertIn('sessions', str(caught.exception))
        self.assertEqual([name for what, name, _ in log if what == 'stop'], ['approvals', 'sessions', 'events'])

    def test_a_method_that_needs_a_service_is_unavailable_until_it_is_ready(self) -> None:
        sessions = Service('sessions')
        made = self.hooks_with(sessions, session_list='sessions')
        answer = lambda: made.methods.dispatch('session.list', {}, H.OWNER, None)   # noqa: E731
        self.assertEqual(answer(), {'code': 'unavailable', 'detail': 'service_starting'}, 'registered and not started')
        made.start_services(H.ServiceContext())
        self.assertEqual(answer(), {'code': 'unavailable', 'detail': 'service_starting'}, 'started and not ready')
        sessions.ready = True
        self.assertEqual(answer(), {'code': 'ok'})
        made.stop_services(H.ServiceContext())
        self.assertEqual(answer(), {'code': 'unavailable', 'detail': 'service_starting'}, 'stopped')

    def test_a_method_cannot_need_a_service_nobody_registered(self) -> None:
        with self.assertRaises(H.HookError) as caught:
            self.hooks_with(Service('events'), session_list='sessions')
        self.assertIn('sessions', str(caught.exception))

    def test_a_service_has_a_name_a_start_a_stop_and_ready_and_a_name_is_used_once(self) -> None:
        made = H.Hooks()
        made.service(Service('events'))
        for bad in (Service('events'), Service(''), object(), type('NoReady', (), {'name': 'x', 'start': print, 'stop': print})()):
            with self.subTest(service=bad):
                with self.assertRaises(H.HookError):
                    made.service(bad)

    def test_the_service_context_names_everything_a_service_is_given(self) -> None:
        self.assertEqual(list(H.ServiceContext.__dataclass_fields__),
                         ['daemon', 'registry', 'control_registry', 'control', 'store', 'state_dir', 'clock', 'closing'])


class AdmissionTest(unittest.TestCase):
    def test_with_nothing_registered_the_gate_refuses_nobody_and_waits_for_nothing(self) -> None:
        made = H.compose(())
        self.assertFalse(made.gate.enabled)
        self.assertTrue(made.gate.enter())
        made.begin_close()
        self.assertTrue(made.gate.enter(), 'as today: a call after closing began is still answered')
        made.finish_close(H.ServiceContext())   # returns: nothing to wait for, although nobody left
        self.assertTrue(made.closing.is_set())

    def test_a_handler_a_service_or_a_store_each_turn_the_gate_on(self) -> None:
        for register in (lambda made: made.methods.register('session.list', lambda ctx, params: {'code': 'ok'}),
                         lambda made: made.service(Service('events')),
                         lambda made: made.set('event_store', lambda state_dir: FakeStore())):
            made = H.Hooks()
            register(made)
            made.freeze()
            self.assertTrue(made.gate.enabled)
        made = H.Hooks()
        made.set('authorize', lambda principal, method, params: None)
        made.freeze()
        self.assertFalse(made.gate.enabled, 'a hook that holds no resource does not need the gate')

    def test_no_call_is_admitted_after_closing_began(self) -> None:
        gate = H.Admission()
        gate.enabled = True
        self.assertTrue(gate.enter())
        gate.close()
        self.assertFalse(gate.enter())
        self.assertEqual(gate.admitted, 1, 'the call admitted before is still inside')
        gate.leave()
        self.assertEqual(gate.admitted, 0)

    def test_closing_waits_for_an_admitted_call_with_its_services_and_store_alive(self) -> None:
        log = []
        store = FakeStore(log)
        made = H.Hooks()
        made.service(Service('sessions', log))
        made.set('event_store', lambda state_dir: store)
        made.freeze()
        context = H.ServiceContext()
        made.open_store('/state')
        made.start_services(context)
        self.assertTrue(made.gate.enter())          # a handler, or its after-commit callback, is running
        made.begin_close()
        self.assertFalse(made.gate.enter())
        finished, at_the_drain, drain = threading.Event(), threading.Event(), made.gate.drain
        made.gate.drain = lambda: (at_the_drain.set(), drain())
        closer = threading.Thread(target=lambda: (made.finish_close(context), finished.set()), daemon=True)
        closer.start()
        at_the_drain.wait()
        # The closer has reached the drain. Everything it did before it is in the log: nothing was stopped
        # or closed under the admitted call, and it cannot get past the drain while the call is inside.
        self.assertEqual([what for what, _, _ in log], ['start'])
        self.assertFalse(store.closed)
        self.assertFalse(finished.is_set())
        made.gate.leave()
        closer.join()
        self.assertTrue(finished.is_set())
        self.assertEqual([(what, name) for what, name, _ in log], [('start', 'sessions'), ('stop', 'sessions'), ('close', 'store')],
                         'the services are stopped after the drain, and the store is closed last')
        self.assertIsNone(made.methods.store)

    @quiet
    def test_a_stalled_drain_is_logged_and_goes_on_waiting(self) -> None:
        gate = H.Admission()
        gate.enabled, gate.stall_log_s = True, 0.001
        gate.enter()
        gate.close()
        waits, original = [], gate._changed.wait

        def wait(timeout):
            waits.append(timeout)
            return original(timeout)
        gate._changed.wait = wait
        drained = threading.Event()
        closer = threading.Thread(target=lambda: (gate.drain(), drained.set()), daemon=True)
        closer.start()
        while len(waits) < 3 and closer.is_alive():   # two stall intervals have passed, or the drain gave up
            time.sleep(0.001)
        self.assertTrue(closer.is_alive(), 'a drain that stalled is still waiting')
        self.assertFalse(drained.is_set())
        gate.leave()
        closer.join()
        self.assertTrue(drained.is_set())

    @quiet
    def test_a_teardown_error_is_raised_only_after_everything_was_stopped_and_closed(self) -> None:
        log = []
        store = FakeStore(log)
        made = H.Hooks()
        made.service(Service('events', log))
        made.service(Service('sessions', log, fail_stop=True))
        made.set('event_store', lambda state_dir: store)
        made.freeze()
        context = H.ServiceContext()
        made.open_store('/state')
        made.start_services(context)
        made.begin_close()
        with self.assertRaises(RuntimeError):
            made.finish_close(context)
        self.assertEqual([(what, name) for what, name, _ in log if what != 'start'],
                         [('stop', 'sessions'), ('stop', 'events'), ('close', 'store')])
        self.assertTrue(store.closed)


def routing(principal, request):
    return {'route_entry': {'route': {'engine': 'codex', 'model': 'gpt-6', 'connection': {'kind': 'subscription', 'id': 'plus'}},
                            'deadline_s': 600.0, 'binary': None},
            'route_decision': {'chosen': 'codex', 'reason': 'hand_pick'}}


class StartRegistrantTest(unittest.TestCase):
    """The parts of the versioned start path that live here: who declared what, the identity, the snapshot."""

    def hooks_with(self, *registrants):
        made = H.Hooks()
        for name, fields, keys, normalize, *more in registrants:
            made.start_registrant(name, fields, keys, normalize, chooses_engine=bool(more and more[0]))
        made.freeze()
        return made

    def test_registering_one_turns_the_versioned_path_on(self) -> None:
        self.assertFalse(H.compose(()).start_on)
        made = self.hooks_with(('workspace', ('workspace',), ('workspace',), lambda principal, request: {}))
        self.assertTrue(made.start_on)
        self.assertEqual(made.declared_fields(), frozenset({'workspace'}))

    def test_registrants_run_routing_first_then_workspace_then_the_others_by_name(self) -> None:
        order = []

        def named(name):
            return lambda principal, request: order.append(name) or {}
        made = self.hooks_with(*((name, (), (), named(name)) for name in ('sessions', 'workspace', 'quota', 'routing', 'containment')))
        self.assertEqual(made.start_order(), ('routing', 'workspace', 'containment', 'quota', 'sessions'))
        made.launch_snapshot(H.OWNER, {})
        self.assertEqual(order, ['routing', 'workspace', 'containment', 'quota', 'sessions'])

    def test_a_registrant_is_asked_with_the_principal_and_the_request_and_sets_only_its_keys(self) -> None:
        seen = []

        def workspace(principal, request):
            seen.append((principal, request))
            return {'workspace': {'mode': 'worktree', 'source_path': '/home/dev/projects/atlas', 'base_ref': 'refs/heads/main',
                                  'worktree_path': None}}
        made = self.hooks_with(('routing', ('hand_pick',), ('route_entry', 'route_decision'), routing, True),
                               ('workspace', ('workspace',), ('workspace',), workspace))
        request = {'tenant': 't1', 'prompt': 'x', 'workspace': {'path': '/home/dev/projects/atlas'},
                   'owner': 'notes.search', 'session_id': 'ses-forged', 'budget': {'cap_usd': 99}}
        launch, refusal = made.launch_snapshot(H.DEVICE, request)
        self.assertIsNone(refusal)
        self.assertEqual(seen, [(H.DEVICE, request)])
        self.assertEqual(set(launch), set(H.LAUNCH_KEYS))
        self.assertEqual(launch['workspace']['source_path'], '/home/dev/projects/atlas')
        self.assertEqual(launch['route_entry']['deadline_s'], 600.0)
        # Nothing is copied from the request: what no registrant set is what the server alone knows.
        self.assertEqual((launch['owner'], launch['session_id'], launch['turn_id'], launch['containment_level'], launch['budget']),
                         ('host', None, None, None, {'cap_usd': 0}))
        self.assertTrue(made.engine_may_be_chosen)

    def test_the_owner_is_the_plugin_only_when_the_principal_is_that_plugin(self) -> None:
        made = self.hooks_with(('quota', (), (), lambda principal, request: {}))
        self.assertEqual(made.launch_snapshot(H.Principal('plugin', 'notes.search'), {'owner': 'host'})[0]['owner'], 'notes.search')
        for principal in (H.OWNER, H.DEVICE, H.Principal('session', 'ses-1'), H.Principal('internal', 'sessions')):
            self.assertEqual(made.launch_snapshot(principal, {'owner': 'notes.search'})[0]['owner'], 'host')

    @quiet
    def test_a_registrant_refuses_with_the_methods_own_code_and_a_failing_one_refuses_the_start(self) -> None:
        def refuse(principal, request):
            return H.Refusal('workspace_invalid', 'not a repository')

        def broken(principal, request):
            raise OSError('cannot stat')
        later = []
        for normalize, expected in ((refuse, {'code': 'workspace_invalid', 'detail': 'not a repository'}),
                                    (lambda principal, request: H.Refusal('workspace_busy'), {'code': 'workspace_busy'}),
                                    (broken, {'code': 'unavailable', 'detail': 'start_registrant_failed'}),
                                    (lambda principal, request: {'owner': 'notes.search'}, {'code': 'unavailable', 'detail': 'start_registrant_failed'}),
                                    (lambda principal, request: None, {'code': 'unavailable', 'detail': 'start_registrant_failed'})):
            made = self.hooks_with(('workspace', ('workspace',), ('workspace',), normalize),
                                   ('zlast', (), (), lambda principal, request: later.append(1) or {}))
            self.assertEqual(made.launch_snapshot(H.OWNER, {}), (None, expected))
        self.assertEqual(later, [], 'a refusal ends the start: no later registrant runs')

    def test_two_registrants_cannot_set_one_key_and_none_can_set_the_owner(self) -> None:
        made = H.Hooks()
        made.start_registrant('routing', ('hand_pick',), ('route_entry', 'route_decision'), routing, chooses_engine=True)
        for name, fields, keys, chooses in (('quota', (), ('route_entry',), False), ('host', (), ('owner',), False),
                                            ('routing', (), (), False), ('other', (), (), True), ('', (), (), False),
                                            ('workspace', ('prompt',), (), False), ('workspace', ('owner',), (), False),
                                            ('workspace', ('principal',), (), False), ('workspace', ('',), (), False)):
            with self.subTest(name=name, fields=fields, keys=keys):
                with self.assertRaises(H.HookError):
                    made.start_registrant(name, fields, keys, lambda principal, request: {}, chooses_engine=chooses)
        with self.assertRaises(H.HookError):
            made.start_registrant('workspace', ('workspace',), ('workspace',), 'not a function')

    def test_the_identity_covers_the_engine_the_prompt_and_only_the_declared_fields_that_are_present(self) -> None:
        made = self.hooks_with(('routing', ('hand_pick',), ('route_entry', 'route_decision'), routing, True),
                               ('workspace', ('workspace',), ('workspace',), lambda principal, request: {}))
        digest = 'a' * 64
        base = {'tenant': 't1', 'engine': 'codex', 'prompt': 'x', 'client_key': 'k-1'}
        one = made.start_intent(base, digest)
        self.assertEqual(one, hashlib.sha256(('{"engine":"codex","prompt_sha256":"%s"}' % digest).encode()).hexdigest())
        # A missing field, a null one and one nobody declared are the same request.
        for same in (dict(base, workspace=None), dict(base, labels={'team': 'x'}), dict(base, containment_level='contained'),
                     dict(base, tenant='t2', client_key='k-2')):
            self.assertEqual(made.start_intent(same, digest), one)
        # A declared field that is present makes it another request; so does the engine, and the prompt.
        with_workspace = made.start_intent(dict(base, workspace={'path': '/a'}), digest)
        self.assertEqual(with_workspace, hashlib.sha256(
            ('{"engine":"codex","prompt_sha256":"%s","workspace":{"path":"/a"}}' % digest).encode()).hexdigest())
        others = {with_workspace, made.start_intent(dict(base, workspace={'path': '/b'}), digest),
                  made.start_intent(dict(base, hand_pick={'engine': 'codex', 'model': 'gpt-6'}), digest),
                  made.start_intent(dict(base, engine='claude_code'), digest), made.start_intent(base, 'b' * 64),
                  made.start_intent({key: value for key, value in base.items() if key != 'engine'}, digest)}
        self.assertEqual(len(others | {one}), 7)
        self.assertEqual(made.declared_present(dict(base, workspace={'path': '/a'}, hand_pick=None)), {'workspace': {'path': '/a'}})
        with self.assertRaises(ValueError):
            made.start_intent(dict(base, workspace={'depth': 2**60}), digest)

    def test_the_launch_context_is_the_stored_launch_object_and_the_records_own_principal(self) -> None:
        launch, _ = self.hooks_with(('routing', ('hand_pick',), ('route_entry', 'route_decision'), routing, True)) \
            .launch_snapshot(H.OWNER, {})
        record = {'run_id': 'run-1', 'launch': launch, 'principal': H.OWNER.public()}
        context = H.LaunchContext.from_record(record)
        self.assertEqual({key: getattr(context, key) for key in H.LAUNCH_KEYS}, launch)
        self.assertIs(context.route_entry, launch['route_entry'], 'the values are the record\'s, not copies or recomputed')
        self.assertEqual(context.principal, H.Principal('owner', 'local'))
        self.assertIsNone(H.LaunchContext.from_record({'run_id': 'run-0'}), 'a record of the path that is off has no launch')
        self.assertIsNone(H.LaunchContext.from_record({'launch': launch}).principal)
        for broken in ({'launch': dict(launch, principal={'kind': 'owner'})}, {'launch': {'owner': 'host'}}, {'launch': 'x'},
                       {'launch': launch, 'principal': 'owner'}, {'launch': launch, 'principal': {'kind': 'root', 'id': 'x'}}):
            with self.subTest(record=broken):
                with self.assertRaises(ValueError):
                    H.LaunchContext.from_record(broken)


if __name__ == "__main__":
    unittest.main()
