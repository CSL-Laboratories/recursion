"""What a refusal prints: no URL userinfo, no raw line break.

The red tests of the s7 triage of the transport sweep (sol lists), entries D11 and D12, from
``sweeps/20261115/s7-triage-sol-lists-claude-opus-5-5-evidence/patches/transport/D1{1,2}-red.patch``;
their bodies are kept as written there, with one test of this draft's own after them. The marker is
synthetic. All filesystem writes stay beneath TMPDIR.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

from recursion_core import control_transport as ct
from recursion_core.control_transport import unix


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux is the tested transport path")
class RefusalTextTests(unittest.TestCase):
    def test_f06_remote_address_refusal_redacts_url_userinfo(self):
        """s7 D11: RemoteBindRefused repeated the whole URL, its userinfo included."""
        marker = "S2B_SYNTHETIC_CANARY"
        with self.assertRaises(ct.RemoteBindRefused) as caught:
            ct.refuse_remote_bind("https://review-user:" + marker + "@example.invalid/control")
        self.assertNotIn(marker, str(caught.exception))

    def test_f06_directory_diagnostic_escapes_line_breaks(self):
        """s7 D12: a directory name holding a line feed was echoed literally in the mode refusal."""
        with tempfile.TemporaryDirectory(prefix="s2b-", dir=os.environ.get("TMPDIR")) as base:
            directory = os.path.join(base, "operator\ninjected-line")
            os.mkdir(directory, 0o700)
            os.chmod(directory, 0o755)
            with self.assertRaises(ct.InsecureSocketPath) as caught:
                unix.ensure_private_dir(directory)
            self.assertNotIn("\n", str(caught.exception))

    def test_the_host_stays_named_and_only_the_userinfo_goes(self):
        marker = "S7_SYNTHETIC_CANARY"
        for address, shown in ((f"https://u:{marker}@example.invalid/control", "example.invalid/control"),
                               (f"user:{marker}@example.invalid:8080", "example.invalid:8080"),
                               (f"tcp://{marker}@10.0.0.1:9\nx", "10.0.0.1:9\\nx")):
            with self.subTest(address=address):
                with self.assertRaises(ct.RemoteBindRefused) as caught:
                    ct.refuse_remote_bind(address)
                text = str(caught.exception)
                self.assertNotIn(marker, text)
                self.assertNotIn("\n", text)
                self.assertIn("<userinfo>@" + shown, text)


if __name__ == "__main__":
    unittest.main()
