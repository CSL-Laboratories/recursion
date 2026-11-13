"""What the control transport does on Windows: a typed refusal, asserted on a real Windows host.

The AF_UNIX suites skip on Windows (no AF_UNIX in CPython there, no ``os.geteuid``, no ``flock``),
so this file is what the Windows gate asserts about the transport. On a POSIX host the same class
runs under the simulation in child_env/tests/test_simulated_windows.py. No socket is opened.
"""

from __future__ import annotations

import os
import socket
import tempfile
import unittest

from recursion_core import control_transport as ct
from recursion_core.control_transport.unix import (
    UnixControlClient,
    UnixControlServer,
    check_peer,
    ensure_private_dir,
    require_unix_transport,
)

WINDOWS_ONLY = "Windows-only assertions; a POSIX host runs them simulated (child_env/tests/test_simulated_windows.py)"


@unittest.skipUnless(os.name == "nt", WINDOWS_ONLY)
class WindowsRefusalTests(unittest.TestCase):
    def setUp(self) -> None:
        # A short base keeps the address checks (run before the platform refusal) from refusing a
        # long TMPDIR path first, as on macOS under the simulation (which reports os.name "nt").
        short = "/tmp" if os.path.isdir("/tmp") else None
        self.base = tempfile.mkdtemp(prefix="ctw-", dir=short)
        self.addCleanup(os.rmdir, self.base)  # fails if a refusal left anything behind
        self.sock_dir = os.path.join(self.base, "run")

    def test_the_refusal_is_the_typed_transport_refusal(self) -> None:
        with self.assertRaises(ct.PlatformNotSupported) as caught:
            require_unix_transport()
        self.assertIsInstance(caught.exception, ct.TransportRefused)
        self.assertIsInstance(caught.exception, NotImplementedError)
        self.assertIn("platform_unsupported", str(caught.exception))

    def test_default_factory_gives_the_labelled_stub(self) -> None:
        with self.assertRaises(ct.PlatformNotSupported) as caught:
            ct.create_server(self.sock_dir, ct.with_ping_health())
        self.assertIn("F40", str(caught.exception))
        with self.assertRaises(ct.PlatformNotSupported):
            ct.create_client(os.path.join(self.sock_dir, "control.sock"))
        self.assertFalse(os.path.exists(self.sock_dir))

    def test_unix_classes_refuse_instead_of_raising_attribute_error(self) -> None:
        # Asked for by class or by a forced platform name, the Unix path still refuses: nothing is bound.
        for platform in ("linux", "darwin"):
            with self.subTest(platform=platform), self.assertRaises(ct.PlatformNotSupported):
                ct.create_server(self.sock_dir, ct.with_ping_health(), platform=platform)
            with self.subTest(platform=platform), self.assertRaises(ct.PlatformNotSupported):
                ct.create_client(os.path.join(self.sock_dir, "control.sock"), platform=platform)
        with self.assertRaises(ct.PlatformNotSupported):
            UnixControlServer(self.sock_dir, ct.with_ping_health())
        with self.assertRaises(ct.PlatformNotSupported):
            UnixControlClient(os.path.join(self.sock_dir, "control.sock"))
        with self.assertRaises(ct.PlatformNotSupported):
            ensure_private_dir(self.sock_dir)
        self.assertFalse(os.path.exists(self.sock_dir))

    def test_network_addresses_keep_their_own_refusal(self) -> None:
        for address in (("127.0.0.1", 0), "127.0.0.1:9000", "tcp://127.0.0.1:9000"):
            with self.subTest(address=address), self.assertRaises(ct.RemoteBindRefused):
                UnixControlServer(address, ct.with_ping_health())
            with self.subTest(address=address), self.assertRaises(ct.RemoteBindRefused):
                UnixControlClient(address)

    def test_peer_credentials_are_unreadable_not_an_attribute_error(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)  # never bound or connected
        self.addCleanup(sock.close)
        with self.assertRaises(OSError):
            ct.get_peer_cred(sock)
        with self.assertRaises(ct.PeerRefused):
            check_peer(sock, ct.get_peer_cred, 0)


if __name__ == "__main__":
    unittest.main()
