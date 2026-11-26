"""An unreadable cgroup or proc observation is not a clean tree.

A missing cgroup directory is still "no directory" (None). A directory the walk
cannot list, or a /proc read that is not "no such process", must not be booked clean.
"""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from recursion_core.daemon import evidence_e2e


@unittest.skipUnless(sys.platform.startswith('linux'), 'cgroup observations are Linux')
class UnreadableEvidenceTests(unittest.TestCase):
    def test_unreadable_cgroup_and_proc_must_not_be_called_clean(self):
        scratch = tempfile.TemporaryDirectory(prefix='rc-evidence-')
        self.addCleanup(scratch.cleanup)
        base = Path(scratch.name)
        harness = evidence_e2e.Harness(base, sys.executable, False)
        cgroup = base / 'fake-cgroup'
        cgroup.mkdir()
        (cgroup / 'cgroup.procs').write_text('')
        denied = cgroup / 'denied-child'
        denied.mkdir()
        (denied / 'cgroup.procs').write_text('456\n')
        os.chmod(denied, 0)
        self.addCleanup(os.chmod, denied, 0o700)
        real_path = evidence_e2e.Path

        def remap(path, *args, **kwargs):
            if path == '/sys/fs/cgroup':
                return cgroup
            return real_path(path, *args, **kwargs)

        try:
            with mock.patch.object(evidence_e2e, 'Path', side_effect=remap):
                pids = harness.cgroup_pids('')
            walk_clean = harness.is_clean({'cgroup_pids': pids, 'pids_alive': {},
                                           'unit': {'ActiveState': 'inactive'}})
        except Exception:
            walk_clean = False
        try:
            with mock.patch.object(Path, 'read_text', side_effect=PermissionError('synthetic proc denial')):
                alive = harness.pid_exists(456)
            proc_clean = harness.is_clean({'cgroup_pids': [], 'pids_alive': {'456': alive},
                                           'unit': {'ActiveState': 'inactive'}})
        except Exception:
            proc_clean = False
        self.assertEqual((walk_clean, proc_clean), (False, False),
                         'unreadable cgroup or proc observation was called clean')
        self.assertIsNone(harness.cgroup_pids('no-such-cgroup-evidence'))
