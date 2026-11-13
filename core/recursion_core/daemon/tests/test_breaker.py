"""The daemon's route breaker keeps trips, run bindings and faults across a restart (CSL-738).

PRODUCT.md d84cdd26 (CSLR-D-012) section 4.2 ("Two breakers") and section 3.1 (runs outlive the
daemon). The store is the real ``plugin_security`` breaker store on disk; a "restart" here is a new
in-memory route breaker and a newly opened store over the same file. Clocks are fakes.
"""
import inspect
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
import unittest

from recursion_core.daemon import breaker as daemon_breaker
from recursion_core.plugin_security import ConnectionBreaker as StoredBreaker
from recursion_core.plugin_security import Refused, create_breaker_store
from recursion_core.routes import ConnectionBreaker as RouteBreaker
from recursion_core.routes import contract as c
from recursion_core.routes.tests.fakes import FakeClock

LINUX = sys.platform.startswith('linux')
LINUX_ONLY = ('Linux-only: the breaker store lives in the daemon state directory, checked by uid; on Windows '
              'open_breaker_store refuses PlatformUnsupported (platform_unsupported), covered by '
              'WindowsRefusalTests in daemon/tests/test_windows_refusal.py')
CONN, MODEL, TENANT = 'subscription:login-a', 'model-x', 'tenant-1'
LIMIT = {'code': c.PLAN_LIMIT, 'limit': c.limit_signal('connection', 'test.typed')}


@unittest.skipUnless(LINUX, LINUX_ONLY)
class DurableRouteBreakerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='rc-breaker-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        os.chmod(self.tmp, 0o700)
        self.state = self.tmp / 'state'
        self.clock = FakeClock()

    def boot(self):
        """One daemon lifetime: a fresh in-memory breaker over the store on disk."""
        stored, created = daemon_breaker.open_breaker_store(self.state, clock=self.clock)
        self.addCleanup(stored.close)
        return daemon_breaker.DurableRouteBreaker(RouteBreaker(self.clock), stored), stored, created

    def test_store_is_created_once_owner_only_under_the_state_directory(self):
        _, stored, created = self.boot()
        path = self.state / 'plugin-security' / 'breaker.sqlite3'
        self.assertTrue(created)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(path.parent).st_mode), 0o700)
        self.assertEqual([p.name for p in path.parent.iterdir() if p.name.endswith('.tmp')], [])
        stored.report(CONN, 'plan_limit')
        _, again, created = self.boot()
        self.assertFalse(created, 'an existing store is opened, never replaced')
        self.assertTrue(again.tripped(CONN))

    def test_a_store_that_cannot_be_read_is_refused_not_replaced(self):
        path = daemon_breaker.store_path(self.state)
        path.parent.mkdir(parents=True, mode=0o700)
        path.write_bytes(b'not a breaker store')
        with self.assertRaises(Refused) as caught:
            daemon_breaker.open_breaker_store(self.state, clock=self.clock)
        self.assertEqual(caught.exception.code, 'breaker_store_invalid')
        self.assertEqual(path.read_bytes(), b'not a breaker store')

    def test_a_typed_connection_limit_survives_a_restart_and_clears_only_through_a_probe(self):
        breaker, stored, _ = self.boot()
        admit = breaker.admit(CONN, MODEL, TENANT, deadline_s=10)
        self.assertTrue(admit['admitted'])
        self.assertEqual(breaker.record(CONN, MODEL, TENANT, dict(LIMIT), admit['probe']), 'opened')
        self.assertTrue(stored.tripped(CONN))
        stored.close()

        # Restart: the in-memory breaker is new and empty; the store still says tripped.
        breaker, stored, _ = self.boot()
        self.assertEqual(breaker.state(CONN), 'closed')
        with self.assertLogs('recursion_core.daemon.breaker', 'WARNING'):
            refused = breaker.admit(CONN, MODEL, 'tenant-2', deadline_s=10)
        self.assertEqual((refused['admitted'], refused['code'], refused['key']), (False, c.BREAKER_OPEN, CONN))
        self.assertEqual(refused['retry_at'], self.clock() + 300.0)
        self.assertTrue(breaker.admit('subscription:other-login', MODEL, TENANT)['admitted'])  # other meters are free

        # A probe that proves nothing spends its lease and leaves the trip in the store.
        self.clock.advance(301)
        probe = breaker.admit(CONN, MODEL, TENANT, deadline_s=10)
        self.assertTrue(probe['admitted'] and probe['probe'])
        self.assertEqual(breaker.admit(CONN, MODEL, 'tenant-2', deadline_s=10)['code'], c.PROBE_IN_FLIGHT)
        self.assertEqual(breaker.record(CONN, MODEL, TENANT, {'code': c.ENGINE_ERROR}, probe['probe']), 'probe_released')
        self.assertTrue(stored.tripped(CONN))

        # The next probe succeeds: the trip clears in memory and in the store.
        probe = breaker.admit(CONN, MODEL, TENANT, deadline_s=10)
        self.assertTrue(probe['admitted'] and probe['probe'])
        self.assertEqual(breaker.record(CONN, MODEL, TENANT, {'code': None}, probe['probe']), 'closed')
        self.assertFalse(stored.tripped(CONN))
        stored.close()
        breaker, stored, _ = self.boot()
        admit = breaker.admit(CONN, MODEL, TENANT)
        self.assertEqual((admit['admitted'], admit['probe']), (True, None))

    def test_only_a_typed_connection_limit_reaches_the_store(self):
        breaker, stored, _ = self.boot()
        for outcome in ({'code': c.DEADLINE_EXCEEDED}, {'code': c.ENGINE_ERROR}, {'code': c.SHORT_FAULT},
                        {'code': c.PLAN_LIMIT, 'limit': None}, {'code': None}):
            breaker.record(CONN, MODEL, TENANT, outcome)
            self.assertFalse(stored.tripped(CONN), outcome)
        # A model bucket's limit fences that model in memory; the connection-keyed store is not tripped.
        model_limit = {'code': c.PLAN_LIMIT, 'limit': c.limit_signal('model', 'test.typed', model=MODEL)}
        self.assertEqual(breaker.record(CONN, MODEL, TENANT, model_limit), 'opened')
        self.assertEqual(breaker.admit(CONN, MODEL, TENANT)['code'], c.BREAKER_OPEN)
        self.assertFalse(stored.tripped(CONN))

    def test_runs_are_bound_to_host_and_a_faulted_run_is_refused_after_a_restart(self):
        breaker, stored, _ = self.boot()
        self.assertIsNone(breaker.bind_run('run-1', CONN, 'claude_code', MODEL))
        self.assertIsNone(breaker.bind_run('run-1', CONN, 'claude_code', MODEL))  # a same-key retry binds the same row
        self.assertEqual(stored.check('run-1', CONN, 'host'), {'provider': 'claude_code', 'model_version': MODEL})
        with self.assertRaises(Refused) as caught:
            stored.check('run-1', CONN, 'some.plugin')  # no plugin can cite the operator's run
        self.assertEqual(caught.exception.code, 'run_not_owned')
        breaker.fault_run('run-1')
        stored.close()
        _, stored, _ = self.boot()
        with self.assertRaises(Refused) as caught:
            stored.check('run-1', CONN, 'host')
        self.assertEqual(caught.exception.code, 'run_faulted')

    def test_what_the_store_cannot_hold_is_reported_not_raised(self):
        breaker, stored, _ = self.boot()
        self.assertTrue(breaker.representable(CONN))
        self.assertFalse(breaker.representable('subscription:login with spaces'))
        with self.assertLogs('recursion_core.daemon.breaker', 'ERROR'):
            self.assertEqual(breaker.bind_run('run-2', CONN, 'claude_code', 'model[1m]'), 'invalid_model_version')
        self.assertFalse(stored.tripped(CONN), 'asking whether an id is representable writes nothing')
        stored.close()  # a store that stopped answering: the route result still stands, and the gap is logged
        with self.assertLogs('recursion_core.daemon.breaker', 'ERROR') as logs:
            self.assertEqual(breaker.record(CONN, MODEL, TENANT, dict(LIMIT)), 'opened')
        self.assertIn('not durable', logs.output[0])


