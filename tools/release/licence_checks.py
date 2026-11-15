#!/usr/bin/env python3
"""Checks for the licence files (lane RELEASE). Run this file from the repository root:

    python -X utf8 -B tools/release/licence_checks.py

LICENSE must be the Apache License 2.0 text unmodified, byte for byte as published at
https://www.apache.org/licenses/LICENSE-2.0.txt; NOTICE must name no copyright holder until
Lucas names one; CONTRIBUTING.md must state the sign-off rule; SECURITY.md must carry no
e-mail address while its contact is unnamed. Named *_checks.py because the frozen gate
fails on a test_*.py file under tools/release/ (see tools/release/README.md once the CI
slice lands). Stdlib only, offline.
"""
from __future__ import annotations

import hashlib
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# https://www.apache.org/licenses/LICENSE-2.0.txt, fetched Nov 14, 2026: 11358 bytes. The same
# bytes ship as /usr/share/common-licenses/Apache-2.0 on Ubuntu 24.04.
APACHE_2_0_SHA256 = "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30"
APACHE_2_0_SIZE = 11358

# Until Lucas names the copyright holder (counsel item K13), NOTICE carries this placeholder
# and no name. Naming the holder is a deliberate edit of this constant and of NOTICE together.
HOLDER = "<holder: Lucas to name>"
CONTACT = "<contact: Lucas to name>"
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")


def read_lf(rel: str) -> bytes:
    """The file's bytes as committed: a Windows checkout may have added CRs."""
    return (ROOT / rel).read_bytes().replace(b"\r\n", b"\n")


class LicenceTests(unittest.TestCase):
    def test_license_is_the_apache_2_0_text_unmodified(self) -> None:
        data = read_lf("LICENSE")
        self.assertEqual(len(data), APACHE_2_0_SIZE)
        self.assertEqual(hashlib.sha256(data).hexdigest(), APACHE_2_0_SHA256)

    def test_notice_names_no_holder_until_lucas_does(self) -> None:
        lines = read_lf("NOTICE").decode("utf-8").splitlines()
        self.assertEqual(lines[0], "Recursion")
        copyright_lines = [line for line in lines if re.match(r"\s*copyright\b", line, re.I)]
        self.assertEqual(copyright_lines, [f"Copyright 2026 {HOLDER}"])
        self.assertIn("Apache License, Version 2.0", read_lf("NOTICE").decode("utf-8"))
        self.assertIn("LICENSE", lines[-1])

    def test_contributing_states_the_sign_off_rule(self) -> None:
        text = read_lf("CONTRIBUTING.md").decode("utf-8")
        section = text.split("## Sign-off (Developer Certificate of Origin)\n", 1)
        self.assertEqual(len(section), 2, "the sign-off section is missing")
        body = section[1].split("\n## ", 1)[0]
        for needle in ("Signed-off-by: Your Name <you@example.com>", "git commit -s",
                       "https://developercertificate.org/", "version 1.1",
                       "An automated agent or bot does not add one"):
            self.assertIn(needle, body)

    def test_security_policy_has_no_address_until_the_contact_is_named(self) -> None:
        text = read_lf("SECURITY.md").decode("utf-8")
        self.assertIn(f"Report it privately to {CONTACT}.", text)
        self.assertEqual(EMAIL.findall(text), [])
        self.assertIn("Please do not report a suspected vulnerability in a public issue", text)


if __name__ == "__main__":
    unittest.main()
