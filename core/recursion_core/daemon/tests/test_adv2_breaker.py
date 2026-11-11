"""ADV2 red-team: how the daemon uses the durable breaker store (``daemon/breaker.py``).

PRODUCT.md d84cdd26 (CSLR-D-012) 3.4 S3: "A tripped connection resets only through a successful
leased probe", 4.2 "Two breakers": an intent from a run on a tripped route, or from a run that
ended in a fault, is refused. ``plugin_security/breaker.py`` is frozen; these tests drive it
through ``DurableRouteBreaker`` with fake clocks and real SQLite files. An ``expectedFailure``
here is a bug on 08775416; a plain test is a hardening that held.
"""
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest

from recursion_core.daemon import breaker as daemon_breaker
from recursion_core.daemon import control_adapter as adapter
from recursion_core.plugin_security import Refused
from recursion_core.routes import ConnectionBreaker
from recursion_core.routes import contract as c

LINUX = sys.platform.startswith('linux')
LINUX_ONLY = 'Linux-only: the breaker store lives in a 0700 state directory checked by uid'
CONN = 'subscription:adv2-login'
CORE = Path(__file__).resolve().parents[3]


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


def limit_outcome():
    return {'code': c.PLAN_LIMIT, 'limit': c.limit_signal('connection', 'adv2-test')}


class FlakyStore:
    """The real frozen store, except that ``tripped`` raises a SQLite error the first ``n`` times.

    Stands in for a transient read failure (SQLITE_BUSY after the busy timeout, an I/O error).
    """

    def __init__(self, real, failures=1):
        self._real, self.failures = real, failures

    def tripped(self, connection):
        if self.failures:
            self.failures -= 1
            raise sqlite3.OperationalError('database is locked')
        return self._real.tripped(connection)

    def __getattr__(self, name):
        return getattr(self._real, name)


