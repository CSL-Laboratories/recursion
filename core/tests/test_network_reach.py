"""The static proof that Recursion sends nothing home: tools/network_reach.py over the shipped package.

Each family of calls that can reach a network is planted in a scratch copy of the package and must
be found and refused; a scan that finds nothing is refused; a row that says the address comes from
the code is refused. Run by itself: python3 core/tests/test_network_reach.py
"""
import importlib.util
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("network_reach", ROOT / "tools" / "network_reach.py")
nr = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("network_reach", nr)
_spec.loader.exec_module(nr)

# One planted fault per family: (family, file it goes in, the code, the name the scan must report).
PLANTS = (
    ("socket", "routing/config.py", "import socket\n\ndef _planted():\n    return socket.create_connection(('h', 1))\n",
     "socket.create_connection"),
    ("ssl", "routing/config.py", "import ssl\n\ndef _planted():\n    return ssl.create_default_context()\n",
     "ssl.create_default_context"),
    ("http", "routing/config.py", "import urllib.request\n\ndef _planted(u):\n    return urllib.request.urlopen(u)\n",
     "urllib.request.urlopen"),
    ("http.client", "routing/config.py", "from http.client import HTTPSConnection\n\ndef _planted():\n"
     "    return HTTPSConnection('h')\n", "http.client.HTTPSConnection"),
    ("asyncio", "routing/config.py", "import asyncio\n\nasync def _planted():\n    return await asyncio.open_connection('h', 1)\n",
     "asyncio.open_connection"),
    ("subprocess", "routing/config.py", "import subprocess as sp\n\ndef _planted():\n    return sp.run(['curl', 'x'])\n",
     "subprocess.run"),
    ("os.system", "routing/config.py", "import os as _o\n\ndef _planted():\n    return _o.system('curl x')\n", "os.system"),
    ("importlib", "routing/config.py", "import importlib\n\ndef _planted(n):\n    return importlib.import_module(n)\n",
     "importlib.import_module"),
    ("__import__", "routing/config.py", "def _planted(n):\n    return __import__(n)\n", "builtins.__import__"),
    ("getattr on a module", "child_env/policy.py", "import socket as _s\n\ndef _planted(n):\n    return getattr(_s, n)\n",
     "getattr(<module>, <a name decided at run time>)"),
    ("entry point", "routing/config.py", "from importlib.metadata import entry_points\n\ndef _planted():\n"
     "    return entry_points(group='g')\n", "importlib.metadata.entry_points"),
    ("eval", "routing/config.py", "def _planted(t):\n    return eval(t)\n", "builtins.eval"),
    ("outside library", "routing/config.py", "import requests\n", "import requests"),
    ("method on an object", "routing/config.py", "def _planted(s):\n    return s.connect(('h', 1))\n", ".connect()"),
    ("literal address", "routing/config.py", "_PLANTED = 'https://reports.example.com/v1'\n",
     "https://reports.example.com/v1"),
    ("machine identity", "routing/config.py", "import uuid\n\ndef _planted():\n    return uuid.getnode()\n",
     "uuid.getnode"),
    ("identity word", "routing/config.py", "_PLANTED = {'telemetry': True}\n", "word: telemetry"),
    ("a second use in a listed place", "cgroup_leaf/__init__.py", "import subprocess\n", "import subprocess"),
)


def _copy_package(dst: Path) -> Path:
    shutil.copytree(ROOT / nr.PACKAGE, dst / nr.PACKAGE,
                    ignore=shutil.ignore_patterns("tests", "__pycache__", "test_*.py"))
    return dst


class StaticProofTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.listing = nr.load_list(ROOT)
        cls.findings = nr.scan(ROOT)

    def test_the_shipped_package_has_a_row_for_every_finding(self):
        self.assertEqual(nr.check(self.findings, self.listing), [])

    def test_the_scan_finds_the_known_doors(self):
        # A scan that misses the control door or the engine start is a broken scan.
        names = {(f.file.rsplit("/", 1)[-1], f.name) for f in self.findings}
        for expected in (("unix.py", "socket.socket"), ("unix.py", ".bind()"), ("launch.py", "subprocess.Popen"),
                         ("gui.py", "wsgiref.simple_server.WSGIServer"), ("loader.py", "builtins.exec")):
            self.assertIn(expected, names)

    def test_no_row_says_the_address_comes_from_the_code(self):
        for row in self.listing["reach"]:
            self.assertIn(row["who"], nr.WHO, row)
            self.assertNotEqual(row["who"], "code", row)
            self.assertTrue(row["says"].strip(), row)
            self.assertTrue(row["address"].strip(), row)

    def test_a_row_whose_address_comes_from_the_code_is_refused(self):
        for who in ("code", "built-in", ""):
            listing = {k: [dict(r) for r in v] for k, v in self.listing.items()}
            listing["reach"][0]["who"] = who
            problems = nr.check(self.findings, listing)
            self.assertTrue(any("never the code itself" in p for p in problems), (who, problems))

    def test_a_row_that_covers_nothing_is_refused(self):
        listing = {k: [dict(r) for r in v] for k, v in self.listing.items()}
        listing["reach"].append({"file": "core/recursion_core/routing/config.py", "function": "gone",
                                 "names": ["socket.socket"], "who": "local", "address": "x", "says": "x"})
        problems = nr.check(self.findings, listing)
        self.assertTrue(any("no longer finds" in p for p in problems), problems)

    def test_an_empty_scan_is_refused(self):
        self.assertTrue(any("empty scan" in p for p in nr.check([], self.listing)))
        with tempfile.TemporaryDirectory() as tmp:
            findings = nr.scan(Path(tmp))  # no package where the scan looks
            self.assertEqual(findings, [])
            self.assertTrue(any("empty scan" in p for p in nr.check(findings, self.listing)))

    def test_each_planted_family_is_found_and_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _copy_package(Path(tmp))
            self.assertEqual(nr.check(nr.scan(root), self.listing), [])  # the copy is clean first
            for family, rel, code, name in PLANTS:
                with self.subTest(family=family):
                    path = root / nr.PACKAGE / rel
                    before = path.read_text(encoding="utf-8")
                    path.write_text(before + "\n\n" + code, encoding="utf-8")
                    try:
                        problems = nr.check(nr.scan(root), self.listing)
                    finally:
                        path.write_text(before, encoding="utf-8")
                    self.assertTrue(any(repr(name) in p for p in problems), (family, problems))

    def test_a_file_the_scan_cannot_read_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _copy_package(Path(tmp))
            (root / nr.PACKAGE / "gui" / "planted.js").write_text("fetch('https://x.example')\n", encoding="utf-8")
            problems = nr.check(nr.scan(root), self.listing)
            self.assertTrue(any("planted.js" in p and "cannot read" in p for p in problems), problems)

    def test_the_compared_hosts_and_the_schema_identifier_are_never_fetched(self):
        # policy.py holds the engines' own hosts only to compare them, and the routing package holds the
        # schema identifier: neither file has any call that could reach a network.
        for f in self.findings:
            if f.kind == "reach":
                self.assertFalse(f.file.endswith("child_env/policy.py"), f)
                self.assertFalse(f.file.startswith("core/recursion_core/routing/"), f)
        literal_files = {r["file"] for r in self.listing.get("literal", [])}
        self.assertIn("core/recursion_core/routing/schema/routing_config.v1.json", literal_files)
        for row in self.listing.get("literal", []):
            self.assertIs(row.get("fetched"), False, row)

    def test_no_identifier_of_a_person_or_a_machine_is_made(self):
        self.assertEqual([f for f in self.findings if f.kind == "identity"], [])
        self.assertEqual(self.listing.get("identity", []), [])


if __name__ == "__main__":
    unittest.main()
