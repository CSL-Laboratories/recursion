"""What is verified is what runs (whole-package signing).

``verify_package`` reads a plugin package once and returns the bytes it checked. These tests put a
second writer (a thread of this process, or a second process) between that read and the use, and
check that nothing it writes is ever run or called signed:

* a file overwritten after it was read, a file added after the folder was listed, a link put in a
  file's place, a folder renamed under the package, a compiled cache written beside a source:
  between the read and the verdict, or between the verdict and the load. Each is refused, nothing
  of the plugin runs, and no live charge is allowed;
* the entry, a module the plugin imports at load or later, and a data file it reads through
  ``importlib.resources`` all come from the verified bytes, whatever the folder holds afterwards;
  an import of a module of the plugin's package that the manifest does not list is refused;
* the record names the digest of the bytes loaded, equal to the digest verified, and says that a
  file the plugin opens by its path is not covered;
* an error report, a warning and a source lookup of the plugin's code show the verified lines,
  never a file planted in the working folder or a sys.path folder under its code's name; a listed
  module that does not compile shows the line of its verified bytes, or no line.

The stress test runs a swapper thread against many thousands of verify-and-load rounds.
"""

from __future__ import annotations

import inspect
import json
import linecache
import os
from pathlib import Path
import py_compile
import queue
import random
import select
import shutil
import subprocess
import sys
import threading
import time
import traceback
import unittest
import warnings
from unittest import mock

from recursion_core.daemon import plugins
from recursion_core.daemon.plugins import PluginHost
from recursion_core.plugin_security import PluginLoader, ReservedEffects, TrustStore, create_ledger
from recursion_core.plugin_security import package as package_module
from recursion_core.plugin_security.manifest import ENTRY_FILE
from recursion_core.plugin_security.package import verify_package
from recursion_core.plugin_security.tests.support import PLUGIN_SOURCE, FakeGilProbe, Key, needs_crypto
from recursion_core.plugin_security.tests.test_manifest_v1 import sha
from recursion_core.plugin_security.tests.test_whole_package import _charge, _code, _sign_listed, _tree, _write

LINUX = sys.platform.startswith("linux")

# Figures of these tests (none of the product).
# How long a second writer may take to answer one swap, in seconds.
MOVER_REPLY_SECONDS = 10
# The stress test: this many verify-and-load rounds, ended early after this many seconds.
STRESS_ROUNDS = 5000
STRESS_SECONDS = 150
# The swapper restores every file and pauses after this many swaps, for this many seconds, so
# that some rounds find the package whole and load it signed.
STRESS_QUIET_EVERY = 64
STRESS_QUIET_SECONDS = 0.002

CHANGED = "package_changed_while_checked"
PLUGIN_ID = "demo.plugin"

GOOD = {
    ENTRY_FILE: PLUGIN_SOURCE,  # the digest the stand-in authority profile names
    "second.py": b"SECOND = 'verified'\n",
    "lib/__init__.py": b"",
    "lib/late.py": b"LATE = 'verified'\n",
    "data.json": b'{"value": "verified"}\n',
}


def _evil(marker: Path) -> bytes:
    return b"open(%r, 'w').write('ran')\nTAG = SECOND = LATE = 'swapped'\n" % str(marker)


# -- second writers ----------------------------------------------------------------------------

_CHILD = r'''
import json, os, sys
for line in sys.stdin:
    for step in json.loads(line):
        op = step[0]
        if op == "write":
            with open(step[1], "wb") as handle:
                handle.write(bytes.fromhex(step[2]))
            os.chmod(step[1], 0o600)
        elif op == "unlink":
            os.unlink(step[1])
        elif op == "symlink":
            os.symlink(step[1], step[2])
        elif op == "rename":
            os.rename(step[1], step[2])
        elif op == "mkdir":
            os.mkdir(step[1], 0o700)
    sys.stdout.write("ok\n")
    sys.stdout.flush()
'''


def _apply(steps: list) -> None:
    for step in steps:
        op = step[0]
        if op == "write":
            Path(step[1]).write_bytes(bytes.fromhex(step[2]))
            os.chmod(step[1], 0o600)
        elif op == "unlink":
            os.unlink(step[1])
        elif op == "symlink":
            os.symlink(step[1], step[2])
        elif op == "rename":
            os.rename(step[1], step[2])
        elif op == "mkdir":
            os.mkdir(step[1], 0o700)