class FrozenContractTests(unittest.TestCase):
    """The store's contract is frozen for its other consumer; the daemon only calls it."""

    def test_the_calls_the_daemon_makes_exist_with_the_frozen_shapes(self):
        def params(function):
            return [(p.name, p.kind.name) for p in inspect.signature(function).parameters.values()]

        self.assertEqual(params(create_breaker_store), [('path', 'POSITIONAL_OR_KEYWORD')])
        self.assertEqual(params(StoredBreaker.__init__),
                         [('self', 'POSITIONAL_OR_KEYWORD'), ('store_path', 'POSITIONAL_OR_KEYWORD'),
                          ('clock', 'KEYWORD_ONLY'), ('probe_lease_seconds', 'KEYWORD_ONLY')])
        self.assertEqual([name for name, _ in params(StoredBreaker.bind_run)],
                         ['self', 'run_id', 'connection', 'plugin_id', 'provider', 'model_version'])
        self.assertEqual([name for name, _ in params(StoredBreaker.check)], ['self', 'run_id', 'connection', 'plugin_id'])
        for name in ('report', 'fault_run', 'begin_probe', 'reset_after_probe', 'tripped', 'close'):
            self.assertTrue(callable(getattr(StoredBreaker, name)), name)

    def test_the_daemon_module_only_calls_the_store(self):
        source = Path(daemon_breaker.__file__).read_text(encoding='utf-8')
        self.assertNotIn('._db', source)
        self.assertNotIn('import sqlite3 as', source)
        self.assertNotIn('.execute(', source)


if __name__ == '__main__':
    unittest.main()
