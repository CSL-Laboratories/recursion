"""The running daemon binds a principal from the socket peer and asks the authorizer.

``compose_control`` serves the control API on the owner-only socket. The peer's uid is
the only thing that names the owner: that principal holds every scope, so a call he
could make before still succeeds, and a field in the request does not change who he is.
A named peer whose uid is not his holds nothing, because this draft has no device
credential. The running daemon refuses that call. A peer whose credentials cannot be
read, and a uid that does not name a process, are refused and are not treated as him.
"""
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

from recursion_core.control.client import SocketAPI
from recursion_core.control_transport.handlers import Connection
from recursion_core.control_transport.peercred import PeerCred
from recursion_core.daemon import Daemon, Registry
from recursion_core.daemon import control_adapter as adapter
from recursion_core.daemon.tests.test_composition import BOOT, ProcessUnitManager, Units

LINUX = sys.platform.startswith('linux')
LINUX_ONLY = ('Linux-only: the composed daemon needs AF_UNIX peer credentials; '
              'on Windows compose_control refuses PlatformUnsupported')


def _peer(uid, pid):
    return Connection(conn_id=1, peer=PeerCred(pid=pid, uid=uid, gid=os.getegid()))


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

    def test_the_owner_keeps_every_scope(self):
        """The same-uid peer is the owner. ``run.list`` and Stop still reach the API. Params do not rename him."""
        _, composed = self.boot()
        self.assertTrue(os.path.exists(composed.server.path))
        listed = SocketAPI(composed.server.path, timeout_s=30).call(
            'run.list', {'principal': 'device', 'grant': 'not-a-grant', 'device_id': 'other'})
        self.assertEqual(listed.get('code'), 'ok', listed)
        self.assertIn('runs', listed)
        stopped = SocketAPI(composed.server.path, timeout_s=30).call('run.stop', {'run_id': 'run-absent'})
        self.assertEqual(stopped.get('code'), 'run_unknown', stopped)
        # A name the table does not have still reaches the API for the owner, who holds every scope.
        unknown = SocketAPI(composed.server.path, timeout_s=30).call('no.such.method', {})
        self.assertEqual(unknown, {'code': 'method_unknown', 'detail': 'no.such.method'})

    def test_a_call_without_the_scope_is_refused_by_the_running_daemon(self):
        """``run.list`` needs ``read``. A named peer who is not the owner holds none. The daemon refuses."""
        _, composed = self.boot()
        # Our pid has a readable cgroup that is not a run unit, so the refusal is the missing scope.
        reply = composed.control.handle(
            {'op': 'control', 'method': 'run.list', 'params': {}},
            _peer(os.geteuid() + 1, os.getpid()))
        self.assertEqual(reply, {'ok': False, 'error': {'code': 'scope_refused', 'detail': 'needs_read'}})
        # A second caller, the owner, still gets the list. The refusal did not stick to the socket.
        self.assertEqual(SocketAPI(composed.server.path, timeout_s=30).call('run.list', {}).get('code'), 'ok')

    def test_an_unknown_principal_is_refused(self):
        """A uid that is not the owner's, with no pid to name, is not a principal and is not the owner."""
        _, composed = self.boot()
        reply = composed.control.handle(
            {'op': 'control', 'method': 'run.list', 'params': {}},
            _peer(os.geteuid() + 1, None))
        self.assertEqual(reply, {'ok': False, 'error': {'code': 'scope_refused', 'detail': 'unknown_principal'}})

    def test_unreadable_peer_credentials_are_refused(self):
        """Credentials that cannot be read are refused. They are never read as the owner."""
        _, composed = self.boot()

        class Raising:
            @property
            def peer(self):
                raise OSError('unreadable')

        class Missing:
            peer = None

        class NotAUid:
            class peer:
                uid = 'owner'
                pid = os.getpid()

        for conn in (Raising(), Missing(), NotAUid()):
            reply = composed.control.handle({'op': 'control', 'method': 'run.list', 'params': {}}, conn)
            self.assertEqual(reply, {'ok': False, 'error': {'code': 'scope_refused',
                                                            'detail': 'peer_credentials_unreadable'}}, conn)

    def test_a_method_not_in_the_table_is_refused_without_every_scope(self):
        """A principal who does not hold every scope is refused a name the table does not have."""
        _, composed = self.boot()
        reply = composed.control.handle(
            {'op': 'control', 'method': 'no.such.method', 'params': {}},
            _peer(os.geteuid() + 1, os.getpid()))
        self.assertEqual(reply, {'ok': False, 'error': {'code': 'scope_refused',
                                                        'detail': 'method_not_in_table'}})


if __name__ == '__main__':
    unittest.main()
