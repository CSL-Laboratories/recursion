"""R4: the concurrency regression itself must reject lost thread failures."""
import threading
import unittest
from unittest import mock

from recursion_core.daemon.tests import test_adv2_gui as gui_tests


@unittest.skipUnless(gui_tests.LINUX, gui_tests.LINUX_ONLY)
class HungReadHarnessTests(unittest.TestCase):
    def test_second_read_exception_and_false_success_fail_the_regression(self):
        for outcome in ('raise', 'success'):
            with self.subTest(outcome=outcome):
                class BrokenRead(gui_tests.Adv2HungPluginTests):
                    def setUp(inner):
                        super().setUp()
                        real = inner.host.operator
                        calls = []

                        def broken(command):
                            calls.append(command)
                            if len(calls) == 2:
                                if outcome == 'raise':
                                    raise RuntimeError('injected second-read crash')
                                return 200, {}
                            return real(command)
                        inner.host.operator = broken

                case = BrokenRead('test_a_second_read_is_not_held_forever_behind_a_hung_plugin')
                result = unittest.TestResult()
                # Silence only the intentionally injected lost thread traceback on the old test.
                with mock.patch.object(threading, 'excepthook'):
                    case.run(result)
                self.assertFalse(result.wasSuccessful(),
                                 'the regression must fail when the second read crashes or returns 200')


if __name__ == '__main__':
    unittest.main()
