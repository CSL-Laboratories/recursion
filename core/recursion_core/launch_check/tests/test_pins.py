"""CSL-732 (B10, R2a): pinned wrapper routes and the dropped Gemini CLI row (candidate tests).

Lucas's owner call of 2026-11-09: Pi, Hermes and Cursor run as beta when the launcher and
the installed package files match a pin; Antigravity CLI (``agy``) when its version, binary
and launcher match a pin; any mismatch is refused until re-pinned. Gemini CLI is not an
engine here (his call of the same day: drop the row). Every install below is a fake built in a temp dir and measured there. No real
engine is run and no pin in these tests is a real one.

Lucas's owner calls of 2026-11-10 (csl-research PR #1159 comment 6777082374): "pins stay
measured from a real install only for Nov 11; no archive-derived pins", and "accept a pinned
.cmd/.ps1 launcher as a check, but execute the pinned entry directly". His answers of the
same day add ``interpreter_unpinned`` (an interpreter outside the pinned tree), the check-only
rule for a bare or .exe path that resolves to a script, and the child_env-only environment.

Run from the repository root:
``PYTHONPATH=core python3 -B -m unittest discover -s core/recursion_core/launch_check/tests -t core -v``.
"""

from __future__ import annotations

import dataclasses
import datetime
import importlib
import os
import shutil
import unittest
from unittest import mock

from recursion_core.launch_check import (
    FLOORS,
    PIN_TABLE_VERSION,
    PINS,
    REFUSAL_CODES,
    REFUSAL_CODES_VERSION,
    Grant,
    LaunchRefused,
    Pin,
    detect,
    grant,
    launch_check,
    launch_plan,
    parse_version,
    table_record,
    tree_sha256,
)
from recursion_core.launch_check import binary as binary_mod
from recursion_core.launch_check import pins as pins_mod
from recursion_core.launch_check.binary import HEX64
from recursion_core.launch_check.floors import check_floors
from recursion_core.launch_check.tests.fixtures import Factory

grant_mod = importlib.import_module("recursion_core.launch_check.grant")
detect_mod = importlib.import_module("recursion_core.launch_check.detect")

F: Factory
PINNED = {"antigravity": "binary+launcher", "cursor": "launcher+tree", "pi": "launcher+tree", "hermes": "launcher+tree"}


def setUpModule() -> None:
    global F
    F = Factory()


def tearDownModule() -> None:
    F.cleanup()


def write(path: str, text: str, mode: int = 0o644) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
    os.chmod(path, mode)
    return path


@dataclasses.dataclass
class Install:
    engine: str
    path: str  # the PATH hit handed to grant()
    target: str  # the pin's relative target
    root: str  # absolute package root or binary
    version: str
    ran: str  # a file the launcher would create if anything ran it
    entry: tuple[str, ...] = ()  # the pin's entry, relative to root
    interpreter: str = ""  # the pin's interpreter outside the tree, if any

    @property
    def bindir(self) -> str:
        return os.path.dirname(self.path)

    def pin(self, version: str | None = None) -> Pin:
        return pins_mod.measure(
            self.engine, PINNED[self.engine], self.path, self.target, version=version or self.version,
            platform="test", measured="2026-11-09", source="fake install built by test_pins.py", entry=self.entry,
            interpreter=self.interpreter,
        )


def pi_install() -> Install:
    """npm global shape: bin/pi -> ../lib/node_modules/<pkg>/dist/cli.js."""
    prefix = F.dir()
    ran = os.path.join(prefix, "ran")
    pkg = os.path.join(prefix, "lib", "node_modules", "@mariozechner", "pi-coding-agent")
    write(os.path.join(pkg, "dist", "cli.js"), f"#!/bin/sh\ntouch {ran}\n", 0o755)
    write(os.path.join(pkg, "dist", "main.js"), "export const main = 1;\n")
    write(os.path.join(pkg, "package.json"), '{"name": "@mariozechner/pi-coding-agent", "version": "0.30.2"}\n')
    write(os.path.join(pkg, "node_modules", "dep", "index.js"), "module.exports = 1;\n")
    os.makedirs(os.path.join(pkg, "node_modules", ".bin"))
    os.symlink("../dep/index.js", os.path.join(pkg, "node_modules", ".bin", "dep"))
    os.makedirs(os.path.join(prefix, "bin"))
    link = os.path.join(prefix, "bin", "pi")
    os.symlink("../lib/node_modules/@mariozechner/pi-coding-agent/dist/cli.js", link)
    return Install("pi", link, "..", pkg, "0.30.2", ran)


WIN_ENTRY = ("node.exe", "dist/cli.js")


def pi_windows_install(ext: str = ".cmd", entry: tuple[str, ...] = WIN_ENTRY) -> Install:
    """Pi's Windows installer shape: agent/bin/pi.cmd and pi.ps1 beside agent/install/releases/<v>.

    Unlike the real Pi, whose Node is on PATH, this fake tree holds its own ``node.exe`` so
    the pin can carry an entry (``entry=()`` builds the pin without one).
    """
    home = F.dir()
    ran = os.path.join(home, "ran")
    root = os.path.join(home, "agent", "install", "releases", "1.5.1")
    write(os.path.join(root, "dist", "cli.js"), "console.log(1);\n")
    F.make("node.exe", "v22.0.0", where=root, dump=ran)
    write(os.path.join(root, "package.json"), '{"version": "1.5.1"}\n')
    bindir = os.path.join(home, "agent", "bin")
    write(os.path.join(bindir, "pi.cmd"), '@echo off\r\nnode "%~dp0..\\install\\releases\\1.5.1\\dist\\cli.js" %*\r\n', 0o755)
    write(os.path.join(bindir, "pi.ps1"), '& node "$PSScriptRoot\\..\\install\\releases\\1.5.1\\dist\\cli.js" @args\n', 0o755)
    return Install("pi", os.path.join(bindir, "pi" + ext), "../install/releases/1.5.1", root, "1.5.1", ran, entry)


def entry_reals(inst: Install) -> list[str]:
    return [os.path.realpath(os.path.join(inst.root, *e.split("/"))) for e in inst.entry]


def flip_first_byte(path: str) -> None:
    with open(path, "r+b") as f:
        first = f.read(1)
        f.seek(0)
        f.write(bytes([first[0] ^ 1]))


def windows():
    """Simulate Windows through the one seam; os.name and os.path stay POSIX."""
    return mock.patch.object(binary_mod, "is_windows", return_value=True)


def hermes_install() -> Install:
    """venv shape: bin/hermes entry point, package under lib/.../site-packages."""
    venv = F.dir()
    ran = os.path.join(venv, "ran")
    pkg = os.path.join(venv, "lib", "python3.13", "site-packages", "hermes_agent")
    write(os.path.join(pkg, "__init__.py"), '__version__ = "0.9.1"\n')
    write(os.path.join(pkg, "cli.py"), "def main():\n    return 0\n")
    launcher = write(os.path.join(venv, "bin", "hermes"), f"#!/bin/sh\ntouch {ran}\n", 0o755)
    return Install("hermes", launcher, "../lib/python3.13/site-packages/hermes_agent", pkg, "0.9.1", ran)


def cursor_install(name: str = "agent", version: str = "2026.10.22-27dcf5f") -> Install:
    """Cursor shape: bin/agent -> versions/<v>/cursor-agent, a script beside its own node."""
    home = F.dir()
    ran = os.path.join(home, "ran")
    root = os.path.join(home, "share", "cursor-agent", "versions", "2026.10.22-27dcf5f")
    write(os.path.join(root, "cursor-agent"), f"#!/bin/sh\ntouch {ran}\n", 0o755)
    write(os.path.join(root, "index.js"), "console.log(1);\n")
    F.make("node", "v22.0.0", where=root, dump=ran)
    os.makedirs(os.path.join(home, "bin"))
    link = os.path.join(home, "bin", name)
    os.symlink(os.path.join(root, "cursor-agent"), link)
    return Install("cursor", link, ".", root, version, ran)


def agy_install() -> Install:
    """Antigravity's installer shape: one native binary copied to bin/agy."""
    d = F.dir()
    ran = os.path.join(d, "ran")
    os.makedirs(os.path.join(d, "bin"))
    binary = F.make("agy", "1.5.3", where=os.path.join(d, "bin"), dump=ran)
    return Install("antigravity", binary, "", binary, "1.5.3", ran)


def agy_launcher_install() -> Install:
    """A launcher script in front of the binary: both are pinned."""
    d = F.dir()
    ran = os.path.join(d, "ran")
    os.makedirs(os.path.join(d, "lib"))
    binary = F.make("antigravity", "1.5.3", where=os.path.join(d, "lib"), dump=ran)
    launcher = write(os.path.join(d, "bin", "agy"), '#!/bin/sh\nexec "$(dirname "$0")/../lib/antigravity" "$@"\n', 0o755)
    return Install("antigravity", launcher, "../lib/antigravity", binary, "1.5.3", ran)


def agy_windows_install(ext: str = ".cmd") -> Install:
    """A Windows script launcher (bin/agy.cmd or agy.ps1) in front of the native binary: both are pinned."""
    d = F.dir()
    ran = os.path.join(d, "ran")
    os.makedirs(os.path.join(d, "lib"))
    binary = F.make("antigravity.exe", "1.5.3", where=os.path.join(d, "lib"), dump=ran)
    text = {".cmd": '@echo off\r\n"%~dp0..\\lib\\antigravity.exe" %*\r\n', ".ps1": '& "$PSScriptRoot\\..\\lib\\antigravity.exe" @args\n'}[ext]
    launcher = write(os.path.join(d, "bin", "agy" + ext), text, 0o755)
    return Install("antigravity", launcher, "../lib/antigravity.exe", binary, "1.5.3", ran)


BUILDERS = {
    "pi": pi_install, "hermes": hermes_install, "cursor": cursor_install,
    "antigravity": agy_install, "antigravity (launcher)": agy_launcher_install,
}
TREE_BUILDERS = {k: BUILDERS[k] for k in ("pi", "hermes", "cursor")}


def table(*pins: Pin):
    return mock.patch.object(pins_mod, "PINS", tuple(pins))


class Base(unittest.TestCase):
    def refused(self, code: str, fn, *a, **kw) -> LaunchRefused:
        with self.assertRaises(LaunchRefused) as cm:
            fn(*a, **kw)
        self.assertEqual(code, cm.exception.code, cm.exception)
        self.assertIn(cm.exception.code, REFUSAL_CODES)
        return cm.exception

    def never_probe(self, spec, realpath, timeout):
        self.fail(f"a pinned route ran {realpath}")

    def g(self, inst: Install) -> Grant:
        return grant(inst.engine, inst.path, probe=self.never_probe)