class _ThreadMover:
    """A second writer on a thread of this process."""

    def __init__(self, test: unittest.TestCase) -> None:
        self._work: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="swap-thread", daemon=True)
        self._thread.start()
        test.addCleanup(self._stop)

    def _run(self) -> None:
        while True:
            item = self._work.get()
            if item is None:
                return
            steps, done = item
            _apply(steps)
            done.set()

    def do(self, steps: list) -> None:
        done = threading.Event()
        self._work.put((steps, done))
        if not done.wait(MOVER_REPLY_SECONDS):
            raise AssertionError("the swap thread did not answer")

    def _stop(self) -> None:
        self._work.put(None)
        self._thread.join(MOVER_REPLY_SECONDS)


class _ProcessMover:
    """A second writer in a second process of the same user."""

    def __init__(self, test: unittest.TestCase) -> None:
        self._proc = subprocess.Popen([sys.executable, "-I", "-c", _CHILD], stdin=subprocess.PIPE,
                                      stdout=subprocess.PIPE, text=True)
        test.addCleanup(self._stop)

    def do(self, steps: list) -> None:
        self._proc.stdin.write(json.dumps(steps) + "\n")
        self._proc.stdin.flush()
        ready, _, _ = select.select([self._proc.stdout], [], [], MOVER_REPLY_SECONDS)
        if not ready or self._proc.stdout.readline().strip() != "ok":
            raise AssertionError("the swap process did not answer")

    def _stop(self) -> None:
        try:
            self._proc.stdin.close()
        except OSError:
            pass
        try:
            self._proc.wait(MOVER_REPLY_SECONDS)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait(MOVER_REPLY_SECONDS)


MOVERS = {"thread": _ThreadMover, "process": _ProcessMover}


def _w(path: Path, data: bytes) -> list:
    return ["write", str(path), data.hex()]


def _swaps(plugin: Path, root: Path, marker: Path) -> dict:
    """kind -> (the file whose read is the moment of the swap, the steps of the swap)."""
    evil = _evil(marker)
    outside = root / "evil.py"
    old = plugin.parent / "demo.old"
    return {
        "entry": (ENTRY_FILE, [_w(plugin / ENTRY_FILE, evil)]),
        "module": ("lib/late.py", [_w(plugin / "lib" / "late.py", evil)]),
        "data": ("data.json", [_w(plugin / "data.json", b'{"value": "swapped"}\n')]),
        "cache": (ENTRY_FILE, [["mkdir", str(plugin / "__pycache__")],
                               _w(plugin / "__pycache__" / "second.cpython-312.pyc", evil)]),
        "link": ("second.py", [_w(outside, evil), ["unlink", str(plugin / "second.py")],
                               ["symlink", str(outside), str(plugin / "second.py")]]),
        "added": (ENTRY_FILE, [_w(plugin / "extra.py", evil)]),
        "folder": (ENTRY_FILE, [["rename", str(plugin / "lib"), str(plugin / "lib.old")],
                                ["mkdir", str(plugin / "lib")], _w(plugin / "lib" / "__init__.py", b""),
                                _w(plugin / "lib" / "late.py", evil)]),
        # The package folder itself renamed away and a new one put at its path, holding the
        # same manifest and signature beside a different plugin.py.
        "package-folder": (ENTRY_FILE, [["rename", str(plugin), str(old)], ["mkdir", str(plugin)]]
                           + [_w(plugin / name, (plugin / name).read_bytes()) for name in
                              ("manifest.json", "manifest.sig", "second.py", "data.json")]
                           + [_w(plugin / ENTRY_FILE, evil)]),
    }


def _ledger(root: Path, installer: Key) -> None:
    store = root / "state" / "plugin-security"
    store.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(root / "state", 0o700)
    os.chmod(store, 0o700)
    create_ledger(str(store / "ledger.sqlite3"), installer.public)


def _host(test: unittest.TestCase, root: Path) -> PluginHost:
    with mock.patch.object(plugins.log, "error"):
        host = PluginHost(root, root / "state", gil_probe=FakeGilProbe(), clock=lambda: 1_000_000)
    test.addCleanup(host.close)
    return host


