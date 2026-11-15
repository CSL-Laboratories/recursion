"""The fake-binary hash and version fixtures (``fake_binaries.py``), checked against the package.

A fixture's recorded SHA-256, size and kind must be what ``binary.sha256_file`` and ``binary.file_kind``
give for its bytes written to disk; a fixture that lies is refused before any test uses it. The version
table is run through ``parse_version`` and through ``grant()`` with a probe that returns the line.

Run from the repository root:
``PYTHONPATH=core python3 -B -m unittest recursion_core.launch_check.tests.test_fake_binaries -v``.
"""

from __future__ import annotations

import dataclasses
import os
import shutil
import tempfile
import unittest

from recursion_core.launch_check import FLOORS, LaunchRefused, ProbeResult, grant, launch_plan, parse_version
from recursion_core.launch_check.binary import HEX64, file_kind, sha256_file
from recursion_core.launch_check.tests.fake_binaries import HASHES, VERSIONS, check_hash_fixture, hash_fixture


class Dir(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="lc-fake-")
        self.addCleanup(shutil.rmtree, self.root, True)

    def put(self, name: str, data: bytes, mode: int = 0o755) -> str:
        path = os.path.join(self.root, name)
        with open(path, "wb") as f:
            f.write(data)
        os.chmod(path, mode)
        return path


class HashFixtureTests(Dir):
    def test_every_fixture_matches_what_the_package_computes(self) -> None:
        self.assertEqual(len(HASHES), len({fx.name for fx in HASHES}))
        for fx in HASHES:
            with self.subTest(fixture=fx.name):
                check_hash_fixture(fx)
                self.assertTrue(HEX64.fullmatch(fx.sha256))
                path = self.put(fx.name, fx.data)
                self.assertEqual((fx.sha256, fx.size), sha256_file(path))
                self.assertEqual(fx.kind, file_kind(path))

    def test_the_fixtures_differ_where_the_tests_need_them_to(self) -> None:
        small, same_size, cut = (hash_fixture(n) for n in (
            "elf-small", "elf-small-same-size-replacement", "elf-small-truncated"))
        self.assertEqual(small.size, same_size.size)
        self.assertNotEqual(small.sha256, same_size.sha256)
        self.assertEqual(small.data[: cut.size], cut.data)
        edge, past = hash_fixture("elf-chunk-edge"), hash_fixture("elf-chunk-edge-plus-one")
        self.assertEqual(edge.data, past.data[: edge.size])
        self.assertNotEqual(edge.sha256, past.sha256)

    def test_a_fixture_that_lies_is_refused(self) -> None:
        fx = hash_fixture("elf-small")
        lies = [
            dataclasses.replace(fx, sha256=hash_fixture("elf-small-same-size-replacement").sha256),
            dataclasses.replace(fx, size=fx.size - 1),
            dataclasses.replace(fx, kind="script"),
            dataclasses.replace(fx, data=fx.data[:-1] + b"\x00"),
        ]
        for lie in lies:
            with self.subTest(lie=lie.sha256[:8] + "/" + str(lie.size) + "/" + lie.kind):
                with self.assertRaises(AssertionError):
                    check_hash_fixture(lie)


@unittest.skipUnless(os.name == "posix", "POSIX file modes")
class HashFixtureGrantTests(Dir):
    """The grant pins exactly the recorded digest, and every replacement after it is refused at launch."""

    def setUp(self) -> None:
        super().setUp()
        self.path = self.put("claude", hash_fixture("elf-small").data)
        self.g = grant("claude_code", self.path, probe=lambda *a: ProbeResult("2.1.318 (Claude Code)", 0, 0))

    def refused(self, code: str) -> None:
        with self.assertRaises(LaunchRefused) as cm:
            launch_plan(self.g, ["claude"])
        self.assertEqual(code, cm.exception.code, cm.exception)

    def test_the_grant_records_the_fixture_digest(self) -> None:
        fx = hash_fixture("elf-small")
        self.assertEqual((fx.sha256, fx.size), (self.g.sha256, self.g.size))
        self.assertEqual([self.path], launch_plan(self.g, ["claude"]).exec_argv())

    def test_a_same_size_replacement_with_the_mtime_restored_is_binary_changed(self) -> None:
        st = os.stat(self.path)
        with open(self.path, "r+b") as f:
            f.write(hash_fixture("elf-small-same-size-replacement").data)
        os.utime(self.path, ns=(st.st_atime_ns, st.st_mtime_ns))
        self.assertEqual(st.st_size, os.stat(self.path).st_size)
        self.refused("binary_changed")

    def test_a_truncated_binary_is_binary_changed(self) -> None:
        with open(self.path, "r+b") as f:
            f.truncate(hash_fixture("elf-small-truncated").size)
        self.refused("binary_changed")

    def test_an_emptied_binary_is_binary_changed(self) -> None:
        with open(self.path, "r+b") as f:
            f.truncate(0)
        self.refused("binary_changed")

    def test_a_deleted_binary_is_binary_missing(self) -> None:
        os.remove(self.path)
        self.refused("binary_missing")

    def test_a_script_or_other_file_is_never_granted(self) -> None:
        for name, code in (("script", "wrapper_refused"), ("other", "wrapper_refused"), ("empty", "wrapper_refused")):
            with self.subTest(fixture=name):
                d = os.path.join(self.root, name + "-dir")
                os.makedirs(d)
                path = os.path.join(d, "claude")
                with open(path, "wb") as f:
                    f.write(hash_fixture(name).data)
                os.chmod(path, 0o755)
                with self.assertRaises(LaunchRefused) as cm:
                    grant("claude_code", path, probe=lambda *a: ProbeResult("2.1.318 (Claude Code)", 0, 0))
                self.assertEqual(code, cm.exception.code)


class VersionFixtureTests(Dir):
    def test_every_line_parses_as_recorded(self) -> None:
        for fx in VERSIONS:
            with self.subTest(engine=fx.engine, raw=fx.raw[:40]):
                parsed = parse_version(FLOORS[fx.engine].scheme, fx.raw)
                self.assertEqual(fx.key, None if parsed is None else parsed.key)
                if parsed is not None:
                    self.assertEqual(fx.prerelease, parsed.prerelease)

    @unittest.skipUnless(os.name == "posix", "POSIX file modes")
    def test_every_line_reaches_the_recorded_outcome_at_grant(self) -> None:
        native = hash_fixture("elf-small").data
        names = {e: FLOORS[e].names[0] for e in FLOORS}
        for i, fx in enumerate(VERSIONS):
            with self.subTest(engine=fx.engine, raw=fx.raw[:40]):
                d = os.path.join(self.root, f"v{i}")
                os.makedirs(d)
                path = self.put(os.path.join(d, names[fx.engine]), native)
                try:
                    grant(fx.engine, path, probe=lambda *a, raw=fx.raw: ProbeResult(raw, 0, 0))
                    outcome = "granted"
                except LaunchRefused as r:
                    outcome = r.code
                self.assertEqual(fx.outcome, outcome)

    def test_the_table_covers_every_probed_engine(self) -> None:
        probed = {e for e, s in FLOORS.items() if s.pin is None}
        self.assertEqual(probed, {fx.engine for fx in VERSIONS if fx.outcome == "granted"})


if __name__ == "__main__":
    unittest.main()