class PinTableTests(unittest.TestCase):
    def test_pinned_engines_and_kinds(self) -> None:
        self.assertEqual(PINNED, {e: s.pin for e, s in FLOORS.items() if s.pin})
        self.assertEqual(set(pins_mod.KINDS), set(PINNED.values()))
        for engine in PINNED:
            self.assertIsNone(FLOORS[engine].functional, engine)  # beta until measured (O50)
            self.assertIsNone(FLOORS[engine].probe_name, engine)

    def test_table_version_and_record(self) -> None:
        self.assertRegex(PIN_TABLE_VERSION, r"^r2a-pins/\d+\.\d+\.\d+-candidate-\d{4}-\d{2}-\d{2}$")
        rec = table_record()
        self.assertEqual(PIN_TABLE_VERSION, rec["pin_table"])
        self.assertEqual(len(PINS), len(rec["pins"]))
        for engine, kind in PINNED.items():
            self.assertEqual(kind, rec["engines"][engine]["pin"])
            self.assertEqual("pin", rec["engines"][engine]["identity"])

    def test_every_committed_pin_is_a_full_measurement(self) -> None:
        # No invented or shortened hash, and no pin for an engine that is not a pinned route.
        for p in PINS:
            with self.subTest(engine=p.engine, version=p.version):
                self.assertIn(p.engine, PINNED)
                self.assertTrue(HEX64.fullmatch(p.launcher_sha256))
                self.assertTrue(HEX64.fullmatch(p.target_sha256))
                self.assertIs(int, type(p.launcher_bytes))
                self.assertGreater(p.launcher_bytes, 0)
                parsed = parse_version(FLOORS[p.engine].scheme, p.version)
                self.assertIsNotNone(parsed)
                check_floors(FLOORS[p.engine], parsed)
                datetime.date.fromisoformat(p.measured)
                self.assertTrue(p.platform and p.source)
                self.assertFalse(pins_mod.archive_sourced(p.source))
                self.assertEqual((), p.entry)  # no entry has been measured
                # Pi's launchers run the Node found on PATH; Cursor's node.exe is inside its tree.
                self.assertEqual("node (PATH)" if p.engine == "pi" else "", p.interpreter)
                if p.target == "":
                    self.assertEqual("binary+launcher", PINNED[p.engine])
                    self.assertEqual(p.launcher_sha256, p.target_sha256)
        keys = [(p.engine, p.launcher_sha256, p.target, p.target_sha256) for p in PINS]
        self.assertEqual(len(keys), len(set(keys)))

    def test_committed_table_has_no_antigravity_pin_and_no_archive_source(self) -> None:
        # Lucas, 2026-11-10: "pins stay measured from a real install only for Nov 11; no archive-derived pins".
        # The 1.5.3 linux_amd64 row was hashed from the release archive, so it is gone until an installed agy is measured.
        self.assertEqual("r2a-pins/1.5.0-candidate-2026-11-10", PIN_TABLE_VERSION)
        self.assertFalse([p for p in PINS if p.engine == "antigravity"])
        self.assertNotIn("antigravity", {r["engine"] for r in table_record()["pins"]})
        self.assertTrue(PINS)
        for p in PINS:
            with self.subTest(engine=p.engine, source=p.source):
                self.assertFalse(pins_mod.archive_sourced(p.source))
        self.assertEqual("binary+launcher", FLOORS["antigravity"].pin)  # the route and its pin kind stay

    def test_archive_sourced_reads_the_source_path(self) -> None:
        base = "https://vendor.example/releases/1.2.3/cli_linux_x64"
        for suffix in pins_mod.ARCHIVE_SUFFIXES:
            for source in (base + suffix, base + suffix.upper(), base + suffix + "?alt=media", base + suffix + "#sha",
                           base + suffix + "?a=1#b", "cli" + suffix, "C:\\Users\\x\\Downloads\\cli" + suffix):
                with self.subTest(source=source):
                    self.assertTrue(pins_mod.archive_sourced(source))
        self.assertEqual(
            {".tar.gz", ".tgz", ".tar.xz", ".txz", ".tar.bz2", ".tbz2", ".tar.zst", ".tar", ".zip", ".7z"},
            set(pins_mod.ARCHIVE_SUFFIXES))
        for source in ("https://cursor.com/install?win32=true", "https://pi.dev/install.ps1", "fake install built by test_pins.py",
                       "installed agy on a linux_amd64 host", "https://vendor.example/install?file=cli.tar.gz",
                       "https://vendor.example/install#cli.zip", "https://vendor.example/tar", ""):
            with self.subTest(source=source):
                self.assertFalse(pins_mod.archive_sourced(source))
        self.assertTrue(pins_mod.archive_sourced(None))  # table bug: fail closed

    def test_measure_refuses_an_archive_source_and_a_bad_entry(self) -> None:
        inst = pi_windows_install()
        kw = dict(version="1.5.1", platform="test", measured="2026-11-10")
        self.assertEqual(WIN_ENTRY, inst.pin().entry)
        for source, entry in (("https://vendor.example/pi.zip", ()), ("fake", ("../x.js",)), ("fake", ("dist/missing.js",)),
                              ("fake", ("dist",))):
            with self.subTest(source=source, entry=entry), self.assertRaises(ValueError):
                pins_mod.measure("pi", "launcher+tree", inst.path, inst.target, source=source, entry=entry, **kw)
        agy = agy_install()
        with self.assertRaises(ValueError):
            pins_mod.measure("antigravity", "binary+launcher", agy.path, "", source="fake", entry=("agy",), **kw)

    def test_committed_windows_amd64_cursor_and_pi_pins_have_the_measured_values(self) -> None:
        # Verified fixtures, measured 2026-11-09 on Lucas's Windows machine "sentry" (hasher at 66fe11b1);
        # the trees are not committed. Reachable on Windows through the pinned .cmd/.ps1 launcher rule.
        self.assertEqual("r2a-pins/1.5.0-candidate-2026-11-10", PIN_TABLE_VERSION)
        self.assertEqual("r2a-refusals/1.7.0-candidate-2026-11-10", REFUSAL_CODES_VERSION)
        cursor = Pin(
            engine="cursor",
            version="2026.11.05-86047df",
            launcher_sha256="b5975caa126e855f4be9b6084791a47eb40dd0babccb297265bf131df033acfc",
            launcher_bytes=2527,
            target="versions/2026.11.05-86047df",
            target_sha256="d9088fb279e9a004d4098b927df9b1257cd05b367cdc0959ba11d082e291bf23",
            platform="windows_amd64",
            measured="2026-11-09",
            source="https://cursor.com/install?win32=true",
        )
        pi = Pin(
            engine="pi",
            version="1.5.1",
            launcher_sha256="c56ef3cdd441b3c00d49e6e36d4a163bbcc715bf32e4de3473ece96efd232c4b",
            launcher_bytes=464,
            target="../install/releases/1.5.1",
            target_sha256="2c477ce6abcd257434138b4affd6f9b12b92ec2527bc8eb50f04948a25323ac7",
            platform="windows_amd64",
            measured="2026-11-09",
            source="https://pi.dev/install.ps1",
            interpreter="node (PATH)",
        )
        pi_ps1 = Pin(
            engine="pi",
            version="1.5.1",
            launcher_sha256="3f422e96483162aad373b8e3e28f282236bdffb5cc1b8bfb6c25a8d918bd31d1",
            launcher_bytes=500,
            target="../install/releases/1.5.1",
            target_sha256="2c477ce6abcd257434138b4affd6f9b12b92ec2527bc8eb50f04948a25323ac7",
            platform="windows_amd64",
            measured="2026-11-09",
            source="https://pi.dev/install.ps1",
            interpreter="node (PATH)",
        )
        cells = (
            (cursor, "/u/cursor-agent/cursor-agent.ps1", "/u/cursor-agent/versions/2026.11.05-86047df", "2026.11.06-86047df"),
            (pi, "/u/.pi/agent/bin/pi.cmd", "/u/.pi/agent/install/releases/1.5.1", "1.5.2"),
            (pi_ps1, "/u/.pi/agent/bin/pi.ps1", "/u/.pi/agent/install/releases/1.5.1", "1.5.2"),
        )
        for pin, launcher, root, other_version in cells:
            with self.subTest(engine=pin.engine, launcher=launcher):
                self.assertIn(pin, PINS)
                self.assertTrue(pins_mod.listed(pin.engine, "launcher+tree", pin.version, pin.launcher_sha256, pin.target_sha256))
                self.assertFalse(pins_mod.listed(pin.engine, "launcher+tree", other_version, pin.launcher_sha256, pin.target_sha256))
                # No entry has been measured, so a grant on this launcher would refuse pin_entry_unset at launch;
                # Pi's refuses interpreter_unpinned first (its Node is on PATH, outside the tree).
                self.assertEqual((), pin.entry)
                self.assertEqual("node (PATH)" if pin.engine == "pi" else "", pin.interpreter)
                # The target is a plain relative path: no ValueError.
                self.assertEqual(os.path.realpath(root), pins_mod.resolve_target(launcher, pin.target))
        self.assertTrue(pins_mod.listed(
            "pi", "launcher+tree", "1.5.1", "3f422e96483162aad373b8e3e28f282236bdffb5cc1b8bfb6c25a8d918bd31d1",
            "2c477ce6abcd257434138b4affd6f9b12b92ec2527bc8eb50f04948a25323ac7"))
        self.assertFalse(pins_mod.listed(
            "pi", "launcher+tree", "1.5.2", "3f422e96483162aad373b8e3e28f282236bdffb5cc1b8bfb6c25a8d918bd31d1",
            "2c477ce6abcd257434138b4affd6f9b12b92ec2527bc8eb50f04948a25323ac7"))
        self.assertEqual([cursor], [p for p in PINS if p.engine == "cursor"])
        # Pi has exactly the two windows_amd64 launchers, pi.cmd then pi.ps1, over one tree.
        self.assertEqual([pi, pi_ps1], [p for p in PINS if p.engine == "pi"])
        self.assertEqual((pi.target, pi.target_sha256, pi.platform), (pi_ps1.target, pi_ps1.target_sha256, pi_ps1.platform))
        self.assertEqual({"cursor", "pi"}, {p.engine for p in PINS})
        self.assertEqual(3, len(PINS))
        # Hermes was measured on Windows for its launchers only; no tree hash exists, so no pin.
        self.assertFalse([p for p in PINS if p.engine == "hermes"])

    def test_target_must_be_a_plain_relative_path(self) -> None:
        self.assertEqual("/a/b/agy", pins_mod.resolve_target("/a/b/agy", ""))
        self.assertEqual(os.path.realpath("/a/b"), pins_mod.resolve_target("/a/b/agy", "."))
        self.assertEqual(os.path.realpath("/a/lib/x"), pins_mod.resolve_target("/a/b/agy", "../lib/x"))
        for bad in ("/etc", "a//b", "./a", "a/./b", "a\\b", "a/"):
            with self.subTest(target=bad), self.assertRaises(ValueError):
                pins_mod.resolve_target("/a/b/agy", bad)


@unittest.skipUnless(os.name == "posix", "POSIX fixtures")
class TreeDigestTests(unittest.TestCase):
    def test_same_tree_same_digest_and_every_change_differs(self) -> None:
        inst = pi_install()
        before = tree_sha256(inst.root)
        self.assertTrue(HEX64.fullmatch(before))
        self.assertEqual(before, tree_sha256(inst.root))
        copy = os.path.join(F.dir(), "copy")
        shutil.copytree(inst.root, copy, symlinks=True)
        self.assertEqual(before, tree_sha256(copy))
        main = os.path.join(copy, "dist", "main.js")
        changes = {
            "edit": lambda: write(main, "export const main = 2;\n"),
            "add": lambda: write(os.path.join(copy, "dist", "extra.js"), ""),
            "remove": lambda: os.unlink(os.path.join(copy, "node_modules", "dep", "index.js")),
            "rename": lambda: os.rename(main, os.path.join(copy, "dist", "main2.js")),
            "exec bit": lambda: os.chmod(main, 0o755),
            "empty dir": lambda: os.makedirs(os.path.join(copy, "dist", "empty")),
            "link text": lambda: (os.unlink(os.path.join(copy, "node_modules", ".bin", "dep")),
                                  os.symlink("../dep/other.js", os.path.join(copy, "node_modules", ".bin", "dep"))),
            "file to link": lambda: (os.unlink(main), os.symlink("cli.js", main)),
        }
        for name, change in changes.items():
            with self.subTest(change=name):
                shutil.rmtree(copy)
                shutil.copytree(inst.root, copy, symlinks=True)
                change()
                self.assertNotEqual(before, tree_sha256(copy))

    def test_links_are_pinned_by_text_and_never_followed(self) -> None:
        outside = F.dir()
        write(os.path.join(outside, "big.js"), "1\n")
        root = F.dir()
        os.symlink(outside, os.path.join(root, "out"))
        before = tree_sha256(root)
        write(os.path.join(outside, "big.js"), "2\n")
        write(os.path.join(outside, "new.js"), "3\n")
        self.assertEqual(before, tree_sha256(root))

    def test_odd_file_types_and_caps_are_refused(self) -> None:
        root = F.dir()
        write(os.path.join(root, "a.js"), "1\n")
        write(os.path.join(root, "b.js"), "2\n")
        with mock.patch.object(pins_mod, "MAX_TREE_ENTRIES", 1), self.assertRaises(ValueError):
            tree_sha256(root)
        with mock.patch.object(pins_mod, "MAX_TREE_BYTES", 3), self.assertRaises(ValueError):
            tree_sha256(root)
        os.mkfifo(os.path.join(root, "pipe"))
        with self.assertRaises(ValueError):
            tree_sha256(root)


