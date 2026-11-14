"""The calls the daemon's own files make into the seams (control/hooks.py): S1 and S3 to S13.

``control/tests/test_seams.py`` tests the hooks themselves, the three tables and the control API's part.
Here a daemon is composed as in ``test_composition`` (everything real but the service manager, with the
stand-in engine), a fake is registered into a hook, and the test sees the daemon's own file call it with
the documented arguments. With nothing registered the composition is the one it was before the seams.
"""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

import recursion_core.daemon as daemon_module
from recursion_core.control import hooks as H, methods
from recursion_core.control.client import SocketAPI
from recursion_core.control.tests.test_seams import FakeStore, Service, WatchedLock as OwnerLock, quiet, row_names_service
from recursion_core.daemon import Daemon, Registry, control_adapter as adapter, launcher
from recursion_core.daemon.tests.test_composition import (
    BOOT, LINUX, LINUX_ONLY, STANDIN, ProcessUnitManager, Units, until,
)
from recursion_core.routes import ClaudeCodeAdapter, contract as c
from recursion_core.routes.tests.fakes import FakeClock

HELD_BOUND_S = 30.0  # only ends a wait that would otherwise never end; it never decides a pass
LIVE = 'helper=0 fast=1'            # the stand-in engine runs until it is stopped
ENDS = 'helper=0 ticks=1 fast=1'    # the stand-in engine ends on its own, with a result


def hooks_of(register=None, **single):
    """Hooks as ``compose()`` leaves them, with what a test's fake lane registered."""
    made = H.Hooks()
    for name, function in single.items():
        made.set(name, function)
    if register is not None:
        register(made)
    made.freeze()
    return made


def peer(pid=None):
    """A socket connection as ``DaemonControl.handle`` sees it."""
    return mock.Mock(peer=mock.Mock(pid=os.getpid() if pid is None else pid))


def held_hook(store, answer=None):
    """A lane's hook that stays inside until it is let go, and writes down whether its store was closed when it
    left. ``(hook, entered, leave, seen)``."""
    entered, leave, seen = threading.Event(), threading.Event(), []

    def hook(*args):
        entered.set()
        leave.wait(HELD_BOUND_S)
        seen.append(store.closed)
        return answer
    return hook, entered, leave, seen


def close_waits(case, made, store, close, entered, leave):
    """Lane code is inside (``entered``) on a thread that no client call's admission covers. ``close`` is called
    now, on a thread of its own. It moves on in one of two ways: it WAITS in the drain (right), or it finishes
    (wrong). Either sets one event, and what is true at that moment is what is asserted, so no schedule decides
    the test. Then the lane code is let go, and the close ends with the store closed."""
    case.assertTrue(entered.wait(HELD_BOUND_S), 'the lane code is inside')
    moved_on, finished, wait = threading.Event(), [], made.gate._changed.wait

    def waiting(timeout=None):
        moved_on.set()
        return wait(timeout)

    def closing():
        try:
            close()
        finally:
            finished.append(store.closed)
            moved_on.set()
    made.gate._changed.wait = waiting
    closer = threading.Thread(target=closing, daemon=True)
    closer.start()
    case.assertTrue(moved_on.wait(HELD_BOUND_S), 'the close moved on, one way or the other')
    case.assertEqual((finished, store.closed, made.gate.admitted), ([], False, 1),
                     'it WAITS: the store is open and the close has not ended while the lane code is inside')
    leave.set()
    closer.join(HELD_BOUND_S)
    case.assertEqual((finished, store.closed), ([True], True))


@unittest.skipUnless(LINUX, LINUX_ONLY)
class Composed(unittest.TestCase):
    """A composed daemon, as in ``test_composition.ComposedDaemonTest``."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='rc-seams-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        os.chmod(self.tmp, 0o700)
        self.state = self.tmp / 'state'
        self.runtime = self.tmp / 'rt'
        self.runtime.mkdir(mode=0o700)
        (self.tmp / 'bin').mkdir(mode=0o700)
        self.engine = self.tmp / 'bin' / 'claude'
        shutil.copyfile(STANDIN, self.engine)
        os.chmod(self.engine, 0o700)
        try:  # with no user message the stand-in engine prints two lines and exits: an exec probe
            probe = subprocess.run([str(self.engine)], stdin=subprocess.DEVNULL, capture_output=True, timeout=20,
                                   env={'PATH': '/usr/bin:/bin'}, check=False).stdout
        except (OSError, subprocess.SubprocessError):
            probe = b''
        if b'stand-in engine got no user message' not in probe:
            self.skipTest('temp directory cannot execute the stand-in engine (noexec mount or no /usr/bin/python3)')
        self.units = Units()
        self.addCleanup(self.kill_units)
        self.environ = {'PATH': '/usr/bin:/bin', 'HOME': str(self.tmp), 'LANG': 'C.UTF-8',
                        'XDG_RUNTIME_DIR': str(self.runtime), 'XDG_CONFIG_HOME': str(self.tmp / 'config')}
        self.routes = {'claude_code': {
            'route': {'engine': 'claude_code', 'model': 'stand-in-model', 'api_key_helper': 'absent',
                      'connection': {'kind': 'subscription', 'id': 'stand-in-login'}},
            'deadline_s': 120.0, 'binary': str(self.engine)}}
        self.open = []
        self.addCleanup(self.shutdown)

    def kill_units(self):
        for proc in self.units.procs.values():
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            proc.wait()

    def new_daemon(self):
        daemon = Daemon(Registry(self.state), ProcessUnitManager(self.units), boot_id=lambda: BOOT)
        daemon.sweep()
        return daemon

    def boot(self, made=None, **options):
        daemon = self.new_daemon()
        try:
            composed = adapter.compose_control(daemon, self.state, environ=self.environ, routes=self.routes,
                                               hooks=made, **options)
        except BaseException:
            daemon.close()
            raise
        composed.control.reconcile()
        self.open.append((daemon, composed))
        return daemon, composed

    def shutdown(self):
        while self.open:
            daemon, composed = self.open.pop()
            try:
                composed.close()
            finally:
                daemon.close()

    def api(self, composed):
        return SocketAPI(composed.server.path, timeout_s=30)

    def request(self, prompt=LIVE, **more):
        return {'tenant': 't1', 'engine': 'claude_code', 'prompt': prompt, **more}

    def start(self, caller, prompt=LIVE, **more):
        answer = caller.call('run.start', self.request(prompt, **more))
        self.assertEqual(answer['code'], 'started', answer)
        return answer['run']['run_id']

    def settled(self, run_id):
        """The run's route worker has written its result: nothing of the run is still being written."""
        self.assertTrue(until(lambda: (self.state / 'runs' / run_id / 'result.json').exists()), f'{run_id} has a result')

    def stop(self, caller, run_id):
        """Stop a run, and wait for its worker to be done with it before the test's folder can go."""
        answer = caller.call('run.stop', {'run_id': run_id})
        self.assertEqual(answer['code'], 'stopped', answer)
        self.settled(run_id)
        return answer

    def page(self, composed, method, path, payload=None):
        # Through the transport the composition gave the page, as the page's own client calls it.
        status, body = composed.client._transport(method, path, b'' if payload is None else json.dumps(payload).encode())
        return status, json.loads(body)


class NothingRegisteredTest(Composed):
    """The composition a daemon gets today: no lane is installed, so no hook has a registrant."""

    def test_the_composition_with_nothing_registered_is_the_one_it_was(self):
        daemon, composed = self.boot()  # the hooks are composed from the installed registrants: none
        made = composed.hooks
        self.assertEqual((made.gate.enabled, made.start_on, made.changes, composed.context.store), (False, False, None, None))
        self.assertIsNone(daemon.registry.mark_changed)
        self.assertEqual(made.methods.registered(), ())
        self.assertEqual(tuple(made.engines.names()), ('claude_code', 'codex'))
        api, control = self.api(composed), composed.control
        routes = api.call('route.list')
        self.assertEqual(sorted(row['id'] for row in routes['routes']),
                         ['engine:claude_code', 'engine:codex', 'plugin_panels', 'surface:cli'])
        # A name the published table does not have is unknown, as before. The table itself is answered now.
        self.assertEqual(api.call('plugin.install'), {'code': 'method_unknown', 'detail': 'plugin.install'})
        self.assertEqual(api.call('run.kill'), {'code': 'method_unknown', 'detail': 'run.kill'})
        listed = api.call('methods.list')
        self.assertEqual((listed['code'], listed['table_version']), ('ok', methods.TABLE_VERSION))
        self.assertEqual([row['name'] for row in listed['methods'] if row['implemented']], list(methods.MANDATORY))
        self.assertEqual(api.call('session.start', {'command_id': 'c-1'}), {'code': 'unsupported', 'detail': 'not_implemented'})
        # The page's three paths answer in their shapes, and it has no fourth.
        self.assertEqual(self.page(composed, 'GET', '/v1/routes'), (200, {'routes': control.call('route.list')['routes']}))
        self.assertEqual(self.page(composed, 'POST', '/v1/runs/stop', {'run_id': 'run-none'}),
                         (200, {'code': 'run_unknown', 'run_id': 'run-none'}))
        self.assertEqual(self.page(composed, 'POST', '/v1/operator', {'command': 'operator.health.inspect'})[0], 503)
        self.assertEqual(self.page(composed, 'POST', '/v1/control', {'method': 'run.list', 'params': {}}), (404, {'error': 'not_found'}))
        for body in (b'', b'not json', b'[]', b'{"method": 7}'):   # the path is not there at all: no body is even read
            self.assertEqual(control.page_transport('POST', '/v1/control', body), (404, b'{"error": "not_found"}'))
        # What the page is handed is the transport with the seams; the method it was handed before is still there,
        # reads only what it read (a stand-in with ``call``, ``route_records`` and ``plugins`` is enough) and has no fourth path.
        self.assertEqual(composed.client._transport, control.page_transport)
        stand_in = mock.Mock(spec=['call', 'route_records', 'plugins'], plugins=None)
        stand_in.call.return_value, stand_in.route_records.return_value = {'code': 'stopped'}, [{'id': 'surface:cli'}]
        old = adapter.DaemonControl.gui_transport
        self.assertEqual(old(stand_in, 'POST', '/v1/runs/stop', b'{"run_id": "run-1"}'), (200, b'{"code": "stopped"}'))
        stand_in.call.assert_called_once_with('run.stop', {'run_id': 'run-1'})
        self.assertEqual(old(stand_in, 'GET', '/v1/routes', b''), (200, b'{"routes": [{"id": "surface:cli"}]}'))
        self.assertEqual(old(stand_in, 'POST', '/v1/operator', b'{"command": "operator.health.inspect"}')[0], 503)
        self.assertEqual(old(stand_in, 'POST', '/v1/control', b'{"method": "run.list", "params": {}}'), (404, b'{"error": "not_found"}'))
        # A peer inside a run unit, or one whose cgroup cannot be read, gets the reply it always got.
        self.assertEqual(control.handle({'op': 'control', 'method': 'run.list', 'params': {}}, peer(0)),
                         {'ok': False, 'error': {'code': 'scope_refused', 'detail': 'peer is inside a run unit'}})
        self.assertEqual(control.handle({'op': 'control', 'method': 'run.list', 'params': {}}, peer()),
                         {'ok': True, 'result': {'code': 'ok', 'runs': []}})
        # A start stores what it stored: three members of identity, no principal, no launch object.
        run_id = self.start(api, client_key='k1', workspace={'path': '/x'}, owner='notes.search')
        record = control.registry.get(run_id)
        self.assertEqual(sorted(record['start_key']), ['client_key', 'engine', 'prompt_sha256'])
        self.assertEqual(set(record) & {'principal', 'launch', 'workspace', 'owner'}, set())
        self.stop(api, run_id)

    def test_a_registrant_that_is_absent_changes_nothing_and_one_that_is_broken_stops_the_composition(self):
        # The composition imports its registrants by their own names, so a lane is faked under its real name, for
        # as long as the block lasts.
        from recursion_core.control.tests.test_seams import FakeLanes
        with contextlib.ExitStack() as installed:
            lanes = FakeLanes(mock.Mock(addCleanup=installed.callback))
            lanes.lane('keys')                                                # a lane's package with no hooks module
            _, composed = self.boot()
            self.assertEqual((composed.hooks.gate.enabled, composed.hooks.methods.registered()), (False, ()))
            self.assertEqual(self.api(composed).call('run.list'), {'code': 'ok', 'runs': []})
            self.shutdown()
        for lane, text in (('events', 'import a_module_nobody_has\n\ndef register(hooks):\n    pass\n'),
                           ('sessions', 'def register(hooks):\n    raise RuntimeError("no")\n')):
            with self.subTest(registrant=lane), contextlib.ExitStack() as installed:
                FakeLanes(mock.Mock(addCleanup=installed.callback)).lane(lane, hooks=text)
                with self.assertRaises(H.HookError):
                    self.boot()
                # Nothing was served and nothing is left: no socket, no token file.
                self.assertFalse((self.runtime / 'recursion' / 'gui-device-token').exists())
                self.assertEqual(self.open, [])
        _, composed = self.boot()  # and the next composition starts
        self.assertEqual(self.api(composed).call('run.list')['code'], 'ok')


