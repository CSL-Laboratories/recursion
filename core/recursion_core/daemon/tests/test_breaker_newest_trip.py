"""E01 (P1, the Nov 15 sweeps' triage of daemon): an older successful probe never clears a newer trip.

After probe A's success closed the in-memory route breaker, a newer connection-scoped limit tripped
the store and dropped A's store lease; ``_clear_stored_trip`` then took a fresh lease and cleared that
newer trip, so a restart admitted the connection. The store here is the real ``plugin_security``
store on disk; the newer trip is made to land exactly between the in-memory close and the store reset.
"""
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

from recursion_core.daemon import breaker as daemon_breaker
from recursion_core.routes import ConnectionBreaker as RouteBreaker
from recursion_core.routes import contract as c

LINUX = sys.platform.startswith('linux')
CONN, MODEL, TENANT = 'subscription:login-a', 'model-x', 'tenant-1'
LIMIT = {'code': c.PLAN_LIMIT, 'limit': c.limit_signal('connection', 'test.typed')}


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@unittest.skipUnless(LINUX, 'Linux-only: the breaker store lives in the daemon state directory, checked by uid')
class NewestTripTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='rc-newest-trip-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        os.chmod(self.tmp, 0o700)
        self.state = self.tmp / 'state'
        self.clock = Clock()

    def boot(self):
        stored, _ = daemon_breaker.open_breaker_store(self.state, clock=self.clock)
        self.addCleanup(stored.close)
        return daemon_breaker.DurableRouteBreaker(RouteBreaker(self.clock), stored), stored

    def _probe_after_a_trip(self, durable):
        self.assertTrue(durable.admit(CONN, MODEL, TENANT)['admitted'])
        durable.record(CONN, MODEL, TENANT, LIMIT)
        self.clock.now += 301
        probe = durable.admit(CONN, MODEL, TENANT, deadline_s=30)
        self.assertTrue(probe['admitted'])
        self.assertIsNotNone(probe['probe'])
        return probe['probe']

    def _assert_newer_trip_kept(self, durable, stored):
        self.assertTrue(stored.tripped(CONN), 'the older probe success cleared the newer stored trip')
        self.assertNotEqual(durable.state(CONN), 'closed')
        again, _ = self.boot()  # a restart: the store still fences the connection
        self.assertFalse(again.admit(CONN, MODEL, TENANT)['admitted'])

    def _success_with_a_newer_trip_landing(self, durable, stored, probe):
        """The probe's success, with a newer connection limit reported through the daemon exactly
        after the in-memory breaker closed and before the store is reset."""
        real_reset = stored.reset_after_probe
        landed = []

        def newer_trip_first(connection, lease, ok):
            if not landed:
                landed.append(durable.record(CONN, MODEL, TENANT, LIMIT))
            return real_reset(connection, lease, ok)

        with mock.patch.object(stored, 'reset_after_probe', side_effect=newer_trip_first):
            durable.record(CONN, MODEL, TENANT, {'code': None}, probe)
        self.assertEqual(landed, ['opened'])

    def test_E01_old_probe_success_keeps_a_newer_trip_that_dropped_its_lease(self):
        durable, stored = self.boot()
        probe = self._probe_after_a_trip(durable)
        self._success_with_a_newer_trip_landing(durable, stored, probe)
        self._assert_newer_trip_kept(durable, stored)

    def test_E01_a_newer_trip_is_kept_when_the_probe_lease_had_also_run_out(self):
        durable, stored = self.boot()
        probe = self._probe_after_a_trip(durable)
        self.clock.now += 301  # past the store's 300 s lease: the fresh-lease path would be taken
        self._success_with_a_newer_trip_landing(durable, stored, probe)
        self._assert_newer_trip_kept(durable, stored)

    def test_the_probe_own_lease_still_clears_its_trip(self):
        durable, stored = self.boot()
        probe = self._probe_after_a_trip(durable)
        self.assertEqual(durable.record(CONN, MODEL, TENANT, {'code': None}, probe), 'closed')
        self.assertFalse(stored.tripped(CONN))
        self.assertEqual(durable.state(CONN), 'closed')


if __name__ == '__main__':
    unittest.main()