@unittest.skipUnless(LINUX, "the plugin install path checks ownership by uid")
@needs_crypto
class SwapBetweenCheckAndUseTests(unittest.TestCase):
    """A second writer between the read and the verdict, or between the verdict and the load."""

    def package(self):
        developer, installer = Key(), Key()
        root = _tree(self)
        # plugin.py needs a lib/ folder too: _sign_listed writes nested names through _write.
        plugin = _sign_listed(self, developer, root, dict(GOOD), anchor=installer.public)
        _ledger(root, installer)
        return root, plugin, installer

    def assert_refused(self, host, root, installer, marker, label):
        record, = host.records
        self.assertEqual((record["state"], record["code"]), ("refused", CHANGED), (label, record))
        self.assertIn("changed while they were being checked", record["detail"])
        self.assertIn("nothing of it was loaded and none of its code ran", record["detail"])
        self.assertIn("install the plugin again from its source", record["detail"])
        self.assertFalse(marker.exists(), "%s: swapped code ran" % label)
        self.assertNotIn(PLUGIN_ID, host._verified, label)
        self.assertEqual(_code(lambda: _charge(host.authority, installer, "live", "live-1")),
                         "package_not_signed_whole", label)

    def run_swap(self, kind, moment, mover_name):
        root, plugin, installer = self.package()
        marker = root / "ran.txt"
        trigger, steps = _swaps(plugin, root, marker)[kind]
        mover = MOVERS[mover_name](self)
        label = "%s/%s/%s" % (kind, moment, mover_name)
        if moment == "read":
            real = package_module._read_file
            fired = []

            def read_then_swap(dir_fd, name, shown, limit):
                data = real(dir_fd, name, shown, limit)
                if shown == trigger and not fired:
                    fired.append(1)
                    mover.do(steps)
                return data

            with mock.patch.object(package_module, "_read_file", read_then_swap):
                host = _host(self, root)
            self.assertTrue(fired, label)
        else:
            real = plugins.verify_package

            def verify_then_swap(source, trust):
                result = real(source, trust)
                mover.do(steps)
                return result

            with mock.patch.object(plugins, "verify_package", verify_then_swap):
                host = _host(self, root)
        self.assert_refused(host, root, installer, marker, label)

    def test_every_kind_of_file_swapped_by_a_thread_or_a_process_is_refused(self):
        for kind in ("entry", "module", "data", "cache", "link", "added", "folder", "package-folder"):
            for moment in ("read", "verified"):
                for mover_name in MOVERS:
                    with self.subTest(kind=kind, moment=moment, mover=mover_name):
                        self.run_swap(kind, moment, mover_name)


@unittest.skipUnless(LINUX, "the plugin install path checks ownership by uid")
@needs_crypto
class ReproducerCaseTests(unittest.TestCase):
    """The four cases of the reproducer, S1 to S4, on a two-file signed package."""

    def setUp(self):
        self.developer, self.installer = Key(), Key()
        self.root = _tree(self)
        self.marker = self.root / "ran.txt"

    def make(self, files):
        plugin = _sign_listed(self, self.developer, self.root, files, anchor=self.installer.public)
        _ledger(self.root, self.installer)
        return plugin

    def live(self, host):
        return _code(lambda: _charge(host.authority, self.installer, "live", "live-1"))

    def test_s1_a_file_overwritten_after_its_read_is_refused(self):
        plugin = self.make({ENTRY_FILE: GOOD[ENTRY_FILE], "second.py": b"second = 1\n"})
        real = package_module._read_file

        def swap(dir_fd, name, shown, limit):
            data = real(dir_fd, name, shown, limit)
            if name == "second.py":
                (plugin / name).write_bytes(b"second = 666\n")
            return data

        with mock.patch.object(package_module, "_read_file", swap):
            host = _host(self, self.root)
        record, = host.records
        self.assertEqual((record["state"], record["code"]), ("refused", CHANGED))
        self.assertEqual(self.live(host), "package_not_signed_whole")

    def test_s2_a_file_added_after_the_listing_is_refused(self):
        plugin = self.make({ENTRY_FILE: GOOD[ENTRY_FILE], "second.py": b"second = 1\n"})
        real = package_module._read_file

        def add(dir_fd, name, shown, limit):
            data = real(dir_fd, name, shown, limit)
            if name == ENTRY_FILE:
                _write(plugin, "extra.py", b"x = 1\n")
            return data

        with mock.patch.object(package_module, "_read_file", add):
            host = _host(self, self.root)
        record, = host.records
        self.assertEqual((record["state"], record["code"]), ("refused", CHANGED))
        self.assertEqual(self.live(host), "package_not_signed_whole")

    def test_s2_control_a_file_there_before_the_walk_loads_unsigned(self):
        plugin = self.make({ENTRY_FILE: GOOD[ENTRY_FILE], "second.py": b"second = 1\n"})
        _write(plugin, "extra.py", b"x = 1\n")
        host = _host(self, self.root)
        record, = host.records
        self.assertEqual((record["state"], record["trust"]), ("loaded", "unsigned"))
        self.assertEqual(self.live(host), "package_not_signed_whole")

    def test_s3_an_entry_changed_and_swapped_again_never_runs(self):
        plugin = self.make({ENTRY_FILE: GOOD[ENTRY_FILE], "second.py": b"second = 1\n"})
        _write(plugin, ENTRY_FILE, b"MARK = 1\n")
        real = plugins._read_owned

        def swap(path, limit, **options):
            if Path(path).name == ENTRY_FILE:
                Path(path).write_bytes(_evil(self.marker))
            return real(path, limit, **options)

        with mock.patch.object(plugins, "_read_owned", swap):
            host = _host(self, self.root)
        record, = host.records
        self.assertEqual(record["state"], "refused")
        self.assertIn(record["code"], ("artifact_digest_mismatch", CHANGED))
        self.assertFalse(self.marker.exists(), "swapped code ran")

    def test_s4_a_listed_file_swapped_after_verify_package_is_refused(self):
        plugin = self.root / "plugins" / "demo"
        source = ("exec(compile(open(%r, 'rb').read(), 'second', 'exec'))\n" % str(plugin / "second.py")).encode()
        self.make({ENTRY_FILE: source, "second.py": b"second = 1\n"})
        real = plugins.verify_package

        def verify_then_swap(directory, trust):
            result = real(directory, trust)
            (plugin / "second.py").write_bytes(b"open(%r, 'w').write('ran')\n" % str(self.marker))
            return result

        with mock.patch.object(plugins, "verify_package", verify_then_swap):
            host = _host(self, self.root)
        record, = host.records
        self.assertEqual((record["state"], record["code"]), ("refused", CHANGED))
        self.assertFalse(self.marker.exists(), "swapped code ran")
        self.assertEqual(self.live(host), "package_not_signed_whole")


