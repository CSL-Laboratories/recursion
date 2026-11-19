"""Behaviours of the MCP client's session that no other test holds: each test goes red when its behaviour is
taken out (support.StubServer, in the test's own process; one stream held in memory)."""

from __future__ import annotations

import io
import queue
import threading
import unittest

from recursion_core.mcp.client import (
    BAD_ANSWER,
    CALL_ABANDONED,
    CLIENT_SHUT,
    MAX_MESSAGE_BYTES,
    MESSAGE_TOO_LARGE,
    McpClient,
    McpClientError,
    McpSessionEnded,
)
from recursion_core.mcp.client import session
from recursion_core.mcp.client.tests.support import SHORT, InThread, make_client, open_session

PING = {"jsonrpc": "2.0", "id": 7, "method": "ping"}
PONG = {"jsonrpc": "2.0", "id": 7, "result": {}}
LIST_CHANGED = {"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}


class _CountingInput:
    """A server's output held in memory; counts every byte the client reads from it."""

    def __init__(self, data: bytes) -> None:
        self._data = io.BytesIO(data)
        self.read_bytes = 0

    def readline(self, size: int = -1) -> bytes:
        line = self._data.readline(size)
        self.read_bytes += len(line)
        return line


class SessionBehaviours(unittest.TestCase):
    def test_an_over_long_line_is_read_only_to_the_bound_plus_one_byte(self):
        server_output = _CountingInput(b"x" * (MAX_MESSAGE_BYTES + 1024 * 1024) + b"\n")
        client = McpClient(server_output, io.BytesIO())
        self.addCleanup(client.close)
        with self.assertRaises(McpSessionEnded) as caught:
            client.open(deadline_s=5.0)
        self.assertEqual(MESSAGE_TOO_LARGE, caught.exception.code)
        self.assertLessEqual(server_output.read_bytes, MAX_MESSAGE_BYTES + 1)

    def test_bad_lines_are_dropped_and_the_waiting_call_still_answered(self):
        client, stub = make_client(self)
        open_session(self, client, stub)
        call = InThread(lambda: client.call_tool("wait"))
        request = stub.expect(self, "tools/call")
        stub.send_bytes(b"\n" + b"\xff\xfe\n" + b"not json\n" + b'{"jsonrpc":\n')
        stub.send(PING)
        self.assertEqual(PONG, stub.next(), "nothing was written back for the bad lines")
        stub.answer(request, {"content": "answered"})
        self.assertEqual({"content": "answered"}, call.get())
        self.assertIsNone(client.ending)

    def test_an_abandoned_call_leaves_the_calls_in_flight_and_its_late_answer_is_dropped(self):
        client, stub = make_client(self)
        open_session(self, client, stub)
        handle = client.begin_call("slow")
        request = stub.expect(self, "tools/call")
        handle.abandon()
        self.assertEqual({}, client._pending, "the call is no longer in flight")
        handle.abandon()  # nothing more: it is no longer in flight
        self.assertEqual(request["id"], stub.expect(self, "notifications/cancelled")["params"]["requestId"])
        stub.answer(request, {"content": "late"})
        stub.send(PING)
        self.assertEqual(PONG, stub.next(), "one cancel only, and nothing for the late answer")
        with self.assertRaises(McpClientError) as caught:
            handle.wait()
        self.assertEqual(CALL_ABANDONED, caught.exception.code)

    def _relist(self, notices_during_listing: int) -> None:
        seen: list = []
        listed = threading.Semaphore(0)
        client, stub = make_client(self, on_tools=lambda tools: (seen.append(tools), listed.release()))
        open_session(self, client, stub)
        stub.send(LIST_CHANGED)
        first = stub.expect(self, "tools/list")
        for _ in range(notices_during_listing):
            stub.send(LIST_CHANGED)
        stub.send(PING)
        self.assertEqual(PONG, stub.next(), "the notices during the listing start no listing of their own")
        stub.answer(first, {"tools": [{"name": "one"}]})
        stub.answer(stub.expect(self, "tools/list"), {"tools": [{"name": "two"}]})
        for _ in range(2):
            self.assertTrue(listed.acquire(timeout=10))
        with self.assertRaises(queue.Empty, msg="exactly one more listing"):
            stub.next(timeout=SHORT)
        self.assertEqual([[{"name": "one"}], [{"name": "two"}]], seen)

    def test_a_change_notice_during_a_listing_makes_exactly_one_more(self):
        self._relist(1)

    def test_two_change_notices_during_one_listing_make_one_more_not_two(self):
        self._relist(2)

    def test_a_cursor_that_is_not_a_string_is_a_bad_answer(self):
        for cursor in (5, ["page-2"]):
            with self.subTest(cursor=cursor):
                client, stub = make_client(self)
                open_session(self, client, stub)
                listing = InThread(client.list_tools)
                stub.answer(stub.expect(self, "tools/list"), {"tools": [], "nextCursor": cursor})
                with self.assertRaises(McpClientError) as caught:
                    listing.get()
                self.assertEqual(BAD_ANSWER, caught.exception.code)

    def test_tools_that_is_not_a_list_is_a_bad_answer(self):
        for page in ({"tools": {"a": {}}}, {}):
            with self.subTest(page=page):
                client, stub = make_client(self)
                open_session(self, client, stub)
                listing = InThread(client.list_tools)
                stub.answer(stub.expect(self, "tools/list"), page)
                with self.assertRaises(McpClientError) as caught:
                    listing.get()
                self.assertEqual(BAD_ANSWER, caught.exception.code)

    def test_a_callback_that_raises_leaves_the_session_open_and_the_call_answered(self):
        def boom(_value):
            raise ValueError("a caller's callback that raises")

        with self.subTest(callback="on_progress"):
            client, stub = make_client(self)
            open_session(self, client, stub)
            call = InThread(lambda: client.call_tool("slow", on_progress=boom))
            request = stub.expect(self, "tools/call")
            stub.send({"jsonrpc": "2.0", "method": "notifications/progress",
                       "params": {"progressToken": request["params"]["_meta"]["progressToken"], "progress": 1}})
            stub.answer(request, {"content": "done"})
            self.assertEqual({"content": "done"}, call.get())
            self.assertIsNone(client.ending)
        with self.subTest(callback="on_record"):
            client, stub = make_client(self, on_record=boom)
            open_session(self, client, stub)
            call = InThread(lambda: client.call_tool("slow"))
            request = stub.expect(self, "tools/call")
            stub.send({"jsonrpc": "2.0", "id": "s1", "method": "roots/list"})
            self.assertEqual("s1", stub.next()["id"])
            stub.answer(request, {"content": "done"})
            self.assertEqual({"content": "done"}, call.get())
            self.assertIsNone(client.ending)
        with self.subTest(callback="on_tools"):
            client, stub = make_client(self, on_tools=boom)
            open_session(self, client, stub)
            listing = InThread(client.list_tools)
            stub.answer(stub.expect(self, "tools/list"), {"tools": [{"name": "a"}]})
            self.assertEqual([{"name": "a"}], listing.get())
            self.assertIsNone(client.ending)

    def test_open_is_called_once(self):
        client, stub = make_client(self)
        open_session(self, client, stub)
        with self.assertRaises(RuntimeError):
            client.open(deadline_s=SHORT)
        stub.send(PING)
        self.assertEqual(PONG, stub.next(), "no second initialize was sent")
        self.assertIsNone(client.ending)

    def test_close_before_open_shuts_the_servers_input(self):
        client, stub = make_client(self)
        client.close()
        self.assertEqual(CLIENT_SHUT, client.ending)
        self.assertIsNone(stub.next(), "the server reads end of input")
        with self.assertRaises(RuntimeError):
            client.open()


class PublicNames(unittest.TestCase):
    def test_server_error_and_tool_call_are_exported(self):
        from recursion_core.mcp.client import SERVER_ERROR, ToolCall

        self.assertIs(session.SERVER_ERROR, SERVER_ERROR)
        self.assertIs(session.ToolCall, ToolCall)

    def test_every_name_of_all_is_present(self):
        import recursion_core.mcp.client as client_package

        self.assertTrue(client_package.__all__)
        for name in client_package.__all__:
            self.assertTrue(hasattr(client_package, name), name)


if __name__ == "__main__":
    unittest.main()
