"""The socket directory is exactly 0700, and the persistent-client test survives a scheduling pause.

The red tests of the s7 triage of the transport sweep (sol lists), entries D14 and D16, from
``sweeps/20261115/s7-triage-sol-lists-claude-opus-5-5-evidence/patches/transport/D1{4,6}-red.patch``;
their bodies are kept as written there, with one test of this draft's own after them.
All filesystem writes stay beneath TMPDIR.
"""

from __future__ import annotations

import os
import stat
import sys
import tempfile
import threading
import unittest
from unittest import mock

from recursion_core import control_transport as ct
from recursion_core.control_transport import unix


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux is the tested transport path")
class ScratchCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="s7-", dir=os.environ.get("TMPDIR"))
        self.addCleanup(self.tmp.cleanup)
        self.directory = os.path.join(self.tmp.name, "run")


class ModeTests(ScratchCase):
    def test_f09_read_only_directory_is_refused_as_an_insecure_path(self):
        """s7 D14: an owned 0500 directory was accepted where the docs promise exactly 0700."""
        os.mkdir(self.directory, 0o700)
        os.chmod(self.directory, 0o500)
        self.addCleanup(os.chmod, self.directory, 0o700)
        with self.assertRaises(ct.InsecureSocketPath):
            unix.ensure_private_dir(self.directory)

    def test_only_exactly_0700_passes(self):
        os.mkdir(self.directory, 0o700)
        self.addCleanup(os.chmod, self.directory, 0o700)
        for mode in (0o500, 0o600, 0o300, 0o710, 0o701, 0o750, 0o777, 0o2700):
            with self.subTest(mode=oct(mode)):
                os.chmod(self.directory, mode)
                with self.assertRaises(ct.InsecureSocketPath):
                    unix.ensure_private_dir(self.directory)
        os.chmod(self.directory, 0o700)
        unix.ensure_private_dir(self.directory)

    def test_a_directory_this_function_creates_is_0700_under_a_masking_umask(self):
        old = os.umask(0o200)
        try:
            unix.ensure_private_dir(self.directory)
            self.assertEqual(stat.S_IMODE(os.lstat(self.directory).st_mode), 0o700)
        finally:
            os.umask(old)


class TimingTests(ScratchCase):
    def test_existing_idle_test_survives_an_ordinary_scheduling_pause(self):
        """s7 D16: the persistent-client test broke when one 0.3 s pause stretched to 0.8 s."""
        # Execute the unchanged repository test. Only isolate its explicit /tmp
        # fixture and deterministically extend one sleep after real read entry.
        import time as real_time
        from types import SimpleNamespace
        from recursion_core.control_transport.tests import test_transport as original
        read_entered = threading.Event()
        real_read = unix.read_frame
        real_mkdtemp = tempfile.mkdtemp
        original_sleep = real_time.sleep
        delays = []

        def read(*args, **kwargs):
            read_entered.set()
            return real_read(*args, **kwargs)

        def paused(seconds):
            self.assertTrue(read_entered.wait(3))
            slept = 0.8 if not delays else seconds
            delays.append(slept)
            original_sleep(slept)

        def make_temp(*args, **kwargs):
            kwargs['dir'] = os.environ['TMPDIR']
            return real_mkdtemp(*args, **kwargs)

        test = original.ConnectionBoundTests(
            'test_busy_client_between_frames_keeps_its_slot_within_the_deadline')
        result = unittest.TestResult()
        with mock.patch.object(original.tempfile, 'mkdtemp', side_effect=make_temp), \
             mock.patch.object(original, 'time', SimpleNamespace(sleep=paused, monotonic=real_time.monotonic)), \
             mock.patch.object(unix, 'read_frame', side_effect=read):
            test.run(result)
        failures = [(t.id(), detail.splitlines()[-1]) for t, detail in result.errors + result.failures]
        self.assertTrue(result.wasSuccessful(), f"correct idle expiry causes original test failure: {failures}")
        self.assertEqual(len(delays), 4)
        self.assertGreaterEqual(delays[0], 0.8)


if __name__ == "__main__":
    unittest.main()
