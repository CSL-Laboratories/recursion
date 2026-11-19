"""The MCP client and a server's untrusted text, and a failed ``open`` (support.StubServer, in the test's own
process)."""

from __future__ import annotations

import json
import queue
import threading
import time
import unittest

from recursion_core.control.mcp import jsonrpc
from recursion_core.control.mcp.framing import decode_line
from recursion_core.mcp.client import (
    BAD_ANSWER,
    CALL_TIMEOUT,
    LATEST_PROTOCOL,
    MAX_SERVER_TEXT_CHARS,
    PROTOCOL_UNSUPPORTED,
    SERVER_REQUEST_REFUSED,
    McpClientError,
    McpServerError,
    McpSessionEnded,
)
from recursion_core.mcp.client import limits, session
from recursion_core.mcp.client.tests.support import InThread, make_client, open_session

PING = {"jsonrpc": "2.0", "id": 7, "method": "ping"}
PONG = {"jsonrpc": "2.0", "id": 7, "result": {}}

# The smallest shape found that holds a call past DEADLINE_S + MARGIN_S when the whole message is printed, on
# both interpreters (one 9,900-deep array: 15 ms on 3.12, 29 ms on 3.14t; 56 of them about 0.8 s and 1.6 s).
NESTED_DEPTH = 9_900
NESTED_COUNT = 56
DEADLINE_S = 0.2
MARGIN_S = 0.3  # decoding the line and handing the error over; well under what printing it costs


def _error_line(request_id: int, message_json: str) -> bytes:
    return ('{"jsonrpc":"2.0","id":%d,"error":{"code":-32000,"message":%s}}\n' % (request_id, message_json)).encode()


def _decodes_on_a_thread(line: bytes) -> bool:
    decoded: list = []
    reader = threading.Thread(target=lambda: decoded.append(decode_line(line).error is None))
    reader.start()
    reader.join()
    return decoded == [True]


class ServerText(unittest.TestCase):
    def test_the_bound_is_one_named_constant_in_the_limits_module(self):
        import recursion_core.mcp.client as client_package

        self.assertEqual(200, limits.MAX_SERVER_TEXT_CHARS)
        self.assertIs(limits.MAX_SERVER_TEXT_CHARS, MAX_SERVER_TEXT_CHARS)
        self.assertIn("MAX_SERVER_TEXT_CHARS", client_package.__all__)

    def test_only_text_is_taken_and_any_other_value_is_marked_with_its_type(self):
        for value, marked in ((42, "(not text: int)"), (1.5, "(not text: float)"), (True, "(not text: bool)"),
                              (None, "(not text: null)"), ([1], "(not text: list)"), ({"a": 1}, "(not text: dict)")):
            with self.subTest(value=value):
                self.assertEqual(marked, session._server_text(value))

    def test_text_is_cut_first_and_only_then_escaped(self):
        self.assertEqual("x" * MAX_SERVER_TEXT_CHARS, session._server_text("x" * (10 * MAX_SERVER_TEXT_CHARS)))
        self.assertEqual("\\x1b" * MAX_SERVER_TEXT_CHARS, session._server_text("\x1b" * (5 * MAX_SERVER_TEXT_CHARS)))
        self.assertEqual("a\\x1b[31m\\tb", session._server_text("a\x1b[31m\tb"))
        self.assertEqual("roots/\\udfff", session._server_text("roots/\udfff"))
        self.assertEqual("\\x00\\x7f\\x85", session._server_text("\x00\x7f\x85"))
        self.assertEqual("plain text, ünïcode", session._server_text("plain text, ünïcode"))


class ServerErrorText(unittest.TestCase):
    def test_a_deeply_nested_error_message_returns_within_the_deadline_and_reading_goes_on(self):
        client, stub = make_client(self)
        open_session(self, client, stub)
        started = time.monotonic()
        nested = client.begin_call("nested", deadline_s=DEADLINE_S)
        first = stub.expect(self, "tools/call")
        other = InThread(lambda: client.call_tool("other"))
        second = stub.expect(self, "tools/call")
        array = "[" * NESTED_DEPTH + "]" * NESTED_DEPTH
        line = _error_line(first["id"], "[" + ",".join([array] * NESTED_COUNT) + "]")
        if not _decodes_on_a_thread(line):
            self.skipTest("this interpreter's decoder refuses a line nested this deep on a thread: the line is dropped")
        stub.send_bytes(line)
        stub.answer(second, {"content": "other"})
        with self.assertRaises(McpServerError) as caught:
            nested.wait()
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, DEADLINE_S + MARGIN_S, "the caller was held past its deadline")
        self.assertEqual("(not text: list)", caught.exception.detail)
        self.assertEqual({"content": "other"}, other.get(), "the reader went on reading")
        self.assertIsNone(client.ending)

    def test_a_message_that_is_not_text_is_kept_as_sent_and_never_printed(self):
        for message, detail in ((42, "(not text: int)"), ({"secret": [1, 2]}, "(not text: dict)")):
            with self.subTest(message=message):
                client, stub = make_client(self)
                open_session(self, client, stub)
                call = InThread(lambda: client.call_tool("odd"))
                request = stub.expect(self, "tools/call")
                error = {"code": -32000, "message": message, "data": {"x": 1}}
                stub.send({"jsonrpc": "2.0", "id": request["id"], "error": error})
                with self.assertRaises(McpServerError) as caught:
                    call.get()
                self.assertEqual(detail, caught.exception.detail)
                self.assertEqual(error, caught.exception.error)

    def test_a_long_or_shaped_message_is_cut_and_escaped(self):
        client, stub = make_client(self)
        open_session(self, client, stub)
        call = InThread(lambda: client.call_tool("odd"))
        request = stub.expect(self, "tools/call")
        stub.send(jsonrpc.error(request["id"], -32000, "\x1b[2J" + "y" * 5000))
        with self.assertRaises(McpServerError) as caught:
            call.get()
        self.assertEqual("\\x1b[2J" + "y" * (MAX_SERVER_TEXT_CHARS - 4), caught.exception.detail)

    def test_a_revision_that_is_not_text_is_marked_not_printed(self):
        client, stub = make_client(self)
        opening = InThread(client.open)
        stub.answer(stub.expect(self, "initialize"), {"protocolVersion": [[["2025-06-18"]]], "capabilities": {}})
        with self.assertRaises(McpSessionEnded) as caught:
            opening.get()
        self.assertEqual(PROTOCOL_UNSUPPORTED, caught.exception.code)
        self.assertEqual("(not text: list)", caught.exception.detail)


