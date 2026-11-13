"""R3 regression: dummy data only; no provider, network or operator credential use."""
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

class PrivateHome(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="r3-regression-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.root.chmod(0o700)
        self.home = self.root / "home"
        self.home.mkdir(mode=0o700)
        self.operator = self.root / "operator"
        self.operator.mkdir(mode=0o700)

from recursion_core.child_env import EnvRefused
from recursion_core.subscription_routes import launch_env, spawn_route_process

@unittest.skipIf(os.name == "nt", "POSIX launch checks")
class OpenAIExecutableTest(PrivateHome):
    def env(self):
        return {"PATH":"/usr/bin:/bin", "HOME":str(self.home), "OPENAI_API_KEY":"FAKE-r3-route-key"}

    def test_unsafe_executable_cannot_receive_route_key(self):
        marker = self.root / "executed"
        script = self.root / "unsafe-engine"
        script.write_text("#!/bin/sh\nprintf '%s' \"$OPENAI_API_KEY\" > '" + str(marker) + "'\n")
        script.chmod(0o777)
        proc = None
        try:
            with self.assertRaises(EnvRefused):
                proc = spawn_route_process([str(script)], self.env(), route_home=str(self.home), credential_env="OPENAI_API_KEY")
        finally:
            if proc is not None:
                proc.wait(timeout=5)
        self.assertFalse(marker.exists())

    def test_relative_executable_is_refused(self):
        with self.assertRaises(EnvRefused) as caught:
            spawn_route_process(["./engine"], self.env(), route_home=str(self.home), credential_env="OPENAI_API_KEY")
        self.assertEqual(caught.exception.cause, "argv0_relative")

    def test_credential_name_and_value_are_checked(self):
        for name, value in (("LD_PRELOAD", "/fake/library"), ("OPENAI_API_KEY", "bad\0key"), ("OPENAI_API_KEY", 123), ("OPENAI_API_KEY", "")):
            with self.subTest(name=name, value_type=type(value).__name__), self.assertRaises(EnvRefused):
                launch_env({"HOME":str(self.home), name:value}, route_home=str(self.home), credential_env=name)
        with self.assertRaises(EnvRefused):
            launch_env({"HOME":str(self.home)}, route_home=str(self.home), credential_env="OPENAI_API_KEY")

    def test_real_child_receives_only_the_explicit_route_key(self):
        marker = self.root / "received"
        script = self.root / "safe-engine"
        script.write_text("#!/bin/sh\nprintf '%s' \"$OPENAI_API_KEY|$ANTHROPIC_API_KEY|$R3_PARENT_SECRET\" > '" + str(marker) + "'\n")
        script.chmod(0o700)
        with mock.patch.dict(os.environ, {"R3_PARENT_SECRET":"FAKE-parent-secret"}):
            proc = spawn_route_process([str(script)], self.env(), route_home=str(self.home), credential_env="OPENAI_API_KEY")
            self.assertEqual(proc.wait(timeout=5), 0)
        self.assertEqual(marker.read_text(), "FAKE-r3-route-key||")