@unittest.skipUnless(LINUX, "the plugin install path checks ownership by uid")
@needs_crypto
class VerifyPackageSecondListingTests(unittest.TestCase):
    """verify_package alone, with no load after it, answers unsigned for a package that changed
    while it was read and checked, and returns the bytes it read, never the new ones."""

    def verify_with(self, trigger, change):
        key = Key()
        root = _tree(self)
        plugin = _sign_listed(self, key, root, dict(GOOD))
        trust = TrustStore({"dev": key.public})
        self.addCleanup(trust.close)
        real = package_module._read_file

        def read_then_change(dir_fd, name, shown, limit):
            data = real(dir_fd, name, shown, limit)
            if shown == trigger:
                change(plugin)
            return data

        with mock.patch.object(package_module, "_read_file", read_then_change):
            return verify_package(plugin, trust)

    def assert_changed(self, result, path):
        self.assertEqual((result.trust, result.reason), ("unsigned", "package_files_changed"))
        self.assertIsNone(result.signer_key_id)
        self.assertIn(path, getattr(result, "changed_while_checked", ()))
        self.assertFalse(result.intact)

    def test_a_file_overwritten_after_its_read(self):
        result = self.verify_with("second.py", lambda plugin: (plugin / "second.py").write_bytes(b"SECOND = 666\n"))
        self.assert_changed(result, "second.py")
        self.assertEqual(result.files["second.py"], GOOD["second.py"])

    def test_a_file_added_after_the_listing(self):
        result = self.verify_with(ENTRY_FILE, lambda plugin: _write(plugin, "extra.py", b"x = 1\n"))
        self.assert_changed(result, "extra.py")
        self.assertNotIn("extra.py", result.files)

    def test_the_package_folder_renamed_while_it_was_read(self):
        def move(plugin):
            os.rename(plugin, plugin.parent / "demo.old")
            shutil.copytree(plugin.parent / "demo.old", plugin)

        self.assert_changed(self.verify_with(ENTRY_FILE, move), ".")

    def test_an_untouched_package_has_nothing_changed_while_checked(self):
        result = self.verify_with("(no file)", lambda plugin: None)
        self.assertEqual((result.trust, getattr(result, "changed_while_checked", None)), ("signed", ()))


SERVED_ENTRY = b"""from . import second
from .lib import late

VALUE = (second.SECOND, late.LATE)


def later():
    from .lib import later as module
    return module.LATER


def data():
    import importlib.resources
    return importlib.resources.files(__package__).joinpath('data.json').read_bytes()


def unlisted():
    from . import extra
    return extra
"""


