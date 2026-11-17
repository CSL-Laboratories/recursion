"""The control socket answers a plugin effect, and a plugin launch is bound to that plugin.

On the tip this branch was cut from, ``DaemonControl.handle`` never calls
``PluginHost.answer_plugin_effect``, so a plugin effect cannot be answered over the
socket. ``run.start`` still binds every launch to ``host``, including one made for a
minted plugin principal. After the wiring, the owner's peer and the device token
answer one effect over the socket, a stranger peer is ``scope_refused``, a call with
no device token is refused, a launch for a minted principal is bound to that plugin,
and a plugin denied ``order`` cannot reach an order through the socket.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import tempfile
import threading
import time
import unittest
from unittest import mock

from recursion_core.control.api import ControlAPI
from recursion_core.control.client import SocketAPI
from recursion_core.control.registry import InMemoryRunRegistry
from recursion_core.control_transport import UnixControlServer, with_ping_health
from recursion_core.daemon import control_adapter as adapter
from recursion_core.daemon import breaker as breaker_module
from recursion_core.daemon import launcher
from recursion_core.daemon import plugins
from recursion_core.daemon.tests import plugin_fixture as fx
from recursion_core.plugin_security import Refused
from recursion_core.plugin_security.tests.support import FakeGilProbe
from recursion_core.routes import ConnectionBreaker

try:
    from recursion_core.approvals import store as approval_rows
except ImportError:  # pragma: no cover - the queue is on this branch
    approval_rows = None

LINUX = os.name == 'posix' and os.uname().sysname == 'Linux'
ORDER_PLUGIN = 'fixture.operator'
OTHER_PLUGIN = 'fixture.other'
READER_PLUGIN = 'fixture.reader'
SOURCE = b'VALUE = 1\n'
CONNECTION = 'conn-a'
MODEL = 'fake-model-1'
HOST_RUN = 'run-host'
OWN_RUN = 'run-own'
OTHER_RUN = 'run-other'
READER_RUN = 'run-reader'


def _effect(plugin_id, run_id, payload=None):
    return {
        'schema': 'csl-effect/vNext', 'kind': 'order.submit', 'plugin_id': plugin_id, 'run_id': run_id,
        'connection': CONNECTION,
        'scope': {'account': 'paper-acct', 'venue': 'paper', 'contract_family': 'fake-futures',
                  'adapter': 'fake-adapter', 'environment': 'paper'},
        'currency': 'USD', 'amount_minor': 1000,
        'payload': {'note': 'one'} if payload is None else payload,
    }


class _Registry:
    """The launch never reaches a unit row. Cleanup then has nothing to fence."""

    def get(self, run_id):
        raise KeyError(run_id)


class _Daemon:
    """A daemon stand-in. The launch binds before any unit is started."""

    closed = False
    registry = _Registry()

    def start(self, run_id, argv):
        return None

    def stop(self, run_id):
        return None


class _Service:
    """Admits on the durable breaker, then calls the real launcher. The unit is not started."""

    def __init__(self, breaker, launch):
        self.breaker, self.launch = breaker, launch
        self.errors = []

    def handle(self, request):
        admitted = self.breaker.admit(CONNECTION, MODEL, request.get('tenant') or 't1')
        if not admitted.get('admitted'):
            self.errors.append(admitted)
            return {'code': 'breaker_open', 'detail': str(admitted.get('code'))}
        try:
            self.launch(['/bin/true'], env={'PATH': '/usr/bin:/bin'})
        except Exception as exc:  # noqa: BLE001 - the binding is what this door is proving
            self.errors.append('%s: %s' % (type(exc).__name__, exc))
            return {'code': 'engine_error', 'detail': type(exc).__name__}
        return {'code': 'ok', 'detail': ''}


@unittest.skipUnless(LINUX, 'the owner-only socket and the plugin host check uid on Linux')
class PluginEffectSocketTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='rc-effect-socket-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        os.chmod(self.tmp, 0o700)
        self.config = self.tmp / 'config' / 'recursion'
        self.state = self.tmp / 'state'
        self.runtime = self.tmp / 'rt'
        self.runtime.mkdir(mode=0o700)
        self.token_path = self.tmp / 'device-token'
        self.token = 'device-token-for-this-daemon'
        self.token_path.write_bytes(self.token.encode() + b'\n')
        os.chmod(self.token_path, 0o600)
        self.host = self._host()
        self.durable = breaker_module.DurableRouteBreaker(ConnectionBreaker(time.monotonic), self.stored)
        self.service = _Service(self.durable, launcher.ProductionLauncher(
            'claude_code', _Daemon(), mock.Mock(), self.state))
        routes = {'claude_code': {
            'route': {'engine': 'claude_code', 'model': MODEL,
                      'connection': {'kind': 'subscription', 'id': CONNECTION}},
            'deadline_s': 30.0, 'binary': None}}
        self.starter = adapter.RouteStarter(self.service, routes, _Daemon(), self.state, breaker=self.durable,
                                            start_timeout_s=8.0)
        registry = InMemoryRunRegistry()
        api = ControlAPI(registry, mock.Mock(), self.starter, time.time)
        self.control = adapter.DaemonControl(api, registry, _Daemon(), self.starter, self.state, routes=routes,
                                             plugins=self.host, breaker=self.durable)
        server = UnixControlServer(str(self.runtime), with_ping_health(self.control.handle), admit=adapter.admit_peer)
        server.start()
        self.addCleanup(server.close)
        self.api = SocketAPI(server.path, timeout_s=15)

    def _host(self, *, owner_uid=None):
        grants = [
            fx.grant_for(SOURCE, plugin_id=ORDER_PLUGIN, permissions=('order',), unsigned_order_acknowledged=True),
            fx.grant_for(SOURCE, plugin_id=OTHER_PLUGIN, permissions=('order',), unsigned_order_acknowledged=True),
            fx.grant_for(SOURCE, plugin_id=READER_PLUGIN, permissions=('read',)),
        ]
        fx.write_install(self.config, grants=grants)
        fx.write_plugin(self.config / 'plugins' / 'order', SOURCE, plugin_id=ORDER_PLUGIN, permissions=('order',))
        fx.write_plugin(self.config / 'plugins' / 'other', SOURCE, plugin_id=OTHER_PLUGIN, permissions=('order',))
        fx.write_plugin(self.config / 'plugins' / 'reader', SOURCE, plugin_id=READER_PLUGIN, permissions=('read',))
        stored, _created = breaker_module.open_breaker_store(self.state, clock=lambda: 0.0)
        self.addCleanup(stored.close)
        self.stored = stored
        options = {'gil_probe': FakeGilProbe(), 'verification_available': (lambda: True),
                   'clock': (lambda: 1_000_000), 'device_token_path': self.token_path}
        if owner_uid is not None:
            options['owner_uid'] = owner_uid
        host = plugins.PluginHost(self.config, self.state, stored, **options)
        self.addCleanup(host.close)
        return host

    def record(self, approval_id):
        with self.host._approval_queue.store.transaction() as tx:  # noqa: SLF001 - the composed queue's own row
            return approval_rows.load(tx, approval_id)

    def owner_of(self, run_id):
        # A run the store has never seen is run_unknown. That is unbound, not an owner.
        absent = ('run_not_owned', 'run_unknown')
        try:
            self.stored.check(run_id, CONNECTION, 'host')
            return 'host'
        except Refused as refused:
            if refused.code not in absent:
                return refused.code
        for plugin_id in (ORDER_PLUGIN, OTHER_PLUGIN, READER_PLUGIN):
            try:
                self.stored.check(run_id, CONNECTION, plugin_id)
                return plugin_id
            except Refused as refused:
                if refused.code not in absent:
                    return refused.code
        return 'unbound'

    def start(self, **extra):
        params = {'tenant': 't1', 'engine': 'claude_code', 'prompt': 'one'}
        params.update(extra)
        return self.api.call('run.start', params)

    def test_a_plugin_effect_cannot_be_answered_over_the_socket_until_the_peer_and_token_are_sent(self):
        """The gap: the socket does not answer a plugin effect. After: the owner and the token do."""
        effect = _effect(ORDER_PLUGIN, OWN_RUN)
        approval_id = self.host.request_plugin_effect(effect)
        answered = self.api.call('plugin.effect.answer', {
            'approval_id': approval_id, 'effect': effect, 'device_token': self.token, 'answer': 'deny'})
        if answered.get('code') != 'ok':
            self.fail('a plugin effect cannot be answered over the socket: %s' % answered)
        self.assertEqual(answered['approval']['state'], 'denied')
        self.assertEqual(answered['approval']['answered_by'], {'kind': 'owner', 'id': 'local'})
        self.assertEqual(self.record(approval_id)['state'], 'denied')
        again = self.api.call('plugin.effect.answer', {
            'approval_id': approval_id, 'effect': effect, 'device_token': self.token, 'answer': 'deny'})
        self.assertNotEqual(again.get('code'), 'ok', again)
        self.assertEqual(self.record(approval_id)['state'], 'denied')

    def test_a_stranger_peer_is_scope_refused_and_no_device_token_is_refused(self):
        effect = _effect(ORDER_PLUGIN, OWN_RUN, {'note': 'stranger'})
        approval_id = self.host.request_plugin_effect(effect)
        self.host._owner_uid = os.geteuid() + 1  # noqa: SLF001 - the socket peer is this uid; the owner is not
        stranger = self.api.call('plugin.effect.answer', {
            'approval_id': approval_id, 'effect': effect, 'device_token': self.token, 'answer': 'deny'})
        self.host._owner_uid = os.geteuid()
        self.assertEqual(stranger.get('code'), 'scope_refused', stranger)
        self.assertEqual(self.record(approval_id)['state'], 'pending')

        missing = self.api.call('plugin.effect.answer', {
            'approval_id': approval_id, 'effect': effect, 'answer': 'deny'})
        self.assertNotEqual(missing.get('code'), 'ok', missing)
        self.assertIn(missing.get('code'), ('request_invalid', 'scope_refused'), missing)
        self.assertEqual(self.record(approval_id)['state'], 'pending')

        wrong = self.api.call('plugin.effect.answer', {
            'approval_id': approval_id, 'effect': effect, 'device_token': 'not-the-token', 'answer': 'deny'})
        self.assertEqual(wrong.get('code'), 'scope_refused', wrong)
        self.assertEqual(self.record(approval_id)['state'], 'pending')

    def test_an_interruption_during_the_socket_answer_is_not_swallowed(self):
        effect = _effect(ORDER_PLUGIN, OWN_RUN, {'note': 'cut'})
        approval_id = self.host.request_plugin_effect(effect)

        def cut(*_args, **_kwargs):
            raise KeyboardInterrupt('synthetic interruption')

        self.host.answer_plugin_effect = cut
        peer = mock.Mock()
        peer.uid, peer.pid = os.geteuid(), os.getpid()
        conn = mock.Mock()
        conn.peer = peer
        with self.assertRaises(KeyboardInterrupt):
            self.control.handle({'op': 'control', 'method': 'plugin.effect.answer', 'params': {
                'approval_id': approval_id, 'effect': effect, 'device_token': self.token, 'answer': 'deny'}}, conn)
        self.assertEqual(self.record(approval_id)['state'], 'pending')

    def test_two_socket_callers_answer_one_effect(self):
        effect = _effect(ORDER_PLUGIN, OWN_RUN, {'note': 'race'})
        approval_id = self.host.request_plugin_effect(effect)
        outcomes = []

        def once():
            try:
                outcomes.append(self.api.call('plugin.effect.answer', {
                    'approval_id': approval_id, 'effect': effect, 'device_token': self.token, 'answer': 'deny'}))
            except BaseException as exc:  # noqa: BLE001 - the second caller is recorded, not lost
                outcomes.append(exc)

        first, second = threading.Thread(target=once), threading.Thread(target=once)
        first.start()
        second.start()
        first.join(10)
        second.join(10)
        self.assertFalse(first.is_alive() or second.is_alive())
        self.assertEqual(len(outcomes), 2)
        oks = [item for item in outcomes if isinstance(item, dict) and item.get('code') == 'ok']
        self.assertEqual(len(oks), 1, outcomes)
        self.assertEqual(self.record(approval_id)['state'], 'denied')

    def test_a_plugin_principal_launch_is_bound_to_that_plugin_and_an_operator_launch_stays_host(self):
        """The gap: a plugin principal's launch is bound to host. After: it is bound to the plugin."""
        operator = self.start()
        operator_id = operator.get('run_id') or (operator.get('run') or {}).get('run_id')
        self.assertEqual(self.owner_of(operator_id), 'host', (operator, self.service.errors))

        plugin = self.start(plugin_id=ORDER_PLUGIN)
        plugin_id = plugin.get('run_id') or (plugin.get('run') or {}).get('run_id')
        owner = self.owner_of(plugin_id)
        if owner == 'host':
            self.fail('a plugin principal launch is bound to host: %s errors=%s' % (plugin, self.service.errors))
        self.assertEqual(owner, ORDER_PLUGIN, (plugin, self.service.errors))

        unknown = self.start(plugin_id='no.such.plugin')
        unknown_id = unknown.get('run_id') or (unknown.get('run') or {}).get('run_id')
        self.assertEqual(self.owner_of(unknown_id), 'unbound', unknown)
        self.assertEqual((unknown.get('start_error') or {}).get('code'), 'scope_refused')

    def test_a_plugin_denied_order_cannot_reach_it_over_the_socket(self):
        """G2, through the socket: a plugin without the order permission cannot reach an order."""
        reader = self.host.principal(READER_PLUGIN)
        order = self.host.principal(ORDER_PLUGIN)
        other = self.host.principal(OTHER_PLUGIN)
        self.host.start_plugin_run(reader, READER_RUN, CONNECTION, 'fake-provider', MODEL)
        self.host.start_plugin_run(order, OWN_RUN, CONNECTION, 'fake-provider', MODEL)
        self.host.start_plugin_run(other, OTHER_RUN, CONNECTION, 'fake-provider', MODEL)
        self.durable.bind_run(HOST_RUN, CONNECTION, 'fake-provider', MODEL)
        calls = []
        real = self.host._approver.request  # noqa: SLF001 - the order recorder the cite must pass through

        def spy(effect):
            calls.append(effect['run_id'])
            return real(effect)

        self.host._approver.request = spy
        denied = self.api.call('plugin.order', {'plugin_id': READER_PLUGIN, 'effect': _effect(READER_PLUGIN, READER_RUN)})
        self.assertEqual((denied.get('code'), denied.get('detail')), ('permission_required', 'order'), denied)
        also = self.api.call('plugin.order', {'plugin_id': READER_PLUGIN, 'effect': _effect(READER_PLUGIN, HOST_RUN)})
        self.assertEqual((also.get('code'), also.get('detail')), ('permission_required', 'order'), also)
        self.assertEqual(calls, [])

        self.host._owner_uid = os.geteuid() + 1  # noqa: SLF001 - a stranger peer must not cite
        stranger = self.api.call('plugin.order', {'plugin_id': ORDER_PLUGIN, 'effect': _effect(ORDER_PLUGIN, OWN_RUN)})
        self.host._owner_uid = os.geteuid()
        self.assertEqual(stranger.get('code'), 'scope_refused', stranger)
        self.assertEqual(calls, [])

    def test_a_plugin_cannot_cite_another_plugins_run_or_the_hosts_over_the_socket(self):
        order = self.host.principal(ORDER_PLUGIN)
        other = self.host.principal(OTHER_PLUGIN)
        self.host.start_plugin_run(order, OWN_RUN, CONNECTION, 'fake-provider', MODEL)
        self.host.start_plugin_run(other, OTHER_RUN, CONNECTION, 'fake-provider', MODEL)
        self.durable.bind_run(HOST_RUN, CONNECTION, 'fake-provider', MODEL)
        calls = []
        real = self.host._approver.request  # noqa: SLF001 - the order recorder the cite must pass through

        def spy(effect):
            calls.append(effect['run_id'])
            return real(effect)

        self.host._approver.request = spy
        host_run = self.api.call('plugin.order', {'plugin_id': ORDER_PLUGIN, 'effect': _effect(ORDER_PLUGIN, HOST_RUN)})
        self.assertEqual((host_run.get('code'), host_run.get('detail')), ('scope_refused', 'run'), host_run)
        other_run = self.api.call('plugin.order', {'plugin_id': ORDER_PLUGIN, 'effect': _effect(ORDER_PLUGIN, OTHER_RUN)})
        self.assertEqual((other_run.get('code'), other_run.get('detail')), ('scope_refused', 'run'), other_run)
        forged = self.api.call('plugin.order', {'plugin_id': ORDER_PLUGIN, 'effect': _effect(OTHER_PLUGIN, OTHER_RUN)})
        self.assertEqual((forged.get('code'), forged.get('detail')), ('scope_refused', 'run'), forged)
        self.assertEqual(calls, [])

        cited = self.api.call('plugin.order', {'plugin_id': ORDER_PLUGIN, 'effect': _effect(ORDER_PLUGIN, OWN_RUN)})
        self.assertEqual(cited.get('code'), 'ok', cited)
        self.assertEqual(calls, [OWN_RUN])
        self.assertEqual(self.record(cited['approval_id'])['state'], 'pending')
        self.assertEqual(self.record(cited['approval_id'])['run_id'], OWN_RUN)


if __name__ == '__main__':
    unittest.main()