class AuthorizeSeamTest(Composed):
    """S5: one authorization call in front of every client entry."""

    def test_every_client_entry_asks_the_authorizer_and_the_daemons_own_stop_does_not(self):
        seen, refusing = [], []

        def authorize(principal, method, params):
            seen.append((principal, method, params))
            return ('scope_refused', 'not yours') if refusing else None
        daemon, composed = self.boot(hooks_of(authorize=authorize))
        api, control = self.api(composed), composed.control
        run_id = self.start(api)
        self.assertEqual(seen, [(H.Principal('owner', 'local'), 'run.start', self.request())])
        refusing.append(True)
        del seen[:]
        refused = {'code': 'scope_refused', 'detail': 'not yours'}
        # The socket: the reply is the transport's refusal, with the authorizer's code.
        for method, params in (('route.list', {}), ('plugin.list', {}), ('run.list', {}), ('run.status', {'run_id': run_id}),
                               ('run.stop', {'run_id': run_id}), ('run.start', self.request()), ('methods.list', {})):
            with self.subTest(socket=method):
                self.assertEqual(api.call(method, params),
                                 {'code': 'control_api_unavailable', 'delivered': True, 'detail': 'daemon:scope_refused'})
                self.assertEqual(control.handle({'op': 'control', 'method': method, 'params': params}, peer()),
                                 {'ok': False, 'error': refused})
        self.assertTrue(all(principal == H.Principal('owner', 'local') for principal, _, _ in seen))
        self.assertEqual([method for _, method, _ in seen][::2],
                         ['route.list', 'plugin.list', 'run.list', 'run.status', 'run.stop', 'run.start', 'methods.list'])
        del seen[:]
        # The page: its Stop and its two reads, each in its own shape.
        self.assertEqual(self.page(composed, 'GET', '/v1/routes'), (403, {'error': 'scope_refused'}))
        self.assertEqual(self.page(composed, 'POST', '/v1/operator', {'command': 'operator.health.inspect'}),
                         (403, {'error': 'scope_refused'}))
        self.assertEqual(self.page(composed, 'POST', '/v1/runs/stop', {'run_id': run_id}), (200, refused))
        # An in-process client call.
        self.assertEqual(control.call('run.list'), refused)
        self.assertEqual(control.call('run.stop', {'run_id': run_id}, H.Principal('internal', 'sessions')), refused)
        self.assertEqual(seen, [(H.DEVICE, 'route.list', {}), (H.DEVICE, 'operator.read', {'command': 'operator.health.inspect'}),
                                (H.DEVICE, 'run.stop', {'run_id': run_id}), (H.OWNER, 'run.list', None),
                                (H.Principal('internal', 'sessions'), 'run.stop', {'run_id': run_id})])
        proc = self.units.procs[f'recursion-run-{run_id}.service']
        self.assertIsNone(proc.poll(), 'no refused Stop reached the run')
        # The daemon's own recovery Stop is not a client's call: it never meets the authorizer, and it runs.
        del seen[:]
        daemon.registry.request_stop(run_id)
        control.reconcile()
        self.assertEqual(control.registry.get(run_id)['state'], 'stopped')
        self.assertIsNotNone(proc.poll())
        self.assertEqual(seen, [])
        self.settled(run_id)
        # A peer inside a run unit is still closed at accept, before any slot and before any authorizer.
        self.assertIs(composed.server._admit, adapter.admit_peer)
        self.assertFalse(adapter.admit_peer(mock.Mock(pid=0)))
        self.assertEqual(seen, [])

    def test_a_registered_authorizer_takes_the_whole_decision_and_one_that_fails_refuses(self):
        seen = []

        def authorize(principal, method, params):
            seen.append(principal)
            if method == 'run.list':
                raise RuntimeError('policy store unreadable')
            return None
        _, composed = self.boot(hooks_of(authorize=authorize))
        control = composed.control
        # Today's predicate travels on the principal; what to do with it is the authorizer's now.
        self.assertEqual(control.handle({'op': 'control', 'method': 'route.list', 'params': {}}, peer(0))['ok'], True)
        self.assertEqual(seen, [H.Principal('owner', 'local', in_run_unit=True)])
        with self.assertLogs('recursion_core.control.hooks', 'ERROR'):
            self.assertEqual(control.call('run.list'), {'code': 'scope_refused', 'detail': 'authorizer_failed'})
            self.assertEqual(self.api(composed).call('run.list')['detail'], 'daemon:scope_refused')

    def test_a_refused_peers_reply_id_is_not_sized(self):
        """As before the seams: the room for a reply's id is worked out for a request that is answered. A peer the
        authorizer refuses costs no sizing of whatever it sent as its id."""
        _, composed = self.boot()
        frame = {'op': 'control', 'method': 'run.list', 'params': {}, 'id': 'x' * 64}
        with mock.patch.object(adapter, '_wire_size', wraps=adapter._wire_size) as sized:
            refused = composed.control.handle(dict(frame), peer(0))     # inside a run unit: refused by default
            self.assertEqual((refused['ok'], refused['error']['code']), (False, 'scope_refused'))
            sized.assert_not_called()
            self.assertEqual(composed.control.handle(dict(frame), peer()), {'ok': True, 'result': {'code': 'ok', 'runs': []}})
            self.assertIn(mock.call(frame['id']), sized.call_args_list, 'an answered request leaves room for its id')

    def test_an_authorizers_own_unavailable_is_its_refusal_on_the_page_and_not_a_closing_daemon(self):
        _, composed = self.boot(hooks_of(authorize=lambda principal, method, params: ('unavailable', 'policy_loading')))
        self.assertFalse(composed.hooks.closing.is_set())
        self.assertEqual(self.page(composed, 'GET', '/v1/routes'), (403, {'error': 'unavailable'}))
        self.assertEqual(self.page(composed, 'POST', '/v1/operator', {'command': 'operator.health.inspect'}),
                         (403, {'error': 'unavailable'}))
        self.assertEqual(self.page(composed, 'POST', '/v1/runs/stop', {'run_id': 'run-none'}),
                         (200, {'code': 'unavailable', 'detail': 'policy_loading'}))

    def test_on_a_real_daemon_the_guis_old_transport_name_is_the_pages_one(self):
        """One page transport. ``gui_transport`` on a real control object is ``page_transport``: the device
        principal, the authorizer and the admission in front. Its old body answers only a stand-in handed to it
        unbound, as the existing tests do."""
        seen = []

        def authorize(principal, method, params):
            seen.append((principal, method))
            return ('scope_refused', 'not yours')
        _, composed = self.boot(hooks_of(authorize=authorize))
        control = composed.control
        self.assertEqual(control.gui_transport('GET', '/v1/routes', b''), (403, b'{"error": "scope_refused"}'))
        status, body = control.gui_transport('POST', '/v1/runs/stop', b'{"run_id": "run-none"}')
        self.assertEqual((status, json.loads(body)), (200, {'code': 'scope_refused', 'detail': 'not yours'}))
        self.assertEqual(control.gui_transport('POST', '/v1/operator', b'{"command": "operator.health.inspect"}'),
                         (403, b'{"error": "scope_refused"}'))
        self.assertEqual(control.gui_transport('POST', '/v1/control', b'{}'), (404, b'{"error": "not_found"}'))
        self.assertEqual(seen, [(H.DEVICE, 'route.list'), (H.DEVICE, 'run.stop'), (H.DEVICE, 'operator.read')])
        stand_in = mock.Mock(spec=['route_records'])
        stand_in.route_records.return_value = []
        self.assertEqual(adapter.DaemonControl.gui_transport(stand_in, 'GET', '/v1/routes', b''), (200, b'{"routes": []}'))
        self.assertEqual(len(seen), 3, 'the stand-in met no authorizer: it is not a client entry of a daemon')


class MethodSeamTest(Composed):
    """S1, S11 and the readiness part of S13: a lane's method, served on every entry."""

    def test_a_table_method_is_served_on_the_socket_the_page_and_in_process_with_its_principal(self):
        seen = []

        def status(ctx, params):
            seen.append((ctx.principal, params, ctx.tx, ctx.closing))
            return {'code': 'ok', 'inner': ctx.call('run.list')['code']}

        def register(made):
            made.methods.register('session.status', status)
            made.set('page_methods', lambda: frozenset({'session.status'}))
        _, composed = self.boot(hooks_of(register))
        made, control = composed.hooks, composed.control
        answer = {'code': 'ok', 'inner': 'ok'}
        self.assertEqual(self.api(composed).call('session.status', {'session_id': 'ses-1'}), answer)
        self.assertEqual(control.call('session.status', {'session_id': 'ses-2'}, H.Principal('internal', 'sessions')), answer)
        self.assertEqual(self.page(composed, 'POST', '/v1/control', {'method': 'session.status', 'params': {'session_id': 'ses-3'}}),
                         (200, answer))
        self.assertEqual(seen, [(H.Principal('owner', 'local'), {'session_id': 'ses-1'}, None, made.closing),
                                (H.Principal('internal', 'sessions'), {'session_id': 'ses-2'}, None, made.closing),
                                (H.DEVICE, {'session_id': 'ses-3'}, None, made.closing)])
        # S11: the page calls only what was listed, and only a well-formed request.
        for payload in ({'method': 'run.list', 'params': {}}, {'method': 'session.list', 'params': {}}, {'method': 'run.stop', 'params': {'run_id': 'x'}}):
            self.assertEqual(self.page(composed, 'POST', '/v1/control', payload), (404, {'error': 'not_found'}))
        for body in (b'', b'not json', b'[]', b'{"method": 7}', b'{"method": "session.status", "params": []}'):
            self.assertEqual(composed.control.page_transport('POST', '/v1/control', body), (400, b'{"error": "request_invalid"}'))
        self.assertEqual(len(seen), 3)
        self.assertEqual(self.api(composed).call('methods.list')['methods'][list(methods.METHODS).index('session.status')],
                         {'name': 'session.status', 'implemented': True, 'beta': True})

    def test_the_page_is_admitted_before_its_list_of_methods_is_asked_for(self):
        """S11 with S13. The list of the page's methods is a lane's own code. A request is admitted before the
        list is asked for: a close waits for a provider that is inside, with the store open, and after the close
        the page is told the daemon is closing and the provider is not asked at all."""
        store, asked = FakeStore(), []
        entered, leave = threading.Event(), threading.Event()
        self.addCleanup(leave.set)

        def listed():
            asked.append(store.closed)
            if len(asked) == 1:
                entered.set()
                leave.wait(HELD_BOUND_S)
                asked.append(store.closed)
            return frozenset({'session.status'})

        def register(made):
            made.methods.register('session.status', lambda ctx, params: {'code': 'ok'})
            made.set('page_methods', listed)
            made.set('event_store', lambda state_dir: store)
        daemon, composed = self.boot(hooks_of(register))
        request, answers = {'method': 'session.status', 'params': {'session_id': 'ses-1'}}, []
        caller = threading.Thread(target=lambda: answers.append(self.page(composed, 'POST', '/v1/control', request)), daemon=True)
        caller.start()
        self.open.remove((daemon, composed))
        self.addCleanup(daemon.close)
        close_waits(self, composed.hooks, store, composed.close, entered, leave)
        caller.join(HELD_BOUND_S)
        self.assertEqual((asked, answers), ([False, False], [(200, {'code': 'ok'})]),
                         'the store was open while the provider was inside, and the admitted request was answered')
        self.assertEqual(self.page(composed, 'POST', '/v1/control', request), (200, {'code': 'unavailable', 'detail': 'daemon_closing'}))
        self.assertEqual(len(asked), 2, 'after the close no lane code runs for the page')

    def test_a_call_made_inside_a_handler_is_authorized_again_as_the_same_principal(self):
        seen = []

        def authorize(principal, method, params):
            seen.append((principal, method))
            return 'scope_refused' if method == 'run.list' and principal.kind == 'device' else None

        def register(made):
            made.methods.register('session.status', lambda ctx, params: {'code': 'ok', 'inner': ctx.call('run.list')})
            made.set('authorize', authorize)
            made.set('page_methods', lambda: frozenset({'session.status'}))
        _, composed = self.boot(hooks_of(register))
        self.assertEqual(composed.control.call('session.status', {})['inner'], {'code': 'ok', 'runs': []})
        self.assertEqual(self.page(composed, 'POST', '/v1/control', {'method': 'session.status', 'params': {}}),
                         (200, {'code': 'ok', 'inner': {'code': 'scope_refused', 'detail': ''}}))
        self.assertEqual(seen, [(H.OWNER, 'session.status'), (H.OWNER, 'run.list'), (H.DEVICE, 'session.status'), (H.DEVICE, 'run.list')])

    def test_a_command_runs_once_in_the_dispatchers_transaction_and_is_replayed_from_its_receipt(self):
        store, calls, after, opened = FakeStore(), [], [], []
        store.register_tables('sessions', 1, 'CREATE TABLE sessions (session_id TEXT PRIMARY KEY, state TEXT NOT NULL)')

        def start(ctx, params):
            calls.append((ctx.principal, params))
            ctx.tx.execute('INSERT INTO sessions VALUES (?, ?)', ('ses-1', 'starting'))
            ctx.after_commit(lambda: after.append('woken'))
            return {'code': 'ok', 'session_id': 'ses-1'}

        def register(made):
            made.methods.register('session.start', start, tenant=methods.INSTALL_TENANT)
            made.set('event_store', lambda state_dir: opened.append(state_dir) or store)
        _, composed = self.boot(hooks_of(register))
        api = self.api(composed)
        params = {'command_id': 'c-1', 'title': 'x'}
        first = api.call('session.start', params)
        self.assertEqual(first, {'code': 'ok', 'session_id': 'ses-1'})
        self.assertEqual(api.call('session.start', params), first)
        self.assertEqual(composed.control.call('session.start', params), first)
        self.assertEqual((calls, after), ([(H.Principal('owner', 'local'), params)], ['woken']))
        self.assertEqual(store.rows, {('sessions', 'ses-1'): {'state': 'starting'}})
        self.assertEqual(api.call('session.start', {'command_id': 'c-1', 'title': 'y'})['code'], 'conflict')
        self.assertEqual((opened, composed.context.store), ([self.state], store))
        self.assertFalse(store.closed)
        self.shutdown()
        self.assertTrue(store.closed)

    def test_a_handler_that_calls_the_control_object_itself_cannot_stop_a_run_inside_its_command(self):
        store, holder, seen = FakeStore(), [], []

        def start(ctx, params):
            control, run_id = holder
            seen.append(('through ctx.call', ctx.call('run.stop', {'run_id': run_id})))
            seen.append(('the object itself', control.call('run.stop', {'run_id': run_id})))
            seen.append(('a start', control.call('run.start', self.request())))
            seen.append(('a read', control.call('run.status', {'run_id': run_id})['code']))
            ctx.after_commit(lambda: seen.append(('after the commit', control.call('run.stop', {'run_id': run_id})['code'])))
            return {'code': 'ok'}

        def register(made):
            made.methods.register('session.start', start, tenant=methods.INSTALL_TENANT)
            made.set('event_store', lambda state_dir: store)
        _, composed = self.boot(hooks_of(register))
        run_id = self.start(composed.control)
        holder.extend([composed.control, run_id])
        launched = len(self.units.started)
        self.assertEqual(self.api(composed).call('session.start', {'command_id': 'c-1'}), {'code': 'ok'})
        refused = {'code': 'request_invalid', 'detail': 'nested_effect'}
        self.assertEqual(seen, [('through ctx.call', refused), ('the object itself', refused), ('a start', refused),
                                ('a read', 'ok'), ('after the commit', 'stopped')],
                         'inside the command nothing is stopped or started; after its commit the Stop goes through')
        self.assertEqual(len(self.units.started), launched)
        self.settled(run_id)

    def test_a_method_that_needs_a_service_waits_for_it_and_the_run_methods_do_not(self):
        log, served = [], []
        directory = self.runtime / 'recursion'

        class Events(Service):
            def start(inner, context):
                # Nothing is served yet: no socket is bound and the page's token is not published.
                served.append(sorted(item.name for item in directory.iterdir() if item.is_socket() or item.name == 'gui-device-token'))
                super().start(context)
        events = Events('events', log)

        row_names_service(self, 'events.read', 'events')   # what the row of a lane with a service would say

        def register(made):
            made.service(events)
            made.methods.register('events.read', lambda ctx, params: {'code': 'ok', 'events': []}, service='events')
        daemon, composed = self.boot(hooks_of(register))
        api = self.api(composed)
        self.assertEqual(served, [[]], 'the service was started before the socket and the door')
        self.assertTrue((directory / 'gui-device-token').exists() and any(item.is_socket() for item in directory.iterdir()))
        self.assertEqual(api.call('events.read', {}), {'code': 'unavailable', 'detail': 'service_starting'})
        # The six the daemon answers itself do not wait for any service.
        run_id = self.start(api)
        self.assertEqual(api.call('run.status', {'run_id': run_id})['code'], 'ok')
        self.assertEqual(api.call('run.list')['code'], 'ok')
        self.assertEqual(api.call('route.list')['code'], 'ok')
        self.assertEqual(api.call('plugin.list')['code'], 'ok')
        self.stop(api, run_id)
        events.ready = True
        self.assertEqual(api.call('events.read', {}), {'code': 'ok', 'events': []})
        # The service was started with the one context, whose fields are the declared ones.
        (what, name, context), = log
        self.assertEqual((what, name), ('start', 'events'))
        self.assertIs(context, composed.context)
        self.assertEqual((context.daemon, context.registry, context.control_registry, context.control, context.store,
                          context.state_dir, context.closing),
                         (daemon, daemon.registry, composed.control.registry, composed.control, None, self.state,
                          composed.hooks.closing))
        self.assertTrue(callable(context.clock))
        self.shutdown()
        self.assertEqual([(what, name) for what, name, _ in log], [('start', 'events'), ('stop', 'events')])