@unittest.skipUnless(os.name == "posix", "POSIX fixtures")
class PinnedGrantTests(Base):
    def test_matching_pin_runs_as_beta_and_nothing_is_executed(self) -> None:
        for name, build in BUILDERS.items():
            with self.subTest(install=name):
                inst = build()
                with table(inst.pin()):
                    g = self.g(inst)
                    rec = g.record()
                    self.assertEqual("granted", rec["outcome"])
                    self.assertTrue(rec["beta"])
                    self.assertEqual(inst.engine, g.engine)
                    self.assertEqual(inst.version, g.version_raw)
                    self.assertEqual(PINNED[inst.engine], g.pin_kind)
                    self.assertEqual(PIN_TABLE_VERSION, g.pin_table)
                    self.assertEqual(os.path.realpath(inst.root), g.pin_target)
                    self.assertEqual(os.path.realpath(inst.path), g.realpath)
                    self.assertTrue(HEX64.fullmatch(g.sha256) and HEX64.fullmatch(g.pin_sha256))
                    argv0 = FLOORS[inst.engine].names[0]
                    plan = launch_plan(g, [argv0, "-p", "hi"])
                    for _ in range(3):
                        self.assertEqual([g.realpath, "-p", "hi"], plan.exec_argv())
                self.assertFalse(os.path.exists(inst.ran), "a pinned route was executed")

    def test_no_pin_recorded_refuses_every_pinned_route(self) -> None:
        for name, build in BUILDERS.items():
            with self.subTest(install=name):
                inst = build()
                with table():
                    r = self.refused("pin_mismatch", self.g, inst)
                    self.assertIn("no pin is recorded", r.detail)
                other = build()
                with table(other.pin()):  # another install's pin is not this install's
                    self.refused("pin_mismatch", self.g, inst)
                self.assertFalse(os.path.exists(inst.ran))

    def test_native_file_with_a_pinned_name_is_refused_unrun(self) -> None:
        # A name plus a version line was enough for agy and agent before; now only a pin is.
        with table(pi_install().pin(), cursor_install().pin(), agy_install().pin(), hermes_install().pin()):
            for engine, name, line in (("pi", "pi", "0.30.2"), ("cursor", "agent", "2026.10.22"),
                                       ("cursor", "cursor-agent", "2026.10.22"), ("antigravity", "agy", "1.5.3"),
                                       ("antigravity", "agy", "antigravity 1.5.3"), ("hermes", "hermes", "hermes 0.9.1")):
                with self.subTest(engine=engine, line=line):
                    d = F.dir()
                    ran = os.path.join(d, "ran")
                    p = F.make(name, line, where=d, dump=ran)
                    r = self.refused("pin_mismatch", grant, engine, p, probe=self.never_probe)
                    self.assertIn("refused until re-pinned", r.detail)
                    self.assertFalse(os.path.exists(ran))

    def test_launcher_mismatch_refuses_until_repinned(self) -> None:
        for name, build in BUILDERS.items():
            with self.subTest(install=name):
                inst = build()
                old = inst.pin()
                real = os.path.realpath(inst.path)
                if name == "antigravity":
                    F.write(real, "1.5.3")  # a new build of the binary
                else:
                    with open(real, "a") as f:
                        f.write("# edited\n")
                with table(old):
                    r = self.refused("pin_mismatch", self.g, inst)
                    self.assertIn("refused until re-pinned", r.detail)
                    self.assertIn("matches no pin", r.detail)
                with table(old, inst.pin()):  # someone re-pins
                    self.assertTrue(self.g(inst).record()["beta"])

    def test_package_file_mismatch_refuses_until_repinned(self) -> None:
        for name, build in TREE_BUILDERS.items():
            edits = {
                "edit": lambda root: write(os.path.join(root, "added-or-edited.js"), "x\n"),
                "remove": lambda root: os.unlink(next(
                    os.path.join(dp, f) for dp, _, fs in os.walk(root) for f in sorted(fs)
                    if f not in ("cli.js", "cursor-agent"))),
            }
            for change, edit in edits.items():
                with self.subTest(install=name, change=change):
                    inst = build()
                    old = inst.pin()
                    edit(inst.root)
                    with table(old):
                        r = self.refused("pin_mismatch", self.g, inst)
                        self.assertIn("installed package files", r.detail)
                        self.assertIn("refused until re-pinned", r.detail)
                    with table(inst.pin()):
                        self.assertTrue(self.g(inst).record()["beta"])
                    self.assertFalse(os.path.exists(inst.ran))

    def test_antigravity_binary_mismatch_behind_an_unchanged_launcher(self) -> None:
        inst = agy_launcher_install()
        old = inst.pin()
        F.write(inst.root, "1.5.4")
        with table(old):
            r = self.refused("pin_mismatch", self.g, inst)
            self.assertIn("binary sha256", r.detail)
            self.assertIn("Antigravity CLI (agy)", r.detail)
            self.assertIn("refused until re-pinned", r.detail)
        with table(inst.pin("1.5.4")):
            self.assertEqual("1.5.4", self.g(inst).version)
        # The binary behind the launcher must be a native executable.
        write(inst.root, "#!/bin/sh\n", 0o755)
        with table(old):
            self.assertIn("not a native executable", self.refused("pin_mismatch", self.g, inst).detail)

    def test_pin_version_meets_the_floors(self) -> None:
        inst = cursor_install()
        cells = [("2025.09.16-abcdef0", "version_refused"), ("2025.09.17-25b418f", None), ("2026.10.22", None),
                 ("2026.13.01", "pin_mismatch"), ("2026.10.22-zz", "pin_mismatch"), ("", "pin_mismatch")]
        for version, want in cells:
            with self.subTest(version=version), table(dataclasses.replace(inst.pin(), version=version)):
                if want is None:
                    self.assertEqual(version, self.g(inst).version_raw)
                else:
                    self.refused(want, self.g, inst)
        agy = agy_install()
        with table(agy.pin("1.5.3-rc.1")):
            self.refused("version_refused", self.g, agy)

    def test_usual_path_refusals_still_come_first(self) -> None:
        inst = pi_install()
        with table(inst.pin()):
            self.refused("path_not_absolute", grant, "pi", "pi")
            self.refused("name_mismatch", grant, "pi", os.path.join(inst.bindir, "pie"))
            self.refused("wrapper_refused", grant, "pi", os.path.join(inst.root, "dist", "main.js"))
            self.refused("wrapper_refused", grant, "pi", os.path.realpath(inst.path))  # cli.js is not the name `pi`
            self.refused("binary_missing", grant, "pi", os.path.join(F.dir(), "pi"))
            os.chmod(os.path.realpath(inst.path), 0o644)
            self.refused("binary_missing", self.g, inst)

    def test_bad_pin_target_is_a_mismatch(self) -> None:
        inst = hermes_install()
        pin = inst.pin()
        for target in ("/etc", "../missing", "../lib/python3.13/site-packages/hermes_agent/cli.py"):
            with self.subTest(target=target), table(dataclasses.replace(pin, target=target)):
                self.refused("pin_mismatch", self.g, inst)
        os.mkfifo(os.path.join(inst.root, "pipe"))
        with table(pin):
            self.assertIn("not a file, directory or link", self.refused("pin_mismatch", self.g, inst).detail)

    def test_archive_sourced_pin_is_never_matched(self) -> None:
        # Lucas, 2026-11-10: "pins stay measured from a real install only for Nov 11; no archive-derived pins".
        sources = ("https://vendor.example/cli/1.5.3/linux-x64/cli_linux_x64.tar.gz",
                   "https://vendor.example/cli/CLI.ZIP?alt=media#frag")
        for name, build in BUILDERS.items():
            for source in sources:
                with self.subTest(install=name, source=source):
                    inst = build()
                    good = inst.pin()  # a perfect match for this install
                    archive = dataclasses.replace(good, source=source)
                    argv = [FLOORS[inst.engine].names[0]]
                    with table(archive):
                        r = self.refused("pin_mismatch", self.g, inst)
                        self.assertIn("archive-derived pins are never matched", r.detail)
                        self.assertIn("real install only", r.detail)
                    with table(good):
                        g = self.g(inst)
                        plan = launch_plan(g, argv)
                        self.assertEqual([g.realpath], plan.exec_argv())
                    with table(archive):  # an existing grant re-checks the table on every use
                        for use in (plan.exec_argv, plan.exec_env):
                            self.assertIn("archive-derived", self.refused("pin_mismatch", use).detail)
                        self.refused("pin_mismatch", launch_plan, g, argv)
                    with table(archive, good):  # the measured pin beside it still matches
                        self.assertEqual(g, self.g(inst))
                        self.assertEqual([g.realpath], plan.exec_argv())
                    self.assertFalse(os.path.exists(inst.ran))

    def test_non_windows_launcher_tree_grant_ignores_the_entry_and_execs_the_realpath(self) -> None:
        inst = dataclasses.replace(pi_install(), entry=("dist/cli.js",))
        self.assertFalse(binary_mod.is_windows())
        with table(inst.pin()):
            g = self.g(inst)
            self.assertEqual((), g.entry)
            plan = launch_plan(g, ["pi", "-p", "hi"])
            self.assertEqual([g.realpath, "-p", "hi"], plan.exec_argv())
            self.assertFalse(g.check_only)
        # On Windows a bare name that resolves to a script is check-only (Lucas's answer 5, 2026-11-10).
        with windows(), table(inst.pin()):
            g = self.g(inst)
            self.assertTrue(g.check_only)
            self.assertEqual(tuple(entry_reals(inst)), g.entry)
            self.assertEqual([*g.entry, "-p", "hi"], launch_plan(g, ["pi", "-p", "hi"]).exec_argv())

    def test_entry_on_a_binary_launcher_pin_is_a_table_bug(self) -> None:
        for build in (agy_install, agy_launcher_install):
            inst = build()
            good = inst.pin()
            bad = dataclasses.replace(good, entry=("agy",))
            with self.subTest(install=build.__name__):
                with table(bad):
                    r = self.refused("pin_mismatch", self.g, inst)
                    self.assertIn("only a launcher+tree pin can carry one", r.detail)
                with table(good):
                    plan = launch_plan(self.g(inst), ["agy"])
                with table(bad):
                    self.refused("pin_mismatch", plan.exec_argv)


