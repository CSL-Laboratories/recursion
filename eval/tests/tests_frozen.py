"""The protocol is frozen with its analysis: FROZEN.sha256 matches the files, and records carry it."""
from __future__ import annotations

import hashlib
import unittest

from eval import PACKAGE_ROOT, runner
from eval import record as record_mod

FROZEN = ("PROTOCOL.md", "analyze.py", "passk.py")


class FrozenTests(unittest.TestCase):
    def test_frozen_hashes_match_the_files(self):
        hashes = runner.frozen_hashes()
        self.assertEqual(sorted(hashes), sorted(FROZEN))
        for name in FROZEN:
            with self.subTest(name):
                self.assertEqual(hashes[name], hashlib.sha256((PACKAGE_ROOT / name).read_bytes()).hexdigest(),
                                 f"{name} changed: a new protocol version needs FROZEN.sha256 and the "
                                 "Versions section of PROTOCOL.md updated in the same commit")

    def test_records_carry_the_frozen_hashes(self):
        hashes = runner.frozen_hashes()
        block = runner.protocol_block()
        self.assertEqual(block["protocol_sha256"], hashes["PROTOCOL.md"])
        self.assertEqual(block["analysis_sha256"],
                         record_mod.sha256_json({"analyze.py": hashes["analyze.py"], "passk.py": hashes["passk.py"]}))


if __name__ == "__main__":
    unittest.main()
