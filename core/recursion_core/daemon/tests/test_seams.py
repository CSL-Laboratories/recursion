"""The calls the daemon's own files make into the seams (control/hooks.py): S1 and S3 to S13.

``control/tests/test_seams.py`` tests the hooks themselves, the three tables and the control API's part.
Here a daemon is composed as in ``test_composition`` (everything real but the service manager, with the
stand-in engine), a fake is registered into a hook, and the test sees the daemon's own file call it with
the documented arguments. With nothing registered the composition is the one it was before the seams.
"""
import contextlib
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

import recursion_core.control.tests.test_seams as control_seams
import recursion_core.daemon as daemon_module
from recursion_core.control import hooks as H, methods
from recursion_core.control.client import SocketAPI
from recursion_core.control.tests.test_seams import (
    LANDED_WITH_AN_ENGINE, FakeLanes, FakeStore, RegionWalk, Service, WatchedLock as OwnerLock,
    answered_from_another_thread, landed, quiet, row_names_service, watch)
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


def close_waits(case, made, store, close, entered, leave, admitted=1):
    """Lane code is inside (``entered``) on a thread that no client call's admission covers. ``close`` is called
    now, on a thread of its own. It moves on in one of two ways: it WAITS in the drain (right), or it finishes
    (wrong). Either sets one event, and what is true at that moment is what is asserted, so no schedule decides
    the test. Then the lane code is let go, and the close ends with the store closed. ``admitted`` is how many
    references are inside at that moment."""
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
    case.assertEqual((finished, store.closed, made.gate.admitted), ([], False, admitted),
                     'it WAITS: the store is open and the close has not ended while the lane code is inside')
    leave.set()
    closer.join(HELD_BOUND_S)
    case.assertEqual((finished, store.closed), ([True], True))


@unittest.skipUnless(LINUX, LINUX_ONLY)
class Composed(unittest.TestCase):
    """A composed daemon, as in ``test_composition.ComposedDaemonTest``.

    A ``boot()`` that is given no hooks composes them from the installed registrants, which is the daemon's own
    path. These tests are about a tree in which no lane has landed, whatever this tree holds on the day they run:
    every registrant that is really here gets a stand-in that registers nothing, for the test (the fixture of the
    control-side tests). A test's own fake lane is installed over that. Each lane tests its own registration."""

    def setUp(self):
        self.lanes = FakeLanes(self)                # made first, so it is cleaned last: after the composition has closed
        self.lanes.stand_in_for_the_installed()
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