@unittest.skipUnless(os.name == "posix", "POSIX fixtures")
class PinnedReverifyTests(Base):
    """A pinned grant is re-checked on every plan use: launcher, tree or binary, and the pin table."""

    def test_drift_after_the_grant_is_binary_changed(self) -> None:
        for name, build in BUILDERS.items():
            with self.subTest(install=name):
                inst = build()
                with table(inst.pin()):
                    g = self.g(inst)
                    argv = [FLOORS[inst.engine].names[0]]
                    plan = launch_plan(g, argv)
                    target = inst.root if os.path.isfile(inst.root) else os.path.join(inst.root, "late.js")
                    original = None
                    if os.path.isfile(target):
                        with open(target, "rb") as f:
                            original = f.read()
                    if original is None:
                        write(target, "added after the grant\n")
                    else:
                        F.write(target, "1.5.3")
                    self.refused("binary_changed", plan.exec_argv)
                    self.refused("binary_changed", plan.exec_env)
                    self.refused("binary_changed", launch_plan, g, argv)
                    if original is None:  # identity is the digest: restoring the files restores the grant
                        os.unlink(target)
                    else:
                        with open(target, "wb") as f:
                            f.write(original)
                    self.assertEqual([g.realpath], plan.exec_argv())

    def test_launcher_edit_or_removal_after_the_grant(self) -> None:
        inst = hermes_install()
        with table(inst.pin()):
            plan = launch_plan(self.g(inst), ["hermes"])
            with open(inst.path, "a") as f:
                f.write("# edited\n")
            self.refused("binary_changed", plan.exec_argv)
            os.unlink(inst.path)
            self.refused("binary_missing", plan.exec_argv)

    def test_package_root_swapped_for_a_link(self) -> None:
        inst = hermes_install()
        with table(inst.pin()):
            plan = launch_plan(self.g(inst), ["hermes"])
            moved = inst.root + ".moved"
            os.rename(inst.root, moved)
            self.refused("binary_changed", plan.exec_argv)  # gone
            os.symlink(moved, inst.root)  # same files, another place
            r = self.refused("binary_changed", plan.exec_argv)
            self.assertIn("resolves elsewhere", r.detail)

    def test_pin_removed_or_table_changed_after_the_grant(self) -> None:
        inst = agy_install()
        with table(inst.pin()):
            g = self.g(inst)
            plan = launch_plan(g, ["agy"])
        with table():
            r = self.refused("pin_mismatch", plan.exec_argv)
            self.assertIn("Antigravity CLI (agy)", r.detail)
        with table(inst.pin("1.5.4")):
            self.refused("pin_mismatch", launch_plan, g, ["agy"])
        with table(inst.pin()), mock.patch.object(pins_mod, "PIN_TABLE_VERSION", "r2a-pins/9.9.9-test"):
            self.refused("grant_stale", plan.exec_argv)
        with table(inst.pin()):
            self.assertEqual([g.realpath], plan.exec_argv())

    def test_edited_pin_fields_are_forged(self) -> None:
        inst = agy_launcher_install()
        other = agy_launcher_install()
        with table(inst.pin(), other.pin()):
            g = self.g(inst)
            o = self.g(other)
            for fake in (dataclasses.replace(g, pin_sha256=o.pin_sha256), dataclasses.replace(g, pin_target=o.pin_target),
                         dataclasses.replace(g, pin_kind=""), dataclasses.replace(g, version_raw="9.9.9", version="9.9.9")):
                self.refused("grant_forged", launch_plan, fake, ["agy"])
        # A grant whose pin kind no longer matches the table is refused even with a valid MAC.
        with table(inst.pin()):
            rows = dict(grant_mod.floors.FLOORS)
            rows["antigravity"] = dataclasses.replace(rows["antigravity"], pin=None)
            with mock.patch.object(grant_mod.floors, "FLOORS", rows):
                self.refused("grant_forged", launch_plan, g, ["agy"])


@unittest.skipUnless(os.name == "posix", "POSIX fixtures")
class PinnedDetectTests(Base):
    def test_pinned_route_offers_its_first_hit_of_any_kind(self) -> None:
        inst = pi_install()
        found = detect("pi", inst.bindir)
        self.assertEqual(inst.path, found.candidate)
        self.assertIn("pinned route (launcher+tree): script", found.note)
        self.assertFalse(os.path.exists(inst.ran))
        self.assertIsNone(detect("pi", F.dir()).candidate)

    def test_cursor_prefers_the_name_whose_launcher_is_pinned(self) -> None:
        inst = cursor_install("cursor-agent")
        stray = F.dir()
        F.script(os.path.join(stray, "agent"), "#!/bin/sh\necho some other agent\n")
        path = os.pathsep.join([stray, inst.bindir])
        self.assertEqual(os.path.join(stray, "agent"), detect("cursor", path).candidate)
        with table(inst.pin()):
            self.assertEqual(inst.path, detect("cursor", path).candidate)
            grants, records = launch_check(path, probe=self.never_probe)
            self.assertEqual({"cursor"}, set(grants))

    def test_launch_check_grants_pinned_routes_as_beta(self) -> None:
        installs = [pi_install(), hermes_install(), cursor_install(), agy_install()]
        path = os.pathsep.join(i.bindir for i in installs)
        with table(*(i.pin() for i in installs)):
            grants, records = launch_check(path, probe=self.never_probe)
            by = {r["engine"]: r for r in records}
            self.assertEqual(set(PINNED), set(grants))
            for engine in PINNED:
                self.assertEqual("granted", by[engine]["outcome"], engine)
                self.assertTrue(by[engine]["beta"], engine)
                self.assertEqual(PINNED[engine], by[engine]["pin_kind"])
        with table(installs[0].pin()):
            grants, records = launch_check(path, probe=self.never_probe)
            by = {r["engine"]: r for r in records}
            self.assertEqual({"pi"}, set(grants))
            for engine in set(PINNED) - {"pi"}:
                self.assertEqual("pin_mismatch", by[engine]["refusal"], engine)
        for i in installs:
            self.assertFalse(os.path.exists(i.ran))


@unittest.skipUnless(os.name == "posix", "POSIX fixtures")
class WindowsPinnedLauncherTests(Base):
    """Lucas, 2026-11-09: "Yes, accept pinned .cmd and .ps1 launchers on Windows, bound to the pin."

    Windows is simulated through ``binary.is_windows()``; no test here runs on real Windows.
    """

    EXTS = (".cmd", ".ps1")

    def test_pinned_cmd_and_ps1_launchers_are_granted_and_launch(self) -> None:
        for ext in self.EXTS:
            with self.subTest(ext=ext):
                inst = pi_windows_install(ext)
                pin = inst.pin()
                with windows(), table(pin):
                    g = self.g(inst)
                    self.assertEqual("launcher+tree", g.pin_kind)
                    self.assertEqual((pin.version, pin.version), (g.version_raw, g.version))
                    self.assertEqual(os.path.realpath(inst.root), g.pin_target)
                    self.assertEqual(pin.target_sha256, g.pin_sha256)
                    self.assertEqual((inst.path, pin.launcher_sha256), (g.path, g.sha256))
                    self.assertTrue(g.record()["beta"])
                    # Lucas, 2026-11-10: "accept a pinned .cmd/.ps1 launcher as a check, but execute the
                    # pinned entry directly". The launcher is hashed and is never in the exec argv.
                    self.assertEqual(tuple(entry_reals(inst)), g.entry)
                    self.assertEqual(g.entry, g.record()["entry"])
                    plan = launch_plan(g, ["pi", "-p", "hi"], {})
                    for _ in range(3):
                        out = plan.exec_argv()
                        self.assertEqual([*entry_reals(inst), "-p", "hi"], out)
                        self.assertNotIn(g.realpath, out)
                        self.assertNotIn(inst.path, out)
                        self.assertFalse([a for a in out if a.lower().endswith((".cmd", ".ps1"))])
                    self.assertEqual({}, plan.exec_env())  # no environment is set for the entry
                    # argv[0] is still the bare engine name, never the launcher's name.
                    self.refused("wrapper_refused", launch_plan, g, ["pi" + ext])
                self.assertFalse(os.path.exists(inst.ran))

    def test_both_launchers_pinned_over_one_tree(self) -> None:
        # The committed Pi shape: pi.cmd and pi.ps1 pins share a target; each launcher matches its own pin.
        cmd = pi_windows_install(".cmd")
        ps1 = dataclasses.replace(cmd, path=os.path.join(cmd.bindir, "pi.ps1"))
        with windows(), table(cmd.pin(), ps1.pin()):
            self.assertNotEqual(self.g(cmd).sha256, self.g(ps1).sha256)
            self.assertEqual(self.g(cmd).pin_sha256, self.g(ps1).pin_sha256)
            self.assertEqual(cmd.path, detect("pi", cmd.bindir).candidate)  # .cmd is searched before .ps1

    def test_extension_case_is_ignored_and_the_name_is_exact(self) -> None:
        inst = pi_windows_install(".cmd")
        upper = os.path.join(inst.bindir, "pi.CMD")
        os.rename(inst.path, upper)
        inst = dataclasses.replace(inst, path=upper)
        with windows(), table(inst.pin()):
            self.assertEqual(upper, self.g(inst).path)
            other = os.path.join(inst.bindir, "PI.cmd")
            shutil.copy(upper, other)
            self.refused("wrapper_refused", grant, "pi", other, probe=self.never_probe)

    def test_detect_finds_pinned_launchers_on_windows(self) -> None:
        for ext in self.EXTS:
            with self.subTest(ext=ext):
                inst = pi_windows_install(ext)
                other = os.path.join(inst.bindir, "pi" + (".ps1" if ext == ".cmd" else ".cmd"))
                os.unlink(other)
                with windows(), table(inst.pin()):
                    self.assertEqual([inst.path], detect_mod.path_hits("pi", inst.bindir, True))
                    self.assertEqual([], detect_mod.path_hits("pi", inst.bindir))
                    found = detect("pi", inst.bindir)
                    self.assertEqual(inst.path, found.candidate)
                    self.assertIn("pinned route (launcher+tree)", found.note)
                    grants, records = launch_check(inst.bindir, probe=self.never_probe)
                    self.assertEqual({"pi"}, set(grants))
                    self.assertEqual(inst.path, grants["pi"].path)
                self.assertFalse(os.path.exists(inst.ran))

    def test_windows_search_order_is_bare_exe_cmd_ps1(self) -> None:
        d = F.dir()
        names = ["pi", "pi.exe", "pi.cmd", "pi.ps1"]
        for name in names:
            write(os.path.join(d, name), "x\n", 0o755)
        write(os.path.join(d, "pi.bat"), "x\n", 0o755)
        with windows():
            self.assertEqual([os.path.join(d, n) for n in names], detect_mod.path_hits("pi", d, True))
            self.assertEqual([os.path.join(d, n) for n in names[:2]], detect_mod.path_hits("pi", d))
            while names:  # the first hit is the candidate, in that order
                self.assertEqual(os.path.join(d, names[0]), detect("pi", d).candidate)
                os.unlink(os.path.join(d, names.pop(0)))
            self.assertIsNone(detect("pi", d).candidate)  # pi.bat is never searched

    def test_non_pinned_engine_is_not_offered_a_cmd_or_ps1(self) -> None:
        d = F.dir()
        for name in ("claude.cmd", "claude.ps1"):
            write(os.path.join(d, name), "@echo off\r\n", 0o755)
        with windows():
            self.assertEqual([], detect_mod.path_hits("claude", d))
            found = detect("claude_code", d)
            self.assertIsNone(found.candidate)
            self.assertEqual((), found.hits)
            grants, records = launch_check(d, probe=self.never_probe)
            self.assertEqual({}, grants)

    def test_unpinned_launcher_is_pin_mismatch(self) -> None:
        for ext in self.EXTS:
            with self.subTest(ext=ext):
                inst = pi_windows_install(ext)
                with windows():
                    with table():
                        self.assertIn("no pin is recorded", self.refused("pin_mismatch", self.g, inst).detail)
                    other = pi_windows_install(ext)
                    flip_first_byte(other.path)
                    with table(other.pin()):  # another launcher's pin
                        self.assertIn("matches no pin", self.refused("pin_mismatch", self.g, inst).detail)
                    # The committed pins are real measurements; this fake launcher matches none of them.
                    self.assertIn("matches no pin", self.refused("pin_mismatch", self.g, inst).detail)
                self.assertFalse(os.path.exists(inst.ran))

    def test_tampered_launcher_byte_before_the_grant_is_pin_mismatch(self) -> None:
        for ext in self.EXTS:
            with self.subTest(ext=ext):
                inst = pi_windows_install(ext)
                pin = inst.pin()
                flip_first_byte(inst.path)
                self.assertEqual(pin.launcher_bytes, os.path.getsize(inst.path))
                with windows(), table(pin):
                    r = self.refused("pin_mismatch", self.g, inst)
                    self.assertIn("matches no pin", r.detail)
                    flip_first_byte(inst.path)  # restored: the pin matches again
                    self.assertEqual(pin.launcher_sha256, self.g(inst).sha256)

    def test_tampered_launcher_byte_after_the_grant_is_binary_changed(self) -> None:
        for ext in self.EXTS:
            with self.subTest(ext=ext):
                inst = pi_windows_install(ext)
                with windows(), table(inst.pin()):
                    g = self.g(inst)
                    plan = launch_plan(g, ["pi"], {})
                    flip_first_byte(inst.path)
                    self.refused("binary_changed", launch_plan, g, ["pi"], {})
                    self.refused("binary_changed", plan.exec_argv)
                    flip_first_byte(inst.path)
                    self.assertEqual(entry_reals(inst), plan.exec_argv())

    def test_tampered_tree_byte_is_refused(self) -> None:
        for ext in self.EXTS:
            with self.subTest(ext=ext):
                inst = pi_windows_install(ext)
                pin = inst.pin()
                victim = os.path.join(inst.root, "dist", "cli.js")
                flip_first_byte(victim)
                with windows(), table(pin):
                    r = self.refused("pin_mismatch", self.g, inst)
                    self.assertIn("installed package files", r.detail)
                    flip_first_byte(victim)
                    g = self.g(inst)
                    flip_first_byte(victim)  # after the grant: the tree is re-hashed on every use
                    self.refused("binary_changed", launch_plan, g, ["pi"], {})

    def test_non_windows_is_unchanged(self) -> None:
        self.assertFalse(binary_mod.is_windows())
        for ext in self.EXTS:
            with self.subTest(ext=ext):
                inst = pi_windows_install(ext)
                with table(inst.pin()):  # pinned, and still a wrapper off Windows
                    self.refused("wrapper_refused", self.g, inst)
                    self.assertEqual([], detect_mod.path_hits("pi", inst.bindir))
                    self.assertEqual([], detect_mod.path_hits("pi", inst.bindir, True))
                    self.assertIsNone(detect("pi", inst.bindir).candidate)
                    grants, records = launch_check(inst.bindir, probe=self.never_probe)
                    self.assertEqual({}, grants)

    def test_other_engines_and_other_extensions_stay_wrapper_refused_on_windows(self) -> None:
        d = F.dir()
        inst = pi_windows_install(".cmd")
        with windows(), table(inst.pin()):
            for name in ("claude.cmd", "claude.ps1", "codex.cmd", "grok.ps1"):
                engine = {"claude": "claude_code"}.get(name.split(".")[0], name.split(".")[0])
                with self.subTest(name=name):
                    p = write(os.path.join(d, name), "@echo off\r\n", 0o755)
                    self.refused("wrapper_refused", grant, engine, p, probe=self.never_probe)
            for name in ("pi.bat", "pi.js", "pi.sh", "pi.psm1", "pi.cmd.bat", "node.cmd", "powershell", "cmd.exe", "pi.exe.cmd"):
                with self.subTest(name=name):
                    p = os.path.join(inst.bindir, name)
                    shutil.copy(inst.path, p)  # the pinned launcher's bytes under another name
                    self.refused("wrapper_refused", grant, "pi", p, probe=self.never_probe)
            # A pinned route's launcher name belongs to that route only.
            self.refused("wrapper_refused", grant, "hermes", inst.path, probe=self.never_probe)
            self.refused("wrapper_refused", grant, "claude_code", inst.path, probe=self.never_probe)