@unittest.skipUnless(LINUX, "the plugin install path checks ownership by uid")
@needs_crypto
class ServedFromTheVerifiedBytesTests(unittest.TestCase):
    """The loader takes the plugin's code and data only from the verified bytes."""

    def setUp(self):
        key = Key()
        self.root = _tree(self)
        self.marker = self.root / "ran.txt"
        self.files = dict(GOOD, **{ENTRY_FILE: SERVED_ENTRY, "lib/later.py": b"LATER = 'verified'\n"})
        self.plugin = _sign_listed(self, key, self.root, self.files)
        self.host = _host(self, self.root)
        self.record, = self.host.records

    def module(self):
        self.assertEqual(self.record.get("state"), "loaded", self.record)
        return self.host.loader._active[PLUGIN_ID].module

    def test_listed_modules_and_data_come_from_the_verified_bytes_after_the_folder_changed(self):
        module = self.module()
        self.assertEqual(self.record["trust"], "signed")
        self.assertEqual(module.VALUE, ("verified", "verified"))
        evil = _evil(self.marker)
        for name in ("second.py", "lib/late.py", "lib/later.py"):
            _write(self.plugin, name, evil)
        _write(self.plugin, "data.json", b'{"value": "swapped"}\n')
        # A compiled cache beside the source, of the swapped code, valid for this interpreter.
        py_compile.compile(str(self.plugin / "lib" / "later.py"), doraise=True)
        self.assertEqual(module.later(), "verified")
        self.assertEqual(module.data(), GOOD["data.json"])
        self.assertFalse(self.marker.exists(), "code from the folder ran")
        served = self.host.loader.served(PLUGIN_ID)
        self.assertEqual(served, {path: sha(self.files[path]) for path in
                                  (ENTRY_FILE, "second.py", "lib/__init__.py", "lib/late.py", "lib/later.py")})

    def test_no_bytecode_cache_is_written_into_the_package(self):
        self.module().later()
        caches = [str(path) for path in self.plugin.rglob("__pycache__")]
        self.assertEqual(caches, [])

    def test_an_import_of_an_unlisted_module_of_the_package_is_refused(self):
        module = self.module()
        _write(self.plugin, "extra.py", _evil(self.marker))
        with self.assertRaises(ImportError) as caught:
            module.unlisted()
        self.assertIn("is not a file of this plugin's signed package, so it was not imported", str(caught.exception))
        self.assertIn("Add the file to the package and sign it again.", str(caught.exception))
        self.assertFalse(self.marker.exists())

    def test_the_record_names_the_digest_loaded_equal_to_the_digest_verified(self):
        self.module()
        expected = package_module.files_digest(self.files) if hasattr(package_module, "files_digest") else "-"
        self.assertEqual(self.record.get("package_sha256"), expected)
        self.assertEqual(self.record.get("loaded_sha256"), expected)
        self.assertEqual(self.host._verified[PLUGIN_ID].files_sha256, expected)

    def test_reads_by_path_are_named_not_covered(self):
        self.module()
        self.assertIn("not covered: a file this plugin opens by its path", self.record.get("path_reads", ""))
        self.assertIn("not covered", self.host.inventory().get("path_reads", ""))
        row, = self.host.route_records()
        self.assertIn("files it opens by path are not covered by its signature", row["blocker"])

    def test_own_package_modules_are_not_reported_unverified(self):
        self.module()
        self.assertNotIn("plugin_imported_unverified_modules", [item["code"] for item in self.record["findings"]])


@unittest.skipUnless(LINUX, "the plugin install path checks ownership by uid")
@needs_crypto
class LoaderTakesOnlyTheVerifiedPackageTests(unittest.TestCase):

    def test_a_package_that_changed_while_checked_is_refused_by_the_loader(self):
        key = Key()
        root = _tree(self)
        plugin = _sign_listed(self, key, root, dict(GOOD))
        host = _host(self, root)
        verified = host._verified[PLUGIN_ID]
        if not hasattr(verified, "changed_while_checked"):
            self.fail("VerifiedPackage has no changed_while_checked")
        moved = verified.__class__(**{**verified.__dict__, "changed_while_checked": ("second.py",)})
        loader = PluginLoader(host._trust, {}, ReservedEffects(), host.health, FakeGilProbe())
        self.addCleanup(loader.close)
        with self.assertRaises(Exception) as caught:
            loader.load(moved.manifest.raw, moved.signature, moved.files[ENTRY_FILE], package=moved)
        self.assertEqual(getattr(caught.exception, "code", None), CHANGED)
        self.assertEqual(package_module.package_changes(plugin, verified), ())


