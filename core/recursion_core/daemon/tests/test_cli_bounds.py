"""CLI bounds for the daemon entry: sweep interval and loopback port.

``--sweep-seconds inf`` and a number above ``threading.TIMEOUT_MAX``, and
``--gui-port`` outside 0 through 65535, must be a usage error. ``cli`` must
not call ``main`` for those values. A finite sweep inside the wait limit, and
a port from 0 through 65535, stay accepted.
"""
import sys
import threading
import unittest
from unittest.mock import patch

from recursion_core.daemon import __main__ as entry


class CliBoundsTests(unittest.TestCase):
    def _assert_cli_rejects(self, flag, value):
        with patch.object(sys, "argv", ["daemon", flag, value]), \
                patch.object(entry, "require_free_threaded"), \
                patch.object(entry, "main") as main:
            with self.assertRaises(SystemExit) as raised:
                entry.cli()
            self.assertEqual(raised.exception.code, 2)
        main.assert_not_called()

    def test_F5_cli_rejects_infinite_sweep_interval(self):
        self._assert_cli_rejects("--sweep-seconds", "inf")

    def test_F5_cli_rejects_interval_over_thread_timeout_limit(self):
        self._assert_cli_rejects("--sweep-seconds", str(threading.TIMEOUT_MAX * 2))

    def test_F5_cli_rejects_port_over_65535(self):
        self._assert_cli_rejects("--gui-port", "65536")

    def test_F5_cli_rejects_negative_port(self):
        self._assert_cli_rejects("--gui-port", "-1")

    def _assert_cli_accepts(self, argv, **expected):
        with patch.object(sys, "argv", ["daemon", *argv]), \
                patch.object(entry, "require_free_threaded"), \
                patch.object(entry, "main") as main:
            entry.cli()
        main.assert_called_once_with(plugin_dirs=(), **expected)

    def test_finite_sweep_inside_timeout_limit_is_accepted(self):
        self._assert_cli_accepts(["--sweep-seconds", "1.5"], port=8765, sweep_seconds=1.5)
        at_limit = float(str(threading.TIMEOUT_MAX))
        self._assert_cli_accepts(
            ["--sweep-seconds", str(threading.TIMEOUT_MAX)],
            port=8765, sweep_seconds=at_limit)

    def test_port_zero_through_65535_is_accepted(self):
        self._assert_cli_accepts(["--gui-port", "0"], port=0)
        self._assert_cli_accepts(["--gui-port", "65535"], port=65535)

    def test_non_positive_sweep_is_still_refused(self):
        self._assert_cli_rejects("--sweep-seconds", "0")
        self._assert_cli_rejects("--sweep-seconds", "-1")
        self._assert_cli_rejects("--sweep-seconds", "nan")


if __name__ == "__main__":
    unittest.main()