@unittest.skipUnless(os.name == "posix", "POSIX fixtures")
class WindowsPinnedEntryTests(Base):
    """Lucas, 2026-11-10: "accept a pinned .cmd/.ps1 launcher as a check, but execute the pinned entry directly".

    Windows is simulated through ``binary.is_windows()``; no test here runs on real Windows.
    """

    EXTS = (".cmd", ".ps1")

    def test_no_entry_in_the_pin_is_granted_and_refused_at_launch(self) -> None:
        for ext in self.EXTS:
            with self.subTest(ext=ext):
                inst = pi_windows_install(ext, entry=())
                with_entry = dataclasses.replace(inst, entry=WIN_ENTRY)
                with windows(), table(inst.pin()):
                    g = self.g(inst)  # the launcher check passed
                    self.assertEqual((), g.entry)
                    self.assertEqual("launcher+tree", g.pin_kind)
                    self.assertTrue(g.record()["beta"])
                    r = self.refused("pin_entry_unset", launch_plan, g, ["pi", "-p", "hi"], {})
                    self.assertIn("accepted as a check, but the pin records no entry to execute", r.detail)
                    self.assertIn("refused until the entry is measured", r.detail)
                    hand = grant_mod.LaunchPlan(g, ("pi",), (), 3)
                    self.refused("pin_entry_unset", hand.exec_argv)
                    self.refused("pin_entry_unset", hand.exec_env)
                    # argv[0] rules still come first.
                    self.refused("wrapper_refused", launch_plan, g, ["pi" + ext])
                    self.refused("argv_empty", launch_plan, g, [])
                with windows(), table(with_entry.pin()):
                    good = launch_plan(self.g(with_entry), ["pi"], {})
                    self.assertEqual(entry_reals(with_entry), good.exec_argv())
                    swapped = dataclasses.replace(good, grant=g)
                    self.refused("pin_entry_unset", swapped.exec_argv)
                    self.refused("pin_entry_unset", swapped.exec_env)
                # The grant decides, not the platform switch at the time of use.
                with table(inst.pin()):
                    self.refused("pin_entry_unset", hand.exec_argv)
                self.assertFalse(os.path.exists(inst.ran))

    def test_entry_file_changed_after_the_grant_is_binary_changed(self) -> None:
        for ext in self.EXTS:
            for item in WIN_ENTRY:
                with self.subTest(ext=ext, item=item):
                    inst = pi_windows_install(ext)
                    victim = os.path.join(inst.root, *item.split("/"))
                    with windows(), table(inst.pin()):
                        g = self.g(inst)
                        plan = launch_plan(g, ["pi"], {})
                        flip_first_byte(victim)
                        r = self.refused("binary_changed", plan.exec_argv)
                        self.assertIn("digest differs from the grant", r.detail)  # the tree digest covers the entry
                        self.refused("binary_changed", launch_plan, g, ["pi"], {})
                        flip_first_byte(victim)
                        self.assertEqual(entry_reals(inst), plan.exec_argv())

    def test_entry_replaced_by_a_link_outside_the_tree_is_refused(self) -> None:
        inst = pi_windows_install(".ps1")
        outside = write(os.path.join(F.dir(), "evil.js"), "console.log(1);\n")  # the same bytes as dist/cli.js
        cli = os.path.join(inst.root, "dist", "cli.js")
        with windows(), table(inst.pin()):
            g = self.g(inst)
            plan = launch_plan(g, ["pi"], {})
            os.unlink(cli)
            os.symlink(outside, cli)
            self.refused("binary_changed", plan.exec_argv)  # the tree digest: a file became a link
            # Were the tree digest ever to miss it, the entry no longer resolves to the granted file.
            with mock.patch.object(pins_mod, "target_sha256", return_value=g.pin_sha256):
                r = self.refused("binary_changed", plan.exec_argv)
                self.assertIn("now resolves elsewhere", r.detail)
                self.refused("binary_changed", launch_plan, g, ["pi"], {})
                os.unlink(cli)  # gone
                self.assertIn("now resolves elsewhere", self.refused("binary_changed", plan.exec_argv).detail)
                os.mkdir(cli)  # not a regular file
                self.assertIn("now resolves elsewhere", self.refused("binary_changed", plan.exec_argv).detail)

    def test_entry_must_be_a_regular_file_inside_the_tree_at_grant_time(self) -> None:
        inst = pi_windows_install(".cmd", entry=())
        outside = write(os.path.join(F.dir(), "evil.js"), "x\n")
        os.symlink(outside, os.path.join(inst.root, "dist", "out.js"))  # in the tree, pinned by its text
        os.symlink("cli.js", os.path.join(inst.root, "dist", "alias.js"))
        pin = inst.pin()
        with windows():
            for entry in (("node.exe", "dist/missing.js"), ("dist",), ("node.exe", "dist/out.js"), ("dist/cli.js/x",)):
                with self.subTest(entry=entry), table(dataclasses.replace(pin, entry=entry)):
                    r = self.refused("pin_mismatch", self.g, inst)
                    self.assertIn("is not a regular file inside the pinned tree", r.detail)
            # A link that stays inside the tree resolves to the file it names; `..` inside the root is fine.
            for entry in (("node.exe", "dist/alias.js"), ("dist/../node.exe", "dist/cli.js")):
                with self.subTest(entry=entry), table(dataclasses.replace(pin, entry=entry)):
                    g = self.g(inst)
                    self.assertEqual(tuple(entry_reals(dataclasses.replace(inst, entry=WIN_ENTRY))), g.entry)
                    self.assertEqual([*g.entry, "-p"], launch_plan(g, ["pi", "-p"], {}).exec_argv())

    def test_bad_entry_in_the_table_is_pin_mismatch(self) -> None:
        inst = pi_windows_install(".cmd")
        pin = inst.pin()
        write(os.path.join(os.path.dirname(inst.root), "outside.js"), "x\n")
        bad = (("../outside.js",), ("node.exe", "dist/../../outside.js"), ("/etc/passwd",), ("node.exe", ""), ("",),
               ("./node.exe",), ("dist//cli.js",), ("dist\\cli.js",), ("C:/node.exe",), (os.path.join(inst.root, "node.exe"),),
               "node.exe", ["node.exe", "dist/cli.js"], (1,), (None,), None)
        with windows():
            with table(pin):
                g = self.g(inst)
                plan = launch_plan(g, ["pi"], {})
            for entry in bad:
                with self.subTest(entry=entry), table(dataclasses.replace(pin, entry=entry)):
                    self.refused("pin_mismatch", self.g, inst)
                    self.refused("pin_mismatch", plan.exec_argv)  # wherever the table is consulted
                    self.refused("pin_mismatch", launch_plan, g, ["pi"], {})
        # The same table bug refuses off Windows and on a bare-name launcher, where no entry is used.
        posix = pi_install()
        for entry in (("../outside.js",), ("/etc/passwd",), "dist/cli.js"):
            with self.subTest(entry=entry), table(dataclasses.replace(posix.pin(), entry=entry)):
                self.refused("pin_mismatch", self.g, posix)

    def test_edited_entry_field_is_forged(self) -> None:
        inst = pi_windows_install(".ps1")
        bare = pi_windows_install(".ps1", entry=())
        with windows(), table(inst.pin(), bare.pin()):
            g = self.g(inst)
            node, cli = g.entry
            for entry in ((cli,), (), (node, os.path.join(inst.root, "package.json")), ("/bin/sh", cli), (cli, node),
                          g.realpath, [node, cli], (node, 1)):
                with self.subTest(entry=entry):
                    fake = dataclasses.replace(g, entry=entry)
                    self.refused("grant_forged", launch_plan, fake, ["pi"], {})
                    self.refused("grant_forged", grant_mod.LaunchPlan(fake, ("pi",), (), 3).exec_argv)
            # A grant with no entry cannot be given one.
            n = self.g(bare)
            self.refused("grant_forged", launch_plan, dataclasses.replace(n, entry=("/bin/sh",)), ["pi"], {})
            self.refused("pin_entry_unset", launch_plan, n, ["pi"], {})
            self.assertEqual([node, cli], launch_plan(g, ["pi"], {}).exec_argv())

    def test_pin_entry_unset_is_a_listed_code(self) -> None:
        self.assertIn("pin_entry_unset", REFUSAL_CODES)
        self.assertEqual(21, len(REFUSAL_CODES))
        self.assertEqual("r2a-refusals/1.7.0-candidate-2026-11-10", REFUSAL_CODES_VERSION)
        self.assertIn("entry", [f.name for f in dataclasses.fields(Grant)])
        self.assertEqual((), {f.name: f for f in dataclasses.fields(Pin)}["entry"].default)