class EngineSeamTest(Composed):
    """S3 and S7: the engines come from the table, and the credential lookup from the hooks."""

    def test_the_adapters_are_built_by_the_tables_factories_with_the_hooks_credential_lookup(self):
        built, asked = [], []

        def factory(launch, clock, credentials, environ, **options):
            built.append((launch, clock, credentials, dict(environ), options))
            return ClaudeCodeAdapter(launch, clock, credentials, environ, **options)

        def source(connection_id, env_name):
            asked.append((connection_id, env_name))
            return 'the-secret' if connection_id == 'work' else None

        def register(made):
            made.engines.register('claude_code', factory, replace=True)
            made.set('credentials', source)
        clock = FakeClock()
        _, composed = self.boot(hooks_of(register), clock=clock)
        (launch, got_clock, credentials, environ, options), = built
        self.assertIsInstance(launch, launcher.ProductionLauncher)
        self.assertEqual((launch.engine, got_clock, environ, options), ('claude_code', clock, self.environ, {'binary': str(self.engine)}))
        self.assertEqual(credentials('work', 'ANTHROPIC_API_KEY'), 'the-secret')
        with self.assertRaises(KeyError) as caught:
            credentials('home', 'ANTHROPIC_API_KEY')     # a source that has nothing gives no empty credential
        self.assertEqual(caught.exception.args, ('home',))
        self.assertEqual(asked, [('work', 'ANTHROPIC_API_KEY'), ('home', 'ANTHROPIC_API_KEY')])
        self.stop(self.api(composed), self.start(self.api(composed)))

    def test_by_default_the_lookup_is_todays_which_has_no_credential(self):
        _, composed = self.boot()
        with self.assertRaises(KeyError) as caught:
            composed.hooks.credentials('work', 'ANTHROPIC_API_KEY')
        self.assertEqual(caught.exception.args, ('work',))

    def test_a_close_waits_for_a_credential_source_that_is_inside_and_after_it_a_launch_is_refused_unasked(self):
        """S7 with S13. An adapter asks for a credential on the run's own thread, after the start may have
        answered. The source holds a lifetime reference of its own; once closing has begun the lookup refuses
        the launch, typed, before the source is asked."""
        built, store = [], FakeStore()
        source, entered, leave, seen = held_hook(store, 'the-secret')
        self.addCleanup(leave.set)

        def factory(launch, clock, credentials, environ, **options):
            built.append(credentials)
            return ClaudeCodeAdapter(launch, clock, credentials, environ, **options)

        def register(made):
            made.engines.register('claude_code', factory, replace=True)
            made.set('credentials', source)
            made.set('event_store', lambda state_dir: store)
        daemon, composed = self.boot(hooks_of(register))
        lookup, = built
        got = []
        user = threading.Thread(target=lambda: got.append(lookup('work', 'ANTHROPIC_API_KEY')), daemon=True)
        user.start()
        self.open.remove((daemon, composed))
        self.addCleanup(daemon.close)
        close_waits(self, composed.hooks, store, composed.close, entered, leave)
        user.join(HELD_BOUND_S)
        self.assertEqual((got, seen), (['the-secret'], [False]), 'the store was open for as long as the source was inside')
        with self.assertRaises(c.RouteError) as caught:
            lookup('work', 'ANTHROPIC_API_KEY')
        self.assertEqual((caught.exception.code, caught.exception.detail, len(seen)), (c.LAUNCH_REFUSED, 'daemon_closing', 1))

    def test_a_registered_factory_that_fails_leaves_no_plugin_host_and_no_breaker_store_open(self):
        closed = []

        class Host(adapter.PluginHost):
            def close(inner):
                closed.append('plugins')
                return super().close()

        class Breaker(adapter.DurableRouteBreaker):
            def close(inner):
                closed.append('breaker')
                return super().close()

        def factory(launch, clock, credentials, environ, **options):
            raise RuntimeError('this engine cannot be built')
        with mock.patch.object(adapter, 'PluginHost', Host), mock.patch.object(adapter, 'DurableRouteBreaker', Breaker):
            with self.assertRaises(RuntimeError):
                self.boot(hooks_of(lambda made: made.engines.register('claude_code', factory, replace=True)))
            self.assertEqual(closed, ['plugins', 'breaker'], 'what was opened before the factories is closed, in that order')
        self.assertEqual(self.open, [])
        _, composed = self.boot()    # what it held is free again: the next composition opens the same stores
        self.assertEqual(composed.control.call('route.list')['breaker_store'], 'durable')

    def test_an_engine_a_lane_registered_is_read_by_all_three_readers_of_the_engine_set(self):
        built = []

        def factory(launch, clock, credentials, environ, **options):
            built.append((launch.engine, options))
            return mock.Mock()

        def register(made):
            made.engines.register('opencode', factory)
        config = self.tmp / 'config' / 'recursion'
        config.mkdir(parents=True, mode=0o700)
        entry = {'model': 'm-1', 'connection': {'kind': 'subscription', 'id': 'login'}, 'deadline_s': 60}
        (config / adapter.ROUTES_FILE).write_text(json.dumps({'routes': {'opencode': entry}}), encoding='utf-8')
        os.chmod(config / adapter.ROUTES_FILE, 0o600)
        # Before a lane registers it the name is catalogue only: the route file that names it is refused, as today.
        with self.assertRaises(adapter.RoutesUnreadable) as caught:
            adapter.load_routes(config / adapter.ROUTES_FILE)
        self.assertEqual(str(caught.exception), "route 'opencode': only claude_code and codex are wired")
        self.routes = None  # read the operator's file
        # The launcher needs the engine's profile too, which that engine's lane asks of the table's keeper.
        profiles = {**launcher.ENGINE_PROFILES, 'opencode': launcher.ENGINE_PROFILES['codex']}
        with mock.patch.object(launcher, 'ENGINE_PROFILES', profiles):
            _, composed = self.boot(hooks_of(register))
        self.assertEqual(built, [('opencode', {})])
        rows = {row['id']: row for row in composed.control.call('route.list')['routes']}
        self.assertEqual([name for name in rows if name.startswith('engine:')], ['engine:claude_code', 'engine:codex', 'engine:opencode'])
        self.assertEqual((rows['engine:opencode']['configured'], rows['engine:codex']['configured']), (True, False))


