"""A call that holds no scope must be refused by the running daemon.

The daemon is the one the lane's composed tests start: ``compose_control`` serves
the control API on the owner-only Unix socket (``ComposedDaemonTest.boot`` in
``test_composition.py``). The transport's uid check authenticates the peer and
grants no scope. ``run.list`` needs ``read``. The running daemon must refuse that
call. It answers the list instead, because ``DaemonControl.handle`` binds no
principal and never asks the authorizer.
"""
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

from recursion_core.control.client import SocketAPI
from recursion_core.daemon import Daemon, Registry
from recursion_core.daemon import control_adapter as adapter
from recursion_core.daemon.tests.test_composition import BOOT, ProcessUnitManager, Units

LINUX = sys.platform.startswith('linux')
LINUX_ONLY = ('Linux-only: the composed daemon needs AF_UNIX peer credentials; '
              'on Windows compose_control refuses PlatformUnsupported')


@unittest.skipUnless(LINUX, LINUX_ONLY)
class RunningDaemonScopeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='rc-daemon-scope-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        os.chmod(self.tmp, 0o700)
        self.state = self.tmp / 'state'
        self.runtime = self.tmp / 'rt'
        self.runtime.mkdir(mode=0o700)
        self.environ = {'PATH': '/usr/bin:/bin', 'HOME': str(self.tmp), 'LANG': 'C.UTF-8',
                        'XDG_RUNTIME_DIR': str(self.runtime), 'XDG_CONFIG_HOME': str(self.tmp / 'config')}
        self.units = Units()
        self.open = []
        self.addCleanup(self.shutdown)

    def boot(self):
        """Same composition as ``ComposedDaemonTest.boot``: a serving socket, not an in-process authorizer."""
        daemon = Daemon(Registry(self.state), ProcessUnitManager(self.units), boot_id=lambda: BOOT)
        daemon.sweep()
        composed = adapter.compose_control(daemon, self.state, environ=self.environ, routes={})
        composed.control.reconcile()
        self.open.append((daemon, composed))
        return daemon, composed

    def shutdown(self):
        while self.open:
            daemon, composed = self.open.pop()
            composed.close()
            daemon.close()

    @unittest.expectedFailure  # the running daemon binds no principal and never calls the authorizer, so a call that holds no scope is not refused
    def test_a_call_without_the_scope_is_refused_by_the_running_daemon(self):
        """``run.list`` needs ``read``. The socket peer holds none. The running daemon must refuse."""
        _, composed = self.boot()
        self.assertTrue(os.path.exists(composed.server.path))
        answer = SocketAPI(composed.server.path, timeout_s=30).call('run.list', {})
        # The handler's own refusal is ``scope_refused`` on the result, or the same code in the
        # error envelope, which the socket client reports as ``daemon:scope_refused`` after delivery.
        # An admission refusal (the peer never reached the handler) is ``delivered`` false and does not count.
        refused = (answer.get('code') == 'scope_refused'
                   or (answer.get('delivered') is True and answer.get('detail') == 'daemon:scope_refused'))
        self.assertTrue(refused, 'run.list needs read and this peer holds no scope; '
                        'the running daemon answered %r' % (answer,))


if __name__ == '__main__':
    unittest.main()