BOOM_ENTRY = SERVED_ENTRY + b"""

def boom():
    from .lib import boom as module
    module.boom()


def entry_boom():
    raise RuntimeError('the verified line of the entry')


def warn():
    from .lib import boom as module
    return module.warn()


def load_bad(kind):
    import importlib
    return importlib.import_module(".lib.bad_" + kind, __package__)
"""

BOOM_MODULE = b"""import warnings


def boom():
    raise RuntimeError('the verified line of lib/boom.py')


def warn():
    warnings.warn('the verified warning of lib/boom.py', UserWarning, stacklevel=1)
"""

# What a stranger plants under a code name: a line for every line number the plugin's code has.
PLANTED = b"".join(b"PLANTED_BY_A_STRANGER = %d\n" % number for number in range(1, 61))
# The name a served module's code had before: a relative path, opened by the line cache.
OLD_MODULE_NAME = "<plugin %s>/lib/boom.py" % PLUGIN_ID

# Listed modules that do not compile, one for every kind of error compile() raises for bytes, and
# the line each error must show: the line of the verified bytes, or None where they have none.
BAD_MODULES = {
    "syntax": (b"A = 1\nB = (\n", "B = (\n"),
    "indent": (b"A = 1\ndef f():\nreturn 1\n", "return 1\n"),
    "tab": (b"if 1:\n\tA = 1\n        B = 2\n", "        B = 2\n"),
    "null": (b"A = 1\nB = '\x00'\n", None),
    "encoding": (b"# -*- coding: no-such-codec -*-\nA = 1\n", None),
    "decode": (b"A = 1\nB = '\xff\xfe'\n", None),
}
BAD_FILES = {"lib/bad_%s.py" % kind: source for kind, (source, _) in BAD_MODULES.items()}
BAD_ERRORS = {"indent": IndentationError, "tab": TabError}