@unittest.skipUnless(LINUX, LINUX_ONLY)
class Adv2BreakerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='rc-adv2-breaker-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        os.chmod(self.tmp, 0o700)
        self.clock = Clock()

    def store(self):
        stored, _ = daemon_breaker.open_breaker_store(self.tmp, clock=self.clock)
        self.addCleanup(stored.close)
        return stored

    def tripped_by_an_earlier_daemon(self):
        earlier = self.store()
        earlier.report(CONN, 'plan_limit')
        return self.store()

    # -- ADV2-1 ----------------------------------------------------------------------------------
    def test_a_transient_store_read_error_does_not_drop_the_restored_trip(self):
        stored = self.tripped_by_an_earlier_daemon()
        durable = daemon_breaker.DurableRouteBreaker(ConnectionBreaker(self.clock), FlakyStore(stored))
        with self.assertLogs('recursion_core.daemon.breaker', 'ERROR'):
            first = durable.admit(CONN, 'm', 't1')
        second = durable.admit(CONN, 'm', 't2')  # the store reads fine again; the trip is still there
        self.assertTrue(stored.tripped(CONN))
        # Expected: the trip from the earlier daemon fences this connection (or the start fails closed).
        self.assertFalse(first['admitted'] and second['admitted'],
                         f'tripped connection admitted twice: {first} {second}')

    def test_a_failed_first_read_fails_closed_and_is_read_again_at_the_next_request(self):
        stored = self.store()  # nothing tripped
        durable = daemon_breaker.DurableRouteBreaker(ConnectionBreaker(self.clock), FlakyStore(stored))
        with self.assertLogs('recursion_core.daemon.breaker', 'ERROR'):
            first = durable.admit(CONN, 'm', 't1')
        self.assertEqual((first['admitted'], first['code']), (False, c.BREAKER_OPEN))
        second = durable.admit(CONN, 'm', 't1')
        self.assertEqual((second['admitted'], second['code']), (True, None))

    def test_control_the_restored_trip_fences_when_the_store_reads(self):
        stored = self.tripped_by_an_earlier_daemon()
        durable = daemon_breaker.DurableRouteBreaker(ConnectionBreaker(self.clock), stored)
        with self.assertLogs('recursion_core.daemon.breaker', 'WARNING'):
            answer = durable.admit(CONN, 'm', 't1')
        self.assertEqual((answer['admitted'], answer['code']), (False, c.BREAKER_OPEN))

    # -- ADV2-2 ----------------------------------------------------------------------------------
    def test_a_long_successful_probe_clears_the_stored_trip(self):
        stored = self.tripped_by_an_earlier_daemon()
        durable = daemon_breaker.DurableRouteBreaker(ConnectionBreaker(self.clock), stored)
        with self.assertLogs('recursion_core.daemon.breaker', 'WARNING'):
            self.assertFalse(durable.admit(CONN, 'm', 't', deadline_s=900)['admitted'])
        self.clock.now += 301  # past the restored cooldown: one probe
        probe = durable.admit(CONN, 'm', 't', deadline_s=900)
        self.assertTrue(probe['admitted'])
        self.assertIsNotNone(probe['probe'])
        self.clock.now += 400  # the probe run takes 400 s, well inside its own 900 s deadline
        effect = durable.record(CONN, 'm', 't', {'code': None}, probe['probe'])
        self.assertEqual(effect, 'closed')                  # the in-memory breaker closed...
        self.assertEqual(durable.state(CONN), 'closed')
        self.assertFalse(stored.tripped(CONN), 'route closed in memory but still tripped in the store')

    def test_the_store_lease_covers_the_longest_route_deadline(self):
        routes = {'codex': {'deadline_s': 60.0}, 'claude_code': {'deadline_s': 900.0}}
        lease_s = daemon_breaker.store_probe_lease_s(routes, ConnectionBreaker(self.clock))
        self.assertEqual(lease_s, 930.0)  # the route breaker's lease: deadline plus its 30 s grace
        self.assertEqual(daemon_breaker.store_probe_lease_s({}, ConnectionBreaker(self.clock)),
                         daemon_breaker.STORE_PROBE_LEASE_S)
        earlier = self.store()
        earlier.report(CONN, 'plan_limit')
        stored, _ = daemon_breaker.open_breaker_store(self.tmp, clock=self.clock, probe_lease_seconds=lease_s)
        self.addCleanup(stored.close)
        durable = daemon_breaker.DurableRouteBreaker(ConnectionBreaker(self.clock), stored)
        with self.assertLogs('recursion_core.daemon.breaker', 'WARNING'):
            durable.admit(CONN, 'm', 't', deadline_s=900)
        self.clock.now += 301
        probe = durable.admit(CONN, 'm', 't', deadline_s=900)
        self.clock.now += 900  # inside the probe's 930 s lease
        with self.assertNoLogs('recursion_core.daemon.breaker', 'WARNING'):
            self.assertEqual(durable.record(CONN, 'm', 't', {'code': None}, probe['probe']), 'closed')
        self.assertFalse(stored.tripped(CONN))

    def test_a_trip_the_store_will_not_clear_is_not_shown_closed(self):
        stored = self.tripped_by_an_earlier_daemon()

        class NoReset(FlakyStore):
            def reset_after_probe(self, connection, lease, probe_ok):
                return False

        durable = daemon_breaker.DurableRouteBreaker(ConnectionBreaker(self.clock), NoReset(stored, failures=0))
        with self.assertLogs('recursion_core.daemon.breaker', 'WARNING'):
            durable.admit(CONN, 'm', 't', deadline_s=60)
        self.clock.now += 301
        probe = durable.admit(CONN, 'm', 't', deadline_s=60)
        with self.assertLogs('recursion_core.daemon.breaker', 'ERROR'):
            effect = durable.record(CONN, 'm', 't', {'code': None}, probe['probe'])
        self.assertEqual(effect, 'none')
        self.assertTrue(stored.tripped(CONN))
        self.assertNotEqual(durable.state(CONN), 'closed')
        self.assertEqual(durable.admit(CONN, 'm', 't')['code'], c.BREAKER_OPEN)

    def test_control_a_short_successful_probe_clears_the_stored_trip(self):
        stored = self.tripped_by_an_earlier_daemon()
        durable = daemon_breaker.DurableRouteBreaker(ConnectionBreaker(self.clock), stored)
        with self.assertLogs('recursion_core.daemon.breaker', 'WARNING'):
            durable.admit(CONN, 'm', 't', deadline_s=900)
        self.clock.now += 301
        probe = durable.admit(CONN, 'm', 't', deadline_s=900)
        self.clock.now += 10
        self.assertEqual(durable.record(CONN, 'm', 't', {'code': None}, probe['probe']), 'closed')
        self.assertFalse(stored.tripped(CONN))

    def test_a_failed_probe_keeps_the_trip_and_an_engine_error_spends_the_lease(self):
        stored = self.tripped_by_an_earlier_daemon()
        durable = daemon_breaker.DurableRouteBreaker(ConnectionBreaker(self.clock), stored)
        with self.assertLogs('recursion_core.daemon.breaker', 'WARNING'):
            durable.admit(CONN, 'm', 't')
        self.clock.now += 301
        probe = durable.admit(CONN, 'm', 't')
        self.assertEqual(durable.record(CONN, 'm', 't', {'code': c.ENGINE_ERROR}, probe['probe']), 'probe_released')
        self.assertTrue(stored.tripped(CONN))
        again = durable.admit(CONN, 'm', 't')  # half-open, released: the next request probes again
        self.assertTrue(again['admitted'])
        self.assertEqual(durable.record(CONN, 'm', 't', limit_outcome(), again['probe']), 'opened')
        self.assertTrue(stored.tripped(CONN))

    # -- the store file --------------------------------------------------------------------------
    def test_store_and_its_sqlite_side_files_are_owner_only(self):
        stored = self.store()
        stored.report(CONN, 'plan_limit')  # forces the WAL and shm files into being
        directory = daemon_breaker.store_path(self.tmp).parent
        self.assertEqual(stat.S_IMODE(os.stat(directory).st_mode), 0o700)
        for path in directory.iterdir():
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode) & 0o077, 0, f'{path.name} readable by others')

    def test_a_corrupt_store_is_refused_and_never_replaced(self):
        path = daemon_breaker.store_path(self.tmp)
        path.parent.mkdir(mode=0o700)
        path.write_bytes(b'SQLite format 3\x00' + b'\xff' * 200)
        with self.assertRaises(Refused) as caught:
            daemon_breaker.open_breaker_store(self.tmp, clock=self.clock)
        self.assertEqual(caught.exception.code, 'breaker_store_invalid')
        self.assertEqual(path.read_bytes()[:16], b'SQLite format 3\x00')

    def test_a_store_path_that_is_a_symlink_is_refused(self):
        elsewhere = self.tmp / 'elsewhere'
        elsewhere.mkdir(mode=0o700)
        daemon_breaker.create_breaker_store(str(elsewhere / 'empty.sqlite3'))  # a valid but empty store
        path = daemon_breaker.store_path(self.tmp)
        path.parent.mkdir(mode=0o700)
        os.symlink(elsewhere / 'empty.sqlite3', path)
        # Every other daemon file refuses a symlink (private_dir, O_NOFOLLOW reads); this one is opened.
        with self.assertRaises((Refused, OSError)):
            stored, _ = daemon_breaker.open_breaker_store(self.tmp, clock=self.clock)
            stored.close()

    def test_symlinked_store_files_and_store_directory_are_refused_typed(self):
        elsewhere = self.tmp / 'elsewhere'
        elsewhere.mkdir(mode=0o700)
        daemon_breaker.create_breaker_store(str(elsewhere / 'empty.sqlite3'))
        path = daemon_breaker.store_path(self.tmp)
        path.parent.mkdir(mode=0o700)
        daemon_breaker.create_breaker_store(str(path))
        os.symlink(elsewhere / 'other-wal', f'{path}-wal')  # a WAL file elsewhere would redirect writes
        with self.assertRaises(Refused) as caught:
            daemon_breaker.open_breaker_store(self.tmp, clock=self.clock)
        self.assertEqual(caught.exception.code, 'breaker_store_untrusted')
        os.unlink(f'{path}-wal')
        os.unlink(path)
        path.parent.rmdir()
        os.symlink(elsewhere, path.parent)  # the store's directory itself
        with self.assertRaises(OSError):
            daemon_breaker.open_breaker_store(self.tmp, clock=self.clock)
        self.assertFalse(os.path.lexists(elsewhere / daemon_breaker.STORE_FILE), 'nothing written through the link')

    def test_a_deleted_store_is_recreated_and_says_so(self):
        stored = self.store()
        stored.report(CONN, 'plan_limit')
        stored.close()
        os.unlink(daemon_breaker.store_path(self.tmp))
        again, created = daemon_breaker.open_breaker_store(self.tmp, clock=self.clock)
        self.addCleanup(again.close)
        self.assertTrue(created, 'a recreated store must be reported so the daemon can log it')
        self.assertFalse(again.tripped(CONN))  # decision 5 in S34: the trip is gone, only a log says so

    def test_trip_survives_sigkill_of_the_writer(self):
        code = ('import os, signal, sys; from recursion_core.daemon import breaker as b\n'
                's, _ = b.open_breaker_store(sys.argv[1], clock=lambda: 0.0)\n'
                's.report(%r, "plan_limit"); s.bind_run("run-k", %r, "host", "claude_code", "m"); s.fault_run("run-k")\n'
                'os.kill(os.getpid(), signal.SIGKILL)\n' % (CONN, CONN))
        done = subprocess.run([sys.executable, '-B', '-c', code, str(self.tmp)], env={'PYTHONPATH': str(CORE)},
                              capture_output=True, timeout=60, check=False)
        self.assertEqual(done.returncode, -signal.SIGKILL, done.stderr)
        stored = self.store()
        self.assertTrue(stored.tripped(CONN))
        with self.assertRaises(Refused) as caught:
            stored.check('run-k', CONN, 'host')
        self.assertEqual(caught.exception.code, 'run_faulted')

    # -- connection ids, run binding -------------------------------------------------------------
    def test_connection_ids_the_store_cannot_hold_are_not_representable(self):
        durable = daemon_breaker.DurableRouteBreaker(ConnectionBreaker(self.clock), self.store())
        for bad in ('subscription:a b', 'x' * 129, 'subscription:l\u0456gin', 'k:\x00', 'k:a\nb', '', 'k:a;b'):
            self.assertFalse(durable.representable(bad), repr(bad))
        for good in ('subscription:a.b@c/d-e_f', 'api_key:' + 'x' * 120):
            self.assertTrue(durable.representable(good), good)
        # representable() writes nothing (core_timeout is ignored by contract)
        self.assertFalse(durable.tripped('subscription:a.b@c/d-e_f'))

    def test_a_run_faulted_twice_and_rebound_identically_is_stable(self):
        stored = self.store()
        durable = daemon_breaker.DurableRouteBreaker(ConnectionBreaker(self.clock), stored)
        self.assertIsNone(durable.bind_run('run-1', CONN, 'claude_code', 'm'))
        self.assertIsNone(durable.bind_run('run-1', CONN, 'claude_code', 'm'))
        durable.fault_run('run-1')
        durable.fault_run('run-1')
        self.assertEqual(durable.bind_run('run-1', 'subscription:other', 'claude_code', 'm'), 'run_connection_mismatch')
        with self.assertRaises(Refused) as caught:
            stored.check('run-1', CONN, 'host')
        self.assertEqual(caught.exception.code, 'run_faulted')

    def test_a_model_name_the_store_cannot_hold_leaves_the_run_unbound_and_effects_refused(self):
        stored = self.store()
        durable = daemon_breaker.DurableRouteBreaker(ConnectionBreaker(self.clock), stored)
        with self.assertLogs('recursion_core.daemon.breaker', 'ERROR'):
            self.assertEqual(durable.bind_run('run-2', CONN, 'claude_code', 'opus[1m]'), 'invalid_model_version')
        with self.assertRaises(Refused) as caught:
            stored.check('run-2', CONN, 'host')
        self.assertEqual(caught.exception.code, 'run_unknown')

    # -- the route inventory's view of the store ---------------------------------------------------
    def test_route_list_does_not_show_an_unreadable_store_as_closed(self):
        stored = self.tripped_by_an_earlier_daemon()
        flaky = FlakyStore(stored, failures=10)
        durable = daemon_breaker.DurableRouteBreaker(ConnectionBreaker(self.clock), flaky)
        routes = {'claude_code': {'route': {'engine': 'claude_code', 'model': 'm',
                                            'connection': {'kind': 'subscription', 'id': 'adv2-login'}},
                                  'deadline_s': 60.0, 'binary': None}}
        control = adapter.DaemonControl(None, None, None, None, self.tmp, routes=routes, breaker=durable)
        with self.assertLogs('recursion_core.daemon.breaker', 'ERROR'):
            row = next(r for r in control.route_records() if r['id'] == 'engine:claude_code')
        # PRODUCT 3.5, O7's operator rules: "refusal, uncertainty, stale and unreconciled states stay visible as text".
        self.assertNotEqual(row['breaker']['state'], 'closed', row)
        self.assertNotEqual(row['blocker'], adapter.NO_BAR_EVIDENCE, row)
        self.assertEqual(row['breaker']['state'], 'unknown', row)
        self.assertIn('could not be read', row['blocker'])


