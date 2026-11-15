"""Real 3.14t daemon startup with a disk plugin; only host services are stand-ins.

No Recursion class, runtime gate, loader, registry, socket, or GUI is mocked. The
PATH fixtures answer the read-only linger and empty-unit-list probes, so the test
cannot discover or stop any of the operator's real services.
"""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import time
import unittest

from recursion_core.control.client import SocketAPI
from recursion_core.daemon.tests import plugin_fixture as fx
from recursion_core.plugin_security.tests.support import needs_crypto

REAL_RUNTIME = (sys.platform.startswith('linux') and sys.implementation.name == 'cpython'
                and sys.version_info[:2] == (3, 14) and sysconfig.get_config_var('Py_GIL_DISABLED'))


@needs_crypto
@unittest.skipUnless(REAL_RUNTIME, 'needs Linux free-threaded CPython 3.14')
class RealEntryTests(unittest.TestCase):
    def test_real_entry_loads_plugin_survives_sweep_and_closes_on_sigterm(self):
        tmp = Path(tempfile.mkdtemp(prefix='rc-entry-r4-'))
        self.addCleanup(shutil.rmtree, tmp, True)
        os.chmod(tmp, 0o700)
        bins = tmp / 'bin'
        bins.mkdir()
        (bins / 'loginctl').write_text('#!/bin/sh\n[ "$1" = show-user ] || exit 91\nprintf "yes\\n"\n')
        (bins / 'systemctl').write_text('#!/bin/sh\n'
                                       '[ "$1" = --user ] && [ "$2" = list-units ] || exit 92\n'
                                       'printf "sweep\\n" >> "$R4_SWEEPS"\n')
        for program in bins.iterdir():
            program.chmod(0o700)
        config = tmp / 'config' / 'recursion'
        fx.write_install(config, grants=[fx.grant_for(fx.OPERATOR_SOURCE)])
        fx.write_plugin(config / 'plugins' / 'fixture')
        runtime = tmp / 'runtime'
        runtime.mkdir(mode=0o700)
        env = {'PATH': str(bins), 'HOME': str(tmp), 'XDG_RUNTIME_DIR': str(runtime),
               'XDG_STATE_HOME': str(tmp / 'state'), 'XDG_CONFIG_HOME': str(tmp / 'config'),
               'PYTHONPATH': str(Path(__file__).resolve().parents[3]),
               'R4_SWEEPS': str(tmp / 'sweeps'), 'PYTHONDONTWRITEBYTECODE': '1'}
        log_path = tmp / 'daemon.log'
        with log_path.open('w') as log:
            child = subprocess.Popen([sys.executable, '-X', 'gil=0', '-B', '-m', 'recursion_core.daemon',
                                      '--gui-port', '0', '--sweep-seconds', '0.05'],
                                     env=env, stdout=log, stderr=subprocess.STDOUT)
            try:
                api = SocketAPI(str(runtime / 'recursion' / 'control.sock'), timeout_s=1)
                deadline = time.monotonic() + 15
                answer = None
                while time.monotonic() < deadline and child.poll() is None:
                    answer = api.call('plugin.list')
                    if answer.get('code') == 'ok' and (tmp / 'sweeps').exists():
                        if len((tmp / 'sweeps').read_text().splitlines()) >= 2:
                            break
                    time.sleep(0.02)
                self.assertIsNone(child.poll(), log_path.read_text())
                self.assertIsNotNone(answer)
                self.assertEqual(answer['code'], 'ok', (answer, log_path.read_text()))
                record, = answer['plugins']
                self.assertEqual((record['state'], record['provides']),
                                 ('loaded', ['panels', 'operator_reads', 'core_client']))
                self.assertIsNone(answer['core_poisoned'])
                self.assertGreaterEqual(len((tmp / 'sweeps').read_text().splitlines()), 2)
                child.terminate()
                self.assertEqual(child.wait(timeout=15), 0, log_path.read_text())
                self.assertEqual(api.call('plugin.list')['code'], 'control_api_unavailable')
                self.assertFalse((runtime / 'recursion' / 'gui-device-token').exists())
            finally:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=15)


if __name__ == '__main__':
    unittest.main()
