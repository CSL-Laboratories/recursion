"""Red-team check (PR #346): every C1-46 cite in child_env names the pinned PRODUCT.md section.

C1-46 (the names the Nov 5 allowlist dropped) lives in PRODUCT.md section 11.5 in every pinned
version, never section 3.8. The offline test reads only this repository. The vault test runs
only when CSL_RESEARCH_VAULT names a local csl-research clone; it never touches the network.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import unittest
from pathlib import Path

HERE = Path(__file__).resolve()
PACKAGE = HERE.parent.parent
ROOT = PACKAGE.parent.parent.parent
PIN = json.loads((ROOT / "contract" / "PRODUCT.pin.json").read_text(encoding="utf-8"))
SECTION = re.compile(r"section (\d+(?:\.\d+)*)")
COMMIT = re.compile(r"PRODUCT\.md `?([0-9a-f]{8,40})")


def _c146_cites():
    for path in sorted(PACKAGE.rglob("*")):
        if path == HERE or path.suffix not in (".py", ".md") or "__pycache__" in path.parts:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "C1-46" in line and SECTION.search(line):
                yield f"{path.relative_to(ROOT)}:{number}", line


class C146CiteTests(unittest.TestCase):
    def test_every_c146_cite_names_section_11_5_of_the_pinned_commit(self):
        cites = list(_c146_cites())
        self.assertTrue(cites, "no C1-46 cite found; the search is broken")
        bad = []
        for where, line in cites:
            sections = SECTION.findall(line)
            commits = COMMIT.findall(line)
            if sections != ["11.5"] or any(not PIN["commit"].startswith(c) for c in commits):
                bad.append(f"{where}: {line.strip()}")
        self.assertEqual(bad, [], "C1-46 cites that do not name PRODUCT.md "
                         f"{PIN['commit'][:8]} section 11.5")

    @unittest.skipUnless(os.environ.get("CSL_RESEARCH_VAULT"),
                         "CSL_RESEARCH_VAULT is unset: the pinned PRODUCT.md is read from a local csl-research clone "
                         "(no network); set it to that clone's path to run this check")
    def test_pinned_section_11_5_carries_c146_and_the_dropped_names(self):
        blob = subprocess.run(
            ["git", "-C", os.environ["CSL_RESEARCH_VAULT"], "show", f"{PIN['commit']}:{PIN['path']}"],
            check=True, capture_output=True, timeout=60).stdout
        self.assertEqual(hashlib.sha256(blob).hexdigest(), PIN["sha256"])
        text = blob.decode("utf-8")
        found = re.search(r"^### 11\.5 .*?(?=^#{2,3} )", text, re.M | re.S)
        self.assertIsNotNone(found, "no section 11.5 in the pinned PRODUCT.md")
        flat = re.sub(r"\s+", " ", found.group(0))
        for needle in ("C1-46", "The Nov 5 tree's allowlist drops names engines need",
                       "mTLS and cert-store names", "`ALL_PROXY`", "`XDG_RUNTIME_DIR`"):
            self.assertIn(needle, flat)
        old = re.search(r"^### 3\.8 .*?(?=^#{2,3} )", text, re.M | re.S)
        self.assertIsNotNone(old)
        self.assertNotIn("C1-46", old.group(0))


if __name__ == "__main__":
    unittest.main()
