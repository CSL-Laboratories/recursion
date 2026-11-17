"""E24 (P2, the Nov 15 sweeps' triage of daemon): no store lease, no route probe.

``DurableRouteBreaker.admit`` logged a refused or failed ``begin_probe`` and admitted the route's
probe anyway: a probe that could never clear the stored trip, beside another holder's live probe.
"""
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

from recursion_core.daemon import breaker as daemon_breaker
from recursion_core.routes import ConnectionBreaker as RouteBreaker
from recursion_core.routes import contract as c

LINUX = sys.platform.startswith('linux')
CONN, MODEL, TENANT = 'subscription:login-a', 'model-x', 'tenant-1'


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@unittest.skipUnless(LINUX, 'Linux-only: the breaker store lives in the daemon state directory, checked by uid')
class ProbeLeaseTests(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp(prefix='rc-probe-lease-'))
        self.addCleanup(shutil.rmtree, tmp, True)
        os.chmod(tmp, 0o700)
        self.clock = Clock()
        self.stored, _ = daemon_breaker.open_breaker_store(tmp / 'state', clock=self.clock)
        self.addCleanup(self.stored.close)
        self.stored.report(CONN, 'plan_limit')  # an earlier daemon's trip
        self.durable = daemon_breaker.DurableRouteBreaker(RouteBreaker(self.clock, cooldown_s=1), self.stored)
        with self.assertLogs('recursion_core.daemon.breaker', 'WARNING'):
            self.assertFalse(self.durable.admit(CONN, MODEL, TENANT)['admitted'])
        self.clock.now += 2

    def test_E24_a_lease_held_elsewhere_refuses_the_route_probe(self):
        other = self.stored.begin_probe(CONN)  # another holder's live store lease
        with self.assertLogs('recursion_core.daemon.breaker', 'ERROR'):
            answer = self.durable.admit(CONN, MODEL, TENANT, deadline_s=30)
        self.assertEqual((answer['admitted'], answer['code'], answer['probe']), (False, c.BREAKER_OPEN, None))
        self.assertTrue(self.stored.tripped(CONN))
        # The route's probe was given back: once that lease is spent, the next request is the probe.
        self.stored.reset_after_probe(CONN, other, False)
        again = self.durable.admit(CONN, MODEL, TENANT, deadline_s=30)
        self.assertTrue(again['admitted'])
        self.assertIsNotNone(again['probe'])
        self.assertEqual(self.durable.record(CONN, MODEL, TENANT, {'code': None}, again['probe']), 'closed')
        self.assertFalse(self.stored.tripped(CONN))

    def test_E24_a_store_error_at_the_lease_refuses_the_route_probe(self):
        with mock.patch.object(self.stored, 'begin_probe', side_effect=sqlite3.OperationalError('disk I/O error')):
            with self.assertLogs('recursion_core.daemon.breaker', 'ERROR'):
                answer = self.durable.admit(CONN, MODEL, TENANT, deadline_s=30)
        self.assertFalse(answer['admitted'])
        self.assertEqual(answer['code'], c.BREAKER_OPEN)
        self.assertTrue(self.durable.admit(CONN, MODEL, TENANT, deadline_s=30)['admitted'])

    def test_an_unreadable_store_at_the_probe_refuses_it(self):
        with mock.patch.object(self.stored, 'tripped', side_effect=sqlite3.OperationalError('locked')), \
                mock.patch.object(self.stored, 'begin_probe', side_effect=sqlite3.OperationalError('locked')):
            with self.assertLogs('recursion_core.daemon.breaker', 'ERROR'):
                answer = self.durable.admit(CONN, MODEL, TENANT, deadline_s=30)
        self.assertFalse(answer['admitted'])


if __name__ == '__main__':
    unittest.main()
