"""CSL-732 (B10, R2a): pinned wrapper routes and the dropped Gemini CLI row (candidate tests).

Lucas's owner call of 2026-11-09: Pi, Hermes and Cursor run as beta when the launcher and
the installed package files match a pin; Antigravity CLI (``agy``) when its version, binary
and launcher match a pin; any mismatch is refused until re-pinned. Gemini CLI is not an
engine here (his call of the same day: drop the row). Every install below is a fake built in a temp dir and measured there. No real
engine is run and no pin in these tests is a real one.

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
from recursion_core.launch_check import pins as pins_mod
from recursion_core.launch_check.binary import HEX64
from recursion_core.launch_check.floors import check_floors
from recursion_core.launch_check.tests.fixtures import Factory

grant_mod = importlib.import_module("recursion_core.launch_check.grant")

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

    @property
    def bindir(self) -> str:
        return os.path.dirname(self.path)

    def pin(self, version: str | None = None) -> Pin:
        return pins_mod.measure(
            self.engine, PINNED[self.engine], self.path, self.target, version=version or self.version,
            platform="test", measured="2026-11-09", source="fake install built by test_pins.py",
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
                if p.target == "":
                    self.assertEqual("binary+launcher", PINNED[p.engine])
                    self.assertEqual(p.launcher_sha256, p.target_sha256)
        keys = [(p.engine, p.launcher_sha256, p.target, p.target_sha256) for p in PINS]
        self.assertEqual(len(keys), len(set(keys)))

    def test_committed_antigravity_1_5_3_pin_has_the_measured_values(self) -> None:
        # Verified fixture, measured 2026-11-09 on linux_amd64 with sha256sum; the 246 MB binary is not committed.
        self.assertEqual("r2a-pins/1.2.0-candidate-2026-11-09", PIN_TABLE_VERSION)
        agy = Pin(
            engine="antigravity",
            version="1.5.3",
            launcher_sha256="7ca212748ca0dfda718efbc39a776a104472e7e9344d4b6ea147c543684f96a3",
            launcher_bytes=246837648,
            target="",
            target_sha256="7ca212748ca0dfda718efbc39a776a104472e7e9344d4b6ea147c543684f96a3",
            platform="linux_amd64",
            measured="2026-11-09",
            source="https://storage.googleapis.com/antigravity-public/antigravity-cli/1.5.3-6604829099950080/linux-x64/cli_linux_x64.tar.gz",
        )
        self.assertIn(agy, PINS)
        self.assertEqual([agy], [p for p in PINS if p.engine == "antigravity"])
        self.assertTrue(pins_mod.listed("antigravity", "1.5.3", "7ca212748ca0dfda718efbc39a776a104472e7e9344d4b6ea147c543684f96a3", "7ca212748ca0dfda718efbc39a776a104472e7e9344d4b6ea147c543684f96a3"))
        self.assertFalse(pins_mod.listed("antigravity", "1.5.4", "7ca212748ca0dfda718efbc39a776a104472e7e9344d4b6ea147c543684f96a3", "7ca212748ca0dfda718efbc39a776a104472e7e9344d4b6ea147c543684f96a3"))

    def test_committed_windows_amd64_cursor_and_pi_pins_have_the_measured_values(self) -> None:
        # Verified fixtures, measured 2026-11-09 on Lucas's Windows machine "sentry" (hasher at 66fe11b1);
        # the trees are not committed. Recorded data: Windows launcher resolution still refuses .cmd/.ps1.
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
        )
        cells = (
            (cursor, "/u/cursor-agent/cursor-agent.ps1", "/u/cursor-agent/versions/2026.11.05-86047df", "2026.11.06-86047df"),
            (pi, "/u/.pi/agent/bin/pi.cmd", "/u/.pi/agent/install/releases/1.5.1", "1.5.2"),
        )
        for pin, launcher, root, other_version in cells:
            with self.subTest(engine=pin.engine):
                self.assertIn(pin, PINS)
                self.assertEqual([pin], [p for p in PINS if p.engine == pin.engine])
                self.assertTrue(pins_mod.listed(pin.engine, pin.version, pin.launcher_sha256, pin.target_sha256))
                self.assertFalse(pins_mod.listed(pin.engine, other_version, pin.launcher_sha256, pin.target_sha256))
                # The target is a plain relative path: no ValueError.
                self.assertEqual(os.path.realpath(root), pins_mod.resolve_target(launcher, pin.target))
        self.assertEqual({"antigravity", "cursor", "pi"}, {p.engine for p in PINS})
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
