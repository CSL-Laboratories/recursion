"""What child_env does on Windows: a typed refusal, asserted on a real Windows host (CSL-785).

The POSIX suites in test_child_env.py skip there, so without this file the Windows gate would
assert nothing about child_env. On a POSIX host the same class runs under the simulation in
test_simulated_windows.py. Every value is fake; no engine, provider or network is used.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

from recursion_core.child_env import EnvRefused, ValidatedEnv, build_child_env, policy, spawn

WINDOWS_ONLY = "Windows-only assertions; a POSIX host runs them simulated (child_env/tests/test_simulated_windows.py)"


@unittest.skipUnless(os.name == "nt", WINDOWS_ONLY)
class WindowsRefusalTests(unittest.TestCase):
    def test_build_child_env_refuses_platform_unsupported_for_every_engine_and_mode(self) -> None:
        home = tempfile.gettempdir()
        for engine in sorted(policy.ENGINE_PROFILES):
            for mode in ("subscription", "api_key", "cloud_provider"):
                with self.subTest(engine=engine, mode=mode), self.assertRaises(EnvRefused) as caught:
                    build_child_env(engine, home=home, auth_mode=mode, parent={"PATH": os.defpath})
                self.assertEqual((None, "platform_unsupported"), (caught.exception.name, caught.exception.cause))

    def test_spawn_refuses_before_any_process_starts(self) -> None:
        # build_child_env never returns here, so the only ValidatedEnv a caller could hold is one made with the seal.
        env = ValidatedEnv({"PATH": os.defpath}, "codex", (), auth_mode="subscription", _seal=ValidatedEnv._seal)
        calls: list = []
        with self.assertRaises(EnvRefused) as caught:
            spawn([sys.executable, "-c", "pass"], env, popen=lambda *args, **kwargs: calls.append(args))
        self.assertEqual("platform_unsupported", caught.exception.cause)
        self.assertEqual([], calls)

    def test_owner_helpers_fail_closed_without_geteuid(self) -> None:
        # No os.geteuid: no helper may raise AttributeError, answer "owned" or stand in a uid.
        self.assertFalse(hasattr(os, "geteuid"))
        self.assertFalse(policy._posix_supported())
        self.assertIsNone(policy._posix_euid())
        here = tempfile.gettempdir()
        self.assertFalse(policy._owned_chain(here))
        self.assertFalse(policy._private_dir(here))
        self.assertEqual("owner_check_unsupported", policy._unsafe_file(os.path.join(here, "csl785-no-such-file")))
        self.assertEqual("owner_check_unsupported", policy._unsafe_dir(here))
        with self.assertRaises(EnvRefused) as caught:
            policy._route_tmp()
        self.assertEqual(("TMPDIR", "tmp_root_unsafe"), (caught.exception.name, caught.exception.cause))


if __name__ == "__main__":
    unittest.main()
