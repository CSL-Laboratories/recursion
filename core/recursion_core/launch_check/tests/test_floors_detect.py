"""CSL-732 (B10, R2a): floor table, strict version parse, static detection, one-pass check (candidate tests).

Run from the repository root:
``PYTHONPATH=core python3 -B -m unittest discover -s core/recursion_core/launch_check/tests -t core -v``.
Fake binaries only; no real engine, network or credential.
"""

from __future__ import annotations

import os
import re
import unittest
from types import MappingProxyType
from unittest import mock

from recursion_core.launch_check import (
    FLOOR_TABLE_VERSION,
    FLOORS,
    LaunchRefused,
    codex_vendored_binary,
    detect,
    detect_all,
    grant,
    launch_check,
    parse_version,
    table_record,
)
from recursion_core.launch_check.detect import _triples
from recursion_core.launch_check.tests.fixtures import Factory

F: Factory


def setUpModule() -> None:
    global F
    F = Factory()


def tearDownModule() -> None:
    F.cleanup()


class FloorTableTests(unittest.TestCase):
    def test_table_version_is_versioned_candidate(self) -> None:
        self.assertRegex(FLOOR_TABLE_VERSION, r"^r2a-floors/\d+\.\d+\.\d+-candidate-\d{4}-\d{2}-\d{2}$")
        self.assertEqual(FLOOR_TABLE_VERSION, table_record()["floor_table"])

    def test_nine_engines_and_binary_names(self) -> None:
        names = {e: s.names for e, s in FLOORS.items()}
        self.assertEqual(
            {
                "claude_code": ("claude",), "codex": ("codex",), "opencode": ("opencode", "opencode2"),
                "grok": ("grok",), "antigravity": ("agy",), "cursor": ("agent", "cursor-agent"),
                "gemini": ("gemini",), "pi": ("pi",), "hermes": ("hermes",),
            },
            names,
        )
        self.assertEqual("hermes-acp", FLOORS["hermes"].probe_name)
        self.assertEqual("date", FLOORS["cursor"].scheme)

    def test_two_slots(self) -> None:
        slots = {e: (s.functional, s.security) for e, s in FLOORS.items()}
        self.assertEqual(
            {
                "claude_code": ("2.1.314", "2.1.260"), "codex": (None, "0.131.0"),
                "opencode": (None, "1.18.33"), "grok": (None, None), "antigravity": (None, None),
                "cursor": (None, "2025.09.17-25b418f"), "gemini": (None, "0.39.1"), "pi": (None, None),
                "hermes": (None, None),
            },
            slots,
        )
        self.assertIn("beta until measured", FLOORS["codex"].functional_basis)
        self.assertIn("0.145.0", FLOORS["codex"].functional_basis)

    def test_table_is_read_only(self) -> None:
        self.assertIsInstance(FLOORS, MappingProxyType)
        with self.assertRaises(TypeError):
            FLOORS["kiro"] = FLOORS["grok"]  # type: ignore[index]

    def test_every_floor_parses_as_a_release(self) -> None:
        for spec in FLOORS.values():
            for floor in (spec.functional, spec.security):
                if floor is not None:
                    with self.subTest(engine=spec.engine, floor=floor):
                        parsed = parse_version(spec.scheme, floor)
                        self.assertIsNotNone(parsed)
                        self.assertIsNone(parsed.prerelease)


