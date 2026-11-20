"""The MCP client's limits on an outside server (``limits.py``): an outside server is untrusted input.

Every limit that fires is a typed result the caller receives and a record handed to ``on_record``; each one
cuts off only that server's session, never another session. The count of breaches belongs to one client
session: a new session starts it again.
"""

from __future__ import annotations

import queue
import threading
import time
import unittest
from unittest import mock

from recursion_core.mcp.client import (
    CALL_DEADLINE_S,
    CALL_TIMEOUT,
    INITIALIZE_DEADLINE_S,
    MAX_BREACHES,
    MAX_MESSAGE_BYTES,
    MAX_TOOL_RESULT_TEXT_BYTES,
    MAX_TOOLS,
    MESSAGE_TOO_LARGE,
    RESULT_CUT,
    SERVER_CUT_OFF,
    SERVER_START_TIMEOUT,
    TOOL_LIST_CUT,
    McpClientError,
    McpSessionEnded,
)
from recursion_core.mcp.client import limits, session
from recursion_core.mcp.client.tests.support import SHORT, InThread, make_client, open_session

KIB = 1024
MIB = 1024 * 1024


def _tools(count: int, start: int = 0) -> list:
    return [{"name": f"t{number}"} for number in range(start, start + count)]


def _text(result: dict) -> str:
    return "".join(item["text"] for item in result["content"] if item.get("type") == "text")


class FirstValues(unittest.TestCase):
    def test_the_six_first_values(self):
        self.assertEqual(5.0, INITIALIZE_DEADLINE_S)
        self.assertEqual(60.0, CALL_DEADLINE_S)
        self.assertEqual(2 * MIB, MAX_MESSAGE_BYTES)
        self.assertEqual(256 * KIB, MAX_TOOL_RESULT_TEXT_BYTES)
        self.assertEqual(512, MAX_TOOLS)
        self.assertEqual(3, MAX_BREACHES)

    def test_each_value_has_one_home(self):
        for name in ("INITIALIZE_DEADLINE_S", "CALL_DEADLINE_S", "MAX_MESSAGE_BYTES",
                     "MAX_TOOL_RESULT_TEXT_BYTES", "MAX_TOOLS", "MAX_BREACHES"):
            with self.subTest(name=name):
                self.assertEqual(getattr(limits, name), getattr(session, name))


class Start(unittest.TestCase):
    def test_a_server_that_answers_initialize_after_ten_seconds_is_given_up_at_five(self):
        records: list = []
        client, stub = make_client(self, on_record=records.append)
        started = time.monotonic()
        opening = InThread(client.open)
        request = stub.expect(self, "initialize")
        late = threading.Timer(10.0, stub.answer, (request, {"protocolVersion": "2025-06-18", "capabilities": {}}))
        late.daemon = True
        late.start()
        self.addCleanup(late.cancel)
        with self.assertRaises(McpSessionEnded) as caught:
            opening.get()
        self.assertLess(time.monotonic() - started, 10.0, "given up before the late answer")
        self.assertEqual(SERVER_START_TIMEOUT, caught.exception.code)
        self.assertEqual(SERVER_START_TIMEOUT, client.ending)
        self.assertIsNone(client.protocol_version)
        self.assertEqual([SERVER_START_TIMEOUT], [r["record"] for r in records])
        self.assertIsNone(stub.next(), "no cancel for initialize; the client shut the server's input")
        with self.assertRaises(McpSessionEnded) as after:
            client.list_tools()
        self.assertEqual(SERVER_START_TIMEOUT, after.exception.code)

    def test_one_start_budget_covers_the_second_request(self):
        client, stub = make_client(self)
        with mock.patch.object(session, "INITIALIZE_DEADLINE_S", 0.3):
            opening = InThread(client.open)
            first = stub.expect(self, "initialize")
            stub.send({"jsonrpc": "2.0", "id": first["id"], "error": {"code": -32602, "message": "no"}})
            stub.expect(self, "initialize")  # never answered
            with self.assertRaises(McpSessionEnded) as caught:
                opening.get()
        self.assertEqual(SERVER_START_TIMEOUT, caught.exception.code)

    def test_a_longer_deadline_asked_for_open_is_bounded(self):
        client, stub = make_client(self)
        with mock.patch.object(session, "INITIALIZE_DEADLINE_S", 0.3):
            opening = InThread(lambda: client.open(deadline_s=3600.0))
            stub.expect(self, "initialize")
            with self.assertRaises(McpSessionEnded) as caught:
                opening.get()
        self.assertEqual(SERVER_START_TIMEOUT, caught.exception.code)