@unittest.skipUnless(LINUX, "the plugin install path checks ownership by uid")
@needs_crypto
class ErrorReportsShowTheVerifiedLinesTests(unittest.TestCase):
    """A file planted in the daemon's working folder or a sys.path folder under the name of the
    plugin's code is never shown as the signed plugin's source; the verified lines are."""

    def setUp(self):
        key = Key()
        self.root = _tree(self)
        self.work, self.on_path = _tree(self), _tree(self)
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(self.work)
        sys.path.insert(0, str(self.on_path))
        self.addCleanup(sys.path.remove, str(self.on_path))
        self.files = dict(GOOD, **{ENTRY_FILE: BOOM_ENTRY, "lib/later.py": b"LATER = 'verified'\n",
                                   "lib/boom.py": BOOM_MODULE}, **BAD_FILES)
        _sign_listed(self, key, self.root, self.files)
        self.host = _host(self, self.root)
        self.record, = self.host.records
        self.assertEqual(self.record.get("state"), "loaded", self.record)
        self.assertEqual(self.record["trust"], "signed")
        self.module = self.host.loader._active[PLUGIN_ID].module
        self.plant(OLD_MODULE_NAME)

    def plant(self, name: str) -> None:
        """Write the stranger's text at ``name``, relative to the working folder and a sys.path folder."""
        for folder in (self.work, self.on_path):
            target = folder / name
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(PLANTED)
            except (IsADirectoryError, FileExistsError, NotADirectoryError):
                pass  # the name is a folder of an earlier plant: nothing can be read there

    def raised(self, call) -> RuntimeError:
        """The error ``call`` raises, with its traceback (assertRaises would drop it), after a
        stranger's file is planted under the name of every plugin frame in it."""
        try:
            call()
        except RuntimeError as exc:
            error = exc
        else:
            self.fail("no error was raised")
        frames = traceback.extract_tb(error.__traceback__)
        self.assertTrue([frame for frame in frames if frame.filename.startswith("<plugin")], frames)
        # The longest name first: a module's name may run through a folder named like the entry.
        for name in sorted({frame.filename for frame in frames if frame.filename.startswith("<plugin")},
                           key=len, reverse=True):
            self.plant(name)
        return error

    def report(self, call) -> str:
        error = self.raised(call)
        linecache.checkcache()
        return "".join(traceback.format_exception(error))

    def test_an_error_in_a_served_module_shows_the_verified_lines_not_a_planted_file(self):
        text = self.report(self.module.boom)
        self.assertNotIn("PLANTED_BY_A_STRANGER", text)
        self.assertIn("raise RuntimeError('the verified line of lib/boom.py')", text)
        self.assertIn("module.boom()", text)  # the entry's own frame, from its verified lines
        self.assertIn('File "<plugin demo.plugin #', text)

    def test_an_error_in_the_entry_shows_its_verified_line(self):
        text = self.report(self.module.entry_boom)
        self.assertNotIn("PLANTED_BY_A_STRANGER", text)
        self.assertIn("raise RuntimeError('the verified line of the entry')", text)

    def test_with_the_line_cache_cleared_no_planted_file_is_shown(self):
        error = self.raised(self.module.boom)
        linecache.clearcache()  # another part of the process may clear it: the name alone holds
        text = "".join(traceback.format_exception(error))
        self.assertNotIn("PLANTED_BY_A_STRANGER", text)
        self.assertIn("the verified line of lib/boom.py", text)  # the message, not a source line

    def test_a_warning_shows_the_verified_line(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            self.module.warn()
        warning, = caught
        self.plant(warning.filename)
        linecache.checkcache()
        text = warnings.formatwarning(warning.message, warning.category, warning.filename, warning.lineno)
        self.assertNotIn("PLANTED_BY_A_STRANGER", text)
        self.assertIn("warnings.warn('the verified warning of lib/boom.py', UserWarning, stacklevel=1)", text)

    def test_a_source_lookup_gives_the_verified_source(self):
        self.report(self.module.boom)
        served = sys.modules[self.module.__name__ + ".lib.boom"]
        self.plant(served.boom.__code__.co_filename)
        linecache.checkcache()
        self.assertEqual(inspect.getsource(served.boom),
                         "def boom():\n    raise RuntimeError('the verified line of lib/boom.py')\n")

    def test_the_lines_leave_the_cache_when_the_plugin_is_taken_out(self):
        self.report(self.module.boom)
        prefix = "<plugin %s #%s" % (PLUGIN_ID, self.module.__name__.rpartition("_")[2])
        names = sorted(name for name in linecache.cache if str(name).startswith(prefix))
        self.assertEqual(names, sorted(prefix + suffix for suffix in
                                       (">", ": second.py>", ": lib/__init__.py>", ": lib/late.py>", ": lib/boom.py>")))
        self.host.loader.close()
        self.assertEqual([name for name in linecache.cache if str(name).startswith(prefix)], [])

    def compile_error(self, kind: str) -> SyntaxError:
        """The error of importing the listed module lib/bad_KIND.py, which does not compile, after a
        stranger's file is planted under that module's code name. The interpreter may read the
        error's line from such a file; neither the report nor the error may hold that text."""
        name = "<plugin %s #%s: lib/bad_%s.py>" % (PLUGIN_ID, self.module.__name__.rpartition("_")[2], kind)
        self.plant(name)
        try:
            self.module.load_bad(kind)
        except SyntaxError as exc:
            error = exc
        else:
            self.fail("lib/bad_%s.py compiled" % kind)
        self.assertIsInstance(error, BAD_ERRORS.get(kind, SyntaxError))
        linecache.checkcache()
        report = "".join(traceback.format_exception(error))
        self.assertNotIn("PLANTED_BY_A_STRANGER", report + repr(error.args) + repr(error.text))
        verified = BAD_MODULES[kind][1]
        self.assertEqual(error.text, verified)
        if verified is not None:
            self.assertIn(verified.strip(), "".join(traceback.format_exception_only(error)))
        return error

    def test_a_syntax_error_in_a_listed_module_shows_its_verified_line_not_a_planted_file(self):
        self.compile_error("syntax")

    def test_an_indentation_error_shows_its_verified_line_not_a_planted_file(self):
        self.compile_error("indent")

    def test_a_tab_error_shows_its_verified_line_not_a_planted_file(self):
        self.compile_error("tab")

    def test_a_null_byte_shows_no_line_and_no_planted_file(self):
        self.compile_error("null")

    def test_an_encoding_line_naming_no_codec_shows_no_line_and_no_planted_file(self):
        self.compile_error("encoding")

    def test_bytes_that_do_not_decode_show_no_line_and_no_planted_file(self):
        self.compile_error("decode")


STRESS_ENTRY = b"""try:
    from . import second
except ImportError:
    second = None
if second is None:
    # Without a served import (the base this test is red on), the listed file is read by path.
    _scope = {}
    exec(compile(open(%r, 'rb').read(), 'second', 'exec'), _scope)
    TAG = _scope['TAG']
else:
    TAG = second.TAG
"""


@unittest.skipUnless(LINUX, "the plugin install path checks ownership by uid")
@needs_crypto
class SwapperThreadStressTests(unittest.TestCase):
    """A thread replaces and adds listed files in a loop while the main thread verifies and loads."""

    def test_no_round_runs_swapped_code_or_calls_other_bytes_signed(self):
        key = Key()
        root = _tree(self)
        plugin = root / "plugins" / "demo"
        good_second, evil_second = b"TAG = 'verified'\n", b"TAG = 'swapped!'\n"  # the same size
        entry = STRESS_ENTRY % str(plugin / "second.py")
        files = {ENTRY_FILE: entry, "second.py": good_second}
        _sign_listed(self, key, root, files)
        evil_entry = b"TAG = 'swapped!'\n" + b"#" * max(0, len(entry) - 18) + b"\n"
        host = _host(self, root)
        stop = threading.Event()
        swaps = [0]

        def swapper():
            rng = random.Random(712)
            while not stop.is_set():
                choice = rng.randrange(6)
                try:
                    if choice == 0:
                        (plugin / "second.py").write_bytes(evil_second)
                    elif choice == 1:
                        (plugin / "second.py").write_bytes(good_second)
                    elif choice == 2:
                        (plugin / ENTRY_FILE).write_bytes(evil_entry)
                    elif choice == 3:
                        (plugin / ENTRY_FILE).write_bytes(entry)
                    elif choice == 4:
                        _write(plugin, "extra.py", b"x = 1\n")
                    else:
                        (plugin / "extra.py").unlink(missing_ok=True)
                except OSError:
                    pass
                swaps[0] += 1
                if swaps[0] % STRESS_QUIET_EVERY == 0:
                    (plugin / "second.py").write_bytes(good_second)
                    (plugin / ENTRY_FILE).write_bytes(entry)
                    (plugin / "extra.py").unlink(missing_ok=True)
                    time.sleep(STRESS_QUIET_SECONDS)

        thread = threading.Thread(target=swapper, name="stress-swapper", daemon=True)
        counts = {"rounds": 0, "signed": 0, "unsigned": 0, "refused": 0, "swapped_ran_signed": 0,
                  "served_not_verified": 0, "digest_differs": 0}
        deadline = time.monotonic() + STRESS_SECONDS
        thread.start()
        try:
            with mock.patch.object(plugins.log, "error"), mock.patch.object(plugins.log, "info"):
                while counts["rounds"] < STRESS_ROUNDS and time.monotonic() < deadline:
                    old = host.loader
                    if hasattr(old, "close"):
                        old.close()
                    host.loader = PluginLoader(host._trust, {}, ReservedEffects(), host.health, FakeGilProbe())
                    host._verified.clear()
                    record = host._load(plugin)
                    counts["rounds"] += 1
                    if record["state"] != "loaded":
                        counts["refused"] += 1
                        continue
                    counts[record["trust"]] += 1
                    if record["trust"] != "signed":
                        continue
                    module = host.loader._active[PLUGIN_ID].module
                    if module.TAG != "verified":
                        counts["swapped_ran_signed"] += 1
                    served = host.loader.served(PLUGIN_ID) if hasattr(host.loader, "served") else None
                    if served != {ENTRY_FILE: sha(entry), "second.py": sha(good_second)}:
                        counts["served_not_verified"] += 1
                    if record.get("loaded_sha256") is None or record.get("loaded_sha256") != record.get("package_sha256"):
                        counts["digest_differs"] += 1
        finally:
            stop.set()
            thread.join(MOVER_REPLY_SECONDS)
            if hasattr(host.loader, "close"):
                host.loader.close()
        counts["swaps"] = swaps[0]
        print("stress:", json.dumps(counts, sort_keys=True), file=sys.stderr)
        self.assertGreater(counts["signed"], 0, counts)
        self.assertEqual(counts["swapped_ran_signed"], 0, counts)
        self.assertEqual(counts["served_not_verified"], 0, counts)
        self.assertEqual(counts["digest_differs"], 0, counts)


if __name__ == "__main__":
    unittest.main()
