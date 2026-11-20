"""Proposed S2b regressions; all fixtures are synthetic and live below TMPDIR.

Run from the repository root with PYTHONPATH=core, under capped.sh.
The assertions describe the intended behavior and intentionally fail on 7cde3ec3.
No real settings, credentials, provider, logind or systemd manager are used.
"""
import builtins
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from recursion_core.control.api import ControlAPI
from recursion_core.control.client import SocketAPI
from recursion_core.control.registry import EMPTY, InMemoryRunRegistry
from recursion_core.control_transport import UnixControlServer
from recursion_core.control_transport.framing import MAX_FRAME_BYTES
from recursion_core.daemon import control_adapter as adapter, engine_user, launcher
from recursion_core.routes import contract


@unittest.skipUnless(sys.platform.startswith('linux'), 'Linux launcher witnesses')
class S2bRegressions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='s2b-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    @staticmethod
    def probe(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0 if argv[0] == 'loginctl' else 3,
                                           'no' if argv[0] == 'loginctl' else 'inactive', '')

    def inaccessible_runtime(self, leaf):
        uid = os.getuid() + 1
        directory = self.root / str(uid)
        path = directory / leaf
        path.parent.mkdir(parents=True)
        path.touch()
        os.chmod(directory, 0)
        self.addCleanup(os.chmod, directory, 0o700)
        # Establish the premise with lstat; lexists suppresses this same error.
        with self.assertRaises(PermissionError):
            os.lstat(path)
        return uid

    def test_f01_manager_socket_eacces_is_not_absence(self):
        uid = self.inaccessible_runtime('systemd/private')
        with self.assertRaises(engine_user.EngineManagerPresent):
            engine_user.check_engine_no_user_manager(
                'csl-engine', uid, operator_uid=os.getuid(), call=self.probe,
                runtime_root=self.root)

    def test_f01_bus_eacces_is_not_absence(self):
        uid = self.inaccessible_runtime('bus')
        with self.assertRaises(engine_user.EngineManagerPresent):
            engine_user.signin_without_session_bus({}, uid, runtime_root=self.root)

    def test_f08_session_refusal_does_not_echo_arbitrary_option_values(self):
        # This is only a synthetic marker, never an actual key or credential.
        marker = 'S2B_PRIVATE_MARKER'
        refused = (
            f'--property=PAMName=login\n{marker}',
            f'-qpPAMName=login\n{marker}',
            f'--prop=PAMName={marker}',
            f'--prope=PAMName=login\n{marker}',
            f'--property=Environment=A\nPAMName={marker}',
            f'-pEnvironment=A\nPAMName={marker}',
            f'X=1\nPAMName={marker}',
            '-p',
            f'PAMName={marker}',
        )
        # '-p' and the following element are two argv elements, not one string.
        argvs = [
            ['systemd-run', element, '--', '/bin/true']
            for element in refused
            if element not in ('-p', f'PAMName={marker}')
        ]
        argvs.append(['systemd-run', '-p', f'PAMName={marker}', '--', '/bin/true'])
        argvs.append(['systemd-run', '--prop', f'PAMName={marker}', '--', '/bin/true'])
        for argv in argvs:
            with self.subTest(argv=argv):
                with self.assertRaises(engine_user.SessionLaunchRefused) as raised:
                    engine_user.refuse_session_launch(argv)
                text = str(raised.exception)
                self.assertNotIn(marker, text)
                self.assertNotIn('\n', text)
                self.assertNotIn('PAMName=', text)
        # A value that merely contains the letters is not the PAMName property.
        # ``-p=`` keeps the '=' in the attached argument, so the key is empty.
        # ``--p`` is an ambiguous prefix (property, path-property, pty, pipe).
        allowed = (
            '--property=Environment=S2B_EXAMPLE=PAMName=login',
            f'--property=Environment=S2B_EXAMPLE=PAMName=login\n{marker}',
            '-p=PAMName=login',
            '--p=PAMName=login',
        )
        for element in allowed:
            with self.subTest(element=element):
                engine_user.refuse_session_launch(['systemd-run', element, '--', '/bin/true'])
        # A second caller: the same spelling still refuses, and a plain command does not.
        with self.assertRaises(engine_user.SessionLaunchRefused):
            engine_user.refuse_session_launch(['systemd-run', '-qpPAMName=login', '--', '/bin/true'])
        engine_user.refuse_session_launch(['/opt/engine/bin/claude', '--print'])


if __name__ == '__main__':
    print(f'Python={sys.version.split()[0]} GIL={getattr(sys, "_is_gil_enabled", lambda: "n/a")()}', flush=True)
    unittest.main(verbosity=2)
