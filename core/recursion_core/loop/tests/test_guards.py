"""Guards the lane's own probes could not reach, made testable without a race: first written by the GPT-6 Astra
Codex read of draft #379 (review-1-379), whose one-rule probes left each of these rules green. Each test uses
only its own regular files, mocked file metadata or in-memory streams."""

from __future__ import annotations

import io
import os
import stat
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from recursion_core.loop.providers import anthropic as a
from recursion_core.loop.providers import base as b
from recursion_core.loop.providers import sse
from recursion_core.loop.tests.support import context, registry, workspace
from recursion_core.loop.tools import read, search_worker
from recursion_core.loop.tools import registry as r


class ReadGuards(unittest.TestCase):
    def test_the_file_is_opened_without_blocking(self) -> None:
        if not hasattr(os, "O_NONBLOCK"):
            self.skipTest("this system has no O_NONBLOCK, and read asks for none there")
        ws = workspace(self, {"file.txt": "safe\n"})
        real = os.open
        flags: list[int] = []

        def watched(path, mode, *args, **kwargs):
            flags.append(mode)
            return real(path, mode, *args, **kwargs)

        with patch.object(read.os, "open", side_effect=watched):
            out = read.run({"path": "file.txt"}, context(ws))
        self.assertEqual(out.status, r.OK)
        self.assertTrue(flags[0] & os.O_NONBLOCK)

    def test_what_was_opened_is_checked_again_and_must_be_a_regular_file(self) -> None:
        ws = workspace(self, {"file.txt": "safe\n"})
        real = os.stat(ws.root / "file.txt")
        fifo = SimpleNamespace(st_mode=stat.S_IFIFO | 0o600, st_size=real.st_size, st_mtime_ns=real.st_mtime_ns)
        with patch.object(read.os, "fstat", return_value=fifo):
            out = registry().dispatch(b.ToolCall("c1", "read", {"path": "file.txt"}), context(ws))
        self.assertEqual(out.status, r.ERROR)
        self.assertIn("not a regular file", out.model_text)

    def test_a_folder_listing_keeps_its_entry_bound(self) -> None:
        ws = workspace(self, {"a.txt": "a", "b.txt": "b"})
        with patch.object(read, "MAX_DIR_ENTRIES", 1):
            out = read.run({"path": "."}, context(ws))
        self.assertNotIn("b.txt", out.text)
        self.assertIn("1 more entries not shown", out.text)

    def test_a_scan_cut_short_records_no_digest(self) -> None:
        ws = workspace(self, {"file.txt": "row\n" * 20000})
        with patch.object(read, "MAX_SCAN_BYTES", 65536):
            out = read.run({"path": "file.txt", "limit": 1}, context(ws))
        self.assertEqual(out.status, r.OK)
        self.assertIsNone(ws.ledger.get(ws.root / "file.txt"))


class SearchGuards(unittest.TestCase):
    def test_a_file_over_the_size_bound_is_skipped(self) -> None:
        ws = workspace(self, {"file.txt": "needle\n"})
        answer = search_worker.search({"root": str(ws.root), "base": str(ws.root / "file.txt"), "pattern": "needle",
                                       "max_results": 10, "max_line_chars": 500, "max_file_bytes": 3})
        self.assertEqual(answer["skipped_large"], 1)
        self.assertEqual(answer["matches"], [])


class StreamGuards(unittest.TestCase):
    def test_an_unfinished_line_over_the_bound_stops_before_another_read(self) -> None:
        buffer = io.BytesIO(b"data: " + b"x" * 20)
        pulls: list[int] = []

        def pull(n: int) -> bytes:
            pulls.append(n)
            return buffer.read(n)

        with patch.object(sse, "MAX_EVENT_CHARS", 12), self.assertRaises(sse.SSEError):
            list(sse.iter_events(pull, chunk_size=13))
        self.assertEqual(len(pulls), 1)

    def test_message_stop_with_a_block_still_open_is_refused(self) -> None:
        provider = a.AnthropicProvider("synthetic-unit-test-marker")
        state = a._StreamState("req-unit")
        state.feed({"type": "message_start", "message": {}}, None, provider)
        state.feed({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}, None,
                   provider)
        state.feed({"type": "message_stop"}, None, provider)
        with self.assertRaises(b.ProtocolError):
            state.finish()

    def test_a_block_started_twice_is_refused(self) -> None:
        provider = a.AnthropicProvider("synthetic-unit-test-marker")
        state = a._StreamState("req-unit")
        state.feed({"type": "message_start", "message": {}}, None, provider)
        block = {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}
        state.feed(block, None, provider)
        with self.assertRaises(b.ProtocolError):
            state.feed(block, None, provider)


class ValueGuards(unittest.TestCase):
    def test_a_strings_max_length_is_checked_before_the_tool_runs(self) -> None:
        ws = workspace(self)
        ran: list[bool] = []
        schema = {"type": "object", "properties": {"s": {"type": "string", "maxLength": 1}}, "required": ["s"]}
        tool = r.Tool(b.ToolSpec("synthetic", "", schema), lambda args, ctx: ran.append(True) or r.ToolOutput("ok"))
        out = r.ToolRegistry([tool]).dispatch(b.ToolCall("c1", "synthetic", {"s": "ab"}), context(ws))
        self.assertEqual(out.status, r.ERROR)
        self.assertEqual(ran, [])

    def test_a_measurements_quality_must_be_one_of_three(self) -> None:
        with self.assertRaises(ValueError):
            b.Measurement(1, "invented", "observed")

    def test_a_known_measurement_is_never_negative(self) -> None:
        with self.assertRaises(ValueError):
            b.observed(-1)


if __name__ == "__main__":
    unittest.main()