NODE_ON_PATH = "node (PATH)"


@unittest.skipUnless(os.name == "posix", "POSIX fixtures")
class InterpreterUnpinnedTests(Base):
    """Lucas, 2026-11-10: "A Windows Pi launch whose interpreter (Node from PATH) is outside the pinned
    tree must refuse with a new typed code `interpreter_unpinned`. Hashing external interpreters is
    post-Nov-11; don't implement it."

    Windows is simulated through ``binary.is_windows()``; no test here runs on real Windows.
    """

    EXTS = (".cmd", ".ps1")

    def test_committed_pi_pins_name_the_interpreter_and_cursor_does_not(self) -> None:
        self.assertEqual([NODE_ON_PATH, NODE_ON_PATH], [p.interpreter for p in PINS if p.engine == "pi"])
        self.assertEqual([""], [p.interpreter for p in PINS if p.engine == "cursor"])
        self.assertEqual("", {f.name: f for f in dataclasses.fields(Pin)}["interpreter"].default)
        self.assertEqual("", {f.name: f for f in dataclasses.fields(Grant)}["interpreter"].default)

    def test_windows_pi_with_an_outside_interpreter_is_granted_and_refused_at_launch(self) -> None:
        for ext in self.EXTS:
            for entry in (WIN_ENTRY, ()):
                with self.subTest(ext=ext, entry=entry):
                    plain = pi_windows_install(ext, entry=entry)
                    inst = dataclasses.replace(plain, interpreter=NODE_ON_PATH)
                    with windows(), table(inst.pin()):
                        g = self.g(inst)  # the launcher check passed
                        self.assertEqual(NODE_ON_PATH, g.interpreter)
                        self.assertEqual(NODE_ON_PATH, g.record()["interpreter"])
                        self.assertTrue(g.check_only)
                        self.assertEqual(tuple(entry_reals(inst)), g.entry)
                        # Checked before pin_entry_unset: the more specific cause, with or without an entry.
                        r = self.refused("interpreter_unpinned", launch_plan, g, ["pi", "-p", "hi"], {})
                        self.assertIn("outside the pinned tree", r.detail)
                        self.assertIn("not hashed", r.detail)
                        self.assertIn("post-Nov 11", r.detail)
                        self.assertIn(NODE_ON_PATH, r.detail)
                        hand = grant_mod.LaunchPlan(g, ("pi",), (), 3)
                        self.refused("interpreter_unpinned", hand.exec_argv)
                        self.refused("interpreter_unpinned", hand.exec_env)
                        # argv[0] rules still come first.
                        self.refused("wrapper_refused", launch_plan, g, ["pi" + ext])
                        self.refused("argv_empty", launch_plan, g, [])
                    # The same install under a pin that names no outside interpreter.
                    with windows(), table(plain.pin()):
                        p = self.g(plain)
                        self.assertEqual("", p.interpreter)
                        if entry:
                            good = launch_plan(p, ["pi", "-p"], {})
                            self.assertEqual([*entry_reals(plain), "-p"], good.exec_argv())
                            swapped = dataclasses.replace(good, grant=g)
                        else:
                            self.refused("pin_entry_unset", launch_plan, p, ["pi"], {})
                            swapped = dataclasses.replace(hand, grant=g)
                    with windows(), table(inst.pin()):
                        self.refused("interpreter_unpinned", swapped.exec_argv)
                        self.refused("interpreter_unpinned", swapped.exec_env)
                    self.assertFalse(os.path.exists(inst.ran))

    def test_any_launcher_kind_on_such_a_pin_is_refused(self) -> None:
        # A bare shebang launcher off Windows is executed by its realpath, unless its pin names an outside interpreter.
        plain = pi_install()
        inst = dataclasses.replace(plain, interpreter=NODE_ON_PATH)
        with table(inst.pin()):
            g = self.g(inst)
            self.assertFalse(g.check_only)
            self.assertEqual(NODE_ON_PATH, g.interpreter)
            self.refused("interpreter_unpinned", launch_plan, g, ["pi"], {})
            self.refused("interpreter_unpinned", grant_mod.LaunchPlan(g, ("pi",), (), 3).exec_argv)
        with table(plain.pin()):
            p = self.g(plain)
            self.assertEqual([p.realpath], launch_plan(p, ["pi"], {}).exec_argv())
        self.assertFalse(os.path.exists(inst.ran))

    def test_edited_interpreter_field_is_forged(self) -> None:
        plain = pi_windows_install(".cmd")
        inst = dataclasses.replace(plain, interpreter=NODE_ON_PATH)
        with windows(), table(inst.pin()):
            g = self.g(inst)
            for value in ("", "node", None, 1, (NODE_ON_PATH,)):
                with self.subTest(interpreter=value):
                    fake = dataclasses.replace(g, interpreter=value)  # strip it to get past the refusal
                    self.refused("grant_forged", launch_plan, fake, ["pi"], {})
                    self.refused("grant_forged", grant_mod.LaunchPlan(fake, ("pi",), (), 3).exec_argv)
            self.refused("interpreter_unpinned", launch_plan, g, ["pi"], {})
        with windows(), table(plain.pin()):
            p = self.g(plain)
            self.refused("grant_forged", launch_plan, dataclasses.replace(p, interpreter=NODE_ON_PATH), ["pi"], {})
            self.assertEqual(entry_reals(plain), launch_plan(p, ["pi"], {}).exec_argv())

    def test_bad_interpreter_in_the_table_is_pin_mismatch(self) -> None:
        inst = pi_windows_install(".cmd")
        pin = inst.pin()
        kw = dict(version="1.5.1", platform="test", measured="2026-11-10", source="fake")
        with windows():
            with table(pin):
                plan = launch_plan(self.g(inst), ["pi"], {})
            for value in (None, 1, ("node",), b"node", True):
                with self.subTest(interpreter=value):
                    with table(dataclasses.replace(pin, interpreter=value)):
                        r = self.refused("pin_mismatch", self.g, inst)
                        self.assertIn("pin interpreter is not text", r.detail)
                        self.refused("pin_mismatch", plan.exec_argv)
                    with self.assertRaises(ValueError):
                        pins_mod.measure("pi", "launcher+tree", inst.path, inst.target, interpreter=value, **kw)

    def test_interpreter_unpinned_is_a_listed_code(self) -> None:
        self.assertIn("interpreter_unpinned", REFUSAL_CODES)
        self.assertEqual(21, len(REFUSAL_CODES))
        self.assertEqual("r2a-refusals/1.7.0-candidate-2026-11-10", REFUSAL_CODES_VERSION)
        self.assertEqual("interpreter_unpinned", LaunchRefused("interpreter_unpinned", "pi", "x").record()["refusal"])