class ParseTests(unittest.TestCase):
    def test_real_shape_lines(self) -> None:
        cases = [
            ("semver", "2.1.318 (Claude Code)", (2, 1, 318)),
            ("semver", "codex-cli 0.160.0", (0, 160, 0)),
            ("semver", "opencode v2.2.5", (2, 2, 5)),
            ("semver", "hermes-acp 0.9.1", (0, 9, 1)),
            ("semver", "0.39.1", (0, 39, 1)),
            ("date", "2026.10.22", (2026, 10, 22, 0, 0, 0)),
            ("date", "2026.10.01-e373342", (2026, 10, 1, 0, 0, 0)),
            ("date", "v2026.10.22", (2026, 10, 22, 0, 0, 0)),
            ("date", "2026.10.01-14-05-09-e373342", (2026, 10, 1, 14, 5, 9)),
            ("date", "2025.09.17-25b418f", (2025, 9, 17, 0, 0, 0)),
        ]
        for scheme, line, key in cases:
            with self.subTest(line=line):
                parsed = parse_version(scheme, line)
                self.assertIsNotNone(parsed)
                self.assertEqual(key, parsed.key)
                self.assertIsNone(parsed.prerelease)

    def test_date_versions_compare_numerically(self) -> None:
        def key(s: str) -> tuple[int, ...]:
            return parse_version("date", s).key

        self.assertLess(key("2025.09.17-25b418f"), key("2026.10.01-e373342"))
        self.assertLess(key("2026.9.30"), key("2026.10.01"))  # not a string compare
        self.assertLess(key("2026.10.01-e373342"), key("2026.10.22"))
        self.assertEqual(key("2026.10.01-aaaaaaa"), key("2026.10.01-bbbbbbb"))
        self.assertLess(key("2026.10.01"), key("2026.10.01-00-00-01-abcdef0"))

    def test_prerelease_is_reported_not_accepted(self) -> None:
        for line in ("2.1.318-beta.1 (Claude Code)", "codex-cli 0.131.0-alpha.2", "v1.0.0-rc1"):
            with self.subTest(line=line):
                self.assertIsNotNone(parse_version("semver", line).prerelease)

    def test_strict_rejects(self) -> None:
        bad = ["", "Claude Code", "2.1", "2.1.318.4", "2.1.318abc", "2.1.318+build.5", "2.1.318-beta+b",
               "02.1.3", "2.01.3", "1234567890.0.0", "2.1.-1", "x2.1.318", "2.1.318_1", None, 5,
               "1." * 200]
        for line in bad:
            with self.subTest(line=line):
                self.assertIsNone(parse_version("semver", line))
        for line in ("2026.13.01", "2026.00.10", "2026.10.32", "26.10.22", "2026.10.22-zz",
                     "2026.10.22.1", "2026.10.22-25-00-00-abcdef0", "2026.10.22-ABCDEF0"):
            with self.subTest(line=line):
                self.assertIsNone(parse_version("date", line))
        self.assertIsNone(parse_version("calver", "2026.10.22"))


@unittest.skipUnless(os.name == "posix", "POSIX fixtures")
class DetectTests(unittest.TestCase):
    def test_native_hit_and_symlinked_native(self) -> None:
        d1, d2 = F.dir(), F.dir()
        real = F.make("claude-2.1.318", "2.1.318 (Claude Code)", where=d2)
        os.symlink(real, os.path.join(d1, "claude"))
        found = detect("claude_code", os.pathsep.join([d1, d2]))
        self.assertEqual(os.path.join(d1, "claude"), found.candidate)
        self.assertEqual(real, found.hits[0].realpath)
        self.assertEqual("native", found.hits[0].kind)

    def test_script_only_is_not_a_candidate(self) -> None:
        d = F.dir()
        F.script(os.path.join(d, "claude"))
        found = detect("claude_code", d)
        self.assertIsNone(found.candidate)
        self.assertEqual("script", found.hits[0].kind)
        self.assertIn("fail closed", found.note)

    def test_wrapper_first_on_path_shadows_later_native(self) -> None:
        d1, d2 = F.dir(), F.dir()
        F.script(os.path.join(d1, "claude"))
        F.make("claude", "2.1.318 (Claude Code)", where=d2)
        found = detect("claude_code", os.pathsep.join([d1, d2]))
        self.assertIsNone(found.candidate)
        self.assertEqual(2, len(found.hits))

    def test_detection_never_executes_a_hit(self) -> None:
        d = F.dir()
        marker = os.path.join(d, "ran")
        F.script(os.path.join(d, "claude"), f"#!/bin/sh\ntouch {marker}\n")
        detect_all(d)
        self.assertFalse(os.path.exists(marker))

    def test_explicit_path_string_not_environ(self) -> None:
        d = F.dir()
        F.make("claude", "2.1.318 (Claude Code)", where=d)
        with mock.patch.dict(os.environ, {"PATH": d}):
            self.assertEqual((), detect("claude_code", "").hits)
        self.assertEqual((), detect("claude_code", "relative/dir" + os.pathsep + ".").hits)

    def test_unknown_engine(self) -> None:
        with self.assertRaises(LaunchRefused) as cm:
            detect("kiro", "/usr/bin")
        self.assertEqual("engine_unknown", cm.exception.code)

    def test_cursor_both_names(self) -> None:
        d = F.dir()
        F.make("cursor-agent", "2026.10.22", where=d)
        found = detect("cursor", d)
        self.assertEqual(os.path.join(d, "cursor-agent"), found.candidate)

    def _npm_codex(self, triple: str | None, layout: str = "vendor") -> tuple[str, str | None]:
        prefix = F.dir()
        pkg = os.path.join(prefix, "lib", "node_modules", "@openai", "codex")
        os.makedirs(os.path.join(pkg, "bin"))
        F.script(os.path.join(pkg, "bin", "codex.js"), "#!/usr/bin/env node\nrequire('child_process')\n")
        os.makedirs(os.path.join(prefix, "bin"))
        os.symlink(os.path.join("..", "lib", "node_modules", "@openai", "codex", "bin", "codex.js"),
                   os.path.join(prefix, "bin", "codex"))
        native = None
        if triple:
            base = pkg if layout == "vendor" else os.path.join(pkg, "node_modules", "@openai", "codex-linux-x64")
            vdir = os.path.join(base, "vendor", triple, "codex")
            os.makedirs(vdir)
            native = F.make("codex", "codex-cli 0.160.0", where=vdir)
        return os.path.join(prefix, "bin"), native

    def test_npm_codex_shim_followed_to_vendored_native(self) -> None:
        triples = _triples()
        if not triples:
            self.skipTest("no known vendor triple on this platform")
        for layout in ("vendor", "optional-dependency"):
            with self.subTest(layout=layout):
                bindir, native = self._npm_codex(triples[0], layout)
                found = detect("codex", bindir)
                self.assertEqual(native, found.candidate)
                g = grant("codex", found.candidate, probe=F.probe)
                self.assertEqual("0.160.0", g.version)

    def test_npm_codex_unrecognised_layout_fails_closed(self) -> None:
        bindir, _ = self._npm_codex(None)
        found = detect("codex", bindir)
        self.assertIsNone(found.candidate)
        self.assertIn("not recognised", found.note)
        bindir, _ = self._npm_codex("riscv64-unknown-none")
        self.assertIsNone(detect("codex", bindir).candidate)

    def test_vendored_lookup_needs_the_npm_path_shape(self) -> None:
        d = F.dir()
        js = F.script(os.path.join(d, "codex.js"), "#!/usr/bin/env node\n")
        self.assertIsNone(codex_vendored_binary(js))