class LaunchSeamTest(Composed):
    """S4, S10 and S6 e: one launch context, read by the launcher's two hooks, the drive owner and the adapter."""

    def workspace(self, log):
        def normalize(principal, request):
            log.append(('registrant', principal, request))
            asked = request.get('workspace')
            if asked is None:
                return {}
            return {'workspace': {'mode': 'worktree', 'source_path': asked['path'], 'base_ref': 'refs/heads/main', 'worktree_path': None}}
        return normalize

    def test_the_adapter_and_the_hooks_of_one_launch_see_the_same_launch_context(self):
        log = []
        folder = self.tmp / 'worktrees' / 'w1'
        folder.mkdir(parents=True, mode=0o700)

        class Seeing(ClaudeCodeAdapter):
            def run(self, req, deadline_at):
                ctx = launcher._current.ctx
                log.append(('adapter', ctx, ctx.launch, req))
                return super().run(req, deadline_at)

        def register(made):
            made.start_registrant('workspace', ('workspace',), ('workspace',), self.workspace(log))
            made.set('drive_owner', lambda ctx, request: log.append(('owner', ctx, ctx.launch, request)))
            made.set('launch_check', lambda ctx, executable: log.append(('check', ctx, ctx.launch, executable)))
            made.set('working_directory', lambda ctx: log.append(('folder', ctx, ctx.launch, None)) or folder)
            made.engines.register('claude_code', Seeing, replace=True)
        _, composed = self.boot(hooks_of(register))
        request = self.request(client_key='k1', workspace={'path': '/home/dev/projects/atlas'}, owner='notes.search')
        with mock.patch.object(composed.breaker, 'bind_run', wraps=composed.breaker.bind_run) as bind:
            answer = composed.control.call('run.start', request, H.DEVICE)
        self.assertEqual(answer['code'], 'started', answer)
        run_id = answer['run']['run_id']
        record = composed.control.registry.get(run_id)
        self.assertEqual([entry[0] for entry in log], ['registrant', 'owner', 'adapter', 'check', 'folder'])
        self.assertEqual(log[0][1:], (H.DEVICE, request))
        seen = {entry[0]: entry for entry in log[1:]}
        context = seen['owner'][2]
        self.assertIsInstance(context, H.LaunchContext)
        for name in ('adapter', 'check', 'folder'):
            self.assertIs(seen[name][2], context, name)
            self.assertIs(seen[name][1], seen['owner'][1], name)
        # The complete object of the stored record, and the principal beside it; nothing recomputed, nothing from params.
        self.assertEqual(context, H.LaunchContext.from_record(record))
        self.assertEqual({key: getattr(context, key) for key in H.LAUNCH_KEYS}, record['launch'])
        self.assertEqual((context.principal, context.owner, context.workspace['source_path'], context.route_entry),
                         (H.DEVICE, 'host', '/home/dev/projects/atlas', None))
        self.assertEqual(seen['check'][3], str(self.engine))  # the resolved executable
        self.assertEqual((seen['owner'][3]['request_id'], seen['owner'][3]['route']['engine']), (run_id, 'claude_code'))
        self.assertEqual(seen['owner'][3], seen['adapter'][3])
        # The unit works in the folder the provider gave; its home is the engine's own, as before.
        started = self.units.started[-1]
        self.assertEqual(str(started['cwd']), str(folder))
        # The launcher binds what the breaker admitted, once, as the host's; the starter binds nothing.
        bind.assert_called_once_with(run_id, 'subscription:stand-in-login', 'claude_code', 'stand-in-model')
        self.stop(composed.control, run_id)

    def test_with_the_start_path_off_the_hooks_are_asked_with_no_launch_context(self):
        log = []
        made = hooks_of(launch_check=lambda ctx, executable: log.append(('check', ctx.run_id, ctx.launch, executable)),
                        working_directory=lambda ctx: log.append(('folder', ctx.run_id, ctx.launch)),
                        drive_owner=lambda ctx, request: log.append(('owner', ctx.run_id, ctx.launch)))
        _, composed = self.boot(made)
        run_id = self.start(composed.control, workspace={'path': '/x'})
        self.assertEqual(log, [('owner', run_id, None), ('check', run_id, None, str(self.engine)), ('folder', run_id, None)])
        self.assertEqual(str(self.units.started[-1]['cwd']), str(self.state / 'homes' / 'claude_code'), 'None is today\'s folder')
        self.stop(composed.control, run_id)

    def test_a_run_a_plugin_owns_is_bound_as_that_plugins(self):
        def register(made):
            made.start_registrant('quota', (), (), lambda principal, request: {})
        _, composed = self.boot(hooks_of(register))
        bind = mock.Mock(return_value=None)    # the keyword exists once the plugin host has added it: a fake breaker
        with mock.patch.object(composed.breaker, 'bind_run', bind):
            run_id = self.start(mock.Mock(call=lambda method, params: composed.control.call(
                method, params, H.Principal('plugin', 'notes.search'))))
            host = self.start(composed.control)
        self.assertEqual(bind.call_args_list,
                         [mock.call(run_id, 'subscription:stand-in-login', 'claude_code', 'stand-in-model', owner='notes.search'),
                          mock.call(host, 'subscription:stand-in-login', 'claude_code', 'stand-in-model')])
        for run in (run_id, host):
            self.stop(composed.control, run)

    @quiet
    def test_a_launch_the_check_refuses_leaves_nothing_behind_and_its_relaunch_is_checked_again(self):
        verdicts, checks = ['binary_changed', RuntimeError('cannot read the binary'), None], []

        def check(ctx, executable):
            checks.append((ctx.run_id, executable))
            verdict = verdicts.pop(0)
            if isinstance(verdict, Exception):
                raise verdict
            return verdict
        _, composed = self.boot(hooks_of(launch_check=check))
        request = self.request(client_key='k1')
        first = composed.control.call('run.start', request)
        self.assertEqual((first['code'], first['start_error']),
                         ('start_failed', {'code': 'launch_refused', 'detail': 'launch_check:binary_changed'}))
        run_id = first['run_id']
        second = composed.control.call('run.start', request)
        self.assertEqual((second['code'], second['run_id'], second['start_error']),
                         ('start_failed', run_id, {'code': 'launch_refused', 'detail': 'launch_check_failed'}))
        # Nothing of the unit was made: no run directory, no FIFO, no unit.
        self.assertFalse((self.state / 'runs' / run_id).exists())
        self.assertEqual(self.units.started, [])
        third = composed.control.call('run.start', request)
        self.assertEqual((third['code'], third['run']['run_id'], third['duplicate']), ('started', run_id, False))
        self.assertEqual(checks, [(run_id, str(self.engine))] * 3)
        self.stop(composed.control, run_id)

    @quiet
    def test_a_folder_that_fails_or_cannot_sit_in_a_unit_refuses_the_launch(self):
        answers = [RuntimeError('worktree add failed'), self.tmp / 'has space', 'relative/path', 7]

        def provide(ctx):
            answer = answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer
        _, composed = self.boot(hooks_of(working_directory=provide))
        for detail in ('working_directory_failed', 'working_directory_not_unit_safe', 'working_directory_not_unit_safe',
                       'working_directory_failed'):
            answer = composed.control.call('run.start', self.request())
            self.assertEqual((answer['code'], answer['start_error']), ('start_failed', {'code': 'launch_refused', 'detail': detail}))
        self.assertEqual(self.units.started, [])

    @quiet
    def test_a_relaunch_asks_for_the_same_runs_folder_again_and_runs_no_registrant(self):
        log, made_folders, asked = [], {}, []

        def provide(ctx):
            asked.append((ctx.run_id, ctx.launch.workspace['source_path']))
            if ctx.run_id not in made_folders:      # made once for a run, then found again
                made_folders[ctx.run_id] = self.tmp / 'worktrees' / f'w{len(made_folders) + 1}'
                made_folders[ctx.run_id].mkdir(parents=True, mode=0o700)
            return made_folders[ctx.run_id]

        def register(made):
            made.start_registrant('workspace', ('workspace',), ('workspace',), self.workspace(log))
            made.set('working_directory', provide)
        _, composed = self.boot(hooks_of(register))
        request = self.request(client_key='k1', workspace={'path': '/home/dev/projects/atlas'})
        private_dir, failed = launcher.private_dir, []

        def run_dir_fails_once(path):
            if Path(path).parent.name == 'runs' and not failed:
                failed.append(path)
                raise OSError('no space left on device')
            return private_dir(path)
        with mock.patch.object(launcher, 'private_dir', run_dir_fails_once):
            first = composed.control.call('run.start', request)
            self.assertEqual(first['code'], 'start_failed', first)
            again = composed.control.call('run.start', request)
        run_id = first['run_id']
        self.assertEqual((again['code'], again['run']['run_id'], again['duplicate']), ('started', run_id, False))
        self.assertEqual(asked, [(run_id, '/home/dev/projects/atlas')] * 2)
        self.assertEqual(list(made_folders), [run_id], 'one folder for the run, not a second')
        self.assertEqual([entry[0] for entry in log], ['registrant'], 'the stored launch object is used: no registrant runs again')
        self.assertEqual(str(self.units.started[-1]['cwd']), str(made_folders[run_id]))
        self.stop(composed.control, run_id)

    def binding_case(self, on):
        """Primary A is open, fallback B is admitted and bound, a later check refuses before a unit exists; on the
        retry A admits. The launcher refuses to bind the run to another connection; nothing rebinds it."""
        self.routes['claude_code']['route']['limits'] = {
            'on_limit': 'fallback',
            'fallback': [{'engine': 'claude_code', 'model': 'fallback-model', 'api_key_helper': 'absent',
                          'connection': {'kind': 'subscription', 'id': 'fallback-login'}}]}
        verdicts = ['refused_once']

        def register(made):
            made.set('launch_check', lambda ctx, executable: verdicts.pop(0) if verdicts else None)
            if on:
                made.start_registrant('quota', (), (), lambda principal, request: {})
        clock = FakeClock()
        _, composed = self.boot(hooks_of(register), clock=clock)
        stored = composed.breaker._stored
        stored.report('subscription:stand-in-login', 'plan_limit')
        request = self.request(client_key='bind')
        with self.assertLogs('recursion_core.daemon.breaker', 'WARNING'):
            first = composed.control.call('run.start', request)
        self.assertEqual((first['code'], first['start_error']),
                         ('start_failed', {'code': 'launch_refused', 'detail': 'launch_check:refused_once'}), first)
        run_id = first['run_id']
        bound = {'provider': 'claude_code', 'model_version': 'fallback-model'}
        self.assertEqual(stored.check(run_id, 'subscription:fallback-login', 'host'), bound)
        clock.advance(3600.0)   # the cooldown of A is over: A admits the next request
        with self.assertLogs('recursion_core.daemon.breaker', 'ERROR'):
            retry = composed.control.call('run.start', request)
        self.assertEqual((retry['code'], retry['run_id'], retry['start_error']),
                         ('start_failed', run_id, {'code': 'launch_refused', 'detail': 'breaker_bind_failed:run_connection_mismatch'}),
                         retry)
        self.assertEqual(stored.check(run_id, 'subscription:fallback-login', 'host'), bound, 'the run is still bound to B')
        self.assertEqual(self.units.started, [])
        self.assertEqual(('launch' in composed.control.registry.get(run_id)), on)

    def test_the_binding_of_a_first_launch_refuses_a_relaunch_on_another_connection_with_the_path_off(self):
        self.binding_case(on=False)

    def test_the_binding_of_a_first_launch_refuses_a_relaunch_on_another_connection_with_the_path_on(self):
        self.binding_case(on=True)

    def test_a_chosen_entry_whose_binary_is_not_the_composed_adapters_is_refused_before_anything_is_stored(self):
        # PROVISIONAL (ruling of Nov 13, 2026): the adapter was built with the configured binary, and that is what
        # every launch of the engine runs; an entry that names another is never stored and launched as this one.
        other = self.tmp / 'bin' / 'elsewhere' / 'claude'
        other.parent.mkdir(mode=0o700)
        shutil.copyfile(self.engine, other)
        os.chmod(other, 0o700)
        choices = [dict(self.routes['claude_code'], binary=str(other)), dict(self.routes['claude_code'], binary=None),
                   json.loads(json.dumps(self.routes['claude_code']))]

        def register(made):
            made.start_registrant('routing', ('hand_pick',), ('route_entry', 'route_decision'), lambda principal, request: {
                'route_entry': choices[0], 'route_decision': None}, chooses_engine=True)
        _, composed = self.boot(hooks_of(register))
        request = {'tenant': 't1', 'prompt': LIVE, 'client_key': 'k1'}
        for refused in range(2):       # another path; and none, where the adapter was built with one
            self.assertEqual(composed.control.call('run.start', request), {'code': 'unavailable', 'detail': 'start_registrant_failed'})
            self.assertEqual((composed.control.registry.list(), self.units.started), ([], []), 'no run, no launch record, no unit')
            del choices[0]
        answer = composed.control.call('run.start', request)     # the same binary the adapter was built with
        self.assertEqual(answer['code'], 'started', answer)
        self.assertEqual(composed.control.registry.get(answer['run']['run_id'])['launch']['route_entry']['binary'], str(self.engine))
        argv = self.units.started[-1]['argv']
        self.assertEqual(argv[argv.index('--') + 1], str(self.engine), 'and it is the binary that ran')
        self.stop(composed.control, answer['run']['run_id'])

    def test_a_chosen_entry_for_an_engine_with_no_composed_adapter_is_refused_before_anything_is_stored(self):
        # PROVISIONAL, the same ruling: where no adapter was built for the entry's engine there is no binary the
        # entry could be the same as, whether it names one or names none. Nothing is stored and nothing launched.
        # (opencode is a name of the catalogue with no factory, so this composition built no adapter for it.)
        for binary in (None, str(self.engine)):
            chosen = dict(json.loads(json.dumps(self.routes['claude_code'])), binary=binary)
            chosen['route'] = dict(chosen['route'], engine='opencode')

            def register(made, chosen=chosen):
                made.start_registrant('routing', ('hand_pick',), ('route_entry', 'route_decision'), lambda principal, request: {
                    'route_entry': chosen, 'route_decision': None}, chooses_engine=True)
            with self.subTest(binary=binary):
                daemon, composed = self.boot(hooks_of(register))
                answer = composed.control.call('run.start', {'tenant': 't1', 'prompt': LIVE, 'client_key': 'k1'})
                self.assertEqual(answer, {'code': 'unavailable', 'detail': 'start_registrant_failed'})
                self.assertEqual((composed.control.registry.list(), self.units.started), ([], []), 'no run, no launch record, no unit')
                self.shutdown()

    def test_a_relaunch_from_a_stored_entry_is_checked_against_the_composition_that_would_launch_it(self):
        # PROVISIONAL, the same ruling, on a relaunch. The entry was chosen and stored under one composition and
        # its start failed. The daemon comes back with another binary for that engine: the retry writes no new
        # starting intent and launches nothing, no registrant runs again, and the stored record is as it was. With
        # the first binary back, the same retry relaunches the run.
        log, verdicts = [], ['not_this_time']
        chosen = json.loads(json.dumps(self.routes['claude_code']))

        def register(made):
            made.start_registrant('routing', ('hand_pick',), ('route_entry', 'route_decision'),
                                  lambda principal, request: log.append('registrant') or {'route_entry': chosen, 'route_decision': None},
                                  chooses_engine=True)
            made.set('launch_check', lambda ctx, executable: verdicts.pop(0) if verdicts else None)
        request = {'tenant': 't1', 'prompt': LIVE, 'client_key': 'k1'}
        _, composed = self.boot(hooks_of(register))
        first = composed.control.call('run.start', request)
        self.assertEqual((first['code'], first['start_error']),
                         ('start_failed', {'code': 'launch_refused', 'detail': 'launch_check:not_this_time'}), first)
        run_id = first['run_id']
        self.shutdown()
        other = self.tmp / 'bin' / 'elsewhere' / 'claude'
        other.parent.mkdir(mode=0o700)
        shutil.copyfile(self.engine, other)
        os.chmod(other, 0o700)
        self.routes['claude_code']['binary'] = str(other)
        _, composed = self.boot(hooks_of(register))
        before = composed.control.registry.get(run_id)
        self.assertEqual(before['launch']['route_entry']['binary'], str(self.engine))
        self.assertEqual(composed.control.call('run.start', request), {'code': 'unavailable', 'detail': 'start_registrant_failed'})
        self.assertEqual((composed.control.registry.get(run_id), self.units.started, log), (before, [], ['registrant']))
        self.shutdown()
        self.routes['claude_code']['binary'] = str(self.engine)
        _, composed = self.boot(hooks_of(register))
        again = composed.control.call('run.start', request)
        self.assertEqual((again['code'], again['run']['run_id'], log), ('started', run_id, ['registrant']), again)
        self.stop(composed.control, run_id)

    def test_a_route_entry_a_registrant_chose_is_what_the_starter_launches(self):
        chosen = json.loads(json.dumps(self.routes['claude_code']))
        chosen['route']['model'] = 'chosen-model'

        def register(made):
            made.start_registrant('routing', ('hand_pick',), ('route_entry', 'route_decision'), lambda principal, request: {
                'route_entry': chosen, 'route_decision': {'chosen': 'claude_code', 'reason': 'default', 'candidates': ['claude_code'],
                                                          'hand_pick': None}}, chooses_engine=True)
            made.set('drive_owner', lambda ctx, request: seen.append(request))
        seen = []
        _, composed = self.boot(hooks_of(register))
        answer = composed.control.call('run.start', {'tenant': 't1', 'prompt': LIVE})   # names no engine
        self.assertEqual(answer['code'], 'started', answer)
        # The request is built from the stored entry, not from the configured one (whose model is another).
        self.assertEqual(self.routes['claude_code']['route']['model'], 'stand-in-model')
        self.assertEqual((answer['run']['engine'], seen[0]['route']['model'], seen[0]['deadline_s']), ('claude_code', 'chosen-model', 120.0))
        self.assertEqual(composed.control.registry.get(answer['run']['run_id'])['launch']['route_entry'], chosen)
        self.stop(composed.control, answer['run']['run_id'])


