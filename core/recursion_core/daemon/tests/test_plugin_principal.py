"""A plugin principal the daemon mints, and a run that principal owns.

On the tip this branch was cut from, every run the daemon binds is owned by ``host``.
There is no plugin principal, so a plugin's order cannot cite a run it started. After
the change, the daemon mints one principal per loaded plugin. A run that principal
starts is bound to the plugin. A plugin without the order permission never reaches an
order. A plugin with it is ``scope_refused`` for another plugin's run or the host's,
and the minted principal still cannot approve its own effect.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import tempfile
import threading
import unittest

from recursion_core.daemon import breaker as breaker_module
from recursion_core.daemon import plugins
from recursion_core.daemon.tests import plugin_fixture as fx
from recursion_core.plugin_security import Refused
from recursion_core.plugin_security.tests.support import FakeGilProbe

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
PROVIDER = 'fake-provider'
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


@unittest.skipUnless(LINUX, 'the plugin host checks file ownership by uid and keeps its stores under the state directory')
class PluginPrincipalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='rc-principal-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        os.chmod(self.tmp, 0o700)
        self.config = self.tmp / 'config' / 'recursion'
        self.state = self.tmp / 'state'
        self.plugins_dir = self.config / 'plugins'

    def opened(self):
        grants = [
            fx.grant_for(SOURCE, plugin_id=ORDER_PLUGIN, permissions=('order',), unsigned_order_acknowledged=True),
            fx.grant_for(SOURCE, plugin_id=OTHER_PLUGIN, permissions=('order',), unsigned_order_acknowledged=True),
            fx.grant_for(SOURCE, plugin_id=READER_PLUGIN, permissions=('read',)),
        ]
        fx.write_install(self.config, grants=grants)
        fx.write_plugin(self.plugins_dir / 'order', SOURCE, plugin_id=ORDER_PLUGIN, permissions=('order',))
        fx.write_plugin(self.plugins_dir / 'other', SOURCE, plugin_id=OTHER_PLUGIN, permissions=('order',))
        fx.write_plugin(self.plugins_dir / 'reader', SOURCE, plugin_id=READER_PLUGIN, permissions=('read',))
        stored, _created = breaker_module.open_breaker_store(self.state, clock=lambda: 0.0)
        self.addCleanup(stored.close)
        host = plugins.PluginHost(self.config, self.state, stored, gil_probe=FakeGilProbe(),
                                  verification_available=lambda: True, clock=lambda: 1_000_000)
        self.addCleanup(host.close)
        self.breaker = stored
        self.host = host
        return host

    def minted(self, host, plugin_id):
        """The principal the daemon minted, or the gap: a plugin cannot own a run."""
        mint = getattr(host, 'principal', None)
        principal = mint(plugin_id) if callable(mint) else None
        kind = getattr(plugins, 'PluginPrincipal', None)
        if kind is None or type(principal) is not kind or principal.id != plugin_id or principal.kind != 'plugin':
            self.fail('a plugin cannot own a run: the daemon minted no principal for %s' % plugin_id)
        return principal

    def record(self, host, approval_id):
        with host._approval_queue.store.transaction() as tx:  # noqa: SLF001 - the composed queue's own row
            return approval_rows.load(tx, approval_id)

    def test_a_plugin_cannot_own_a_run_until_the_daemon_mints_its_principal(self):
        """The gap on this tip: a plugin cannot own a run. The operator run stays with host."""
        host = self.opened()
        by_id = {record['plugin_id']: record for record in host.records}
        self.assertEqual(by_id[ORDER_PLUGIN]['state'], 'loaded')
        self.assertIn('order', by_id[ORDER_PLUGIN]['permissions'])
        self.assertNotIn('order', by_id[READER_PLUGIN]['permissions'])

        durable = breaker_module.DurableRouteBreaker(object(), self.breaker)
        self.assertIsNone(durable.bind_run(HOST_RUN, CONNECTION, PROVIDER, MODEL))
        with self.assertRaises(Refused) as refused:
            self.breaker.check(HOST_RUN, CONNECTION, ORDER_PLUGIN)
        self.assertEqual(refused.exception.code, 'run_not_owned')
        self.assertEqual(self.breaker.check(HOST_RUN, CONNECTION, 'host')['provider'], PROVIDER)

        principal = self.minted(host, ORDER_PLUGIN)
        self.assertIs(host.principal(ORDER_PLUGIN), principal)
        self.assertIsNone(host.principal('no.such.plugin'))
        for forged in ({'kind': 'plugin', 'id': ORDER_PLUGIN}, ORDER_PLUGIN, plugins.PluginPrincipal(ORDER_PLUGIN)):
            with self.assertRaises(plugins.PluginRefused) as refused:
                host.start_plugin_run(forged, 'run-forged', CONNECTION, PROVIDER, MODEL)
            self.assertEqual((refused.exception.code, refused.exception.detail), ('scope_refused', 'plugin'))
        with self.assertRaises(Refused) as refused:
            self.breaker.check('run-forged', CONNECTION, ORDER_PLUGIN)
        self.assertEqual(refused.exception.code, 'run_unknown')

        started = host.start_plugin_run(principal, OWN_RUN, CONNECTION, PROVIDER, MODEL)
        self.assertEqual(started, {'run_id': OWN_RUN, 'owner': ORDER_PLUGIN, 'connection': CONNECTION,
                                   'provider': PROVIDER, 'model_version': MODEL})
        self.assertEqual(self.breaker.check(OWN_RUN, CONNECTION, ORDER_PLUGIN)['model_version'], MODEL)
        with self.assertRaises(Refused) as refused:
            self.breaker.check(OWN_RUN, CONNECTION, 'host')
        self.assertEqual(refused.exception.code, 'run_not_owned')
        # The same binding again is the same row. A different owner cannot take it.
        self.assertEqual(host.start_plugin_run(principal, OWN_RUN, CONNECTION, PROVIDER, MODEL)['owner'], ORDER_PLUGIN)
        with self.assertRaises(Refused) as refused:
            self.breaker.bind_run(OWN_RUN, CONNECTION, 'host', PROVIDER, MODEL)
        self.assertEqual(refused.exception.code, 'run_binding_mismatch')
        # An operator run bound the durable way is still host, and this call does not use that path.
        host._breaker = durable  # noqa: SLF001 - the host must not bind a plugin run through the host-only breaker
        with self.assertRaises(plugins.PluginRefused) as refused:
            host.start_plugin_run(principal, 'run-not-taken', CONNECTION, PROVIDER, MODEL)
        self.assertEqual(refused.exception.code, 'breaker_unavailable')
        with self.assertRaises(Refused) as refused:
            self.breaker.check('run-not-taken', CONNECTION, 'host')
        self.assertEqual(refused.exception.code, 'run_unknown')

    def test_a_plugin_denied_order_cannot_reach_it_and_one_with_it_cites_only_its_run(self):
        """G2: no order permission, no order. With it, only a run this principal started."""
        host = self.opened()
        order_principal = self.minted(host, ORDER_PLUGIN)
        other_principal = self.minted(host, OTHER_PLUGIN)
        reader_principal = self.minted(host, READER_PLUGIN)
        self.assertIsNot(order_principal, other_principal)
        host.start_plugin_run(reader_principal, READER_RUN, CONNECTION, PROVIDER, MODEL)
        host.start_plugin_run(order_principal, OWN_RUN, CONNECTION, PROVIDER, MODEL)
        host.start_plugin_run(other_principal, OTHER_RUN, CONNECTION, PROVIDER, MODEL)
        breaker_module.DurableRouteBreaker(object(), self.breaker).bind_run(HOST_RUN, CONNECTION, PROVIDER, MODEL)

        calls = []
        real = host._approver.request  # noqa: SLF001 - the order recorder the cite must pass through

        def spy(effect):
            calls.append(effect['run_id'])
            return real(effect)

        host._approver.request = spy

        def refused(principal, effect):
            with self.assertRaises(plugins.PluginRefused) as caught:
                host.cite_plugin_order(principal, effect)
            return caught.exception

        # The plugin that started READER_RUN still cannot reach an order.
        denial = refused(reader_principal, _effect(READER_PLUGIN, READER_RUN))
        self.assertEqual((denial.code, denial.detail), ('permission_required', 'order'))
        # A host run does not become a way around the missing permission.
        denial = refused(reader_principal, _effect(READER_PLUGIN, HOST_RUN))
        self.assertEqual((denial.code, denial.detail), ('permission_required', 'order'))
        self.assertEqual(calls, [])

        denial = refused(order_principal, _effect(ORDER_PLUGIN, HOST_RUN))
        self.assertEqual((denial.code, denial.detail), ('scope_refused', 'run'))
        denial = refused(order_principal, _effect(ORDER_PLUGIN, OTHER_RUN))
        self.assertEqual((denial.code, denial.detail), ('scope_refused', 'run'))
        denial = refused(order_principal, _effect(OTHER_PLUGIN, OTHER_RUN))
        self.assertEqual((denial.code, denial.detail), ('scope_refused', 'run'))
        denial = refused({'kind': 'plugin', 'id': ORDER_PLUGIN}, _effect(ORDER_PLUGIN, OWN_RUN))
        self.assertEqual((denial.code, denial.detail), ('scope_refused', 'plugin'))
        self.assertEqual(calls, [])

        with self.assertRaises(Refused) as unknown:
            host.cite_plugin_order(order_principal, _effect(ORDER_PLUGIN, 'run-missing'))
        self.assertEqual(unknown.exception.code, 'run_unknown')
        self.assertEqual(calls, [])

        effect = _effect(ORDER_PLUGIN, OWN_RUN)
        approval_id = host.cite_plugin_order(order_principal, effect)
        self.assertEqual(calls, [OWN_RUN])
        record = self.record(host, approval_id)
        self.assertEqual(record['state'], 'pending')
        self.assertEqual(record['run_id'], OWN_RUN)
        self.assertEqual(record['effect']['plugin_id'], ORDER_PLUGIN)
        # The principal that started the run cannot approve the order it cited.
        answered = host.answer_plugin_effect(order_principal, approval_id, effect, authority=None)
        self.assertEqual(answered, {'code': 'scope_refused', 'detail': 'approve'})
        self.assertEqual(self.record(host, approval_id)['state'], 'pending')

        # A second citation is a second order, still only this run.
        again = host.cite_plugin_order(order_principal, _effect(ORDER_PLUGIN, OWN_RUN, {'note': 'two'}))
        self.assertNotEqual(again, approval_id)
        self.assertEqual(calls, [OWN_RUN, OWN_RUN])
        self.assertEqual(self.record(host, again)['run_id'], OWN_RUN)

        # The permission is read when the order is cited, not from a copy taken at load.
        live = host.loader.permissions
        host.loader.permissions = lambda plugin_id: tuple(item for item in live(plugin_id) if item != 'order')
        denial = refused(order_principal, _effect(ORDER_PLUGIN, OWN_RUN, {'note': 'three'}))
        self.assertEqual((denial.code, denial.detail), ('permission_required', 'order'))
        self.assertEqual(calls, [OWN_RUN, OWN_RUN])

    def test_an_interruption_a_store_refusal_and_a_second_caller(self):
        host = self.opened()
        principal = self.minted(host, ORDER_PLUGIN)
        real_bind = host._breaker.bind_run  # noqa: SLF001 - put the store's own bind back after the interruption

        def cut(run_id, connection, plugin_id, provider, model_version):
            # The same arguments the store's bind takes. The interruption is inside that call.
            raise KeyboardInterrupt('synthetic interruption')

        host._breaker.bind_run = cut
        with self.assertRaises(KeyboardInterrupt):
            host.start_plugin_run(principal, 'run-cut', CONNECTION, PROVIDER, MODEL)
        host._breaker.bind_run = real_bind
        with self.assertRaises(Refused) as refused:
            self.breaker.check('run-cut', CONNECTION, ORDER_PLUGIN)
        self.assertEqual(refused.exception.code, 'run_unknown')

        with self.assertRaises(Refused) as refused:
            host.start_plugin_run(principal, 'bad run', CONNECTION, PROVIDER, MODEL)
        self.assertEqual(refused.exception.code, 'invalid_run_id')

        barrier = threading.Barrier(2)
        errors = []

        def start(run_id):
            try:
                barrier.wait(2)
                host.start_plugin_run(principal, run_id, CONNECTION, PROVIDER, MODEL)
            except Exception as exc:  # noqa: BLE001 - the assertion below reports the caller's failure
                errors.append(exc)

        threads = [threading.Thread(target=start, args=(run_id,)) for run_id in ('run-a', 'run-b')]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.breaker.check('run-a', CONNECTION, ORDER_PLUGIN)['provider'], PROVIDER)
        self.assertEqual(self.breaker.check('run-b', CONNECTION, ORDER_PLUGIN)['provider'], PROVIDER)

        host.start_plugin_run(principal, OWN_RUN, CONNECTION, PROVIDER, MODEL)

        def interrupt(_effect):
            raise KeyboardInterrupt('synthetic interruption during the order')

        host._approver.request = interrupt  # noqa: SLF001 - the order recorder must not swallow the interruption
        with self.assertRaises(KeyboardInterrupt):
            host.cite_plugin_order(principal, _effect(ORDER_PLUGIN, OWN_RUN))
        # The run is still this plugin's. The interrupted order was not recorded.
        self.assertEqual(self.breaker.check(OWN_RUN, CONNECTION, ORDER_PLUGIN)['provider'], PROVIDER)

        def explode(_effect):
            raise RuntimeError('synthetic failure during the order')

        host._approver.request = explode
        with self.assertRaises(RuntimeError):
            host.cite_plugin_order(principal, _effect(ORDER_PLUGIN, OWN_RUN, {'note': 'two'}))


if __name__ == '__main__':
    unittest.main()
