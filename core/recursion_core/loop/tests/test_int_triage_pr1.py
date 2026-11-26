"""The integration triage of draft #379 (sweep S6/int-379), from a native Windows run of the draft merged with master.

Three faults showed there: a guard test that names a flag Windows lacks, a search worker whose pipe is not UTF-8
(and fixtures written in text mode, which Windows turns into CRLF), and a literal U+FFFD that read counted as
decoding damage. The first two are witnessed here without Windows, by taking the system's part away; the two tests
that need the real thing run on Windows only (the sweep's own F1 and F2 witnesses)."""

from __future__ import annotations

import codecs
import os
import pathlib
import random
import subprocess
import unittest
from unittest import mock

from recursion_core.loop.providers import base as b
from recursion_core.loop.tests import test_guards
from recursion_core.loop.tests.support import context, registry, workspace
from recursion_core.loop.tools import read, search
from recursion_core.loop.tools import registry as r


def _guard_result() -> unittest.TestResult:
    result = unittest.TestResult()
    test_guards.ReadGuards("test_the_file_is_opened_without_blocking").run(result)
    return result


class NoNonblockFlag(unittest.TestCase):
    def test_the_guard_test_runs_where_the_system_has_no_such_flag(self) -> None:
        saved = getattr(os, "O_NONBLOCK", None)
        if saved is not None:
            del os.O_NONBLOCK  # as on Windows
        try:
            result = _guard_result()
        finally:
            if saved is not None:
                os.O_NONBLOCK = saved
        self.assertEqual((result.errors, result.failures), ([], []))
        self.assertEqual(result.testsRun, 1)

    @unittest.skipUnless(hasattr(os, "O_NONBLOCK"), "this system has the flag")
    def test_where_the_flag_exists_the_guard_test_runs_and_passes(self) -> None:
        result = _guard_result()
        self.assertEqual((result.errors, result.failures, result.skipped), ([], [], []))

    @unittest.skipUnless(os.name == "nt", "native Windows gate witness")
    def test_the_guard_test_has_no_unix_only_attribute_error_on_windows(self) -> None:
        result = _guard_result()
        self.assertEqual((result.errors, result.failures), ([], []))


class SearchWorkerPipe(unittest.TestCase):
    def test_the_worker_is_started_in_utf8_mode(self) -> None:
        ws = workspace(self, {"words.txt": "caf\u00e9\n"})
        real = subprocess.Popen
        commands: list[list[str]] = []

        def watched(command, *args, **kwargs):
            commands.append(list(command))
            return real(command, *args, **kwargs)

        with mock.patch.object(search.subprocess, "Popen", side_effect=watched):
            out = registry().dispatch(b.ToolCall("s1", "search", {"pattern": "caf"}), context(ws))
        self.assertEqual(out.status, r.OK, out.model_text)
        self.assertEqual(out.model_text, "words.txt:1:caf\u00e9\n")
        (command,) = commands
        self.assertIn(("-X", "utf8"), list(zip(command, command[1:])))
        self.assertLess(command.index("utf8"), command.index(str(search.WORKER)))

    @unittest.skipUnless(os.name == "nt", "native Windows pipe encoding witness")
    def test_a_unicode_result_comes_back_whole_on_windows(self) -> None:
        ws = workspace(self, {"words.txt": "caf\u00e9\n"})
        out = registry().dispatch(b.ToolCall("s2", "search", {"pattern": "caf"}), context(ws))
        self.assertEqual(out.status, r.OK, out.model_text)
        self.assertEqual(out.model_text, "words.txt:1:caf\u00e9\n")


class FixtureBytes(unittest.TestCase):
    def test_a_text_fixture_is_written_with_the_line_ends_it_has(self) -> None:
        def as_windows_does(path, content, encoding=None, errors=None, newline=None):
            return path.write_bytes(content.replace("\n", "\r\n").encode(encoding or "utf-8"))

        with mock.patch.object(pathlib.Path, "write_text", autospec=True, side_effect=as_windows_does):
            ws = workspace(self, {"lf.txt": "one\ntwo\n", "cr.txt": "a\r\nb\r\n"})
        self.assertEqual((ws.root / "lf.txt").read_bytes(), b"one\ntwo\n")
        self.assertEqual((ws.root / "cr.txt").read_bytes(), b"a\r\nb\r\n")


class LiteralReplacementCharacter(unittest.TestCase):
    def test_a_literal_replacement_character_is_valid_text(self) -> None:
        content = "replacement: \ufffd\n"
        self.assertFalse(read.is_binary(content.encode("utf-8")))
        ws = workspace(self, {"log.txt": content})
        out = registry().dispatch(b.ToolCall("f1", "read", {"path": "log.txt"}), context(ws))
        self.assertEqual(out.status, r.OK, out.model_text)
        self.assertIn("replacement: \ufffd", out.model_text)

    def test_a_file_of_nothing_else_is_text(self) -> None:
        self.assertFalse(read.is_binary(("\ufffd" * 300).encode("utf-8")))

    def test_damage_under_the_threshold_beside_literal_ones_is_text(self) -> None:
        self.assertFalse(read.is_binary(("\ufffd" * 50 + "a" * 2000).encode("utf-8") + b"\xff\xff"))

    def test_damage_over_the_threshold_beside_literal_ones_is_binary(self) -> None:
        self.assertTrue(read.is_binary(("\ufffd" * 50 + "a" * 400).encode("utf-8") + b"\xff" * 20))

    def test_bytes_that_are_not_utf8_are_still_binary(self) -> None:
        self.assertTrue(read.is_binary(b"\xff\xfe" * 100))
        self.assertTrue(read.is_binary(b"ok " * 20 + b"\xe2\x82" * 5))

    def test_the_count_is_the_one_a_decoder_that_stops_at_each_error_gives(self) -> None:
        def reference(sample: bytes) -> int:
            bad = at = 0
            view = memoryview(sample)
            while at < len(sample):
                try:
                    codecs.utf_8_decode(view[at:], "strict", False)
                    break
                except UnicodeDecodeError as exc:
                    bad += 1
                    at += exc.end
            return bad

        pieces = [b"a", b"\xc3\xa9", b"\xe2\x82\xac", b"\xf0\x9f\x98\x80", b"\xef\xbf\xbd", b"\xef\xbf", b"\xef", b"\xf0",
                  b"\xe2\x82", b"\xff", b"\x80", b"\xc0\xaf", b"\xed\xa0\x80", b"\n"]
        rng = random.Random(379)
        for _ in range(400):
            sample = b"".join(rng.choice(pieces) for _ in range(rng.randint(0, 40)))
            with self.subTest(sample=sample):
                self.assertEqual(read._damaged(sample), reference(sample))


if __name__ == "__main__":
    unittest.main()