class ResultReaderSeamTest(Composed):
    """S8: run.status asks the reader, and only when the run has no result of its own."""

    def test_only_run_status_asks_and_only_for_a_run_with_no_result(self):
        asked = []
        theirs = {'status': 'ok', 'code': None, 'detail': 'kept by the lane that owns the turns', 'turns': 3}

        def reader(run_id):
            asked.append(run_id)
            return dict(theirs)
        _, composed = self.boot(hooks_of(read_result=reader))
        api = self.api(composed)
        started = api.call('run.start', self.request())
        run_id = started['run']['run_id']
        self.assertNotIn('result', started['run'])
        self.assertNotIn('result', api.call('run.list')['runs'][0])
        self.assertEqual(asked, [], 'neither run.start nor run.list asks')
        self.assertEqual(api.call('run.status', {'run_id': run_id})['run']['result'], theirs)
        self.assertEqual(asked, [run_id])
        self.stop(api, run_id)
        # A run with a result of its own, in memory or in result.json, is answered from it: nobody is asked.
        ended = self.start(api, ENDS)
        self.assertTrue(until(lambda: (self.state / 'runs' / ended / 'result.json').exists()))
        del asked[:]
        own = api.call('run.status', {'run_id': ended})['run']['result']
        self.assertEqual((own['status'], asked), ('ok', []))
        self.assertNotIn('turns', own)


class FakeService:
    """A route service that launches nothing: the request ends at once, refused."""

    def __init__(self):
        self.requests = []

    def handle(self, request):
        self.requests.append((request, launcher._current.ctx))
        return c.result(request['request_id'], c.LAUNCH_REFUSED, detail='fake service')


@unittest.skipUnless(LINUX, LINUX_ONLY)
class DriveOwnerSeamTest(unittest.TestCase):
    """S9: the top of the starter's drive."""

    ROUTES = {'claude_code': {'route': {'engine': 'claude_code', 'model': 'm-1', 'connection': {'kind': 'subscription', 'id': 'login'}},
                              'deadline_s': 60.0, 'binary': None}}
    PARAMS = {'run_id': 'run-1', 'tenant': 't1', 'engine': 'claude_code', 'prompt': 'p'}

    def starter(self, chooser=None, hooks=True):
        folder = tempfile.mkdtemp(prefix='rc-seams-owner-')
        self.addCleanup(shutil.rmtree, folder, True)
        service = FakeService()
        made = hooks_of(**({'drive_owner': chooser} if chooser is not None else {})) if hooks else None
        return adapter.RouteStarter(service, self.ROUTES, mock.Mock(closed=False), folder, hooks=made,
                                    start_timeout_s=HELD_BOUND_S), service

    def refused(self, starter, params=None, *record):
        with self.assertRaises(adapter.StartRefused) as caught:
            starter(dict(params or self.PARAMS), *record)
        return caught.exception.code, caught.exception.detail

    def test_the_starter_builds_the_launch_context_from_the_stored_record_or_refuses_it(self):
        launch = {'route_entry': None, 'route_decision': None, 'workspace': None, 'containment_level': None,
                  'session_id': 'ses-1', 'turn_id': 'turn-1', 'owner': 'host', 'budget': {'cap_usd': 0}}
        record = {'run_id': 'run-1', 'launch': launch, 'principal': {'kind': 'session', 'id': 'ses-1'}}
        starter, service = self.starter()
        self.assertEqual(self.refused(starter, None, record), ('launch_refused', 'fake service'))
        self.assertEqual(self.refused(starter, None, {'run_id': 'run-1'}), ('launch_refused', 'fake service'))   # a legacy record
        self.assertEqual(self.refused(starter), ('launch_refused', 'fake service'))                               # the path that is off
        contexts = [ctx.launch for _, ctx in service.requests]
        self.assertEqual(contexts, [H.LaunchContext.from_record(record), None, None])
        self.assertEqual((contexts[0].session_id, contexts[0].principal), ('ses-1', H.Principal('session', 'ses-1')))
        # A stored launch object that is not whole is never launched as if there were none.
        for broken in ({'run_id': 'run-1', 'launch': {'owner': 'host'}}, {'run_id': 'run-1', 'launch': 'x'},
                       {'run_id': 'run-1', 'launch': launch, 'principal': {'kind': 'root', 'id': 'x'}},
                       {'run_id': 'run-1', 'launch': dict(launch, extra=1)}):
            with self.subTest(record=broken):
                self.assertEqual(self.refused(starter, None, broken), ('launch_refused', 'launch_snapshot_invalid'))
        self.assertEqual(len(service.requests), 3, 'a refused snapshot reached no adapter')

    def test_with_no_owner_the_run_is_driven_as_today(self):
        asked = []
        for chooser, hooks in ((None, False), (None, True), (lambda ctx, request: asked.append((ctx.run_id, request['request_id'])), True)):
            starter, service = self.starter(chooser, hooks)
            self.assertEqual(self.refused(starter), ('launch_refused', 'fake service'))
            self.assertEqual(len(service.requests), 1)
            self.assertTrue(until(lambda: not starter.active('run-1')))
        self.assertEqual(asked, [('run-1', 'run-1')])

    @quiet
    def test_an_owner_that_fails_before_it_accepts_leaves_the_run_on_todays_path(self):
        def chooser(ctx, request):
            raise RuntimeError('cannot decide')
        for bad in (chooser, lambda ctx, request: 'not an owner'):
            starter, service = self.starter(bad)
            self.assertEqual(self.refused(starter), ('launch_refused', 'fake service'))
            self.assertEqual(len(service.requests), 1)

    def test_an_owner_that_accepts_is_handed_the_run_and_keeps_it_until_it_releases_it_once(self):
        handed = []

        class Owner:
            def run(self, ctx, request, service, record_result, fault_run, settle, release):
                handed.append((ctx, request, service, record_result, fault_run, settle, release))
                ctx.finished(c.result(ctx.run_id, c.LAUNCH_REFUSED, detail='the owner ended its first turn'))
        asked = []
        starter, service = self.starter(lambda ctx, request: asked.append((ctx, request)) or Owner())
        self.assertEqual(self.refused(starter), ('launch_refused', 'the owner ended its first turn'))
        (ctx, request, got_service, record_result, fault_run, settle, release), = handed
        self.assertEqual(asked, [(ctx, request)])
        self.assertIsInstance(ctx, launcher.RunContext)
        self.assertEqual((ctx.run_id, request['request_id'], request['route'], request['prompt'], request['deadline_s']),
                         ('run-1', 'run-1', self.ROUTES['claude_code']['route'], 'p', 60.0))
        self.assertIs(got_service, service)
        self.assertEqual((record_result, fault_run, settle), (starter._record_result, starter._fault_run, starter._settle))
        self.assertEqual(service.requests, [], 'the owner drives the run; the starter did not')
        # The run stays active until the owner releases it, and the release counts once.
        self.assertTrue(starter.active('run-1'))
        self.assertEqual(self.refused(starter), ('start_in_flight', ''))
        release()
        self.assertFalse(starter.active('run-1'))
        self.refused(starter)                       # the same run id, started again: a new owner holds it
        self.assertTrue(starter.active('run-1'))
        release()                                   # the first owner's release, a second time: it does nothing
        self.assertTrue(starter.active('run-1'))
        handed[1][-1]()
        self.assertFalse(starter.active('run-1'))

    @quiet
    def test_an_owner_that_fails_after_it_accepted_does_not_strand_the_run(self):
        class Owner:
            def run(self, ctx, *rest):
                raise RuntimeError('lost the worker')
        starter, service = self.starter(lambda ctx, request: Owner())
        code, detail = self.refused(starter)
        self.assertEqual((code, detail), (c.ENGINE_ERROR, 'drive owner failure: RuntimeError'))
        self.assertTrue(until(lambda: not starter.active('run-1')))
        self.assertEqual(service.requests, [])

    def test_the_owner_is_called_with_the_run_bound_to_its_thread_and_binds_any_other_thread_itself(self):
        """As on today's path, the thread of the hand-over is bound to the run, so the route service called there
        launches for it. Another thread is the owner's to bind."""
        seen = []

        def bound(ctx):
            return getattr(launcher._current, 'ctx', None) is ctx

        class Owner:
            def run(self, ctx, request, service, record_result, fault_run, settle, release):
                seen.append(('the thread of the hand-over', bound(ctx)))

                def elsewhere():
                    seen.append(('another thread', bound(ctx)))
                    with launcher.bound_run(ctx):
                        seen.append(('another thread, bound by the owner', bound(ctx)))
                worker = threading.Thread(target=elsewhere, daemon=True)
                worker.start()
                worker.join(HELD_BOUND_S)
                result = service.handle(request)          # the route service, called by the owner on this thread
                ctx.finished(result)
                release()
        starter, service = self.starter(lambda ctx, request: Owner())
        self.assertEqual(self.refused(starter), ('launch_refused', 'fake service'))
        self.assertEqual(seen, [('the thread of the hand-over', True), ('another thread', False),
                                ('another thread, bound by the owner', True)])
        (request, ctx), = service.requests
        self.assertEqual(ctx.run_id, 'run-1', 'the route service saw the run of the hand-over')
        self.assertTrue(until(lambda: not starter.active('run-1')))

    @quiet
    def test_an_owner_that_fails_after_it_launched_has_its_tree_settled_and_is_released_even_when_that_fails(self):
        for settle_fails in (False, True):
            with self.subTest(settle_fails=settle_fails):
                settled, uncaught = [], []

                class Owner:
                    def run(self, ctx, *rest):
                        ctx.process = mock.Mock()          # it launched an engine, and then lost its worker
                        raise RuntimeError('lost the worker')

                def settle(ctx):
                    settled.append(ctx.run_id)
                    if settle_fails:
                        raise OSError('the tree could not be read')
                starter, service = self.starter(lambda ctx, request: Owner())
                starter._settle = settle
                with mock.patch.object(threading, 'excepthook', lambda args: uncaught.append(args.exc_type)):
                    self.assertEqual(self.refused(starter), (c.ENGINE_ERROR, 'drive owner failure: RuntimeError'))
                    self.assertTrue(until(lambda: not starter.active('run-1')), 'released, whatever the settling did')
                    self.assertTrue(until(lambda: uncaught or not settle_fails))
                self.assertEqual((settled, uncaught), (['run-1'], [OSError] if settle_fails else []),
                                 'what is left of its tree was settled, once; a failure of that is not swallowed')


