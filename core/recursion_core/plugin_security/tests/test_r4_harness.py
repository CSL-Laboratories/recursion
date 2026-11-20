"""R4: a failed mutant assertion must not contaminate the next mutant."""
import unittest

from recursion_core.plugin_security import authority, signing
from recursion_core.plugin_security.tests import test_mutants
from recursion_core.plugin_security.tests.support import needs_crypto


@needs_crypto
class MutantIsolationTests(unittest.TestCase):
    def test_failed_mutant_restores_the_verifier_before_the_next_mutant(self):
        restored = []

        class FailedMutant(test_mutants.SameUidMutantTests):
            def _attempt(inner, name):
                if name == 'rewrite_ledger_rows':
                    restored.append(authority.verify is signing.verify)
                result = super()._attempt(name)
                if name == 'patch_verify_function':
                    return 'injected assertion failure'
                return result

        result = unittest.TestResult()
        try:
            FailedMutant('test_mutant_table').run(result)
        finally:
            authority.verify = signing.verify
        self.assertTrue(result.failures, 'the injected assertion must actually fail')
        self.assertEqual(result.errors, [])
        self.assertEqual(restored, [True], 'the next mutant inherited the patched verifier')


def setUpModule():
    # Every store refuses an SQLite with the WAL reset bug unless the person accepts the risk
    # (daemon/tests/test_sqlite_wal_bug.py). This module tests other behaviour, so it runs on a stubbed
    # repaired version, whatever SQLite this Python has.
    import sqlite3
    import unittest
    from unittest import mock
    stub = mock.patch.object(sqlite3, 'sqlite_version', '3.51.3')
    stub.start()
    unittest.addModuleCleanup(stub.stop)


if __name__ == '__main__':
    unittest.main()
