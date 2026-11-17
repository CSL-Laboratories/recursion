"""A start whose launch-check registrant is not there is refused, and no child runs.

The registrant is left unregistered by making the find_spec guard miss. ``run.start``
goes through the composed daemon and reaches the launcher. The seam answers one typed
refusal, ``launch_check_absent``, and starts no child process.

This belongs on the launch-check draft. On master the same refusal would meet every
launch, because that tree has no launch-check registrant at all.
"""
from __future__ import annotations

import importlib.util
import unittest
from unittest import mock

from recursion_core.daemon.tests.test_composition import LINUX, LINUX_ONLY
from recursion_core.daemon.tests.test_seams import Composed

_ABSENT = ('recursion_core.launch_check', 'recursion_core.launch_check.hooks')


def _miss(name, package=None):
    """The find_spec guard, told that the launch-check registrant is not installed."""
    if name in _ABSENT:
        return None
    return _miss.real(name, package)


_miss.real = importlib.util.find_spec


@unittest.skipUnless(LINUX, LINUX_ONLY)
class AbsentLaunchCheckTest(Composed):
    """The composed daemon, with the launch-check registrant unregistered."""

    def test_an_unregistered_launch_check_refuses_the_start_and_starts_no_child(self):
        with mock.patch.object(importlib.util, 'find_spec', _miss):
            _daemon, composed = self.boot()
        answer = composed.control.call('run.start', self.request())
        self.assertEqual((answer.get('code'), answer.get('start_error')),
                         ('start_failed', {'code': 'launch_refused', 'detail': 'launch_check_absent'}),
                         answer)
        self.assertEqual(self.units.started, [])
        self.assertEqual(self.units.procs, {})
        run_id = answer.get('run_id')
        self.assertTrue(run_id)
        self.assertFalse((self.state / 'runs' / run_id).exists())


if __name__ == '__main__':
    unittest.main()