class ToolResult(unittest.TestCase):
    def test_a_one_mebibyte_result_is_cut_at_256_kib_and_marked(self):
        records: list = []
        client, stub = make_client(self, on_record=records.append)
        open_session(self, client, stub)
        call = InThread(lambda: client.call_tool("big"))
        request = stub.expect(self, "tools/call")
        stub.answer(request, {"content": [{"type": "text", "text": "x" * MIB}], "isError": False})
        result = call.get()
        self.assertEqual(256 * KIB, len(_text(result).encode()))
        self.assertEqual({"limit_bytes": 256 * KIB, "text_bytes": MIB}, result["_meta"][RESULT_CUT])
        self.assertFalse(result["isError"])
        self.assertEqual([RESULT_CUT], [r["record"] for r in records])
        self.assertIsNone(client.ending)

    def test_a_result_at_the_limit_is_untouched(self):
        client, stub = make_client(self)
        open_session(self, client, stub)
        call = InThread(lambda: client.call_tool("full"))
        # the server's _meta, {"k":1}, is 7 bytes of the same budget
        content = [{"type": "text", "text": "a" * (128 * KIB)}, {"type": "text", "text": "b" * (128 * KIB - 7)}]
        stub.answer(stub.expect(self, "tools/call"), {"content": content, "_meta": {"k": 1}})
        self.assertEqual({"content": content, "_meta": {"k": 1}}, call.get())

    def test_the_cut_counts_text_only_and_keeps_whole_characters(self):
        client, stub = make_client(self)
        open_session(self, client, stub)
        image = {"type": "image", "data": "AAAA", "mimeType": "image/png"}
        with mock.patch.object(session, "MAX_TOOL_RESULT_TEXT_BYTES", 5):
            call = InThread(lambda: client.call_tool("mixed"))
            stub.answer(stub.expect(self, "tools/call"), {"content": [
                {"type": "text", "text": "abc"}, image, {"type": "text", "text": "déf"},
                {"type": "text", "text": "gone"}]})
            result = call.get()
        self.assertEqual([{"type": "text", "text": "abc"}, image, {"type": "text", "text": "d"}], result["content"])
        self.assertEqual({"limit_bytes": 5, "text_bytes": 3 + 4 + 4}, result["_meta"][RESULT_CUT])


class ToolList(unittest.TestCase):
    def test_a_list_of_600_tools_is_listed_to_512_typed(self):
        records: list = []
        client, stub = make_client(self, on_record=records.append)
        open_session(self, client, stub)
        listing = InThread(client.list_tools)
        stub.answer(stub.expect(self, "tools/list"), {"tools": _tools(600)})
        tools = listing.get()
        self.assertEqual(_tools(512), tools)
        self.assertEqual(TOOL_LIST_CUT, tools.cut)
        self.assertEqual(_tools(512), client.tools)
        self.assertEqual([{"record": TOOL_LIST_CUT, "limit": 512, "listed": 600}], records)
        self.assertIsNone(client.ending)

    def test_no_page_is_asked_for_past_the_limit(self):
        client, stub = make_client(self)
        open_session(self, client, stub)
        listing = InThread(client.list_tools)
        stub.answer(stub.expect(self, "tools/list"), {"tools": _tools(300), "nextCursor": "2"})
        stub.answer(stub.expect(self, "tools/list"), {"tools": _tools(300, 300), "nextCursor": "3"})
        tools = listing.get()
        self.assertEqual(_tools(512), tools)
        self.assertEqual(TOOL_LIST_CUT, tools.cut)
        with self.assertRaises(queue.Empty):
            stub.next(timeout=SHORT)

    def test_a_list_of_exactly_512_is_whole(self):
        client, stub = make_client(self)
        open_session(self, client, stub)
        listing = InThread(client.list_tools)
        stub.answer(stub.expect(self, "tools/list"), {"tools": _tools(512)})
        tools = listing.get()
        self.assertEqual(_tools(512), tools)
        self.assertIsNone(tools.cut)


