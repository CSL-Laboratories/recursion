"""Stop removes an empty cgroup directory under the manager root and records proof removed.

A directory that still holds entries is left in place and stays proof empty. The logical
cgroup path on the record is not a host path; the manager joins it to its own root.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import tempfile
import time
import unittest

from recursion_core.control.api import ControlAPI
from recursion_core.daemon import Daemon, Registry
from recursion_core.daemon import control_adapter as adapter
from recursion_core.daemon import launcher
from recursion_core.daemon.tests.test_adv1_stop_restart import GROUP, LINUX, ScriptedManager, show

BOOT = 'boot-removal'


@unittest.skipUnless(LINUX, 'the daemon is Linux only')
class ManagerRemovalReceiptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='rc-stop-rm-', dir=os.environ.get('TMPDIR')))
        os.chmod(self.tmp, 0o700)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = self.tmp / 'cg'

    def _api(self, manager):
        daemon = Daemon(Registry(self.tmp / 'state'), manager, boot_id=lambda: BOOT)
        self.addCleanup(daemon.close)
        daemon.registry.add('run-x', BOOT)
        registry = adapter.DurableRunRegistry(daemon.registry, processes=adapter.pid_alive(manager))
        registry.put({'run_id': 'run-x', 'tenant': 't', 'engine': 'claude_code', 'state': 'running',
                      'cgroup': GROUP, 'started_at': 1.0, 'stop': None, 'start_key': None})
        api = ControlAPI(registry, launcher.UnitKillTree(daemon, poll_s=0.01), lambda p: {}, time.time,
                         confirm_timeout_s=0.3)
        return api

    def _forgotten(self):
        return ScriptedManager(self.root, show(LoadState='not-found', ActiveState='inactive', SubState='dead',
                                               ControlGroup='', InvocationID='', MainPID='0',
                                               ExecMainStartTimestampMonotonic='0'), stop='not_loaded')

    def test_stop_removes_an_empty_directory_under_the_manager_root(self):
        unit = 'recursion-run-run-x.service'
        directory = self.root / 'app.slice' / unit
        directory.mkdir(parents=True)
        api = self._api(self._forgotten())
        answer = api.call('run.stop', {'run_id': 'run-x'})
        proof = api.call('run.status', {'run_id': 'run-x'})['run']['stop_attestation']
        self.assertEqual(answer['code'], 'stopped', answer)
        self.assertEqual(proof['proof'], 'removed', proof)
        self.assertEqual(proof['cgroup'], GROUP)
        self.assertFalse(directory.exists())
        self.assertNotIn('exit_code', answer)

    def test_a_directory_that_still_holds_entries_stays_proof_empty(self):
        directory = self.root / GROUP.lstrip('/')
        directory.mkdir(parents=True)
        (directory / 'cgroup.procs').write_text('')
        (directory / 'cgroup.events').write_text('populated 0\n')
        api = self._api(self._forgotten())
        answer = api.call('run.stop', {'run_id': 'run-x'})
        proof = api.call('run.status', {'run_id': 'run-x'})['run']['stop_attestation']
        self.assertEqual((answer['code'], proof['proof']), ('stopped', 'empty'), (answer, proof))
        self.assertTrue(directory.is_dir())


if __name__ == '__main__':
    unittest.main()
