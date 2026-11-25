"""The static proof that Recursion sends nothing home: tools/network_reach.py over the shipped package.

Each family of calls that can reach a network is planted in a scratch copy of the package and must
be found and refused; a scan that finds nothing is refused; a row that says the address comes from
the code is refused. The proof is a list of the only things allowed: a module that is not on the
allow list, a refused construct, and a listed call whose text changed are each refused, and each
way of calling home that a first read of this proof got past it is planted here and must be refused
by its own rule. Run by itself: python3 core/tests/test_network_reach.py
"""
import importlib.util
from pathlib import Path
import shutil
import subprocess
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
    ("__import__", "routing/config.py", "def _planted(n):\n    return __import__(n)\n", "__import__"),
    ("getattr on a module", "child_env/policy.py", "import socket as _s\n\ndef _planted(n):\n    return getattr(_s, n)\n",
     "getattr(<object>, <a name decided at run time>)"),
    ("entry point", "routing/config.py", "from importlib.metadata import entry_points\n\ndef _planted():\n"
     "    return entry_points(group='g')\n", "importlib.metadata.entry_points"),
    ("eval", "routing/config.py", "def _planted(t):\n    return eval(t)\n", "eval"),
    ("outside library", "routing/config.py", "import requests\n", "requests"),
    ("method on an object", "routing/config.py", "def _planted(s):\n    return s.connect(('h', 1))\n", ".connect()"),
    ("literal address", "routing/config.py", "_PLANTED = 'https://reports.example.com/v1'\n",
     "https://reports.example.com/v1"),
    ("machine identity", "routing/config.py", "import uuid\n\ndef _planted():\n    return uuid.getnode()\n",
     "uuid.getnode"),
    ("identity word", "routing/config.py", "_PLANTED = {'telemetry': True}\n", "word: telemetry"),
    ("a second use in a listed place", "cgroup_leaf/__init__.py", "import subprocess\n", "import subprocess"),
)