class LandedLaneTest(Composed):
    """What ``Composed.setUp`` promises, held: a lane that has really landed does not change what these tests
    compose. A module that plays a lane's real registrant is in the tree BEFORE the fixture is made."""

    def setUp(self):
        real, _, _ = landed(self, LANDED_WITH_AN_ENGINE)
        self.assertEqual(importlib.util.find_spec('recursion_core.control.scopes').origin, real.__file__,
                         'it is installed: a composition would find it, and its engine')
        super().setUp()

    def test_a_boot_that_is_given_no_hooks_is_the_bare_composition_whatever_has_landed(self):
        _, composed = self.boot()
        made = composed.hooks
        self.assertEqual((made.gate.enabled, made.methods.registered(), made.commands.names(), tuple(made.engines.names())),
                         (False, (), (), ('claude_code', 'codex')))


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

    def test_a_credential_source_registered_alone_is_waited_for_and_is_not_asked_once_closing_has_begun(self):
        """The fifth reads. The same with ``credentials`` as the only hook a lane registered: no store, no
        service, no handler. The gate is on all the same (``Admission`` in ``control/hooks.py``): the
        composition's close waits for the source while it is inside, and after it the lookup refuses the launch
        before the source is asked. (With the gate off the close went past the source, and the source was asked
        again afterwards.) The engine factory that is replaced here only hands the test the lookup. It is a
        registration too: that the source by itself turns the gate on is held in the control-side file."""
        built, ended = [], mock.Mock(closed=False)  # no store here: this says the close has ended, as a store's flag does
        source, entered, leave, seen = held_hook(ended, 'the-secret')
        self.addCleanup(leave.set)

        def factory(launch, clock, credentials, environ, **options):
            built.append(credentials)
            return ClaudeCodeAdapter(launch, clock, credentials, environ, **options)

        def register(made):
            made.engines.register('claude_code', factory, replace=True)    # only so that the test gets the lookup
            made.set('credentials', source)
        made = hooks_of(register)
        self.assertEqual((sorted(made._single), made.gate.enabled), (['credentials'], True), 'the source is the only hook: the gate is on')
        daemon, composed = self.boot(made)
        lookup, = built
        got = []
        user = threading.Thread(target=lambda: got.append(lookup('work', 'ANTHROPIC_API_KEY')), daemon=True)
        user.start()
        self.open.remove((daemon, composed))
        self.addCleanup(daemon.close)

        def close():
            composed.close()
            ended.closed = True
        close_waits(self, composed.hooks, ended, close, entered, leave)
        user.join(HELD_BOUND_S)
        self.assertEqual((got, seen), (['the-secret'], [False]), 'the close had not ended while the source was inside')
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
    def test_a_launch_refused_as_closing_began_is_recorded_by_its_start_before_the_close_can_end(self):
        """A launch that was inside its check when closing began is refused when the check returns. Its record is
        not written on the run's own thread: the start that asked for the launch is still waiting for it, inside
        its own admission, and writes ``start_failed`` itself before it answers. The close waits for every
        admitted call, and the registry is closed only after the close (``daemon/__main__.py``), so that write
        cannot lose to it: no record is left ``starting`` for a run that never launched. The client is answered
        ``start_failed`` with the refusal under ``start_error``, never a bare ``launch_refused``."""
        store = FakeStore()
        check, entered, leave, seen = held_hook(store)
        self.addCleanup(leave.set)
        daemon, composed = self.boot(hooks_of(launch_check=check, event_store=lambda state_dir: store))
        registry, writes, answers = composed.control.api._registry, [], []
        put = registry.put

        def watched(rec):
            writes.append((rec.get('state'), store.closed))
            return put(rec)
        user = threading.Thread(target=lambda: answers.append(composed.control.call('run.start', self.request(client_key='k1'))),
                                daemon=True)
        self.open.remove((daemon, composed))
        self.addCleanup(daemon.close)
        with mock.patch.object(registry, 'put', watched):
            user.start()
            # Two references are inside: the start's own admission, and the launcher's around its hook.
            close_waits(self, composed.hooks, store, composed.close, entered, leave, admitted=2)
            user.join(HELD_BOUND_S)
        answer, = answers
        self.assertEqual((answer['code'], answer['start_error'], answer['recorded']),
                         ('start_failed', {'code': 'launch_refused', 'detail': 'daemon_closing'}, True))
        self.assertEqual(writes, [('starting', False), ('start_failed', False)],
                         'both writes of the start were made with the store open: the close had not ended')
        self.assertEqual(registry.get(answer['run_id'])['state'], 'start_failed')
        self.assertEqual((seen, self.units.started), ([False], []))
        self.assertFalse((self.state / 'runs' / answer['run_id']).exists())

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

    def chooses(self, log=None):
        """A start registrant that chooses the configured entry itself, as a router would."""
        def register(made):
            made.start_registrant('routing', ('hand_pick',), ('route_entry', 'route_decision'),
                                  lambda principal, request: (log.append('registrant') if log is not None else None) or {
                                      'route_entry': json.loads(json.dumps(self.routes['claude_code'])), 'route_decision': None},
                                  chooses_engine=True)
        return register

    def test_only_an_unreplaced_seed_constructor_has_an_executable_the_composition_can_vouch_for(self):
        """PROVISIONAL (the second read's B1). What the composition compared a chosen entry with was the option it
        HANDED the factory, not what the returned adapter runs. A factory a lane put in the seed's place can run
        anything: here it builds the real adapter with another binary than the one it was given. Its engine has
        no executable the composition can vouch for, so a chosen entry is refused before anything is stored,
        although the entry and the configuration say the same path."""
        other = self.tmp / 'bin' / 'elsewhere' / 'claude'
        other.parent.mkdir(mode=0o700)
        shutil.copyfile(self.engine, other)
        os.chmod(other, 0o700)
        built = []

        def replacement(launch, clock, credentials, environ, **options):
            built.append(options)
            return ClaudeCodeAdapter(launch, clock, credentials, environ, binary=str(other))

        def register(made):
            self.chooses()(made)
            made.engines.register('claude_code', replacement, replace=True)
        _, composed = self.boot(hooks_of(register))
        self.assertEqual(built, [{'binary': str(self.engine)}], 'the factory was handed the configured binary')
        answer = composed.control.call('run.start', {'tenant': 't1', 'prompt': LIVE, 'client_key': 'k1'})
        self.assertEqual(answer, {'code': 'unavailable', 'detail': 'start_registrant_failed'})
        self.assertEqual((composed.control.registry.list(), self.units.started), ([], []), 'no run, no launch record, no unit')
        # A subclass in the seed's place is not the seed either.
        self.shutdown()

        class Mine(ClaudeCodeAdapter):
            pass

        def register_subclass(made):
            self.chooses()(made)
            made.engines.register('claude_code', Mine, replace=True)
        _, composed = self.boot(hooks_of(register_subclass))
        self.assertEqual(composed.control.call('run.start', {'tenant': 't1', 'prompt': LIVE, 'client_key': 'k1'}),
                         {'code': 'unavailable', 'detail': 'start_registrant_failed'})
        self.assertEqual(composed.control.registry.list(), [])

    def test_a_relaunch_is_refused_when_the_factory_changed_behind_an_unchanged_configuration(self):
        """PROVISIONAL, the same on a relaunch. The entry was chosen and stored under the seed constructor and its
        start failed. The daemon comes back with the SAME route file and another factory for that engine. The
        retry writes no new starting intent, launches nothing and runs no registrant; the stored record is as
        it was. With the seed constructor back, the same retry relaunches the run."""
        log, verdicts = [], ['not_this_time']

        def register(made, factory=None):
            self.chooses(log)(made)
            made.set('launch_check', lambda ctx, executable: verdicts.pop(0) if verdicts else None)
            if factory is not None:
                made.engines.register('claude_code', factory, replace=True)
        request = {'tenant': 't1', 'prompt': LIVE, 'client_key': 'k1'}
        _, composed = self.boot(hooks_of(register))
        first = composed.control.call('run.start', request)
        self.assertEqual(first['code'], 'start_failed', first)
        run_id = first['run_id']
        self.shutdown()
        _, composed = self.boot(hooks_of(lambda made: register(made, lambda *args, **options: ClaudeCodeAdapter(*args, **options))))
        before = composed.control.registry.get(run_id)
        self.assertEqual(composed.control.call('run.start', request), {'code': 'unavailable', 'detail': 'start_registrant_failed'})
        self.assertEqual((composed.control.registry.get(run_id), self.units.started, log), (before, [], ['registrant']))
        self.shutdown()
        _, composed = self.boot(hooks_of(register))
        again = composed.control.call('run.start', request)
        self.assertEqual((again['code'], again['run']['run_id'], log), ('started', run_id, ['registrant']), again)
        self.stop(composed.control, run_id)

    def test_with_no_binary_configured_and_none_chosen_both_mean_the_constructors_default(self):
        """PROVISIONAL, the constructor default. The route file names no binary and the chosen entry names none:
        both mean the command the seed constructor launches by default, found on the child's PATH. That entry is
        launchable. One that names a path is not: the adapter would launch its default, not that path."""
        self.routes['claude_code']['binary'] = None
        self.environ['PATH'] = f"{self.tmp / 'bin'}:/usr/bin:/bin"       # where the default command name is found
        choices = [dict(json.loads(json.dumps(self.routes['claude_code'])), binary=str(self.engine)),
                   json.loads(json.dumps(self.routes['claude_code']))]

        def register(made):
            made.start_registrant('routing', ('hand_pick',), ('route_entry', 'route_decision'),
                                  lambda principal, request: {'route_entry': choices[0], 'route_decision': None}, chooses_engine=True)
        _, composed = self.boot(hooks_of(register))
        request = {'tenant': 't1', 'prompt': LIVE, 'client_key': 'k1'}
        self.assertEqual(composed.control.call('run.start', request), {'code': 'unavailable', 'detail': 'start_registrant_failed'})
        self.assertEqual(composed.control.registry.list(), [])
        del choices[0]
        answer = composed.control.call('run.start', request)
        self.assertEqual(answer['code'], 'started', answer)
        run_id = answer['run']['run_id']
        self.assertIsNone(composed.control.registry.get(run_id)['launch']['route_entry']['binary'])
        argv = self.units.started[-1]['argv']
        self.assertEqual(argv[argv.index('--') + 1], str(self.engine), 'the default name, found on the PATH, is what ran')
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

    @quiet
    def test_an_owner_that_fails_while_the_composition_closes_is_released_and_its_run_is_not_settled(self):
        """The second read's B3. "Unless the daemon is closing" is the whole of the close: while it waits for the
        hand-over the daemon has not said closed yet, and the composition is closing. An owner that fails then
        is released, its run gets the engine-error result, and nothing of the run is settled: closing leaves a
        run alive."""
        store = FakeStore()
        entered, leave, settled, process = threading.Event(), threading.Event(), [], mock.Mock()
        self.addCleanup(leave.set)

        class Owner:
            def run(self, ctx, request, service, record_result, fault_run, settle, release):
                ctx.process = process                  # it launched an engine
                entered.set()
                leave.wait(HELD_BOUND_S)
                raise RuntimeError('lost the worker')
        made = self.made(store, drive_owner=lambda ctx, request: Owner())
        starter, service = self.starter(made, 0)
        starter._settle = lambda ctx: settled.append((ctx.run_id, made.gate.closed))
        with self.assertRaises(adapter.StartRefused) as caught:
            starter(dict(DriveOwnerSeamTest.PARAMS))
        self.assertEqual(caught.exception.code, 'launch_timeout')
        self.assertFalse(starter._daemon.closed, 'the daemon itself has not said closed')
        close_waits(self, made, store, lambda: (made.begin_close(), made.finish_close(H.ServiceContext())), entered, leave)
        self.assertTrue(until(lambda: not starter.active('run-1')), 'released')
        self.assertEqual(settled, [], 'nothing of the run was settled while the composition closed')
        # The third reads: its descriptors are closed all the same, and nothing is fenced. The unit is left alive.
        process.close.assert_called_once_with()
        process.fence.assert_not_called()

    @quiet
    def test_an_owner_that_fails_once_the_daemon_has_said_closed_has_its_descriptors_closed_and_nothing_fenced(self):
        """The same on the daemon's own closed flag, with nothing of the composition closing: the descriptors
        are closed, nothing is settled or fenced, and the run is released, also when closing them fails."""
        for failing in (False, True):
            store, settled, process = FakeStore(), [], mock.Mock()
            if failing:
                process.close.side_effect = OSError('a descriptor was closed already')

            class Owner:
                def run(self, ctx, request, service, record_result, fault_run, settle, release):
                    ctx.process = process
                    raise RuntimeError('lost the worker')
            made = self.made(store, drive_owner=lambda ctx, request: Owner())
            starter, service = self.starter(made, HELD_BOUND_S)
            starter._daemon.closed = True
            starter._settle = lambda ctx: settled.append(ctx.run_id)
            uncaught = []
            with self.subTest(closing_the_descriptors_fails=failing):
                with mock.patch.object(threading, 'excepthook', lambda args: uncaught.append(args.exc_type)):
                    with self.assertRaises(adapter.StartRefused):
                        starter(dict(DriveOwnerSeamTest.PARAMS))
                    self.assertTrue(until(lambda: not starter.active('run-1')), 'released')
                    self.assertTrue(until(lambda: uncaught or not failing))
                self.assertEqual((settled, uncaught), ([], [OSError] if failing else []), 'a failure to close them is not swallowed')
                process.close.assert_called_once_with()
                process.fence.assert_not_called()

    @quiet
    def test_a_settling_that_is_under_way_when_closing_begins_fences_nothing(self):
        """The third reads. An accepted owner failed while nothing was closing, so what it launched is being
        settled: the engine is given a moment to exit, then its tree is fenced. Closing begins in that moment.
        The close waits for the settling (it is inside the hand-over), and the settling fences nothing: not on
        its next time round, and not after its moment has run out. The descriptors are closed either way."""
        for when in ('its next time round', 'after its moment has run out'):
            store, process = FakeStore(), mock.Mock()
            inside, leave, asked = threading.Event(), threading.Event(), []
            self.addCleanup(leave.set)

            class Owner:
                def run(self, ctx, request, service, record_result, fault_run, settle, release):
                    ctx.process = process
                    raise RuntimeError('lost the worker')

            def reconcile(run_id, asked=asked, inside=inside, leave=leave):
                asked.append(run_id)
                inside.set()
                leave.wait(HELD_BOUND_S)
                return mock.Mock(state='running')
            made = self.made(store, drive_owner=lambda ctx, request: Owner())
            folder = tempfile.mkdtemp(prefix='rc-seams-settle-')
            self.addCleanup(shutil.rmtree, folder, True)
            starter = adapter.RouteStarter(FakeService(), DriveOwnerSeamTest.ROUTES, mock.Mock(closed=False, reconcile=reconcile),
                                           folder, hooks=made, start_timeout_s=0, exit_grace_s=HELD_BOUND_S)
            # The settling's clock, for the second case: its moment has run out as soon as it has asked once, so it
            # leaves its rounds by the clock and stands just before the fence. (Waits elsewhere keep the real clock.)
            clock = (lambda asked=asked: 10 * HELD_BOUND_S if asked else 0.0) if when == 'after its moment has run out' else adapter.time.monotonic
            with self.subTest(closing_is_seen=when), mock.patch.object(adapter.time, 'monotonic', clock):
                with self.assertRaises(adapter.StartRefused):
                    starter(dict(DriveOwnerSeamTest.PARAMS))
                close_waits(self, made, store, lambda: (made.begin_close(), made.finish_close(H.ServiceContext())), inside, leave)
                process.fence.assert_not_called()
                process.close.assert_called_once_with()
                self.assertEqual(len(asked), 1, 'it did not go on asking after closing had begun')
            self.assertTrue(until(lambda: not starter.active('run-1')), 'released')

    def test_with_nothing_registered_a_settling_fences_as_it_always_did_while_a_close_runs(self):
        """The other side of the last test. With hooks nobody registered into the gate is off, and a settling is
        what it was before the seams: a close of the composition, which shuts that gate all the same, does not
        keep it from fencing. Only the daemon's own closed flag does, as before."""
        for daemon_closed, fenced in ((False, [mock.call('result_recorded')]), (True, [])):
            bare = hooks_of()
            folder = tempfile.mkdtemp(prefix='rc-seams-settle-')
            self.addCleanup(shutil.rmtree, folder, True)
            reconcile = mock.Mock(return_value=mock.Mock(state='running'))
            starter = adapter.RouteStarter(FakeService(), DriveOwnerSeamTest.ROUTES, mock.Mock(closed=daemon_closed, reconcile=reconcile),
                                           folder, hooks=bare, exit_grace_s=HELD_BOUND_S)
            ctx = launcher.RunContext('run-1')
            ctx.process = mock.Mock()
            bare.begin_close()
            self.assertEqual((bare.gate.enabled, bare.gate.closed), (False, True))
            # The settling's clock: its moment has run out once it has asked about the run, or at once when the
            # daemon has said closed and it never asks.
            clock = lambda: 10 * HELD_BOUND_S if reconcile.call_count else 0.0   # noqa: E731
            with self.subTest(the_daemon_has_said_closed=daemon_closed), mock.patch.object(adapter.time, 'monotonic', clock), \
                    mock.patch.object(adapter.time, 'sleep', lambda seconds: None):
                starter._settle(ctx)
            self.assertEqual((ctx.process.fence.call_args_list, ctx.process.close.call_count), (fenced, 1))

    def test_a_settling_makes_its_fence_with_the_gate_held_open_and_a_close_that_begins_then_waits_for_it(self):
        """The fifth reads, restated for the admission gate. The decision to fence and the start of the fence
        are one step: a shutting that has already set the gate's flag fences nothing, and a fence that has
        started is finished. The unit stop does not hold the gate lock. A shutting sets that flag and then
        waits, without the lock, until the fence returns; a call that arrives once the flag is set is refused
        at once. Nothing is traced."""
        process, inside, leave = mock.Mock(), threading.Event(), threading.Event()
        seen, owned = [], []
        self.addCleanup(leave.set)
        made = hooks_of(launch_check=lambda ctx, executable: None)   # one hook that is asked on a run's thread
        self.assertTrue(made.gate.enabled)

        def fence(reason):
            seen.append((reason, made.gate.closed))
            owned.append(made.gate._lock._is_owned())
            inside.set()
            leave.wait(HELD_BOUND_S)
            seen.append((reason, made.gate.closed))
        process.fence.side_effect = fence
        folder = tempfile.mkdtemp(prefix='rc-seams-settle-')
        self.addCleanup(shutil.rmtree, folder, True)
        starter = adapter.RouteStarter(FakeService(), DriveOwnerSeamTest.ROUTES, mock.Mock(closed=False), folder,
                                       hooks=made, exit_grace_s=0)
        control = adapter.DaemonControl(mock.Mock(call=lambda method, params, principal=None: {'code': 'ok'}),
                                        None, None, starter, folder, hooks=made)
        ctx = launcher.RunContext('run-1')
        ctx.process = process
        settling = threading.Thread(target=starter._settle, args=(ctx,), daemon=True)
        finished = []

        def shutting():
            try:
                made.begin_close()
            finally:
                finished.append(True)
        close = threading.Thread(target=shutting, name='the close', daemon=True)
        settling.start()
        self.assertTrue(inside.wait(HELD_BOUND_S), 'the fence is being made')
        self.assertEqual((seen[0], owned), (('result_recorded', False), [False]),
                         'the fence started with the gate open, and the stop does not hold the gate lock')
        close.start()
        self.assertTrue(until(lambda: made.gate.closed), 'the shut flag is set while the fence is still inside')
        self.assertEqual(finished, [], 'a shutting does not return until the fence returns')
        self.assertFalse(made.gate._lock._is_owned())
        refused = control.call('run.status', {'run_id': 'run-b'})
        self.assertEqual(refused, {'code': 'unavailable', 'detail': 'daemon_closing'})
        self.assertEqual(finished, [], 'the refusal did not wait for the fence')
        leave.set()
        settling.join(HELD_BOUND_S)
        close.join(HELD_BOUND_S)
        self.assertEqual(seen[0], ('result_recorded', False))
        self.assertEqual(seen[1][0], 'result_recorded')
        self.assertEqual((finished, made.gate.closed, made.closing.is_set(), process.close.call_count),
                         ([True], True, True, 1))
        self.assertEqual(made.gate._fences, 0)
        # A settling that comes to its fence once shutting has begun fences nothing, and closes its descriptors.
        late = launcher.RunContext('run-2')
        late.process = mock.Mock()
        starter._settle(late)
        self.assertEqual((late.process.fence.call_count, late.process.close.call_count), (0, 1))

    def test_a_fence_that_raises_drops_its_count_and_frees_the_gate(self):
        """An exception from the fence is raised with the gate lock free and the fence count back at zero, and
        the descriptors are still closed. A shutting after that returns."""
        made = hooks_of(launch_check=lambda ctx, executable: None)
        folder = tempfile.mkdtemp(prefix='rc-seams-fence-exc-')
        self.addCleanup(shutil.rmtree, folder, True)
        starter = adapter.RouteStarter(FakeService(), DriveOwnerSeamTest.ROUTES, mock.Mock(closed=False), folder,
                                       hooks=made, exit_grace_s=0)
        process = mock.Mock()
        process.fence.side_effect = RuntimeError('the unit stop failed')
        ctx = launcher.RunContext('run-1')
        ctx.process = process
        with self.assertRaises(RuntimeError):
            starter._settle(ctx)
        self.assertEqual((made.gate._fences, made.gate._lock._is_owned(), process.close.call_count), (0, False, 1))
        made.begin_close()
        self.assertTrue(made.gate.closed)

    def test_an_interruption_of_a_fence_drops_its_count_and_frees_the_gate(self):
        """An interruption of the fence is raised with the gate lock free and the fence count back at zero.
        The fence was entered, so it is not asked again. The descriptors are still closed, and a shutting
        after that returns."""
        made = hooks_of(launch_check=lambda ctx, executable: None)
        folder = tempfile.mkdtemp(prefix='rc-seams-fence-int-')
        self.addCleanup(shutil.rmtree, folder, True)
        starter = adapter.RouteStarter(FakeService(), DriveOwnerSeamTest.ROUTES, mock.Mock(closed=False), folder,
                                       hooks=made, exit_grace_s=0)
        process, asked = mock.Mock(), []

        def fence(reason):
            asked.append(reason)
            raise KeyboardInterrupt
        process.fence.side_effect = fence
        ctx = launcher.RunContext('run-1')
        ctx.process = process
        with self.assertRaises(KeyboardInterrupt):
            starter._settle(ctx)
        self.assertEqual((asked, made.gate._fences, made.gate._lock._is_owned(), process.close.call_count),
                         (['result_recorded'], 0, False, 1))
        made.begin_close()
        self.assertTrue(made.gate.closed)

    def test_a_settling_fence_keeps_a_second_start_and_stop_of_the_same_run_out(self):
        """While a settling's fence is inside the unit stop, a second start and a second stop of that same run
        do not enter the manager. Two hundred rounds. Removing the fence lets the rivals run during the stop."""
        folder = tempfile.mkdtemp(prefix='rc-seams-same-')
        self.addCleanup(shutil.rmtree, folder, True)
        os.chmod(folder, 0o700)

        class Manager:
            def __init__(self):
                self.inside = threading.Event()
                self.release = threading.Event()
                self.primary = False
                self.overlap = []

            def begin(self):
                self.inside.clear()
                self.release.clear()
                self.primary = True
                self.overlap.clear()

            def stop(self, unit):
                if self.primary:
                    self.primary = False
                    self.inside.set()
                    self.release.wait(HELD_BOUND_S)
                    return None
                if not self.release.is_set():
                    self.overlap.append(('stop', unit))
                return None

            def start(self, unit, argv):
                if not self.release.is_set():
                    self.overlap.append(('start', unit))

            def observe(self, unit):
                return 'running'

            def processes_gone(self, unit):
                return False

            def owned_units(self):
                return set()

        manager = Manager()
        daemon = Daemon(Registry(folder), manager, boot_id=lambda: 'boot-1')
        self.addCleanup(daemon.close)
        self.addCleanup(manager.release.set)
        made = hooks_of(launch_check=lambda ctx, executable: None)
        starter = adapter.RouteStarter(FakeService(), DriveOwnerSeamTest.ROUTES, daemon, folder, hooks=made,
                                       exit_grace_s=0)
        real_lock = daemon.lock
        arrived = []
        both = threading.Event()

        class Watched:
            def acquire(self, blocking=True, timeout=-1):
                if threading.current_thread().name.startswith('rival-'):
                    arrived.append(threading.current_thread().name)
                    if len(arrived) >= 2:
                        both.set()
                return real_lock.acquire(blocking, timeout)

            def release(self):
                real_lock.release()

            def _is_owned(self):
                return real_lock._is_owned()

            def __enter__(self):
                self.acquire()
                return self

            def __exit__(self, *exc):
                self.release()

        daemon.lock = Watched()
        for round_index in range(200):
            run_id = f'run-{round_index}'
            daemon.registry.add(run_id, 'boot-1')
            manager.begin()
            arrived.clear()
            both.clear()
            process = mock.Mock()
            process.fence.side_effect = lambda reason, run_id=run_id: daemon.stop(run_id)
            ctx = launcher.RunContext(run_id)
            ctx.process = process
            done = {'start': None, 'stop': None}

            def rival_start(run_id=run_id):
                try:
                    daemon.start(run_id, ['argv'])
                except BaseException as exc:  # noqa: BLE001 - the run is already recorded; the rival must return
                    done['start'] = (type(exc).__name__, manager.release.is_set())
                else:
                    done['start'] = ('returned', manager.release.is_set())

            def rival_stop(run_id=run_id):
                try:
                    daemon.stop(run_id)
                except BaseException as exc:  # noqa: BLE001 - a rival that fails still has to have waited
                    done['stop'] = (type(exc).__name__, manager.release.is_set())
                else:
                    done['stop'] = ('returned', manager.release.is_set())

            settling = threading.Thread(target=starter._settle, args=(ctx,), daemon=True)
            settling.start()
            self.assertTrue(manager.inside.wait(HELD_BOUND_S), f'round {round_index}: the fence reached the unit stop')
            starter_thread = threading.Thread(target=rival_start, name='rival-start', daemon=True)
            stopper = threading.Thread(target=rival_stop, name='rival-stop', daemon=True)
            starter_thread.start()
            stopper.start()
            self.assertTrue(both.wait(HELD_BOUND_S), f'round {round_index}: both rivals asked for the run lock')
            self.assertEqual((done['start'], done['stop'], manager.overlap), (None, None, []),
                             f'round {round_index}: a second start and stop stay out while the fence is inside')
            manager.release.set()
            settling.join(HELD_BOUND_S)
            starter_thread.join(HELD_BOUND_S)
            stopper.join(HELD_BOUND_S)
            self.assertFalse(settling.is_alive() or starter_thread.is_alive() or stopper.is_alive())
            self.assertEqual(manager.overlap, [])
            self.assertEqual((done['start'][1], done['stop'][1]), (True, True))
            self.assertEqual(process.close.call_count, 1)

    def test_an_unrelated_call_returns_while_a_settling_fence_stops_the_unit(self):
        """R1. With the gate on, a settling's fence stops the unit. Another run's ``run.status``, and
        ``route.list``, return while that stop is still inside, and a call that was already admitted
        leaves then too. The gate's lock is not held across the stop."""
        gate_owned, inside, release, left = [], threading.Event(), threading.Event(), threading.Event()

        class Manager:
            def stop(self, unit):
                gate_owned.append(made.gate._lock._is_owned())
                inside.set()
                release.wait(HELD_BOUND_S)
                left.set()

            def start(self, unit, argv):
                return None

            def observe(self, unit):
                return 'running'

            def processes_gone(self, unit):
                return False

            def owned_units(self):
                return set()

        folder = tempfile.mkdtemp(prefix='rc-seams-fence-')
        self.addCleanup(shutil.rmtree, folder, True)
        os.chmod(folder, 0o700)
        daemon = Daemon(Registry(folder), Manager(), boot_id=lambda: 'boot-1')
        self.addCleanup(daemon.close)
        self.addCleanup(release.set)
        daemon.registry.add('run-1', 'boot-1')
        made = hooks_of(launch_check=lambda ctx, executable: None)
        self.assertTrue(made.gate.enabled)
        starter = adapter.RouteStarter(FakeService(), DriveOwnerSeamTest.ROUTES, daemon, folder, hooks=made,
                                       exit_grace_s=0)
        control = adapter.DaemonControl(mock.Mock(call=lambda method, params, principal=None: {'code': 'ok'}),
                                        None, daemon, starter, folder, hooks=made)
        process = mock.Mock()
        process.fence.side_effect = lambda reason: daemon.stop('run-1')
        ctx = launcher.RunContext('run-1')
        ctx.process = process
        settling = threading.Thread(target=starter._settle, args=(ctx,), daemon=True)
        settling.start()
        self.assertTrue(inside.wait(HELD_BOUND_S), 'the unit stop is inside')
        begun = time.monotonic()
        status, routes = {}, {}

        def read_status():
            status['answer'] = control.call('run.status', {'run_id': 'run-b'})
            status['left'] = left.is_set()
            status['s'] = time.monotonic() - begun

        def read_routes():
            routes['answer'] = control.call('route.list')
            routes['left'] = left.is_set()
            routes['s'] = time.monotonic() - begun

        status_thread = threading.Thread(target=read_status, daemon=True)
        routes_thread = threading.Thread(target=read_routes, daemon=True)
        status_thread.start()
        routes_thread.start()
        status_thread.join(0.4)
        routes_thread.join(0.4)
        self.assertFalse(status_thread.is_alive(), 'run.status returned while the other run was still stopping')
        self.assertFalse(routes_thread.is_alive(), 'route.list returned while the stop was still inside')
        self.assertFalse(status['left'])
        self.assertFalse(routes['left'])
        self.assertLess(status['s'], 0.4)
        self.assertLess(routes['s'], 0.4)
        self.assertEqual(status['answer']['code'], 'ok')
        self.assertEqual(routes['answer']['code'], 'ok')
        self.assertEqual(gate_owned, [False], 'the gate lock is not held across the stop')
        # A call already admitted leaves while the stop is still inside: its authorizer returns, then leave.
        held, authorizer_back = threading.Event(), threading.Event()

        def authorize(principal, method, params):
            if method == 'run.status' and (params or {}).get('run_id') == 'run-held':
                held.set()
                authorizer_back.wait(HELD_BOUND_S)
                admitted['back'] = time.monotonic()
            return None

        made._single['authorize'] = authorize
        admitted = {}

        def held_call():
            admitted['answer'] = control.call('run.status', {'run_id': 'run-held'})
            admitted['after_s'] = time.monotonic() - admitted['back']
            admitted['left'] = left.is_set()

        admitted_thread = threading.Thread(target=held_call, daemon=True)
        admitted_thread.start()
        self.assertTrue(held.wait(HELD_BOUND_S), 'the call was admitted and is in its authorizer')
        authorizer_back.set()
        admitted_thread.join(0.4)
        self.assertFalse(admitted_thread.is_alive(), 'an admitted call left while the stop was still inside')
        self.assertFalse(admitted['left'])
        self.assertLess(admitted['after_s'], 0.4)
        self.assertEqual(admitted['answer']['code'], 'ok')
        release.set()
        settling.join(HELD_BOUND_S)
        self.assertFalse(settling.is_alive())
        self.assertEqual(process.close.call_count, 1)

    @quiet
    def test_an_owner_registered_alone_is_waited_for_and_its_settling_fences_nothing_once_closing_began(self):
        """The fourth reads. A lane can register ``drive_owner`` and nothing else: no service, no handler, no
        store. The gate is on all the same (``Admission`` in ``control/hooks.py``), so what S9 says of closing
        holds for that owner too. It fails before it has made any worker, what it launched is being settled, and
        closing begins in that moment: the close WAITS for the settling, which is inside the hand-over, and the
        settling fences nothing, on its next time round and after its moment has run out. No tracing is used."""
        for when in ('its next time round', 'after its moment has run out'):
            process, ended = mock.Mock(), mock.Mock(closed=False)
            inside, leave, asked = threading.Event(), threading.Event(), []
            self.addCleanup(leave.set)

            class Owner:
                def run(self, ctx, request, service, record_result, fault_run, settle, release):
                    ctx.process = process
                    raise RuntimeError('failed before it made any worker')

            def reconcile(run_id, asked=asked, inside=inside, leave=leave):
                asked.append(run_id)
                inside.set()
                leave.wait(HELD_BOUND_S)
                return mock.Mock(state='running')
            made = hooks_of(drive_owner=lambda ctx, request: Owner())
            self.assertEqual((sorted(made._single), made.gate.enabled), (['drive_owner'], True), 'the owner alone: the gate is on')
            folder = tempfile.mkdtemp(prefix='rc-seams-settle-')
            self.addCleanup(shutil.rmtree, folder, True)
            starter = adapter.RouteStarter(FakeService(), DriveOwnerSeamTest.ROUTES, mock.Mock(closed=False, reconcile=reconcile),
                                           folder, hooks=made, start_timeout_s=0, exit_grace_s=HELD_BOUND_S)
            clock = (lambda asked=asked: 10 * HELD_BOUND_S if asked else 0.0) if when == 'after its moment has run out' else adapter.time.monotonic

            def close(made=made, ended=ended):
                made.begin_close()
                made.finish_close(H.ServiceContext())
                ended.closed = True     # there is no store here: this flag says the close has ended, as a store's does
            with self.subTest(closing_is_seen=when), mock.patch.object(adapter.time, 'monotonic', clock):
                with self.assertRaises(adapter.StartRefused):
                    starter(dict(DriveOwnerSeamTest.PARAMS))
                close_waits(self, made, ended, close, inside, leave)
                process.fence.assert_not_called()
                process.close.assert_called_once_with()
                self.assertEqual(len(asked), 1, 'it did not go on asking after closing had begun')
            self.assertTrue(until(lambda: not starter.active('run-1')), 'released')

    def test_with_an_owner_registered_alone_a_run_is_refused_once_closing_has_begun(self):
        asked = []
        made = hooks_of(drive_owner=lambda ctx, request: asked.append(ctx.run_id))
        starter, service = self.starter(made, HELD_BOUND_S)
        made.begin_close()
        with self.assertRaises(adapter.StartRefused) as caught:
            starter(dict(DriveOwnerSeamTest.PARAMS))
        self.assertEqual((caught.exception.code, caught.exception.detail), (c.LAUNCH_REFUSED, 'daemon_closing'))
        self.assertEqual((asked, service.requests), ([], []), 'the owner was not asked and nothing was driven')
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

    def test_a_close_waits_for_a_launch_that_is_inside_one_of_its_hooks_and_that_launch_then_starts_nothing(self):
        for hook in ('launch_check', 'working_directory'):
            with self.subTest(hook=hook):
                store, folders = FakeStore(), []
                held, entered, leave, seen = held_hook(store)
                self.addCleanup(leave.set)
                hooks = {hook: held}
                if hook == 'launch_check':  # the folder's provider comes after the check: it must not be asked
                    hooks['working_directory'] = lambda ctx: folders.append(ctx.run_id)
                made = self.made(store, **hooks)
                ctx, manager, state, call, outcome = self.launch(made)
                user = threading.Thread(target=call, daemon=True)
                user.start()
                close_waits(self, made, store, lambda: (made.begin_close(), made.finish_close(H.ServiceContext())), entered, leave)
                user.join(HELD_BOUND_S)
                self.assertEqual((seen, outcome), ([False], [(c.LAUNCH_REFUSED, 'daemon_closing')]),
                                 'the store was open while the hook was inside; closing had begun when it returned, so nothing is launched')
                self.assertEqual(folders, [], 'refused as the check returned: no folder is asked for, which a provider may have to make')
                manager.prepare.assert_not_called()

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

    def test_a_launch_hook_registered_alone_is_waited_for_and_its_launch_then_starts_nothing(self):
        """The fifth reads. A lane can register ``launch_check`` or ``working_directory`` and nothing else: no
        service, no handler, no store, no owner. The gate is on all the same (``Admission``), so what S13
        says holds for that hook too: a close waits for a launch that is inside it, and that launch, when the
        hook returns, prepares and starts nothing. (With the gate off the close went past it, the daemon closed
        its registry under the hook, and the launch went on into a closed daemon.)"""
        for hook in ('launch_check', 'working_directory'):
            with self.subTest(hook=hook):
                ended = mock.Mock(closed=False)     # there is no store here: this says the close has ended, as a store's flag does
                held, entered, leave, seen = held_hook(ended)
                self.addCleanup(leave.set)
                made = hooks_of(**{hook: held})
                self.assertEqual((sorted(made._single), made.gate.enabled), ([hook], True), 'the hook alone: the gate is on')
                ctx, manager, state, call, outcome = self.launch(made)
                user = threading.Thread(target=call, daemon=True)
                user.start()

                def close(made=made, ended=ended):
                    made.begin_close()
                    made.finish_close(H.ServiceContext())
                    ended.closed = True
                close_waits(self, made, ended, close, entered, leave)
                user.join(HELD_BOUND_S)
                self.assertEqual((seen, outcome), ([False], [(c.LAUNCH_REFUSED, 'daemon_closing')]),
                                 'the close had not ended while the hook was inside; closing had begun when it returned')
                manager.prepare.assert_not_called()
                self.assertFalse((state / 'runs').exists(), 'nothing of the unit was made')

    def test_with_a_launch_hook_registered_alone_nothing_is_launched_once_closing_has_begun(self):
        for hook in ('launch_check', 'working_directory'):
            with self.subTest(hook=hook):
                asked = []
                made = hooks_of(**{hook: lambda *args: asked.append(hook)})
                ctx, manager, state, call, outcome = self.launch(made)
                made.begin_close()
                call()
                self.assertEqual((outcome, asked), ([(c.LAUNCH_REFUSED, 'daemon_closing')], []), 'refused before the hook is asked')
                manager.prepare.assert_not_called()

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