@unittest.skipUnless(os.name == "posix", "POSIX fixtures")
class CheckOnlyResolutionTests(Base):
    """Lucas, 2026-11-10: "Extend the check-only rule to a bare or .exe granted path that resolves to a
    script (.cmd/.ps1, by content or resolution as appropriate). It fails closed, with tests."

    Windows is simulated through ``binary.is_windows()``; no test here runs on real Windows.
    """

    def linked(self, inst: Install, name: str) -> Install:
        """The same install, reached through ``bin/<name>``, a link to its pinned launcher."""
        link = os.path.join(inst.bindir, name)
        os.symlink(os.path.basename(inst.path), link)
        return dataclasses.replace(inst, path=link)

    def copied(self, inst: Install, name: str, text: str | None = None) -> Install:
        """The same install, reached through ``bin/<name>``: a file with the launcher's bytes, or ``text``."""
        path = os.path.join(inst.bindir, name)
        if text is None:
            shutil.copy(inst.path, path)
        else:
            write(path, text, 0o755)
        return dataclasses.replace(inst, path=path)

    def assert_check_only(self, inst: Install) -> Grant:
        g = self.g(inst)
        self.assertTrue(g.check_only)
        self.assertTrue(g.record()["check_only"])
        self.assertEqual(tuple(entry_reals(inst)), g.entry)
        if inst.entry:
            plan = launch_plan(g, ["pi", "-p", "hi"], {})
            out = plan.exec_argv()
            self.assertEqual([*entry_reals(inst), "-p", "hi"], out)  # the entry, never the launcher
            self.assertNotIn(g.realpath, out)
            self.assertNotIn(g.path, out)
        else:
            self.refused("pin_entry_unset", launch_plan, g, ["pi", "-p", "hi"], {})
            self.refused("pin_entry_unset", grant_mod.LaunchPlan(g, ("pi",), (), 3).exec_argv)
        return g

    def test_bare_link_to_a_pinned_ps1_is_check_only_by_resolution_on_posix(self) -> None:
        self.assertFalse(binary_mod.is_windows())
        for entry in (WIN_ENTRY, ()):
            with self.subTest(entry=entry):
                inst = self.linked(pi_windows_install(".ps1", entry=entry), "pi")
                with table(inst.pin()):
                    g = self.assert_check_only(inst)
                    self.assertTrue(g.realpath.endswith(os.sep + "pi.ps1"))
                    self.assertEqual("launcher+tree", g.pin_kind)
                self.assertFalse(os.path.exists(inst.ran))

    def test_exe_link_to_a_pinned_cmd_is_check_only_by_resolution(self) -> None:
        for entry in (WIN_ENTRY, ()):
            for name, upper in (("pi.exe", False), ("pi.EXE", False), ("pi", True)):
                with self.subTest(entry=entry, name=name, upper=upper):
                    inst = pi_windows_install(".cmd", entry=entry)
                    if upper:  # the resolved extension is read in any case
                        renamed = os.path.join(inst.bindir, "pi-real.CMD")
                        os.rename(inst.path, renamed)
                        inst = dataclasses.replace(inst, path=renamed)
                    inst = self.linked(inst, name)
                    with windows(), table(inst.pin()):
                        g = self.assert_check_only(inst)
                        self.assertTrue(g.realpath.lower().endswith(".cmd"))
                    with table(inst.pin()):  # by resolution, so off Windows too
                        self.assert_check_only(inst)
                    self.assertFalse(os.path.exists(inst.ran))

    def test_bare_or_exe_file_with_script_content_is_check_only_on_windows(self) -> None:
        shebang = "#!/bin/sh\nexit 0\n"
        for entry in (WIN_ENTRY, ()):
            for name, text in (("pi", None), ("pi.exe", None), ("pi", shebang), ("pi.exe", shebang), ("pi", "")):
                with self.subTest(entry=entry, name=name, text=text):
                    inst = self.copied(pi_windows_install(".cmd", entry=entry), name, text)
                    with windows(), table(inst.pin()):  # batch text, shebang text or an empty file: not native
                        g = self.assert_check_only(inst)
                        self.assertEqual(os.path.realpath(inst.path), g.realpath)
                    self.assertFalse(os.path.exists(inst.ran))

    def test_native_bare_or_exe_launcher_is_not_check_only(self) -> None:
        for name in ("pi", "pi.exe"):
            with self.subTest(name=name):
                inst = pi_windows_install(".cmd")
                native = F.make(name, "1.5.1", where=inst.bindir, dump=inst.ran)
                inst = dataclasses.replace(inst, path=native)
                for ctx in (windows, table):  # on Windows and off it: today's behaviour
                    with ctx(), table(inst.pin()):
                        g = self.g(inst)
                        self.assertFalse(g.check_only)
                        self.assertEqual((), g.entry)
                        self.assertEqual([g.realpath, "-p"], launch_plan(g, ["pi", "-p"], {}).exec_argv())
                self.assertFalse(os.path.exists(inst.ran))

    def test_non_windows_bare_script_launchers_are_unchanged(self) -> None:
        self.assertFalse(binary_mod.is_windows())
        cases = [pi_install(), hermes_install(), cursor_install(), agy_launcher_install(),
                 self.copied(pi_windows_install(".cmd"), "pi"), self.copied(pi_windows_install(".cmd"), "pi.exe")]
        for inst in cases:
            with self.subTest(engine=inst.engine, path=os.path.basename(inst.path)), table(inst.pin()):
                g = self.g(inst)
                self.assertFalse(g.check_only)
                self.assertEqual((), g.entry)
                argv0 = FLOORS[inst.engine].names[0]
                self.assertEqual([g.realpath, "-p"], launch_plan(g, [argv0, "-p"], {}).exec_argv())
            self.assertFalse(os.path.exists(inst.ran))

    def test_binary_launcher_pin_behind_a_script_launcher_on_windows_executes_the_pinned_binary(self) -> None:
        native = agy_install()
        with windows(), table(native.pin()):  # the launcher is the native binary: unchanged
            g = self.g(native)
            self.assertFalse(g.check_only)
            self.assertEqual([g.realpath], launch_plan(g, ["agy"], {}).exec_argv())
        script = agy_launcher_install()
        with windows(), table(script.pin()):  # check-only by content: the pin's measured binary is the entry
            g = self.g(script)
            self.assertTrue(g.check_only)
            self.assertEqual((os.path.realpath(script.root),), g.entry)
            self.assertEqual((g.pin_target,), g.entry)
            out = launch_plan(g, ["agy", "-p"], {}).exec_argv()
            self.assertEqual([os.path.realpath(script.root), "-p"], out)
            self.assertNotIn(g.realpath, out)  # never the launcher
        self.assertFalse(os.path.exists(script.ran))

    def test_unreadable_file_for_the_content_check_is_binary_missing(self) -> None:
        inst = self.copied(pi_windows_install(".cmd"), "pi")
        with windows(), table(inst.pin()):
            with mock.patch.object(grant_mod, "file_kind", side_effect=PermissionError("denied")):
                r = self.refused("binary_missing", self.g, inst)
                self.assertIn("unreadable", r.detail)
            self.assert_check_only(inst)

    def test_edited_check_only_field_is_forged(self) -> None:
        inst = self.linked(pi_windows_install(".ps1"), "pi")
        with table(inst.pin()):
            g = self.assert_check_only(inst)
            for value in (False, 0, 1, None, "true"):
                with self.subTest(check_only=value):
                    fake = dataclasses.replace(g, check_only=value)  # False would exec the launcher's realpath
                    self.refused("grant_forged", launch_plan, fake, ["pi"], {})
                    self.refused("grant_forged", grant_mod.LaunchPlan(fake, ("pi",), (), 3).exec_argv)
            self.refused("grant_forged", launch_plan, dataclasses.replace(g, check_only=False, entry=()), ["pi"], {})
        posix = pi_install()
        with table(posix.pin()):
            p = self.g(posix)
            self.assertFalse(p.check_only)
            self.refused("grant_forged", launch_plan, dataclasses.replace(p, check_only=True), ["pi"], {})
            self.assertEqual([p.realpath], launch_plan(p, ["pi"], {}).exec_argv())

    def test_authenticate_rejects_a_non_bool_check_only_even_with_a_valid_mac(self) -> None:
        inst = self.linked(pi_windows_install(".ps1"), "pi")
        with table(inst.pin()):
            g = self.g(inst)
            for field, value in (("check_only", 1), ("check_only", None), ("interpreter", None), ("interpreter", 1)):
                with self.subTest(field=field, value=value):
                    values = {**dataclasses.asdict(g), field: value}
                    values.pop("mac")
                    remade = Grant(mac=grant_mod._mac(values), **values)  # same-process key: the MAC verifies
                    r = self.refused("grant_forged", launch_plan, remade, ["pi"], {})
                    self.assertIn("check_only is not a bool or interpreter is not text", r.detail)

    def test_path_repointed_after_the_grant_is_binary_changed(self) -> None:
        inst = self.linked(pi_windows_install(".ps1"), "pi")
        native = F.make("pi-native", "1.5.1", where=inst.bindir, dump=inst.ran)
        cmd = dataclasses.replace(inst, path=os.path.join(inst.bindir, "pi.cmd"))
        with table(inst.pin(), cmd.pin()):
            g = self.assert_check_only(inst)
            plan = launch_plan(g, ["pi"], {})
            for target in ("pi.cmd", os.path.basename(native)):  # another pinned launcher, then a native file
                with self.subTest(target=target):
                    os.unlink(inst.path)
                    os.symlink(target, inst.path)
                    r = self.refused("binary_changed", plan.exec_argv)
                    self.assertIn("now resolves elsewhere", r.detail)
                    self.refused("binary_changed", plan.exec_env)
                    self.refused("binary_changed", launch_plan, g, ["pi"], {})
            os.unlink(inst.path)
            os.symlink("pi.ps1", inst.path)
            self.assertEqual(entry_reals(inst), plan.exec_argv())
        self.assertFalse(os.path.exists(inst.ran))