class CutOff(unittest.TestCase):
    def _cut_result(self, client, stub) -> dict:
        call = InThread(lambda: client.call_tool("big"))
        stub.answer(stub.expect(self, "tools/call"), {"content": [{"type": "text", "text": "x" * 64}]})
        return call.get()

    def _timed_out(self, client, stub) -> McpClientError:
        """One call past the client's own deadline (lowered here to 0.1 s), a breach; its request is read."""

        with mock.patch.object(session, "CALL_DEADLINE_S", 0.1), self.assertRaises(McpClientError) as caught:
            InThread(lambda: client.call_tool("slow")).get()
        stub.expect(self, "tools/call")
        return caught.exception

    def test_a_third_breach_cuts_the_server_off_for_the_rest_of_the_session(self):
        records: list = []
        client, stub = make_client(self, on_record=records.append)
        open_session(self, client, stub)
        for _ in range(2):
            self._timed_out(client, stub)
            stub.expect(self, "notifications/cancelled")
            self.assertIsNone(client.ending)
        third = self._timed_out(client, stub)
        self.assertEqual(CALL_TIMEOUT, third.code, "the third breach still reaches its caller, typed")
        self.assertEqual(SERVER_CUT_OFF, client.ending)
        self.assertEqual([CALL_TIMEOUT] * 3 + [SERVER_CUT_OFF], [r["record"] for r in records])
        self.assertEqual({"record": SERVER_CUT_OFF, "breaches": 3}, records[-1])
        stub.expect(self, "notifications/cancelled")  # sent for the third call before the session ended
        self.assertIsNone(stub.next(), "the client shut the server's input")
        with self.assertRaises(McpSessionEnded) as caught:
            client.call_tool("after")
        self.assertEqual(SERVER_CUT_OFF, caught.exception.code)

    def test_timeouts_at_the_call_limit_count_and_a_shorter_deadline_the_caller_asked_for_does_not(self):
        client, stub = make_client(self)
        open_session(self, client, stub)
        for _ in range(MAX_BREACHES):
            with self.assertRaises(McpClientError) as caught:
                InThread(lambda: client.call_tool("slow", deadline_s=0.1)).get()
            self.assertEqual(CALL_TIMEOUT, caught.exception.code)
            stub.expect(self, "tools/call")
            stub.expect(self, "notifications/cancelled")
        self.assertIsNone(client.ending)
        with mock.patch.object(session, "CALL_DEADLINE_S", 0.1):
            for number in range(MAX_BREACHES):
                with self.assertRaises(McpClientError) as caught:
                    InThread(lambda: client.call_tool("slow")).get()
                self.assertEqual(CALL_TIMEOUT, caught.exception.code)
                if number < MAX_BREACHES - 1:
                    self.assertIsNone(client.ending)
        self.assertEqual(SERVER_CUT_OFF, client.ending)

    def test_breaches_of_different_limits_add_up(self):
        client, stub = make_client(self)
        open_session(self, client, stub)
        with mock.patch.object(session, "MAX_TOOL_RESULT_TEXT_BYTES", 8), mock.patch.object(session, "MAX_TOOLS", 2):
            self._cut_result(client, stub)  # mended by the cut: recorded, not counted
            listing = InThread(client.list_tools)
            stub.answer(stub.expect(self, "tools/list"), {"tools": _tools(3)})
            self.assertEqual(TOOL_LIST_CUT, listing.get().cut)  # mended by the cut: recorded, not counted
        self._timed_out(client, stub)
        stub.expect(self, "notifications/cancelled")
        with mock.patch.object(session, "MAX_LIST_PAGES", 1):
            listing = InThread(client.list_tools)
            stub.answer(stub.expect(self, "tools/list"), {"tools": [], "nextCursor": "2"})
            self.assertEqual(TOOL_LIST_CUT, listing.get().cut)  # a listing that would not end: counted
        self.assertIsNone(client.ending)
        self._timed_out(client, stub)
        self.assertEqual(SERVER_CUT_OFF, client.ending)

    def test_a_new_session_starts_the_count_again(self):
        for _ in range(2):
            client, stub = make_client(self)
            open_session(self, client, stub)
            for _ in range(MAX_BREACHES - 1):
                self._timed_out(client, stub)
                stub.expect(self, "notifications/cancelled")
            self.assertIsNone(client.ending)

    def test_a_cut_off_server_leaves_another_session_untouched(self):
        cut, cut_stub = make_client(self)
        other, other_stub = make_client(self)
        open_session(self, cut, cut_stub)
        open_session(self, other, other_stub)
        for _ in range(MAX_BREACHES):
            self._timed_out(cut, cut_stub)
            cut_stub.expect(self, "notifications/cancelled")
        self.assertEqual(SERVER_CUT_OFF, cut.ending)
        call = InThread(lambda: other.call_tool("echo"))
        other_stub.answer(other_stub.expect(self, "tools/call"), {"content": [{"type": "text", "text": "ok"}]})
        self.assertEqual("ok", _text(call.get()))
        self.assertIsNone(other.ending)


class Message(unittest.TestCase):
    def test_a_message_over_two_mebibytes_is_refused_as_before_and_recorded(self):
        records: list = []
        client, stub = make_client(self, on_record=records.append)
        open_session(self, client, stub)
        call = InThread(lambda: client.call_tool("big"))
        stub.expect(self, "tools/call")
        stub.send_bytes(b"x" * (MAX_MESSAGE_BYTES + 1) + b"\n")
        with self.assertRaises(McpSessionEnded) as caught:
            call.get()
        self.assertEqual(MESSAGE_TOO_LARGE, caught.exception.code)
        self.assertEqual([{"record": MESSAGE_TOO_LARGE, "limit_bytes": MAX_MESSAGE_BYTES}], records)


if __name__ == "__main__":
    unittest.main()