class ClientSideHookAloneTest(Composed):
    """The sixth reads. S13 for what a lane registers that is asked inside a client's call: the authorizer, the
    result reader and the page's list of methods, registered with nothing else, alone or with one another; and a
    lane's part of a start (S6), which no read had named and which stood the same way. The gate was off for them,
    so a close went past a call that was inside one, and the daemon closed its registry under it; and the page's
    provider was asked again after the close. The gate is on now whenever anything at all is registered
    (``Hooks.anything_registered``; every way of registering, alone and in pairs, is held in the control-side
    file)."""

    def test_a_close_waits_for_a_client_side_hook_that_is_inside_with_nothing_else_registered(self):
        for names in (('authorize',), ('read_result',), ('page_methods',), ('authorize', 'read_result'),
                      ('authorize', 'page_methods'), ('read_result', 'page_methods')):
            with self.subTest(registered=names):
                target = 'page_methods' if 'page_methods' in names else names[0]
                ended = mock.Mock(closed=False)     # no store here: this says the close has ended, as a store's flag does
                held, entered, leave, seen = held_hook(ended, frozenset({'route.list'}) if target == 'page_methods' else None)
                self.addCleanup(leave.set)
                made = hooks_of(**{name: held if name == target else (lambda *args: None) for name in names})
                self.assertEqual((sorted(made._single), made.gate.enabled), (sorted(names), True))
                daemon, composed = self.boot(made)
                control = composed.control
                if target == 'read_result':     # a run with no result of its own, so that its status asks the reader
                    control.registry.put({'run_id': 'run-read', 'tenant': 't1', 'engine': 'claude_code', 'state': 'starting',
                                          'cgroup': None, 'started_at': 1, 'stop': None, 'start_key': None})
                call = {'authorize': lambda: control.call('run.list'),
                        'read_result': lambda: control.call('run.status', {'run_id': 'run-read'}),
                        'page_methods': lambda: self.page(composed, 'POST', '/v1/control', {'method': 'route.list'})}[target]
                answers = []
                caller = threading.Thread(target=lambda: answers.append(call()), daemon=True)
                caller.start()
                self.open.remove((daemon, composed))

                def close(composed=composed, daemon=daemon, ended=ended):
                    composed.close()
                    daemon.close()      # as the daemon's main does, right after the composition's close
                    ended.closed = True
                close_waits(self, made, ended, close, entered, leave)
                caller.join(HELD_BOUND_S)
                self.assertEqual((seen, len(answers)), ([False], 1),
                                 'neither the composition nor the daemon had closed while the hook was inside, and the call was answered')

    def test_a_close_waits_for_a_start_registrant_that_is_inside_with_nothing_else_registered(self):
        ended = mock.Mock(closed=False)     # no store here: this says the close has ended, as a store's flag does
        held, entered, leave, seen = held_hook(ended, H.Refusal('workspace_busy'))   # it refuses, so no run is made
        self.addCleanup(leave.set)
        made = hooks_of(lambda made: made.start_registrant('workspace', (), (), held))
        self.assertEqual((made.start_on, sorted(made._single), made.gate.enabled), (True, [], True))
        daemon, composed = self.boot(made)
        answers = []
        caller = threading.Thread(target=lambda: answers.append(composed.control.call('run.start', self.request())), daemon=True)
        caller.start()
        self.open.remove((daemon, composed))

        def close():
            composed.close()
            daemon.close()      # as the daemon's main does, right after the composition's close
            ended.closed = True
        close_waits(self, made, ended, close, entered, leave)
        caller.join(HELD_BOUND_S)
        self.assertEqual((seen, answers), ([False], [{'code': 'workspace_busy'}]),
                         'neither the composition nor the daemon had closed while the registrant was inside, and its refusal was the answer')

    def test_after_a_close_a_page_provider_registered_alone_is_not_asked_and_a_call_is_refused(self):
        asked = []
        made = hooks_of(page_methods=lambda: asked.append('the provider') or frozenset({'route.list'}))
        daemon, composed = self.boot(made)
        self.assertEqual(self.page(composed, 'POST', '/v1/control', {'method': 'route.list'})[0], 200)
        self.assertEqual(asked, ['the provider'])
        composed.close()
        self.assertEqual(self.page(composed, 'POST', '/v1/control', {'method': 'route.list'}),
                         (200, {'code': 'unavailable', 'detail': 'daemon_closing'}))
        self.assertEqual(composed.control.call('run.list'), {'code': 'unavailable', 'detail': 'daemon_closing'})
        self.assertEqual(asked, ['the provider'], 'after the close no lane code runs for the page')


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
class CompositionCloseRegionTest(RegionWalk, unittest.TestCase):
    """The composition's own close, walked like the four regions of ``control/hooks.py`` (the walk, and what it
    asserts at every point, are in ``control/tests/test_seams.py``). Every traceable point between the entry of
    ``Composition.close`` and its return is interrupted once: in the close and its steps here, and in the hooks'
    whole teardown under them. Each time the interruption reaches the caller, the close's ownership is free, and
    the close is complete or nothing of it was done and it can be asked for again. The same oracles as there:
    the service instances say that a stop of each returned, both references to the store are gone, each of the
    three locks the close can take is free (its own, the lifecycle, the gate's), and a close that did not finish
    left everything it touches as it was. How the interruption is delivered (raised from the trace callback of
    the point it is aimed at, which is stricter than a real signal) is said once, in that file's
    ``OwnershipRegionTest``, with what pure Python cannot protect and the one kind of point 3.14 lets pass."""

    FILES = (*RegionWalk.FILES, adapter.__file__)

    def setUp(self):
        """Asked before every test of this class, and so before each walk: a step of the close that must return
        and keeps failing is taken twice and no more, and the close then ends unfinished
        (``Composition._close``, ``_close_until_done``). Where that is not so a walk goes round without end and
        keeps everything it catches: two changed copies of the close did exactly that, were told apart from a
        sound one only by a tool's limit of time, and took a machine's memory (Nov 15, 2026). So the rule is
        asked here first, with a step that gives in after a few calls and so always ends, and a close that does
        not hold it fails every test of the class at once."""
        super().setUp()
        composed, store, closed = self.composition()
        asked = []

        def begin():
            asked.append(len(asked))
            if len(asked) <= 6:
                raise RuntimeError('a step that cannot return')
        composed.hooks.begin_close = begin       # the first step of the close, which must return
        try:
            with self.assertRaisesRegex(RuntimeError, 'a step that cannot return'):
                composed.close()
            found = (len(asked), composed._closed, composed._close_step, list(closed))
        finally:
            del composed.hooks.begin_close       # the hooks' own again: the close can be asked for again, and ends
            composed.close()
        self.assertEqual(found, (2, False, 0, []), 'a step that must return and keeps failing is taken twice and no '
                         'more; the close is then unfinished and has touched nothing after that step')
        self.assertEqual((composed._closed, store.closed), (True, True), 'and asked for again, it ends')

    def at_the_marker(self) -> bool:
        """Here a region is under a region: the lifecycle's close under the composition's. The marker of that
        INNER region's own function is the same point one level down (3.14 only, and only a trace callback raises
        there). What it leaves the outer close cannot mend: the lifecycle stays owned by this thread, the close
        asks for the teardown again and is refused as a call from inside a lifecycle method, and it ends
        unfinished. It is let pass by the same name, in that one function and no other."""
        code, offset, outermost, event = self.raised_at
        self.raised_at = (code, offset, outermost or code is H.Hooks.finish_close.__code__, event)
        return super().at_the_marker()

    def composition(self):
        """A composition over stand-ins, around the hooks the control-side walk makes: a store open, two services
        started (two, so that every loop of the teardown under the close goes round), two waiters on the closing
        signal, and service instances that count the stops of them that returned. Cheap enough to make a few
        hundred times. ``(the composition, its store, what its close closed)``."""
        made, store = control_seams.OwnershipRegionTest('test_the_gates_close_at_every_event').lifecycle()
        closed: list = []

        def part(name):
            return mock.Mock(close=lambda: closed.append(name))
        composed = adapter.Composition(mock.Mock(), part('server'), part('door'), plugins=part('plugins'), breaker=part('breaker'),
                                       hooks=made, context=H.ServiceContext(registry=mock.Mock(mark_changed=None)))
        return composed, store, closed

    REGIONS = {
        'Composition.close': (lambda self: self.composition(), lambda trio: trio[0].close(), lambda trio: trio[0]._closing,
                              lambda trio: trio[0]._closed and control_seams._lifecycle_closed((trio[0].hooks, trio[1]))
                              and {'server', 'door', 'plugins', 'breaker'} <= set(trio[2])),
    }
    LOCKS = {       # the close's own ownership, and the two under it
        'Composition.close': lambda trio: (trio[0]._closing, trio[0].hooks._lifecycle, trio[0].hooks.gate._lock),
    }
    STATE = {       # everything the close touches: what the teardown touches, how far the close has got, what it closed
        'Composition.close': lambda trio: (control_seams._teardown_touches((trio[0].hooks, trio[1])), trio[0]._closed,
                                           trio[0]._close_step, tuple(trio[2])),
    }

    def test_the_compositions_close_has_no_loop_and_no_try_written_in_it(self):
        self.straight(adapter, 'Composition', 'close')

    def test_a_close_called_from_inside_the_close_is_refused_and_takes_nothing_from_the_one_under_way(self):
        """The refusal is before the region, where the region's own "this thread owns it already" could not tell a
        close gone round again from a close called from inside the close: the refused call closes nothing and
        does not release the ownership of the close that is under way."""
        composed, store, closed = self.composition()
        composed._closing.acquire()         # this thread is inside the close
        try:
            with self.assertRaises(H.HookError):
                composed.close()
            self.assertTrue(composed._closing._is_owned(), 'the close under way still owns the close')
            self.assertEqual((composed._closed, store.closed, closed), (False, False, []), 'and nothing was closed')
        finally:
            composed._closing.release()
        composed.close()
        self.assertEqual((composed._closed, store.closed, composed._closing._is_owned()), (True, True, False))

    @quiet
    def test_the_compositions_close_at_every_event(self):
        self.walk('Composition.close', self.EVENTS)

    @quiet
    def test_the_compositions_close_at_every_instruction(self):
        self.by_instruction('Composition.close')


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

    def test_an_interruption_at_the_first_step_of_the_close_does_not_end_it(self):
        """The second read's B2. A call is inside. The close is interrupted at its very first step, as it takes
        the admission condition: before the condition is taken, and after. It used to raise there, with the
        composition marked closed, the gate open and the call still admitted, and a later close did nothing. Now
        the same owner goes on: the gate is shut, the close waits for the call with the store open, everything
        is closed once, and only then is the interruption raised. The condition is left free."""
        for when in ('before', 'after'):
            log, entered, leave = [], threading.Event(), threading.Event()
            self.addCleanup(leave.set)
            made, store, events = self.registered(log, entered, leave)
            daemon, composed = self.boot(made)
            self.open.remove((daemon, composed))
            self.addCleanup(daemon.close)
            caller = threading.Thread(target=lambda: composed.control.call('events.follow', {'session_id': 'ses-1'}), daemon=True)
            caller.start()
            self.assertTrue(entered.wait(HELD_BOUND_S), 'the call was admitted and is inside')
            counts, close_plugins, outcome = [], composed.plugins.close, []
            composed.plugins.close = lambda: (counts.append('plugins'), close_plugins())[1]
            lock = watch(made.gate)
            lock.interrupt_next = when                  # the next taking of the admission condition is the close's first step

            def close():
                try:
                    composed.close()
                    outcome.append(('returned', store.closed, list(counts)))
                except KeyboardInterrupt:
                    outcome.append(('interrupted', store.closed, made.gate.admitted, list(counts)))
            with self.subTest(interrupted=when):
                close_waits(self, made, store, close, entered, leave)
                caller.join(HELD_BOUND_S)
                self.assertEqual(outcome, [('interrupted', True, 0, ['plugins'])],
                                 'raised only once the whole close was over: the store closed, nobody inside, the plugin host closed')
                self.assertTrue(made.gate.closed and made.closing.is_set())
                composed.close()                        # it is closed: a later close does nothing
                self.assertEqual(counts, ['plugins'])
                self.assertEqual((lock._is_owned(), answered_from_another_thread(self, made.gate)), (False, [False]))

    def about_to_close(self):
        """A composition with a service and a store, about to be closed by the test itself, and what its close
        closed of the things that come after the drain. ``(made, store, composed, counts)``."""
        made, store, events = self.registered([], threading.Event(), threading.Event())
        daemon, composed = self.boot(made)
        self.open.remove((daemon, composed))
        self.addCleanup(daemon.close)
        self.addCleanup(composed.close)  # nothing to do once the test has closed it; a test that fails leaves nothing open
        counts, close_plugins = [], composed.plugins.close
        composed.plugins.close = lambda: (counts.append('plugins'), close_plugins())[1]
        return made, store, composed, counts

    def test_a_step_of_the_close_that_is_interrupted_before_it_did_anything_is_taken_again(self):
        """An interruption can land in a step before the step has done anything. Closing the admission, the wait
        for the admitted calls and the transport's stop are each interrupted so, once. The step is taken again:
        the gate is shut when the transport is asked to stop, everything is closed once, and the interruption is
        raised only then. (The wait for the admitted calls closes the admission again itself, hence its count.)"""
        for step, taken in (('begin_close', 3), ('finish_close', 2), ('transport', 2)):
            made, store, composed, counts = self.about_to_close()
            target, name = (composed.server, 'close') if step == 'transport' else (made, step)
            calls, shut, real = [], [], getattr(target, name)

            def once_interrupted(*args, calls=calls, real=real):
                calls.append(args)
                if len(calls) == 1:
                    raise KeyboardInterrupt
                return real(*args)
            setattr(target, name, once_interrupted)
            stop_transport = composed.server.close

            def stopping(shut=shut, made=made, stop_transport=stop_transport):
                shut.append(made.gate.closed)
                return stop_transport()
            composed.server.close = stopping
            with self.subTest(step=step):
                with self.assertRaises(KeyboardInterrupt):
                    composed.close()
                self.assertEqual((len(calls), set(shut), store.closed, counts), (taken, {True}, True, ['plugins']))
                composed.close()                        # it is closed: a later close does nothing, and raises nothing
                self.assertEqual((len(calls), counts), (taken, ['plugins']))

    def test_an_interruption_on_the_way_back_from_taking_the_close_is_inside_the_protected_region(self):
        """The third reads. The composition's close is interrupted as the helper that takes its ownership is
        entered (nothing is taken yet), at its return (it is taken), and at the first line after it. Each time
        the whole close is done before the interruption is raised, and the ownership is free: a later close
        returns, on this thread and on another. Before, the interruption escaped with the ownership held and the
        gate open, and the daemon went on to close its registry under whatever was still inside."""
        import inspect
        import sys
        method = adapter.Composition.close
        lines, start = inspect.getsourcelines(method)
        target = start + next(index for index, line in enumerate(lines) if line.strip() == 'self._close_until_done(kept)')
        for where in ('the call of the taking', 'the return of the taking', 'the first line after it'):
            made, store, composed, counts = self.about_to_close()
            fired = []

            def trace(frame, event, arg, where=where, fired=fired):
                taking = frame.f_code is H.own_teardown.__code__ and frame.f_back.f_code is method.__code__
                if not fired and ((where == 'the call of the taking' and event == 'call' and taking)
                                  or (where == 'the return of the taking' and event == 'return' and taking)
                                  or (where == 'the first line after it' and event == 'line'
                                      and frame.f_code is method.__code__ and frame.f_lineno == target)):
                    fired.append(where)
                    raise KeyboardInterrupt(where)
                return trace
            with self.subTest(interrupted_at=where):
                before = sys.gettrace()
                sys.settrace(trace)
                try:
                    with self.assertRaises(KeyboardInterrupt):
                        composed.close()
                finally:
                    sys.settrace(before)
                self.assertEqual((fired, composed._closing._is_owned(), made.gate.closed, store.closed, counts),
                                 ([where], False, True, True, ['plugins']))
                composed.close()
                later = []
                other = threading.Thread(target=lambda: later.append(composed.close()), daemon=True)
                other.start()
                other.join(HELD_BOUND_S)
                self.assertEqual((later, counts), ([None], ['plugins']), 'a later close returns, and closes nothing again')

    def test_an_interruption_between_two_steps_of_the_close_does_not_end_it(self):
        """Not every interruption lands inside a step. One that lands between two of them is kept too, and the
        same owner goes on: the close ends only when its last step has been taken."""
        made, store, composed, counts = self.about_to_close()

        class Transport:
            """Stands where the server stood. The first READING of its ``close`` is interrupted: that is outside
            any step of the close."""
            read = 0

            def __init__(inner, server):
                inner.server = server

            @property
            def close(inner):
                inner.read += 1
                if inner.read == 1:
                    raise KeyboardInterrupt
                return inner.server.close
        composed.server = Transport(composed.server)
        with self.assertRaises(KeyboardInterrupt):
            composed.close()
        self.assertEqual((store.closed, counts, made.gate.closed), (True, ['plugins'], True))
        composed.close()
        self.assertEqual(counts, ['plugins'])

    def test_a_step_that_must_return_is_taken_once_more_after_a_failure_and_never_without_end(self):
        """The wait for the admitted calls must return before anything after it is touched. After an ordinary
        failure it is taken once more. If it fails a second time running it cannot return on this thread (a
        close entered from inside a lifecycle method is refused each time it asks): the close stops there, raises,
        and has closed nothing that comes after. It is not taken again without end. A later close goes on from
        that step."""
        for failures, outcome in ((1, (RuntimeError, 2, True, ['plugins'])), (2, (H.HookError, 2, False, []))):
            made, store, composed, counts = self.about_to_close()
            calls, real = [], made.finish_close

            def failing(context, calls=calls, real=real, failures=failures):
                calls.append(context)
                if len(calls) <= failures:
                    raise (RuntimeError('lost') if failures == 1 else H.HookError('a lifecycle method was called from inside another one'))
                return real(context)
            made.finish_close = failing
            with self.subTest(failures=failures):
                raised, taken, closed, after = outcome
                with self.assertRaises(raised):
                    composed.close()
                self.assertEqual((len(calls), store.closed, counts), (taken, closed, after))
                composed.close()                        # goes on from that step, or finds it all closed
                self.assertEqual((len(calls), store.closed, counts), (taken + (0 if closed else 1), True, ['plugins']))

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
