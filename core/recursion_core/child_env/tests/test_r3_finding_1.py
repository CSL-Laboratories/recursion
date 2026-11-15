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

from recursion_core.child_env import EnvRefused, build_child_env

@unittest.skipIf(os.name == "nt", "POSIX file ownership")
class MissingTraversalTest(PrivateHome):
    def test_missing_component_cannot_hide_unsafe_tail(self):
        public = self.root / "public"
        public.mkdir(mode=0o777)
        public.chmod(0o777)
        ca = public / "ca.pem"
        ca.write_text("dummy CA")
        ca.chmod(0o666)
        missing = self.home / ".claude"
        up = "/.." * len(missing.parts[1:])
        values = {
            "PATH": str(missing) + up + str(public),
            "NODE_EXTRA_CA_CERTS": str(missing) + up + str(ca),
            "SSL_CERT_FILE": str(missing) + up + "/proc/self/environ",
        }
        for name, value in values.items():
            with self.subTest(name=name), self.assertRaises(EnvRefused):
                build_child_env("claude_code", home=str(self.home), auth_mode="subscription", parent={name:value})

    def test_missing_component_inside_link_target_cannot_hide_dotdot(self):
        link = self.root / "link"
        link.symlink_to(str(self.home / "missing") + "/../../../proc/self/environ")
        with self.assertRaises(EnvRefused):
            build_child_env("codex", home=str(self.home), auth_mode="subscription", parent={"SSL_CERT_FILE":str(link)})

    def test_ordinary_missing_private_path_is_still_accepted(self):
        value = str(self.home / "future" / "ca.pem")
        env = build_child_env("codex", home=str(self.home), auth_mode="subscription", parent={"SSL_CERT_FILE":value})
        self.assertEqual(env["SSL_CERT_FILE"], value)