# Every way of calling home that a first read planted against this proof (38; 23 of them passed the
# proof before the allow list). Each must now be refused, by the rule named last. Nothing planted runs:
# it is only scanned. NEW is a new shipped module; 'pair' plants a second file and a shipped importer.
H = "rep" + "orts"
NEW = "daemon/planted.py"
READ_PLANTS = {
    "01 import inside a function": (NEW, "new", "def go(h, p):\n    import socket\n    return socket.create_connection((h, p))\n", "no row in"),
    "02 importlib.import_module with a built name": (NEW, "new", "import importlib\ndef go():\n    return importlib.import_module('so' + 'cket')\n", "refused (built-name)"),
    "03 __import__ with a built name": (NEW, "new", "def go():\n    return __import__('so' + 'cket')\n", "refused (built-name)"),
    "04a getattr(os, 'sys'+'tem')": (NEW, "new", "import os\ndef go(c):\n    return getattr(os, 'sys' + 'tem')(c)\n", "refused (built-name)"),
    "04b sys.modules and getattr with a built name": (NEW, "new", "import sys\ndef go(h, p):\n    s = sys.modules['so' + 'cket']\n    return getattr(s, 'create_' + 'connection')((h, p))\n", "refused (built-name)"),
    "05 a name bound through vars of a module": (NEW, "new", "import sys\ndef go(h, p):\n    ns = vars(sys.modules['socket'])\n    return ns['create_' + 'connection']((h, p))\n", "refused (built-name)"),
    "06a exec of a text": (NEW, "new", "def go(t):\n    exec(t)\n", "refused (text-as-code)"),
    "06b exec through vars(builtins)": (NEW, "new", "import builtins\ndef go(t):\n    vars(builtins)['ex' + 'ec'](t)\n", "refused (built-name)"),
    "06c eval through __builtins__": (NEW, "new", "def go(t):\n    b = __builtins__\n    f = b['ev' + 'al'] if isinstance(b, dict) else getattr(b, 'ev' + 'al')\n    return f(t)\n", "refused (built-name)"),
    "07a import ctypes": (NEW, "new", "import ctypes\ndef go():\n    return ctypes.CDLL(None)\n", "refused (foreign-function)"),
    "07b import _ctypes": (NEW, "new", "import _ctypes\ndef go():\n    return _ctypes.dlopen(None, 0)\n", "refused (foreign-function)"),
    "08a os.system with a built program": (NEW, "new", "import os\ndef go(u):\n    return os.system('cu' + 'rl -s ' + u)\n", "no row in"),
    "08b posix.system": (NEW, "new", "import posix\ndef go(u):\n    return posix.system('cu' + 'rl -s ' + u)\n", "refused (private-module)"),
    "08c sys.modules['subprocess'].run": (NEW, "new", "import sys\ndef go(u):\n    return sys.modules['subprocess'].run(['cu' + 'rl', '-s', u])\n", "refused (built-name)"),
    "08d import _posixsubprocess": (NEW, "new", "import _posixsubprocess\ndef go():\n    return _posixsubprocess.fork_exec\n", "refused (private-module)"),
    "09a a new dependency": (NEW, "new", "import requests\ndef go(u):\n    return requests.get(u)\n", "refused (unlisted-module)"),
    "10a a module loaded by path through importlib.machinery": (NEW, "new", "import importlib.machinery\ndef go(p):\n    return importlib.machinery.SourceFileLoader('x', p).load_module()\n", "no row in"),
    "10b a module loaded by path through zipimport": (NEW, "new", "import zipimport\ndef go(p):\n    return zipimport.zipimporter(p).load_module('x')\n", "refused (private-module)"),
    "10c a test_*.py module imported by a shipped one": ("daemon/test_cache.py", "pair", ("import socket\ndef go(h, p):\n    return socket.create_connection((h, p))\n", "daemon/warm.py", "from .test_cache import go\n"), "refused (outside-package)"),
    "10d a module of a tests folder imported by a shipped one": ("daemon/tests/cache_helper.py", "pair", ("import socket\ndef go(h, p):\n    return socket.create_connection((h, p))\n", "daemon/warm.py", "from .tests.cache_helper import go\n"), "refused (outside-package)"),
    "11a a .pth file in the package": ("zz_planted.pth", "new", "import os; os.system('true')\n", "cannot read"),
    "11b a .pth file beside the package": ("../zz_planted.pth", "new", "import os; os.system('true')\n", "runs by itself"),
    "12a pickle.loads of a bytes literal": (NEW, "new", "import pickle\nBLOB = b\"cposix\\nsystem\\n(Strue\\ntR.\"\ndef go():\n    return pickle.loads(BLOB)\n", "refused (unpickling)"),
    "12b pickle of a shipped .json data file": (NEW, "new", "import base64, json, pathlib, pickle\ndef go():\n    d = json.loads((pathlib.Path(__file__).parent / 'warm.json').read_text())\n    return pickle.loads(base64.b64decode(d['blob']))\n", "refused (unpickling)"),
    "13a asyncio loop.create_connection": (NEW, "new", "import asyncio\nasync def go(h, p):\n    loop = asyncio.get_running_loop()\n    return await loop.create_connection(asyncio.Protocol, h, p)\n", "no row in"),
    "13b an asyncio loop method by a built name": (NEW, "new", "import asyncio\nasync def go(h, p):\n    loop = asyncio.get_running_loop()\n    return await getattr(loop, 'create_' + 'connection')(asyncio.Protocol, h, p)\n", "refused (built-name)"),
    "14a a UDP datagram through socket": (NEW, "new", "import socket\ndef go(h, b):\n    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)\n    s.sendto(b, (h, 53))\n", "no row in"),
    "14b a UDP datagram through _socket": (NEW, "new", "import _socket\ndef go(h, b):\n    s = _socket.socket(2, 2)\n    getattr(s, 'send' + 'to')(b, (h, 53))\n", "refused (private-module)"),
    "14c SysLogHandler": (NEW, "new", "import logging, logging.handlers\ndef go(h):\n    logging.getLogger('x').addHandler(logging.handlers.SysLogHandler(address=(h, 514)))\n", "refused (network-log-handler)"),
    "15a socket.gethostbyname": (NEW, "new", "import socket\ndef go(h):\n    return socket.gethostbyname(h)\n", "no row in"),
    "15b _socket.gethostbyname": (NEW, "new", "import _socket\ndef go(h):\n    return _socket.gethostbyname(h)\n", "refused (private-module)"),
    "15c a subclass of WSGIServer": (NEW, "new", "from wsgiref.simple_server import WSGIServer\nclass S(WSGIServer):\n    pass\n", "no row in"),
    "16a HTTPHandler": (NEW, "new", "import logging.handlers\ndef go(h):\n    logging.handlers.HTTPHandler(h, '/v1/ev' + 'ents', method='POST').emit(None)\n", "refused (network-log-handler)"),
    "16b the control client turned into a TCP client": ("control_transport/unix.py", "replace", [
        ("class UnixControlClient", "sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)", "sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)"),
        ("class UnixControlClient", "sock.connect(self.path)", "sock.connect(('.'.join(['" + H + "', 'example', 'org']), 443))")], "refused (changed-call)"),
    "16c the launch helper made to run another program": ("child_env/launch.py", "replace", [
        ("", "return popen([executable, *argv[1:]], **kwargs)", "return popen(['cu' + 'rl', '-s', '.'.join(['" + H + "', 'example', 'org'])], **kwargs)")], "refused (changed-call)"),
    "17a an argument annotation that runs": (NEW, "new", "def go(x: __import__('os').system('true') = None):\n    return x\n", "refused (built-name)"),
    "17b a return annotation that runs": (NEW, "new", "def go() -> __import__('so' + 'cket').create_connection(('h', 1)):\n    return None\n", "refused (built-name)"),
    "18 an address built from character codes": (NEW, "new", "import urllib.request\ndef go():\n    return urllib.request.urlopen(''.join(map(chr, [104, 116, 116, 112])))\n", "refused (unlisted-module)"),
}