@unittest.skipUnless(LINUX, LINUX_ONLY)
class Adv2StarterTests(unittest.TestCase):
    """``RouteStarter`` refusals that need no engine: the start never reaches a thread or a launcher."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='rc-adv2-starter-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def starter(self, connection_id, **kw):
        routes = {'codex': {'route': {'engine': 'codex', 'model': 'm',
                                      'connection': {'kind': 'subscription', 'id': connection_id}},
                            'deadline_s': 60.0, 'binary': None}}
        return adapter.RouteStarter(service=None, routes=routes, daemon=None, state_dir=self.tmp, **kw)

    def test_breaker_store_unavailable_refuses_before_any_binding(self):
        starter = self.starter('ok', breaker_unavailable='breaker_store_invalid')
        with self.assertRaises(adapter.StartRefused) as caught:
            starter({'run_id': 'run-1', 'engine': 'codex', 'tenant': 't', 'prompt': 'p'})
        self.assertEqual((caught.exception.code, caught.exception.detail),
                         ('breaker_store_unavailable', 'breaker_store_invalid'))

    def test_an_unrepresentable_connection_refuses_the_start(self):
        stored, _ = daemon_breaker.open_breaker_store(self.tmp, clock=Clock())
        self.addCleanup(stored.close)
        durable = daemon_breaker.DurableRouteBreaker(ConnectionBreaker(Clock()), stored)
        starter = self.starter('has space', breaker=durable)
        with self.assertRaises(adapter.StartRefused) as caught:
            starter({'run_id': 'run-1', 'engine': 'codex', 'tenant': 't', 'prompt': 'p'})
        self.assertEqual(caught.exception.code, 'breaker_connection_unrepresentable')


if __name__ == '__main__':
    unittest.main()