class FailedOpenEndsTheSession(unittest.TestCase):
    def _assert_ended(self, client, stub, opening, code):
        with self.assertRaises(McpClientError) as caught:
            opening.get()
        try:
            after = stub.next(timeout=5.0)  # None: the server reads end of input
        except queue.Empty:
            after = "nothing within 5 s"
        # (the code the caller is given, the session's ending, what the server reads next): no
        # notifications/cancelled for initialize, then end of input
        self.assertEqual((code, code, None), (caught.exception.code, client.ending, after))
        self.assertIsInstance(caught.exception, McpSessionEnded)
        self.assertIsNone(client.protocol_version)
        with self.assertRaises(McpSessionEnded) as later:
            client.call_tool("after")
        self.assertEqual(code, later.exception.code)
        with self.assertRaises(RuntimeError):
            client.open()

    def test_silence_after_initialize_ends_the_session_and_never_cancels_it(self):
        client, stub = make_client(self)
        opening = InThread(lambda: client.open(deadline_s=0.5))
        request = stub.expect(self, "initialize")
        self._assert_ended(client, stub, opening, CALL_TIMEOUT)
        stub.answer(request, {"protocolVersion": LATEST_PROTOCOL, "capabilities": {}})  # late: dropped
        self.assertEqual(CALL_TIMEOUT, client.ending)

    def test_silence_after_the_second_initialize_ends_the_session(self):
        client, stub = make_client(self)
        opening = InThread(lambda: client.open(deadline_s=0.5))
        first = stub.expect(self, "initialize")
        stub.send(jsonrpc.error(first["id"], jsonrpc.INVALID_PARAMS, "no"))
        stub.expect(self, "initialize")
        self._assert_ended(client, stub, opening, CALL_TIMEOUT)

    def test_an_initialize_result_that_is_not_an_object_ends_the_session(self):
        client, stub = make_client(self)
        opening = InThread(client.open)
        stub.answer(stub.expect(self, "initialize"), [LATEST_PROTOCOL])
        self._assert_ended(client, stub, opening, BAD_ANSWER)


class RefusedRequestRecord(unittest.TestCase):
    def _refuse(self, method, request_id="s1"):
        records: list = []
        client, stub = make_client(self, on_record=records.append)
        open_session(self, client, stub)
        stub.send_bytes((json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method}) + "\n").encode())
        answer = stub.next()
        self.assertEqual(request_id, answer["id"], "the id is echoed exactly as the server sent it")
        self.assertEqual(jsonrpc.METHOD_NOT_FOUND, answer["error"]["code"])
        stub.send(PING)
        self.assertEqual(PONG, stub.next())
        self.assertEqual(1, len(records))
        return records[0]

    def test_a_method_of_a_mebibyte_is_kept_as_a_marked_excerpt(self):
        method = "m" * (1024 * 1024)
        self.assertEqual({"record": SERVER_REQUEST_REFUSED, "method": "m" * MAX_SERVER_TEXT_CHARS,
                          "method_excerpt": {"length": len(method), "cut": True}}, self._refuse(method))

    def test_a_method_holding_escape_tab_or_a_lone_surrogate_is_escaped_and_marked(self):
        for method, kept in (("a\x1b[31m\tb", "a\\x1b[31m\\tb"), ("roots/\udfff", "roots/\\udfff")):
            with self.subTest(method=method):
                self.assertEqual({"record": SERVER_REQUEST_REFUSED, "method": kept,
                                  "method_excerpt": {"length": len(method), "cut": False}},
                                 self._refuse(method, request_id="id-\udfff"))

    def test_a_short_plain_method_is_recorded_as_sent(self):
        for method in ("roots/list", "r" * MAX_SERVER_TEXT_CHARS, "outils/liste-modifiée"):
            with self.subTest(length=len(method)):
                self.assertEqual({"record": SERVER_REQUEST_REFUSED, "method": method}, self._refuse(method))

    def test_one_character_over_the_bound_is_cut(self):
        method = "r" * (MAX_SERVER_TEXT_CHARS + 1)
        self.assertEqual({"record": SERVER_REQUEST_REFUSED, "method": "r" * MAX_SERVER_TEXT_CHARS,
                          "method_excerpt": {"length": MAX_SERVER_TEXT_CHARS + 1, "cut": True}}, self._refuse(method))


if __name__ == "__main__":
    unittest.main()