def _copy_package(dst: Path) -> Path:
    shutil.copytree(ROOT / nr.PACKAGE, dst / nr.PACKAGE,
                    ignore=shutil.ignore_patterns("tests", "__pycache__", "test_*.py"))
    return dst


def _plant(root: Path, rel, mode, body):
    pkg = root / nr.PACKAGE
    path = pkg / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if mode == "new":
        path.write_text(body, encoding="utf-8")
    elif mode == "pair":
        path.write_text(body[0], encoding="utf-8")
        (pkg / body[1]).write_text(body[2], encoding="utf-8")
    else:
        text = path.read_text(encoding="utf-8")
        for anchor, old, new in body:
            at = text.index(anchor)
            i = text.index(old, at)
            text = text[:i] + new + text[i + len(old):]
        path.write_text(text, encoding="utf-8")


def _copy_listing(listing):
    return {k: [dict(r) for r in v] for k, v in listing.items()}


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
                         ("gui.py", "wsgiref.simple_server.WSGIServer"), ("loader.py", "exec")):
            self.assertIn(expected, names)

    def test_no_row_says_the_address_comes_from_the_code(self):
        for row in self.listing["reach"]:
            self.assertIn(row["who"], nr.WHO, row)
            self.assertNotEqual(row["who"], "code", row)
            self.assertTrue(row["says"].strip(), row)
            self.assertTrue(row["address"].strip(), row)

    def test_a_row_whose_address_comes_from_the_code_is_refused(self):
        for who in ("code", "built-in", ""):
            listing = _copy_listing(self.listing)
            listing["reach"][0]["who"] = who
            problems = nr.check(self.findings, listing)
            self.assertTrue(any("never the code itself" in p for p in problems), (who, problems))

    def test_a_row_that_covers_nothing_is_refused(self):
        listing = _copy_listing(self.listing)
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

    def test_every_way_a_first_read_got_past_the_proof_is_refused_by_its_rule(self):
        self.assertEqual(len(READ_PLANTS), 38)
        with tempfile.TemporaryDirectory() as tmp:  # the control: a clean copy has no problem at all
            self.assertEqual(nr.check(nr.scan(_copy_package(Path(tmp))), self.listing), [])
        for name, (rel, mode, body, rule) in READ_PLANTS.items():
            with self.subTest(plant=name), tempfile.TemporaryDirectory() as tmp:
                root = _copy_package(Path(tmp))
                _plant(root, rel, mode, body)
                problems = nr.check(nr.scan(root), self.listing)
                where = Path(body[1] if mode == "pair" else rel).name  # the file the refusal must name
                self.assertTrue(any(rule in p and where in p for p in problems), (name, rule, where, problems[:5]))

    def test_only_the_modules_of_the_allow_list_are_imported(self):
        rows = self.listing["module"]
        self.assertTrue(rows)
        for row in rows:
            self.assertTrue(row["reason"].strip(), row)
            self.assertNotIn(row["name"].split(".")[0], nr.REFUSED_MODULES, row)
        allowed = {row["name"] for row in rows}
        for f in self.findings:
            if f.kind == "module":
                self.assertIn(f.name, allowed, f)

    def test_a_module_row_cannot_allow_a_refused_module(self):
        listing = _copy_listing(self.listing)
        listing["module"].append({"name": "ctypes", "reason": "x"})
        problems = nr.check(self.findings, listing)
        self.assertTrue(any("cannot allow ctypes" in p for p in problems), problems)

    def test_a_module_row_without_a_reason_or_without_a_use_is_refused(self):
        listing = _copy_listing(self.listing)
        listing["module"][0]["reason"] = " "
        listing["module"].append({"name": "colorsys", "reason": "x"})
        problems = nr.check(self.findings, listing)
        self.assertTrue(any("has no reason" in p for p in problems), problems)
        self.assertTrue(any("'colorsys', which no shipped file imports" in p for p in problems), problems)

    def test_a_construct_row_allows_only_the_calls_it_holds(self):
        self.assertTrue(self.listing["construct"])
        for row in self.listing["construct"]:
            self.assertIn(row["construct"], nr.CONSTRUCTS, row)
            self.assertTrue(row["reason"].strip(), row)
        listing = _copy_listing(self.listing)
        listing["construct"][0]["calls"] = [c + " " for c in listing["construct"][0]["calls"]]
        self.assertTrue(any("refused (changed-call)" in p for p in nr.check(self.findings, listing)))
        listing["construct"][0]["calls"] = []
        self.assertTrue(any("holds no call" in p for p in nr.check(self.findings, listing)))

    def test_every_reach_row_pins_the_text_of_its_calls(self):
        for i, row in enumerate(self.listing["reach"]):
            self.assertEqual(len(row["calls"]), len(row["names"]), row)
        listing = _copy_listing(self.listing)
        listing["reach"][1]["calls"] = []
        problems = nr.check(self.findings, listing)
        self.assertTrue(any("refused (changed-call)" in p and listing["reach"][1]["function"] in p
                            for p in problems), problems)

    def test_a_scan_that_cannot_read_a_file_says_so_and_is_red(self):
        for bad in (b"def broken(:\n", b"x = '\xff'\n"):
            with self.subTest(bad=bad), tempfile.TemporaryDirectory() as tmp:
                root = _copy_package(Path(tmp))
                (root / "tools").mkdir()
                for f in ("network_reach.py", "network_reach.toml"):
                    shutil.copy2(ROOT / "tools" / f, root / "tools" / f)
                (root / nr.PACKAGE / "daemon" / "planted.py").write_bytes(bad)
                run = subprocess.run([sys.executable, str(root / "tools" / "network_reach.py")],
                                     capture_output=True, text=True, timeout=120)
                self.assertEqual(run.returncode, 1, run.stdout + run.stderr)
                self.assertIn("NOT PROVEN", run.stdout)
                self.assertIn("cannot be read", run.stdout)

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
