"""Native Windows witness for the Linux-only launcher pipe check.

On Linux this module skips. On native Windows it runs the pipe check and requires that check
to skip, rather than raise OSError WinError 10093. The skip itself is also pinned on Linux by
``test_the_launcher_pipe_check_skips_when_the_platform_is_not_linux`` in ``test_pi_parser``,
which evaluates the platform as ``win32``.
"""
import sys
import unittest

from recursion_core.routes.engines.fixtures.pi import test_pi_parser as parser_tests


class WindowsWireGuardTest(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "win32", "native Windows witness")
    def test_linux_pipe_wire_check_is_skipped_by_the_original_module(self):
        case = parser_tests.RpcModeTest(
            "test_each_record_is_one_line_through_the_launchers_own_send"
        )
        result = unittest.TestResult()
        case.run(result)
        self.assertEqual(result.testsRun, 1)
        self.assertEqual(result.errors, [], result.errors)
        self.assertEqual(result.failures, [], result.failures)
        self.assertEqual(len(result.skipped), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