@unittest.skipUnless(LINUX, LINUX_ONLY)
class RunThreadLifetimeTest(unittest.TestCase):
    """S13 on a run's own thread. The start that made the thread may have answered already, so no client call's
    admission covers the drive-owner chooser (S9) or the launcher with its two hooks (S10, S4). Each holds a
    lifetime reference of its own: a close waits for it with the store open, and once closing has begun it is
    refused before any lane code runs. (The credential source, S7, is in ``EngineSeamTest``: its lookup is made
    by the composition.)"""

    def made(self, store, **single):
        made = hooks_of(event_store=lambda state_dir: store, **single)
        made.open_store('/state')
        self.addCleanup(made.finish_close, H.ServiceContext())
        return made

    def starter(self, made, start_timeout_s):
        folder = tempfile.mkdtemp(prefix='rc-seams-life-')
        self.addCleanup(shutil.rmtree, folder, True)
        service = FakeService()
        return adapter.RouteStarter(service, DriveOwnerSeamTest.ROUTES, mock.Mock(closed=False), folder, hooks=made,
                                    start_timeout_s=start_timeout_s), service

    def launch(self, made):
        """The production launcher over stand-ins, called as an adapter calls it: ``(context, unit manager, state
        folder, call, outcome)``. The stand-in manager prepares no unit, so a launch that gets that far ends
        there with an ``OSError``."""
        state = Path(tempfile.mkdtemp(prefix='rc-seams-launch-'))
        self.addCleanup(shutil.rmtree, state, True)
        os.chmod(state, 0o700)
        manager = mock.Mock()
        manager.prepare.side_effect = OSError('this test has no unit manager')
        launch = launcher.ProductionLauncher('claude_code', mock.Mock(), manager, state, hooks=made)
        ctx, outcome = launcher.RunContext('run-1'), []

        def call():
            try:
                with launcher.bound_run(ctx):
                    launch(['/bin/sh'], env={'PATH': '/usr/bin:/bin'})
                outcome.append('launched')
            except c.RouteError as exc:
                outcome.append((exc.code, exc.detail))
            except OSError:
                outcome.append('reached the unit manager')
        return ctx, manager, state, call, outcome

    def test_a_close_waits_for_the_chooser_of_a_run_whose_start_has_already_answered(self):
        store = FakeStore()
        chooser, entered, leave, seen = held_hook(store)
        self.addCleanup(leave.set)
        made = self.made(store, drive_owner=chooser)
        starter, service = self.starter(made, 0)
        with self.assertRaises(adapter.StartRefused) as caught:
            starter(dict(DriveOwnerSeamTest.PARAMS))
        self.assertEqual(caught.exception.code, 'launch_timeout', 'the start has answered; the run\'s own thread goes on')
        close_waits(self, made, store, lambda: (made.begin_close(), made.finish_close(H.ServiceContext())), entered, leave)
        self.assertEqual(seen, [False], 'the store was open for as long as the chooser was inside')
        self.assertTrue(until(lambda: not starter.active('run-1')))

    def test_a_close_waits_for_the_hand_over_to_an_owner_of_a_run_whose_start_has_already_answered(self):
        """``owner.run`` is a hand-over, and closing waits for it: the lifetime reference of the chooser is held
        until ``owner.run`` has returned."""
        store = FakeStore()
        inside, entered, leave, seen = held_hook(store)
        self.addCleanup(leave.set)

        class Owner:
            def run(self, ctx, request, service, record_result, fault_run, settle, release):
                inside()
                release()
        made = self.made(store, drive_owner=lambda ctx, request: Owner())
        starter, service = self.starter(made, 0)
        with self.assertRaises(adapter.StartRefused) as caught:
            starter(dict(DriveOwnerSeamTest.PARAMS))
        self.assertEqual(caught.exception.code, 'launch_timeout', 'the start has answered; the run\'s own thread goes on')
        close_waits(self, made, store, lambda: (made.begin_close(), made.finish_close(H.ServiceContext())), entered, leave)
        self.assertEqual(seen, [False], 'the store was open for as long as the hand-over lasted')
        self.assertTrue(until(lambda: not starter.active('run-1')))

    def test_once_closing_has_begun_a_run_is_refused_before_its_chooser_is_asked(self):
        store, asked = FakeStore(), []
        made = self.made(store, drive_owner=lambda ctx, request: asked.append(ctx.run_id))
        starter, service = self.starter(made, HELD_BOUND_S)
        made.begin_close()
        with self.assertRaises(adapter.StartRefused) as caught:
            starter(dict(DriveOwnerSeamTest.PARAMS))
        self.assertEqual((caught.exception.code, caught.exception.detail), (c.LAUNCH_REFUSED, 'daemon_closing'))
        self.assertEqual((asked, service.requests), ([], []), 'nothing was chosen and nothing was driven')
        self.assertTrue(until(lambda: not starter.active('run-1')))

    def test_a_close_waits_for_a_launch_that_is_inside_one_of_its_hooks(self):
        for hook in ('launch_check', 'working_directory'):
            with self.subTest(hook=hook):
                store = FakeStore()
                held, entered, leave, seen = held_hook(store)
                self.addCleanup(leave.set)
                made = self.made(store, **{hook: held})
                ctx, manager, state, call, outcome = self.launch(made)
                user = threading.Thread(target=call, daemon=True)
                user.start()
                close_waits(self, made, store, lambda: (made.begin_close(), made.finish_close(H.ServiceContext())), entered, leave)
                user.join(HELD_BOUND_S)
                self.assertEqual((seen, outcome), ([False], ['reached the unit manager']),
                                 'the store was open while the hook was inside, and until the launch it belongs to had ended')

    def test_once_closing_has_begun_nothing_is_launched_and_no_launch_hook_is_asked(self):
        store, asked = FakeStore(), []
        made = self.made(store, launch_check=lambda ctx, executable: asked.append('check'),
                         working_directory=lambda ctx: asked.append('folder'))
        ctx, manager, state, call, outcome = self.launch(made)
        made.begin_close()
        call()
        self.assertEqual((outcome, asked), ([(c.LAUNCH_REFUSED, 'daemon_closing')], []))
        manager.prepare.assert_not_called()
        # With hooks nobody registered into the gate is off: a launch during a close is the launch it always was.
        bare = hooks_of()
        bare.begin_close()
        ctx, manager, state, call, outcome = self.launch(bare)
        call()
        self.assertEqual(outcome, ['reached the unit manager'])

    def test_a_start_that_was_abandoned_while_a_launch_hook_ran_launches_nothing(self):
        """The hook takes longer than the start waits: the start gives up and is answered while the hook is
        inside. When the hook returns nothing is launched for it: no run directory, no unit."""
        for hook in ('launch_check', 'working_directory'):
            with self.subTest(hook=hook):
                asked = []

                def spans_the_wait(ctx, *rest):
                    asked.append(hook)
                    self.assertTrue(ctx.abandon(), 'the start gave up while the hook was inside')
                    return None
                hooks = {'launch_check': lambda ctx, executable: asked.append('launch_check'),
                         'working_directory': lambda ctx: asked.append('working_directory'), hook: spans_the_wait}
                made = hooks_of(**hooks)
                ctx, manager, state, call, outcome = self.launch(made)
                call()
                self.assertEqual(outcome, [(c.LAUNCH_REFUSED, 'start_abandoned')])
                # The check comes first. A start abandoned during it asks for no folder: no lane code runs for it.
                self.assertEqual(asked, ['launch_check'] if hook == 'launch_check' else ['launch_check', 'working_directory'])
                manager.prepare.assert_not_called()
                self.assertFalse((state / 'runs').exists(), 'nothing of the unit was made')


class WatchedLock:
    """A registry's lock, counting its holds, and saying when a thread asks for it and whether this thread holds it."""

    def __init__(self, inner):
        self._inner, self._mine = inner, threading.local()
        self.holds = 0
        self.asked = threading.Event()

    def acquire(self, *args, **kwargs):
        self.asked.set()
        got = self._inner.acquire(*args, **kwargs)
        if got:
            self._mine.depth = getattr(self._mine, 'depth', 0) + 1
            if self._mine.depth == 1:
                self.holds += 1
        return got

    def release(self):
        self._mine.depth -= 1
        self._inner.release()

    def held(self):
        return getattr(self._mine, 'depth', 0) > 0

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()