@unittest.skipUnless(os.name == "posix", "POSIX fixtures")
class MeasuredBinaryEntryTests(Base):
    """Lucas, 2026-11-10 (calls 13/14): "accept a pinned .cmd/.ps1 launcher as a check, but execute the
    pinned entry directly". Restated for him the same day: a pin with a measured binary or entry behind a
    script launcher has the launcher checked and that binary or entry executed directly, a binary+launcher
    pin included; ``pin_entry_unset`` only when no binary or entry has been measured.

    Windows is simulated through ``binary.is_windows()``; no test here runs on real Windows.
    """

    EXTS = (".cmd", ".ps1")

    def test_binary_launcher_pin_behind_a_cmd_executes_the_pinned_binary_never_the_launcher(self) -> None:
        for ext in self.EXTS:
            with self.subTest(ext=ext):
                inst = agy_windows_install(ext)
                binary = os.path.realpath(inst.root)
                with windows(), table(inst.pin()):
                    self.assertEqual((), inst.pin().entry)  # the pin row carries no entry: its binary is the entry
                    g = self.g(inst)
                    self.assertTrue(g.check_only)
                    self.assertTrue(g.record()["check_only"])
                    self.assertEqual("binary+launcher", g.pin_kind)
                    self.assertEqual(binary, g.pin_target)
                    self.assertEqual((binary,), g.entry)
                    self.assertEqual(binary_mod.sha256_file(binary)[0], g.pin_sha256)
                    self.assertEqual(inst.pin().target_sha256, g.pin_sha256)
                    self.assertTrue(g.realpath.endswith("agy" + ext))
                    plan = launch_plan(g, ["agy", "-p", "hi"], {"A": "1"})
                    out = plan.exec_argv()
                    self.assertEqual(binary, out[0])
                    self.assertEqual([binary, "-p", "hi"], out)
                    self.assertNotIn(g.path, out)  # never the launcher
                    self.assertNotIn(g.realpath, out)
                    self.assertEqual({"A": "1"}, plan.exec_env())
                    # argv[0] of the request is still the bare engine name.
                    self.refused("wrapper_refused", launch_plan, g, ["agy" + ext], {})
                # The grant decides, not the platform switch at the time of use.
                with table(inst.pin()):
                    self.assertEqual([binary, "-p", "hi"], plan.exec_argv())
                self.assertFalse(os.path.exists(inst.ran))

    def test_binary_changed_after_the_grant_is_binary_changed(self) -> None:
        inst = agy_windows_install(".cmd")
        binary = os.path.realpath(inst.root)
        with windows(), table(inst.pin()):
            g = self.g(inst)
            plan = launch_plan(g, ["agy"], {})
            with open(binary, "rb") as f:
                original = f.read()
            st = os.stat(binary)

            def restore() -> None:
                if os.path.lexists(binary):
                    os.unlink(binary)
                with open(binary, "wb") as f:
                    f.write(original)
                os.chmod(binary, 0o755)
                self.assertEqual([binary], plan.exec_argv())

            # A same-size native rewrite with the mtime restored: the full re-hash decides.
            F.write(binary, "1.5.3", dump=inst.ran)
            self.assertEqual(st.st_size, os.stat(binary).st_size)
            os.utime(binary, ns=(st.st_atime_ns, st.st_mtime_ns))
            r = self.refused("binary_changed", plan.exec_argv)
            self.assertIn("digest differs from the grant", r.detail)
            self.refused("binary_changed", plan.exec_env)
            self.refused("binary_changed", launch_plan, g, ["agy"], {})
            restore()
            # No longer native.
            flip_first_byte(binary)
            self.refused("binary_changed", plan.exec_argv)
            restore()
            # Replaced by a link to another native file.
            other = F.make("other.exe", "1.5.3", dump=inst.ran)
            os.unlink(binary)
            os.symlink(other, binary)
            r = self.refused("binary_changed", plan.exec_argv)
            self.assertIn("now resolves elsewhere", r.detail)
            restore()
            # Removed, then present without its exec bit.
            os.unlink(binary)
            self.refused("binary_changed", plan.exec_argv)
            restore()
            os.chmod(binary, 0o644)
            self.refused("binary_missing", plan.exec_argv)
            os.chmod(binary, 0o755)
            self.assertEqual([binary], plan.exec_argv())
            # The launcher is still re-hashed although it is never executed.
            flip_first_byte(inst.path)
            self.refused("binary_changed", plan.exec_argv)
        self.assertFalse(os.path.exists(inst.ran))

    def test_edited_entry_is_forged(self) -> None:
        inst = agy_windows_install(".cmd")
        with windows(), table(inst.pin()):
            g = self.g(inst)
            binary = g.pin_target
            other = F.make("other.exe", "1.5.3", dump=inst.ran)
            for entry in ((), (g.realpath,), (other,), ("/bin/sh", binary), (binary, binary), (binary, "-c"), binary, [binary]):
                with self.subTest(entry=entry):
                    fake = dataclasses.replace(g, entry=entry)
                    self.refused("grant_forged", launch_plan, fake, ["agy"], {})
                    self.refused("grant_forged", grant_mod.LaunchPlan(fake, ("agy",), (), 4).exec_argv)
                    self.refused("grant_forged", grant_mod.LaunchPlan(fake, ("agy",), (), 4).exec_env)
            self.refused("grant_forged", launch_plan, dataclasses.replace(g, check_only=False), ["agy"], {})
            self.refused("grant_forged", launch_plan, dataclasses.replace(g, check_only=False, entry=()), ["agy"], {})
            self.refused("grant_forged", launch_plan, dataclasses.replace(g, pin_target=other), ["agy"], {})
            # Even re-MACed with the same-process key, the entry must be exactly (pin_target,).
            for entry in ((), (g.realpath,), (other,), (binary, binary)):
                with self.subTest(remade=entry):
                    values = {**dataclasses.asdict(g), "entry": entry}
                    values.pop("mac")
                    remade = Grant(mac=grant_mod._mac(values), **values)
                    r = self.refused("grant_forged", launch_plan, remade, ["agy"], {})
                    self.assertIn("is not its pinned binary", r.detail)
                    r = self.refused("grant_forged", grant_mod.verify, remade)
                    self.assertIn("is not its pinned binary", r.detail)
            self.assertEqual([binary], launch_plan(g, ["agy"], {}).exec_argv())
        self.assertFalse(os.path.exists(inst.ran))

    def test_binary_mismatch_before_the_grant_is_pin_mismatch(self) -> None:
        inst = agy_windows_install(".cmd")
        pin = inst.pin()
        F.write(inst.root, "1.5.3", dump=inst.ran)
        with windows(), table(pin):
            self.refused("pin_mismatch", self.g, inst)
        inst = agy_windows_install(".cmd")
        pin = inst.pin()
        os.chmod(inst.root, 0o644)
        with windows(), table(pin):
            self.refused("binary_missing", self.g, inst)

    def test_pinned_binary_without_exec_bit_is_binary_missing_at_grant_and_later_uses(self) -> None:
        for ext in self.EXTS:
            with self.subTest(ext=ext):
                # Grant time: the pinned binary behind the launcher has no exec bit.
                inst = agy_windows_install(ext)
                pin = inst.pin()
                os.chmod(inst.root, 0o644)
                with windows(), table(pin):
                    self.refused("binary_missing", self.g, inst)
                os.chmod(inst.root, 0o755)
                self.assertFalse(os.path.exists(inst.ran))
                # Later uses: the exec bit is lost after the grant.
                inst = agy_windows_install(ext)
                binary = os.path.realpath(inst.root)
                with windows(), table(inst.pin()):
                    g = self.g(inst)
                    self.assertTrue(g.check_only)
                    self.assertEqual("binary+launcher", g.pin_kind)
                    plan = launch_plan(g, ["agy"], {})
                    self.assertEqual([binary], plan.exec_argv())
                    os.chmod(binary, 0o644)
                    self.refused("binary_missing", plan.exec_argv)
                    self.refused("binary_missing", launch_plan, g, ["agy"], {})
                    os.chmod(binary, 0o755)
                    self.assertEqual([binary], plan.exec_argv())
                    self.assertEqual([binary], launch_plan(g, ["agy"], {}).exec_argv())
                self.assertFalse(os.path.exists(inst.ran))

    def test_launcher_tree_pin_with_no_entry_and_no_interpreter_is_pin_entry_unset(self) -> None:
        inst = pi_windows_install(".cmd", entry=())
        with windows(), table(inst.pin()):
            self.assertEqual((), inst.pin().entry)
            self.assertEqual("", inst.pin().interpreter)
            g = self.g(inst)
            self.assertTrue(g.check_only)
            self.assertEqual("launcher+tree", g.pin_kind)
            self.assertEqual((), g.entry)
            self.assertEqual("", g.interpreter)
            self.refused("pin_entry_unset", launch_plan, g, ["pi", "-p", "hi"], {})
            self.refused("pin_entry_unset", grant_mod.LaunchPlan(g, ("pi",), (), 3).exec_argv)
        self.assertFalse(os.path.exists(inst.ran))

    def test_script_named_launcher_that_is_itself_the_binary_has_nothing_behind_it(self) -> None:
        d = F.dir()
        ran = os.path.join(d, "ran")
        path = F.make("agy.cmd", "1.5.3", where=d, dump=ran)  # a native file under a script name; target ""
        inst = Install("antigravity", path, "", path, "1.5.3", ran)
        with windows(), table(inst.pin()):
            g = self.g(inst)
            self.assertTrue(g.check_only)
            self.assertEqual(g.realpath, g.pin_target)
            self.assertEqual((), g.entry)  # the launcher is never executed, and no binary is measured behind it
            r = self.refused("pin_entry_unset", launch_plan, g, ["agy"], {})
            self.assertIn("no binary is measured behind it", r.detail)
            self.refused("grant_forged", launch_plan, dataclasses.replace(g, entry=(g.realpath,)), ["agy"], {})
            values = {**dataclasses.asdict(g), "entry": (g.realpath,)}
            values.pop("mac")
            self.refused("grant_forged", launch_plan, Grant(mac=grant_mod._mac(values), **values), ["agy"], {})
        self.assertFalse(os.path.exists(ran))

    def test_interpreter_unpinned_still_comes_before_the_entry(self) -> None:
        for inst in (dataclasses.replace(agy_windows_install(".cmd"), interpreter=NODE_ON_PATH),
                     dataclasses.replace(pi_windows_install(".cmd", entry=()), interpreter=NODE_ON_PATH)):
            with self.subTest(engine=inst.engine), windows(), table(inst.pin()):
                g = self.g(inst)
                self.assertTrue(g.check_only)
                argv0 = FLOORS[inst.engine].names[0]
                self.refused("interpreter_unpinned", launch_plan, g, [argv0], {})

    def test_committed_cursor_and_pi_rows_stay_refused(self) -> None:
        self.assertEqual("r2a-pins/1.5.0-candidate-2026-11-10", PIN_TABLE_VERSION)
        for pin in pins_mod.PINS:
            self.assertEqual("launcher+tree", PINNED[pin.engine])
            self.assertEqual((), pin.entry)  # no measured entry: pin_entry_unset, or interpreter_unpinned for Pi
            self.assertEqual(NODE_ON_PATH if pin.engine == "pi" else "", pin.interpreter)


@unittest.skipUnless(os.name == "posix", "POSIX fixtures")
class ChildEnvOnlyTests(Base):
    """Lucas, 2026-11-10: "A launch uses the Recursion route's child_env, never the environment the launcher
    would have set; nothing ambient is inherited, which is Lucas's route-owned design on #320."
    """

    def test_exec_env_is_exactly_the_env_passed_to_launch_plan(self) -> None:
        inst = pi_windows_install(".cmd")
        child_env = {"PATH": "/route/bin", "HOME": "/route/home", "PI_ROUTE": "1"}
        ambient = {"PATH": "/ambient/bin", "CSL732_AMBIENT_SECRET": "x", "NODE_OPTIONS": "--require /evil.js"}
        with windows(), table(inst.pin()), mock.patch.dict(os.environ, ambient):
            g = self.g(inst)
            self.assertTrue(g.check_only)
            plan = launch_plan(g, ["pi"], child_env)
            for _ in range(2):
                self.assertEqual(child_env, plan.exec_env())
            self.assertEqual(entry_reals(inst), plan.exec_argv())
            child_env["LATE"] = "1"  # the plan holds its own copy
            self.assertNotIn("LATE", plan.exec_env())
            # No env given is an empty environment, never os.environ.
            self.assertEqual({}, launch_plan(g, ["pi"]).exec_env())
            self.assertEqual({}, launch_plan(g, ["pi"], None).exec_env())
            self.assertEqual({}, grant_mod.LaunchPlan(g, ("pi",), (), 3).exec_env())
        self.assertFalse(os.path.exists(inst.ran))


@unittest.skipUnless(os.name == "posix", "POSIX fixtures")
class GeminiDroppedTests(Base):
    """Gemini CLI is not a route (Lucas, 2026-11-09): a `gemini` on PATH is unknown, never granted."""

    def gemini_bundle(self) -> str:
        d = F.dir()
        return F.script(os.path.join(d, "gemini"), "#!/usr/bin/env node\nconsole.log('0.40.0')\n")

    def test_gemini_has_no_row_and_no_name(self) -> None:
        self.assertNotIn("gemini", FLOORS)
        self.assertNotIn("gemini", table_record()["engines"])
        self.assertFalse([e for e, s in FLOORS.items() if "gemini" in s.names])
        self.assertFalse([p for p in PINS if p.engine == "gemini"])
        self.assertNotIn("sign_in_refused", REFUSAL_CODES)
        self.assertNotIn("auth", [f.name for f in dataclasses.fields(grant_mod.LaunchPlan)])
        self.assertFalse(hasattr(grant_mod, "check_auth"))

    def test_a_gemini_binary_is_not_granted(self) -> None:
        # Native with a version that met the old floor, and the real JS-bundle shape: both unknown.
        for path in (F.make("gemini", "0.39.1"), F.make("gemini", "gemini 9.9.9"), self.gemini_bundle()):
            with self.subTest(path=path):
                r = self.refused("engine_unknown", grant, "gemini", path, probe=self.never_probe)
                self.assertEqual("gemini", r.engine)
                self.refused("engine_unknown", detect, "gemini", os.path.dirname(path))
                # No other row takes a file named `gemini`.
                for engine in FLOORS:
                    r = self.refused("name_mismatch", grant, engine, path, probe=self.never_probe)
                    self.assertEqual(engine, r.engine)

    def test_launch_check_ignores_a_gemini_on_path(self) -> None:
        d = F.dir()
        F.make("gemini", "0.39.1", where=d)
        grants, records = launch_check(d, probe=self.never_probe)
        self.assertEqual({}, grants)
        self.assertEqual(set(FLOORS), {r["engine"] for r in records})
        self.assertEqual({"absent"}, {r["outcome"] for r in records})

    def test_a_grant_cannot_be_planned_as_gemini(self) -> None:
        g = grant("grok", F.make("grok", "grok 1.0.0"), probe=F.probe)
        self.refused("argv0_mismatch", launch_plan, g, ["gemini", "-p", "hi"])
        self.refused("grant_forged", launch_plan, dataclasses.replace(g, engine="gemini"), ["gemini"])
        with self.assertRaises(TypeError):  # the auth statement went with the row
            launch_plan(g, ["grok"], auth="api_key")  # type: ignore[call-arg]

    def test_a_pin_cannot_make_gemini_a_route(self) -> None:
        script = self.gemini_bundle()
        pin = pins_mod.measure("gemini", "launcher+tree", script, ".", version="0.40.0", platform="test",
                               measured="2026-11-09", source="fake")
        with table(pin):
            self.refused("engine_unknown", grant, "gemini", script, probe=self.never_probe)
            self.refused("engine_unknown", detect, "gemini", os.path.dirname(script))
            grants, records = launch_check(os.path.dirname(script), probe=self.never_probe)
            self.assertEqual({}, grants)
            self.assertNotIn("gemini", {r["engine"] for r in records})


if __name__ == "__main__":
    unittest.main()