@unittest.skipUnless(os.name == "posix", "POSIX fixtures")
class LaunchCheckTests(unittest.TestCase):
    def test_one_record_per_engine_and_grants_only_when_clean(self) -> None:
        d = F.dir()
        F.make("claude", "2.1.318 (Claude Code)", where=d)
        F.make("codex", "codex-cli 0.160.0", where=d)
        F.make("opencode", "opencode v2.2.5", where=d)
        F.make("gemini", "0.38.0", where=d)
        F.script(os.path.join(d, "agent"), "#!/bin/sh\nexec node index.js \"$@\"\n")
        grants, records = launch_check(d, probe=F.probe)
        by = {r["engine"]: r for r in records}
        self.assertEqual(set(FLOORS), set(by))
        self.assertEqual({"claude_code", "codex", "opencode"}, set(grants))
        self.assertEqual("granted", by["claude_code"]["outcome"])
        self.assertEqual("2.1.318 (Claude Code)", by["claude_code"]["version_raw"])
        self.assertFalse(by["claude_code"]["beta"])
        self.assertTrue(by["codex"]["beta"])
        self.assertEqual("granted", by["opencode"]["outcome"])
        self.assertTrue(by["opencode"]["beta"])
        self.assertEqual("version_refused", by["gemini"]["refusal"])
        self.assertEqual("wrapper_refused", by["cursor"]["refusal"])
        for engine in ("grok", "antigravity", "pi", "hermes"):
            self.assertEqual("absent", by[engine]["outcome"])
        for r in records:
            self.assertEqual(FLOOR_TABLE_VERSION, r["floor_table"])
            if r["outcome"] == "granted":
                self.assertRegex(r["sha256"], re.compile(r"^[0-9a-f]{64}$"))

    def test_repeated_passes_are_stable(self) -> None:
        d = F.dir()
        F.make("claude", "2.1.318 (Claude Code)", where=d)
        F.make("codex", "codex-cli 0.131.0", where=d)
        first = None
        for _ in range(5):
            grants, records = launch_check(d, probe=F.probe)
            digest = {e: g.sha256 for e, g in grants.items()}
            first = first or digest
            self.assertEqual(first, digest)


if __name__ == "__main__":
    unittest.main()