class FakeConsumer:
    """The consumer of the change marker, following the protocol in ``control/hooks.py``, with a list for its log.

    One 'transaction' appends the event and keeps the run's new last digest. Parsing, hashing and that commit
    happen with the registry's lock released: ``outside`` records that for every one of them."""

    def __init__(self, registry, marker, lock=None):
        self.registry, self.marker, self.lock = registry, marker, lock
        self.events, self.last, self.outside = [], {}, []

    def _free(self):
        self.outside.append(self.lock is None or not self.lock.held())

    def log(self, run_id):
        row, text = self.registry.snapshot(run_id)          # one hold of the source's lock
        self._free()
        control = json.loads(text) if text is not None else None
        image = {'row': None if row is None else [row.run_id, row.unit, row.stop_requested, row.state, row.boot_id, row.detail],
                 'control': control}
        digest = hashlib.sha256(json.dumps(image, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        self._free()
        if self.last.get(run_id) != digest:                 # equal to the LAST image logged for this run: nothing new
            self._free()
            self.events.append((run_id, row, control))
            self.last[run_id] = digest

    def take(self):
        ids, dropped = self.marker.take()
        for run_id in sorted(ids):
            self.log(run_id)
        if dropped:
            self.scan()
        return ids, dropped

    def scan(self, page=2):
        after = None
        while True:
            ids = self.registry.run_ids(after, page)       # one bounded page, then the lock is released
            self._free()
            if not ids:
                return
            if after is not None and not all(run_id > after for run_id in ids):
                raise AssertionError(f'a page after {after!r} began with {ids[0]!r}: it must start strictly after its cursor')
            for run_id in ids:
                self.log(run_id)
            after = ids[-1]


def doc(run_id, **more):
    return json.dumps({'run_id': run_id, **more}, sort_keys=True)


@unittest.skipUnless(LINUX, LINUX_ONLY)
class ChangeMarkerRegistryTest(unittest.TestCase):
    """S12 at the daemon registry: the producer's four writers, and the two reads of the source."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='rc-seams-marker-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        os.chmod(self.tmp, 0o700)

    def registry(self, name='state', marker=True):
        registry = Registry(self.tmp / name)
        self.addCleanup(registry.close)
        made = hooks_of(lambda hooks: hooks.enable_changes()) if marker else hooks_of()
        if made.changes is not None:
            registry.mark_changed = made.mark_changed
        return registry, made.changes

    def test_each_of_the_four_writers_marks_its_run_when_it_returns_and_a_writer_that_raises_marks_nothing(self):
        registry, marker = self.registry()
        registry.add('run-a', BOOT)
        self.assertEqual(marker.take(), ({'run-a'}, False))
        registry.request_stop('run-a')
        self.assertEqual(marker.take(), ({'run-a'}, False))
        registry.state('run-a', 'terminated', 'gone')
        self.assertEqual(marker.take(), ({'run-a'}, False))
        registry.control_put('run-b', doc('run-b'))
        self.assertEqual(marker.take(), ({'run-b'}, False))
        for write in (lambda: registry.add('run-a', BOOT), lambda: registry.request_stop('run-none'),
                      lambda: registry.control_put('run-c', {'not': 'text'}), lambda: registry.add('bad id', BOOT)):
            with self.assertRaises(Exception):
                write()
        self.assertEqual(marker.take(), (set(), False))
        self.assertEqual(marker.poll_interval_s, 0.1)

    def test_with_no_consumer_nothing_is_called_and_a_marker_that_raises_never_reaches_the_writer(self):
        registry, marker = self.registry(marker=False)
        self.assertIsNone(marker)
        self.assertIsNone(registry.mark_changed)
        registry.add('run-a', BOOT)
        calls = []

        def raising(run_id):
            calls.append(run_id)
            raise RuntimeError('the marker broke')
        registry.mark_changed = raising
        registry.request_stop('run-a')
        registry.state('run-a', 'terminated')
        registry.control_put('run-a', doc('run-a'))
        registry.add('run-b', BOOT)
        self.assertEqual(calls, ['run-a', 'run-a', 'run-a', 'run-b'])
        self.assertEqual((registry.get('run-a').state, registry.get('run-a').stop_requested), ('terminated', True))

    def test_with_the_marker_set_the_registry_file_is_byte_for_byte_the_bases(self):
        def written(name, marker):
            registry, changes = self.registry(name, marker)
            with mock.patch.object(daemon_module.time, 'time', return_value=1_800_000_000.0):
                registry.add('run-a', BOOT)
                registry.control_put('run-a', doc('run-a', state='starting'))
                registry.request_stop('run-a')
                registry.state('run-a', 'terminated', 'gone')
                registry.control_put('run-c', doc('run-c'))
                registry.add('run-b', BOOT)
            tables = sorted(row[0] for row in registry.db.execute("SELECT name FROM sqlite_master WHERE type='table'"))
            return registry.path.read_bytes(), tables, changes
        base, base_tables, _ = written('base', False)
        marked, tables, changes = written('marked', True)
        self.assertEqual(marked, base)
        self.assertEqual(tables, base_tables)
        self.assertEqual(tables, ['control_runs', 'runs'], 'no table is added')
        self.assertEqual(changes.take(), ({'run-a', 'run-b', 'run-c'}, False))

    def test_a_page_and_a_snapshot_are_bounded_indexed_reads_each_in_one_hold_of_the_lock(self):
        registry, _ = self.registry()
        for run_id in ('run-a', 'run-c', 'run-e'):
            registry.add(run_id, BOOT)
        self.assertEqual(registry.run_ids(None, 10), ['run-a', 'run-c', 'run-e'], 'no control table yet: it contributes nothing')
        self.assertEqual(registry.snapshot('run-a')[1], None)
        for run_id in ('run-b', 'run-c', 'run-d', 'run-f'):
            registry.control_put(run_id, doc(run_id))
        # In EITHER table, in order, strictly after the cursor, at most the limit.
        self.assertEqual(registry.run_ids(None, 10), ['run-a', 'run-b', 'run-c', 'run-d', 'run-e', 'run-f'])
        self.assertEqual(registry.run_ids(None, 2), ['run-a', 'run-b'])
        self.assertEqual(registry.run_ids('run-b', 3), ['run-c', 'run-d', 'run-e'])
        self.assertEqual(registry.run_ids('run-f', 3), [])
        for after, limit in ((None, 0), (None, True), (None, '2'), (7, 2)):
            with self.assertRaises(ValueError):
                registry.run_ids(after, limit)
        row, text = registry.snapshot('run-c')
        self.assertEqual((row.run_id, row.state, row.stop_requested, text), ('run-c', 'starting', False, doc('run-c')))
        self.assertEqual(registry.snapshot('run-b'), (None, doc('run-b')), 'a run with only a control record')
        self.assertEqual(registry.snapshot('run-e')[1], None, 'a run with only a daemon row')
        self.assertEqual(registry.snapshot('run-none'), (None, None))
        # What each read asks of the file: seeks and point lookups on the primary keys, nothing else.
        registry.lock = lock = WatchedLock(registry.lock)
        statements = []
        registry.db.set_trace_callback(statements.append)
        before = lock.holds
        registry.run_ids('run-b', 2)
        self.assertEqual(lock.holds - before, 1)
        page = [text for text in statements if 'sqlite_master' not in text]
        self.assertEqual(page, ["SELECT run_id FROM runs WHERE run_id > 'run-b' ORDER BY run_id LIMIT 2",
                                "SELECT run_id FROM control_runs WHERE run_id > 'run-b' ORDER BY run_id LIMIT 2"])
        del statements[:]
        before = lock.holds
        registry.snapshot('run-c')
        self.assertEqual(lock.holds - before, 1)
        point = [text for text in statements if 'sqlite_master' not in text]
        self.assertEqual(point, ["SELECT run_id, unit, stop, state, boot_id, detail FROM runs WHERE run_id='run-c'",
                                 "SELECT record FROM control_runs WHERE run_id='run-c'"])
        registry.db.set_trace_callback(None)
        for text in page + point:
            plan = ' '.join(row[-1] for row in registry.db.execute('EXPLAIN QUERY PLAN ' + text))
            self.assertIn('SEARCH', plan, text)
            self.assertNotIn('SCAN', plan, text)
        for text in page + point + statements:
            self.assertFalse(any(word in text.upper() for word in ('OFFSET', 'UNION', 'INSERT', 'UPDATE', 'DELETE')), text)

    def test_a_consumer_logs_each_image_once_and_an_observed_a_b_a_three_times(self):
        registry, marker = self.registry()
        registry.lock = lock = WatchedLock(registry.lock)
        consumer = FakeConsumer(registry, marker, lock)
        registry.control_put('run-c', doc('run-c'))        # a control record with no daemon row
        registry.add('run-d', BOOT)                         # a daemon row with no control record
        consumer.take()
        self.assertEqual([(run_id, row is None, control) for run_id, row, control in consumer.events],
                         [('run-c', True, {'run_id': 'run-c'}), ('run-d', False, None)])
        self.assertEqual(consumer.events[1][1].run_id, 'run-d')
        consumer.scan()
        self.assertEqual(len(consumer.events), 2, 'a scan of what was logged logs nothing again')
        # A, then B, then A, each observed: three events. The dedupe is against the last image, not all of them.
        del consumer.events[:]
        for state in ('running', 'stop_pending', 'running'):
            registry.control_put('run-c', doc('run-c', state=state))
            consumer.take()
        self.assertEqual([control.get('state') for _, _, control in consumer.events], ['running', 'stop_pending', 'running'])
        # Changes that come and go between two reads are not kept.
        registry.control_put('run-c', doc('run-c', state='stop_pending'))
        registry.control_put('run-c', doc('run-c', state='running'))
        self.assertEqual(consumer.take(), ({'run-c'}, False))
        self.assertEqual(len(consumer.events), 3)
        self.assertTrue(all(consumer.outside), 'parsing, hashing and the commit are outside the source lock')

    def test_a_record_published_by_a_write_that_then_raised_is_logged_by_the_next_scan(self):
        registry, marker = self.registry()
        consumer = FakeConsumer(registry, marker)
        registry.add('run-a', BOOT)
        consumer.take()
        with mock.patch.object(daemon_module, '_sync_directory', side_effect=daemon_module.RegistryDurabilityError('fsync')):
            with self.assertRaises(daemon_module.RegistryDurabilityError):
                registry.control_put('run-x', doc('run-x', state='starting'))   # renamed into place, then the sync failed
            self.assertEqual(marker.take(), (set(), False), 'a writer that raised marked nothing')
            # While the registry refuses reads, both reads of the source are refused, each by the guard in itself:
            # the consumer waits and tries again.
            with self.assertRaises(daemon_module.RegistryDurabilityError):
                registry.snapshot('run-x')
            with self.assertRaises(daemon_module.RegistryDurabilityError):
                registry.run_ids(None, 10)
        # The first read after the sync can finish is a snapshot: it reads the file, where the record is.
        self.assertEqual(registry.snapshot('run-x'), (None, doc('run-x', state='starting')))
        consumer.scan()                       # the same registry object: no restart
        self.assertEqual([(run_id, control) for run_id, _, control in consumer.events[1:]],
                         [('run-x', {'run_id': 'run-x', 'state': 'starting'})])

    def test_a_mark_made_while_the_consumer_holds_the_markers_lock_is_dropped_and_never_waits(self):
        registry, marker = self.registry()
        consumer = FakeConsumer(registry, marker)
        with marker._lock:                  # the consumer, paused inside take()
            registry.add('run-a', BOOT)     # returns: one attempt on the lock, which cannot block
            registry.control_put('run-a', doc('run-a'))
        self.assertEqual(marker.take(), (set(), True))
        marker._dropped = True
        consumer.take()                     # a drop asks for a scan, and the scan logs the change
        self.assertEqual([run_id for run_id, _, _ in consumer.events], ['run-a'])


class PerRunLock:
    """A lock for each run, in place of the daemon-wide one: the thread says which run it works on."""

    def __init__(self):
        self.run = threading.local()
        self._locks, self._guard = {}, threading.Lock()

    def _lock(self):
        with self._guard:
            return self._locks.setdefault(getattr(self.run, 'id', None), threading.RLock())

    def __enter__(self):
        self._lock().acquire()
        return self

    def __exit__(self, *exc):
        self._lock().release()


class ChangeMarkerStopTest(Composed):
    """S12 against a Stop: the marker never changes a Stop's answer and never makes it wait."""

    def marked(self):
        daemon, composed = self.boot(hooks_of(lambda made: made.enable_changes()))
        self.assertEqual(daemon.registry.mark_changed, composed.hooks.mark_changed)
        return daemon, composed, composed.hooks.changes

    def test_a_close_takes_the_marker_off_the_registry(self):
        daemon, composed, marker = self.marked()
        run_id = self.start(composed.control)
        self.assertIn(run_id, marker.take()[0])
        self.stop(composed.control, run_id)
        self.shutdown()
        self.assertIsNone(daemon.registry.mark_changed, 'a closed composition leaves nothing of its own on the daemon\'s registry')

    def shape(self, composed, run_id, answer):
        """What a Stop answered and left, without the values that name the run."""
        self.settled(run_id)
        after = composed.control.call('run.status', {'run_id': run_id})['run']
        return (sorted(answer), answer['code'], answer.get('state'), answer.get('recorded'), answer.get('reason'),
                after['state'], after['observed']['state'], after['stop_attestation']['proof'])

    def base_shape(self):
        _, composed = self.boot()
        run_id = self.start(composed.control)
        shape = self.shape(composed, run_id, composed.control.call('run.stop', {'run_id': run_id}))
        self.shutdown()
        self.assertEqual(shape[1:], ('stopped', 'stopped', None, None, 'stopped', 'terminated', 'empty'))
        return shape

    def test_a_marker_that_raises_at_any_of_stops_writes_leaves_the_stop_answer_the_bases(self):
        base = self.base_shape()
        for writer in ('request_stop', 'state', 'control_put'):
            with self.subTest(marker_raises_at=writer):
                daemon, composed, _ = self.marked()
                run_id = self.start(composed.control)
                registry, real, inside, raised = daemon.registry, {}, threading.local(), []
                real[writer] = getattr(registry, writer)

                def write(*args, _real=real[writer], **kwargs):
                    inside.writer = True
                    try:
                        return _real(*args, **kwargs)
                    finally:
                        inside.writer = False

                def mark(marked_id):
                    if getattr(inside, 'writer', False):
                        raised.append(marked_id)
                        raise RuntimeError('the marker broke')
                with mock.patch.object(registry, writer, write):
                    registry.mark_changed = mark
                    answer = composed.control.call('run.stop', {'run_id': run_id})
                self.assertEqual(self.shape(composed, run_id, answer), base)
                self.assertIn(run_id, raised, f'the marker was called from {writer}')
                self.shutdown()

    def paused_consumer(self, per_run):
        """A consumer paused INSIDE take(), holding the marker's lock, while Stops are issued."""
        base = self.base_shape()
        daemon, composed, marker = self.marked()
        consumer = FakeConsumer(daemon.registry, marker)
        runs = [self.start(composed.control), self.start(composed.control)]
        consumer.take()
        consumer.scan()
        logged = len(consumer.events)
        if per_run:
            daemon.lock = PerRunLock()
        answers = {}

        def stop(run_id):
            if per_run:
                daemon.lock.run.id = run_id
            answers[run_id] = composed.control.call('run.stop', {'run_id': run_id})
        with marker._lock:
            threads = [threading.Thread(target=stop, args=(run_id,), daemon=True) for run_id in runs]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(HELD_BOUND_S)
                self.assertFalse(thread.is_alive(), 'a Stop waited for the marker')
            # Both Stops completed while the consumer still held the marker's lock.
            self.assertEqual(sorted(answers), sorted(runs))
        for run_id in runs:
            self.assertEqual(self.shape(composed, run_id, answers[run_id]), base)
        ids, dropped = marker.take()
        self.assertEqual((ids, dropped), (set(), True), 'the marks were dropped, not waited for')
        consumer.scan()   # the next scan logs the change
        stopped = [control for run_id, _, control in consumer.events[logged:] if control is not None]
        self.assertEqual(sorted(control['run_id'] for control in stopped if control['state'] == 'stopped'), sorted(runs))

    def test_a_stop_issued_while_the_consumer_is_paused_inside_take_completes_and_the_next_scan_logs_it(self):
        self.paused_consumer(per_run=False)

    def test_the_same_with_a_lock_per_run_in_place_of_the_daemon_wide_one(self):
        self.paused_consumer(per_run=True)

    def paused_reader(self, per_run):
        """A reader paused while it holds the registry's lock: a Stop can wait for it, and is truthful after it."""
        base = self.base_shape()
        daemon, composed, marker = self.marked()
        run_id = self.start(composed.control)
        daemon.registry.lock = lock = WatchedLock(daemon.registry.lock)
        if per_run:
            daemon.lock = PerRunLock()
        consumer = FakeConsumer(daemon.registry, marker, lock)
        answers = []

        def stop():
            if per_run:
                daemon.lock.run.id = run_id
            answers.append(composed.control.call('run.stop', {'run_id': run_id}))
        thread = threading.Thread(target=stop, daemon=True)
        with lock:                          # the reader, paused inside a read of the source
            lock.asked.clear()
            thread.start()
            self.assertTrue(lock.asked.wait(HELD_BOUND_S), 'the Stop reached the registry')
            self.assertEqual(answers, [], 'and waits there: the lock promises it nothing sooner')
        thread.join(HELD_BOUND_S)
        self.assertFalse(thread.is_alive(), 'the Stop went on once the reader let go')
        self.assertEqual(self.shape(composed, run_id, answers[0]), base)
        consumer.take()
        consumer.scan()
        self.assertEqual([control['state'] for _, _, control in consumer.events if control is not None][-1], 'stopped')
        self.assertTrue(all(consumer.outside), 'parsing, hashing and the commit are outside the source lock')

    def test_a_stop_can_wait_for_a_reader_that_holds_the_registrys_lock_and_is_truthful_after_it(self):
        self.paused_reader(per_run=False)

    def test_the_same_reader_with_a_lock_per_run_in_place_of_the_daemon_wide_one(self):
        self.paused_reader(per_run=True)


class ServicesSeamTest(Composed):
    """S13: what is started before the socket is served, and what a close waits for."""

    def registered(self, log, entered, leave, *, callback=False, poll=False):
        store = FakeStore(log)
        store.register_tables('sessions', 1, 'CREATE TABLE intents (command_id TEXT PRIMARY KEY, what TEXT NOT NULL)')
        events = Service('events', log)
        events.ready = True
        # Only the two reads of a log name a service in the published table. The callback case is a command, so it
        # says what the row of a lane with a service would say.
        row_names_service(self, 'session.start' if callback else 'events.follow', 'events')

        def held(ctx, params):
            if poll:                     # a long poll: it waits for an event, or for the daemon to close
                entered.set()
                ctx.closing.wait(HELD_BOUND_S)
                log.append(('woken', 'poll', None))
                return {'code': 'unavailable', 'detail': 'daemon_closing'}

            def wait():
                entered.set()
                leave.wait(HELD_BOUND_S)
                log.append(('left', 'callback' if callback else 'handler', None))
            if callback:
                ctx.tx.execute('INSERT INTO intents VALUES (?, ?)', (params['command_id'], 'intent'))
                ctx.after_commit(wait)
            else:
                wait()
            return {'code': 'ok'}

        def register(made):
            made.service(events)
            made.set('event_store', lambda state_dir: store)
            if callback:
                made.methods.register('session.start', held, service='events', tenant=methods.INSTALL_TENANT)
            else:
                made.methods.register('events.follow', held, service='events')
            made.set('page_methods', lambda: frozenset({'session.start', 'events.follow'}))
        return hooks_of(register), store, events

    def held_close(self, entry, **kind):
        """A call that is inside the daemon when it closes: the close waits for it, with its service and store alive."""
        log, entered, leave = [], threading.Event(), threading.Event()
        self.addCleanup(leave.set)
        made, store, events = self.registered(log, entered, leave, **kind)
        daemon, composed = self.boot(made)
        method = 'session.start' if kind.get('callback') else 'events.follow'
        params = {'command_id': 'c-1'} if kind.get('callback') else {'session_id': 'ses-1'}
        answers = []
        call = {'socket': lambda: self.api(composed).call(method, params),
                'page': lambda: self.page(composed, 'POST', '/v1/control', {'method': method, 'params': params}),
                'in-process': lambda: composed.control.call(method, params)}[entry]
        caller = threading.Thread(target=lambda: answers.append(call()), daemon=True)
        caller.start()
        self.assertTrue(entered.wait(HELD_BOUND_S), 'the call was admitted and is inside')
        composed.server._idle_timeout_s = 0.0   # the transport's timed join of a busy worker: its wait is not the drain
        draining, drain = threading.Event(), made.gate.drain
        closed = []

        def close():
            with mock.patch.object(made.gate, 'drain', lambda: (draining.set(), drain())):
                composed.close()
            closed.append(True)
        closer = threading.Thread(target=close, daemon=True)
        closer.start()
        self.open.remove((daemon, composed))
        self.addCleanup(daemon.close)
        self.assertTrue(draining.wait(HELD_BOUND_S), 'the transports stopped admitting and the close reached its wait')
        if not kind.get('poll'):
            # The call is still inside: its service runs, its store is open, and the close has not returned.
            self.assertEqual(([what for what, _, _ in log if what in ('stop', 'close', 'left')], store.closed, closed), ([], False, []))
            self.assertEqual(made.gate.admitted, 1)
            # No new client call enters, on any entry.
            closing = {'code': 'unavailable', 'detail': 'daemon_closing'}
            self.assertEqual(composed.control.call('run.list'), closing)
            self.assertEqual(composed.control.call('run.stop', {'run_id': 'run-none'}, H.DEVICE), closing)
            self.assertEqual(self.page(composed, 'POST', '/v1/runs/stop', {'run_id': 'run-none'}), (200, closing))
            self.assertEqual(self.page(composed, 'GET', '/v1/routes'), (503, {'error': 'unavailable: daemon_closing'}))
            self.assertEqual(self.page(composed, 'POST', '/v1/operator', {'command': 'operator.health.inspect'}),
                             (503, {'error': 'unavailable: daemon_closing'}))
            self.assertEqual(composed.control.handle({'op': 'control', 'method': 'run.list', 'params': {}}, peer()),
                             {'ok': True, 'result': closing})
            self.assertTrue(made.closing.is_set())
            leave.set()
        closer.join(HELD_BOUND_S)
        caller.join(HELD_BOUND_S)
        self.assertFalse(closer.is_alive() or caller.is_alive(), 'the close and the call both ended')
        self.assertEqual(closed, [True])
        order = [(what, name) for what, name, _ in log if what != 'start']
        inside = ('woken', 'poll') if kind.get('poll') else ('left', 'callback' if kind.get('callback') else 'handler')
        self.assertEqual(order, [inside, ('stop', 'events'), ('close', 'store')])
        self.assertTrue(store.closed)
        self.assertIsNone(daemon.registry.mark_changed)
        return answers

    def test_a_close_waits_for_a_handler_that_is_inside_on_the_socket(self):
        self.held_close('socket')

    def test_a_close_waits_for_a_handler_that_is_inside_from_the_page(self):
        self.assertEqual(self.held_close('page'), [(200, {'code': 'ok'})])

    def test_a_close_waits_for_a_handler_that_is_inside_in_process(self):
        self.assertEqual(self.held_close('in-process'), [{'code': 'ok'}])

    def test_a_close_waits_for_an_after_commit_callback_on_the_socket(self):
        self.held_close('socket', callback=True)

    def test_a_close_waits_for_an_after_commit_callback_from_the_page(self):
        self.assertEqual(self.held_close('page', callback=True), [(200, {'code': 'ok'})])

    def test_a_close_waits_for_an_after_commit_callback_in_process(self):
        self.assertEqual(self.held_close('in-process', callback=True), [{'code': 'ok'}])

    def test_a_close_wakes_a_long_poll_and_waits_for_it_to_leave(self):
        self.assertEqual(self.held_close('in-process', poll=True), [{'code': 'unavailable', 'detail': 'daemon_closing'}])

    def test_a_long_poll_on_the_socket_is_woken_too(self):
        self.held_close('socket', poll=True)

    def test_a_close_shuts_the_gate_before_it_stops_the_transport(self):
        """The order of a close. FIRST no new client call is admitted and whoever waits is told; only THEN does
        the transport stop. At the moment the transport is asked to stop, the closing is already signalled and
        a call made in this process is already refused."""
        log, entered, leave = [], threading.Event(), threading.Event()
        self.addCleanup(leave.set)
        made, store, events = self.registered(log, entered, leave)
        daemon, composed = self.boot(made)
        seen, stop_transport = [], composed.server.close

        def stopping():
            seen.append((made.closing.is_set(), composed.control.call('run.list')))
            return stop_transport()
        composed.server.close = stopping
        self.open.remove((daemon, composed))
        self.addCleanup(daemon.close)
        composed.close()
        self.assertEqual(seen, [(True, {'code': 'unavailable', 'detail': 'daemon_closing'})])
        self.assertTrue(store.closed)

    def two_closers(self, interrupted):
        """The composition's close has one owner. The first close is held inside a service's stop. A second close
        arrives then, on another thread (and is interrupted while it waits, when ``interrupted``). What each
        closer finds at the moment its own call ends is written down then, so no schedule decides the test."""
        log, in_stop, go_on = [], threading.Event(), threading.Event()
        self.addCleanup(go_on.set)
        store = FakeStore(log)

        class Held(Service):
            def stop(inner, context):
                in_stop.set()
                go_on.wait(HELD_BOUND_S)
                super().stop(context)

        def register(made):
            made.service(Held('events', log))
            made.set('event_store', lambda state_dir: store)
        daemon, composed = self.boot(hooks_of(register))
        self.open.remove((daemon, composed))
        self.addCleanup(daemon.close)
        counts, close_plugins = [], composed.plugins.close
        composed.plugins.close = lambda: (counts.append('plugins'), close_plugins())[1]
        composed._closing = OwnerLock()
        done = []

        def close(name):
            try:
                composed.close()
                done.append((name, 'returned', store.closed, list(counts)))
            except KeyboardInterrupt:
                done.append((name, 'interrupted', store.closed, list(counts)))
        first = threading.Thread(target=close, args=('first',), daemon=True)
        first.start()
        self.assertTrue(in_stop.wait(HELD_BOUND_S), 'the first close is inside the service\'s stop')
        if interrupted:
            composed._closing.interrupt_next = 'before'     # the next thread that asks for the close is interrupted
        second = threading.Thread(target=close, args=('second',), daemon=True)
        second.start()
        self.assertTrue(composed._closing.contended.wait(HELD_BOUND_S), 'the second close has arrived')
        self.assertEqual((store.closed, counts), (False, []), 'nothing is closed under the first')
        go_on.set()
        first.join(HELD_BOUND_S)
        second.join(HELD_BOUND_S)
        self.assertEqual(sorted(done), [('first', 'returned', True, ['plugins']),
                                        ('second', 'interrupted' if interrupted else 'returned', True, ['plugins'])],
                         'the second ended only when the whole close was complete, and closed nothing itself')
        composed.close()            # and a close after it returns at once
        self.assertEqual((counts, [what for what, _, _ in log if what in ('stop', 'close')]), (['plugins'], ['stop', 'close']))

    def test_a_second_close_of_the_composition_waits_for_the_first_and_closes_nothing_itself(self):
        self.two_closers(interrupted=False)

    def test_a_second_close_that_is_interrupted_while_it_waits_goes_on_waiting(self):
        self.two_closers(interrupted=True)

    def test_a_failure_inside_the_close_is_raised_once_after_everything_was_closed(self):
        log, entered, leave = [], threading.Event(), threading.Event()
        made, store, events = self.registered(log, entered, leave)
        daemon, composed = self.boot(made)
        self.open.remove((daemon, composed))
        self.addCleanup(daemon.close)
        stop_transport = composed.server.close

        def failing():
            stop_transport()
            raise OSError('the socket file could not be removed')
        composed.server.close = failing
        with self.assertRaises(OSError):
            composed.close()
        self.assertEqual(([(what, name) for what, name, _ in log if what != 'start'], store.closed),
                         ([('stop', 'events'), ('close', 'store')], True), 'the rest of the close ran before the failure was raised')
        composed.close()            # it is closed: a later close does nothing, and raises nothing

    def test_a_close_called_from_inside_the_close_is_refused_and_does_not_wait_for_itself(self):
        log, seen = [], []
        store = FakeStore(log)

        class Closes(Service):
            def stop(inner, context):
                try:
                    composed.close()
                    seen.append('ran')
                except H.HookError:
                    seen.append('refused')
                super().stop(context)

        def register(made):
            made.service(Closes('events', log))
            made.set('event_store', lambda state_dir: store)
        daemon, composed = self.boot(hooks_of(register))
        self.open.remove((daemon, composed))
        self.addCleanup(daemon.close)
        composed.close()
        self.assertEqual((seen, store.closed, [what for what, _, _ in log if what in ('stop', 'close')]),
                         (['refused'], True, ['stop', 'close']))

    @quiet
    def test_a_start_that_fails_part_way_releases_everything_it_made(self):
        token = self.runtime / 'recursion' / 'gui-device-token'
        for failing in ('service', 'store', 'server', 'door'):
            with self.subTest(failing=failing):
                log = []
                store = FakeStore(log)
                first, second = Service('events', log), Service('sessions', log, fail_start=(failing == 'service'))

                def register(made):
                    made.service(first)
                    made.service(second)
                    made.enable_changes()
                    made.set('event_store', (lambda state_dir: store) if failing != 'store'
                             else (lambda state_dir: (_ for _ in ()).throw(OSError('cannot open the store'))))
                patches = contextlib.ExitStack()
                if failing == 'server':
                    patches.enter_context(mock.patch.object(adapter.UnixControlServer, 'start', side_effect=OSError('address in use')))
                if failing == 'door':
                    patches.enter_context(mock.patch.object(adapter.DeviceDoor, 'publish', side_effect=OSError('read-only runtime dir')))
                daemon = self.new_daemon()
                self.addCleanup(daemon.close)
                with patches, self.assertRaises((RuntimeError, OSError)):
                    adapter.compose_control(daemon, self.state, environ=self.environ, routes=self.routes, hooks=hooks_of(register))
                # Every service whose start was tried was stopped, in reverse, the one that failed part way among
                # them; the store was closed after them; nothing is served.
                started = [name for what, name, _ in log if what == 'start']
                self.assertEqual(started, {'service': ['events', 'sessions'], 'store': []}.get(failing, ['events', 'sessions']))
                expected = {'store': []}.get(failing, [('stop', 'sessions'), ('stop', 'events'), ('close', 'store')])
                self.assertEqual([(what, name) for what, name, _ in log if what != 'start'], expected)
                self.assertEqual(store.closed, failing != 'store')
                self.assertIsNone(daemon.registry.mark_changed)
                self.assertFalse(token.exists())
                # What it held is free again: the same daemon composes, serves and closes.
                composed = adapter.compose_control(daemon, self.state, environ=self.environ, routes=self.routes)
                try:
                    self.assertEqual(self.api(composed).call('run.list'), {'code': 'ok', 'runs': []})
                    self.assertTrue(token.exists())
                finally:
                    composed.close()
                daemon.close()


if __name__ == '__main__':
    unittest.main()
