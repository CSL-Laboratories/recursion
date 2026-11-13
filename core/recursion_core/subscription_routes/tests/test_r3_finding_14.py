"""R3-14: the existing security test must detect a removed refusal, independent of policy tables."""
import io
import os
import types
import unittest
from unittest import mock
from recursion_core.subscription_routes import policy
from recursion_core.subscription_routes.tests import test_subscription_routes as original

@unittest.skipIf(os.name == "nt", "POSIX home checks in the test under mutation")
class NonvacuousProviderTest(unittest.TestCase):
    def test_original_test_detects_removed_anthropic_oauth_refusal(self):
        mutated = {name:policy.Engine(spec.engine,spec.providers,
                    tuple(p for p in spec.subscription_providers if p != "anthropic-oauth"),spec.subscription_auth)
                   for name,spec in policy.ENGINES.items()}
        table = types.MappingProxyType(mutated)
        suite = unittest.TestLoader().loadTestsFromName("test_pi_and_hermes_anthropic_oauth_selection",original.SubscriptionProviderTests)
        with mock.patch.object(policy,"ENGINES",table), mock.patch.object(original,"ENGINES",table), \
             mock.patch.object(policy,"_looks_like_claude_login",lambda p:p.startswith("claude")):
            result = unittest.TextTestRunner(stream=io.StringIO()).run(suite)
        self.assertFalse(result.wasSuccessful(), "security test passed after both production refusal mechanisms were removed")

    @unittest.skipIf(os.name == "nt", "POSIX subprocess launch checks")
    def test_spawn_route_process_launches_isolated_child_with_safe_process_flags(self):
        import subprocess
        import shlex
        import tempfile
        from pathlib import Path
        from recursion_core.subscription_routes import validated_launch

        with tempfile.TemporaryDirectory(prefix="r3-finding14-") as temp_dir:
            root = Path(temp_dir)
            root.chmod(0o700)
            home = root / "home"
            home.mkdir(mode=0o700)
            result_file = root / "child-env.txt"
            executable = root / "safe-dummy-engine"
            executable.write_text(
                """#!/bin/sh
printf '%s|%s|%s' "$OPENAI_API_KEY" "$ANTHROPIC_API_KEY" "$R3_PARENT_SECRET" > """ + shlex.quote(str(result_file)) + """
""",
                encoding="utf-8",
            )
            executable.chmod(0o700)
            child_env = {"PATH": "/usr/bin:/bin", "HOME": str(home), "OPENAI_API_KEY": "FAKE-r3-finding14-route-key"}
            observed = {}
            real_popen = subprocess.Popen

            def recording_popen(*args, **kwargs):
                observed["args"] = args
                observed["kwargs"] = kwargs.copy()
                return real_popen(*args, **kwargs)

            with mock.patch.dict(os.environ, {"R3_PARENT_SECRET": "FAKE-parent-only-secret"}), \
                 mock.patch.object(validated_launch.subprocess, "Popen", side_effect=recording_popen):
                proc = validated_launch.spawn_route_process(
                    [str(executable)], child_env, route_home=str(home), credential_env="OPENAI_API_KEY"
                )
                self.assertEqual(proc.wait(timeout=5), 0)

            self.assertEqual(result_file.read_text(encoding="utf-8"), "FAKE-r3-finding14-route-key||")
            self.assertEqual(observed["args"][0], [str(executable)])
            options = observed["kwargs"]
            self.assertFalse(options["shell"])
            self.assertTrue(options["close_fds"])
            self.assertTrue(options["start_new_session"])
            self.assertEqual(options["env"]["OPENAI_API_KEY"], "FAKE-r3-finding14-route-key")
            self.assertNotIn("R3_PARENT_SECRET", options["env"])
            self.assertNotIn("ANTHROPIC_API_KEY", options["env"])
