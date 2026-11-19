"""The MCP client against a scripted stub server in the test's own process (support.StubServer)."""

from __future__ import annotations

import threading
import time
import unittest
from unittest import mock

from recursion_core.control.mcp import jsonrpc
from recursion_core.mcp.client import (
    BAD_ANSWER,
    CALL_ABANDONED,
    CALL_DEADLINE_S,
    CALL_TIMEOUT,
    CLIENT_SHUT,
    INITIALIZE_REFUSED,
    OUTPUT_FAILED,
    LATEST_PROTOCOL,
    LEGACY_PROTOCOL,
    MAX_MESSAGE_BYTES,
    MESSAGE_TOO_LARGE,
    PROTOCOL_UNSUPPORTED,
    SERVER_GONE,
    SERVER_REQUEST_REFUSED,
    McpClientError,
    McpServerError,
    McpSessionEnded,
)
from recursion_core.mcp.client import session
from recursion_core.mcp.client.tests.support import HANG_GUARD_S, InThread, make_client, open_session

REFUSED_METHODS = ("sampling/createMessage", "elicitation/create", "roots/list", "vendor/unknown-method")


class FirstValues(unittest.TestCase):
    def test_the_two_bounds_this_slice_uses(self):
        self.assertEqual(2 * 1024 * 1024, MAX_MESSAGE_BYTES)
        self.assertEqual(60.0, CALL_DEADLINE_S)


class ServerRequestsAreRefused(unittest.TestCase):
    def test_requests_sent_mid_call_are_refused_and_the_call_finishes(self):
        records: list = []
        client, stub = make_client(self, on_record=records.append)
        open_session(self, client, stub)
        call = InThread(lambda: client.call_tool("echo", {"text": "hi"}))
        request = stub.expect(self, "tools/call")
        self.assertEqual({"name": "echo", "arguments": {"text": "hi"}, "_meta": {"progressToken": request["id"]}},
                         request["params"])
        for number, method in enumerate(REFUSED_METHODS):
            stub.send({"jsonrpc": "2.0", "id": f"s{number}", "method": method, "params": {}})
            answer = stub.next()
            self.assertEqual(f"s{number}", answer["id"], method)
            self.assertEqual(jsonrpc.METHOD_NOT_FOUND, answer["error"]["code"], method)
            self.assertNotIn("result", answer)
        self.assertFalse(call.finished(), "the call is still waiting for its own answer")
        stub.answer(request, {"content": [{"type": "text", "text": "hi"}], "isError": False})
        self.assertEqual({"content": [{"type": "text", "text": "hi"}], "isError": False}, call.get())
        self.assertEqual([{"record": SERVER_REQUEST_REFUSED, "method": m} for m in REFUSED_METHODS], records)

    def test_the_servers_ping_is_answered(self):
        records: list = []
        client, stub = make_client(self, on_record=records.append)
        open_session(self, client, stub)
        stub.send({"jsonrpc": "2.0", "id": 7, "method": "ping"})
        self.assertEqual({"jsonrpc": "2.0", "id": 7, "result": {}}, stub.next())
        self.assertEqual([], records)


    def test_a_request_that_is_not_valid_json_rpc_is_answered_as_invalid(self):
        records: list = []
        client, stub = make_client(self, on_record=records.append)
        open_session(self, client, stub)
        stub.send({"jsonrpc": "1.0", "id": "s1", "method": "roots/list"})
        answer = stub.next()
        self.assertEqual("s1", answer["id"])
        self.assertEqual(jsonrpc.INVALID_REQUEST, answer["error"]["code"])
        self.assertEqual([], records)


class Negotiation(unittest.TestCase):
    def test_a_legacy_answer_to_the_newer_request_continues_on_legacy(self):
        client, stub = make_client(self)
        opening = InThread(client.open)
        request = stub.expect(self, "initialize")
        self.assertEqual(LATEST_PROTOCOL, request["params"]["protocolVersion"])
        self.assertEqual({}, request["params"]["capabilities"])
        stub.answer(request, {"protocolVersion": LEGACY_PROTOCOL, "capabilities": {"tools": {}},
                              "serverInfo": {"name": "stub", "version": "1"}})
        stub.expect(self, "notifications/initialized")
        opening.get()
        self.assertEqual(LEGACY_PROTOCOL, client.protocol_version)
        listing = InThread(client.list_tools)
        stub.answer(stub.expect(self, "tools/list"), {"tools": [{"name": "a"}]})
        self.assertEqual([{"name": "a"}], listing.get())

    def test_the_newer_answer_continues_on_the_newer_revision(self):
        client, stub = make_client(self)
        open_session(self, client, stub, version=LATEST_PROTOCOL)
        self.assertEqual(LATEST_PROTOCOL, client.protocol_version)

    def test_a_refused_newer_request_is_asked_again_for_legacy(self):
        client, stub = make_client(self)
        opening = InThread(client.open)
        first = stub.expect(self, "initialize")
        stub.send(jsonrpc.error(first["id"], jsonrpc.INVALID_PARAMS, "Unsupported protocol version"))
        second = stub.expect(self, "initialize")
        self.assertEqual(LEGACY_PROTOCOL, second["params"]["protocolVersion"])
        self.assertNotEqual(first["id"], second["id"])
        stub.answer(second, {"protocolVersion": LEGACY_PROTOCOL, "capabilities": {}})
        stub.expect(self, "notifications/initialized")
        opening.get()
        self.assertEqual(LEGACY_PROTOCOL, client.protocol_version)

    def test_a_second_refusal_ends_the_session(self):
        client, stub = make_client(self)
        opening = InThread(client.open)
        for _ in range(2):
            request = stub.expect(self, "initialize")
            stub.send(jsonrpc.error(request["id"], jsonrpc.INVALID_PARAMS, "no"))
        with self.assertRaises(McpSessionEnded) as caught:
            opening.get()
        self.assertEqual(INITIALIZE_REFUSED, caught.exception.code)
        self.assertEqual(INITIALIZE_REFUSED, client.ending)

    def test_a_revision_the_client_does_not_speak_ends_the_session(self):
        for asked_twice, answered in ((False, "2024-11-05"), (True, LATEST_PROTOCOL)):
            with self.subTest(answered=answered, asked_twice=asked_twice):
                client, stub = make_client(self)
                opening = InThread(client.open)
                request = stub.expect(self, "initialize")
                if asked_twice:  # asked for legacy, answered with the newer one
                    stub.send(jsonrpc.error(request["id"], jsonrpc.INVALID_PARAMS, "no"))
                    request = stub.expect(self, "initialize")
                stub.answer(request, {"protocolVersion": answered, "capabilities": {}})
                with self.assertRaises(McpSessionEnded) as caught:
                    opening.get()
                self.assertEqual(PROTOCOL_UNSUPPORTED, caught.exception.code)
                self.assertEqual(PROTOCOL_UNSUPPORTED, client.ending)
                self.assertIsNone(client.protocol_version)


class Tools(unittest.TestCase):
    def test_tools_are_listed_page_by_page(self):
        client, stub = make_client(self)
        open_session(self, client, stub)
        listing = InThread(client.list_tools)
        first = stub.expect(self, "tools/list")
        self.assertNotIn("cursor", first["params"])
        stub.answer(first, {"tools": [{"name": "a"}], "nextCursor": "page-2"})
        second = stub.expect(self, "tools/list")
        self.assertEqual("page-2", second["params"]["cursor"])
        stub.answer(second, {"tools": [{"name": "b"}]})
        self.assertEqual([{"name": "a"}, {"name": "b"}], listing.get())
        self.assertEqual([{"name": "a"}, {"name": "b"}], client.tools)

    def test_a_cursor_given_twice_is_a_bad_answer(self):
        client, stub = make_client(self)
        open_session(self, client, stub)
        listing = InThread(client.list_tools)
        stub.answer(stub.expect(self, "tools/list"), {"tools": [], "nextCursor": "same"})
        stub.answer(stub.expect(self, "tools/list"), {"tools": [], "nextCursor": "same"})
        with self.assertRaises(McpClientError) as caught:
            listing.get()
        self.assertEqual(BAD_ANSWER, caught.exception.code)

    def test_the_list_is_read_again_on_list_changed(self):
        seen: list = []
        listed = threading.Event()
        client, stub = make_client(self, on_tools=lambda tools: (seen.append(tools), listed.set()))
        open_session(self, client, stub)
        stub.send({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})
        stub.answer(stub.expect(self, "tools/list"), {"tools": [{"name": "new"}]})
        self.assertTrue(listed.wait(HANG_GUARD_S))
        self.assertEqual([[{"name": "new"}]], seen)
        self.assertEqual([{"name": "new"}], client.tools)


class Calls(unittest.TestCase):
    def test_progress_reaches_the_callback_before_the_result(self):
        progress: list = []
        client, stub = make_client(self)
        open_session(self, client, stub)
        call = InThread(lambda: client.call_tool("slow", on_progress=progress.append))
        request = stub.expect(self, "tools/call")
        token = request["params"]["_meta"]["progressToken"]
        stub.send({"jsonrpc": "2.0", "method": "notifications/progress",
                   "params": {"progressToken": "not-ours", "progress": 9}})
        for step in (1, 2):
            stub.send({"jsonrpc": "2.0", "method": "notifications/progress",
                       "params": {"progressToken": token, "progress": step, "total": 2}})
        stub.answer(request, {"content": [], "isError": False})
        call.get()
        self.assertEqual([1, 2], [p["progress"] for p in progress])

    def test_a_call_past_its_deadline_is_cancelled_and_its_late_answer_dropped(self):
        client, stub = make_client(self)
        open_session(self, client, stub)
        started = time.monotonic()
        call = InThread(lambda: client.call_tool("slow", deadline_s=0.2))
        request = stub.expect(self, "tools/call")
        with self.assertRaises(McpClientError) as caught:
            call.get()
        self.assertEqual(CALL_TIMEOUT, caught.exception.code)
        self.assertLess(time.monotonic() - started, HANG_GUARD_S)
        cancelled = stub.expect(self, "notifications/cancelled")
        self.assertEqual(request["id"], cancelled["params"]["requestId"])
        stub.answer(request, {"content": [{"type": "text", "text": "late"}]})  # dropped
        again = InThread(lambda: client.call_tool("fast"))
        second = stub.expect(self, "tools/call")
        stub.answer(second, {"content": [{"type": "text", "text": "second"}]})
        self.assertEqual("second", again.get()["content"][0]["text"])
        self.assertIsNone(client.ending)

    def test_an_abandoned_call_is_cancelled(self):
        client, stub = make_client(self)
        open_session(self, client, stub)
        handle = client.begin_call("slow")
        request = stub.expect(self, "tools/call")
        handle.abandon()
        with self.assertRaises(McpClientError) as caught:
            handle.wait()
        self.assertEqual(CALL_ABANDONED, caught.exception.code)
        self.assertEqual(request["id"], stub.expect(self, "notifications/cancelled")["params"]["requestId"])

    def test_a_server_that_does_not_read_never_holds_the_caller(self):
        client, stub = make_client(self)
        open_session(self, client, stub)
        stub.stall()  # the stub reads one more line at most, then nothing
        text = "x" * (512 * 1024)  # far more than a pipe holds while nobody reads it

        def calls() -> None:
            for _ in range(4):
                client.begin_call("big", {"text": text}, deadline_s=0.2)
            client.call_tool("big", deadline_s=0.2)

        with self.assertRaises(McpClientError) as caught:
            InThread(calls).get()
        self.assertEqual(CALL_TIMEOUT, caught.exception.code)
        self.assertIsNone(client.ending)

    def test_a_server_error_answer_is_typed(self):
        client, stub = make_client(self)
        open_session(self, client, stub)
        call = InThread(lambda: client.call_tool("nope"))
        request = stub.expect(self, "tools/call")
        stub.send(jsonrpc.error(request["id"], jsonrpc.INVALID_PARAMS, "Unknown tool: nope"))
        with self.assertRaises(McpServerError) as caught:
            call.get()
        self.assertEqual(jsonrpc.INVALID_PARAMS, caught.exception.error["code"])
        self.assertIsNone(client.ending)


    def test_a_result_that_is_not_an_object_is_a_bad_answer(self):
        client, stub = make_client(self)
        open_session(self, client, stub)
        call = InThread(lambda: client.call_tool("odd"))
        stub.answer(stub.expect(self, "tools/call"), [1, 2])
        with self.assertRaises(McpClientError) as caught:
            call.get()
        self.assertEqual(BAD_ANSWER, caught.exception.code)

    def test_an_answer_whose_id_is_only_numerically_equal_is_not_taken(self):
        client, stub = make_client(self)
        open_session(self, client, stub)
        call = InThread(lambda: client.call_tool("exact"))
        request = stub.expect(self, "tools/call")
        stub.send({"jsonrpc": "2.0", "id": float(request["id"]), "result": {"content": "float"}})
        stub.answer(request, {"content": "int"})
        self.assertEqual({"content": "int"}, call.get())

    def test_the_named_deadline_bounds_every_call(self):
        client, stub = make_client(self)
        open_session(self, client, stub)
        with mock.patch.object(session, "CALL_DEADLINE_S", 0.2):
            for asked in ({}, {"deadline_s": 3600.0}):
                with self.subTest(asked=asked):
                    call = InThread(lambda: client.call_tool("slow", **asked))
                    stub.expect(self, "tools/call")
                    with self.assertRaises(McpClientError) as caught:
                        call.get()
                    self.assertEqual(CALL_TIMEOUT, caught.exception.code)
                    stub.expect(self, "notifications/cancelled")


class Ending(unittest.TestCase):
    def test_a_line_over_two_mebibytes_ends_the_session_typed(self):
        client, stub = make_client(self)
        open_session(self, client, stub)
        call = InThread(lambda: client.call_tool("big"))
        stub.expect(self, "tools/call")
        stub.send_bytes(b"x" * (MAX_MESSAGE_BYTES + 100) + b"\n")
        with self.assertRaises(McpSessionEnded) as caught:
            call.get()
        self.assertEqual(MESSAGE_TOO_LARGE, caught.exception.code)
        self.assertEqual(MESSAGE_TOO_LARGE, client.ending)
        self.assertIsNone(stub.next(), "the client shut the server's input")
        with self.assertRaises(McpSessionEnded):
            client.call_tool("after")

    def test_a_line_of_exactly_two_mebibytes_is_read(self):
        client, stub = make_client(self)
        open_session(self, client, stub)
        call = InThread(lambda: client.call_tool("big"))
        request = stub.expect(self, "tools/call")
        head = ('{"jsonrpc":"2.0","id":%d,"result":{"content":[]}}' % request["id"]).encode()
        stub.send_bytes(head + b" " * (MAX_MESSAGE_BYTES - len(head) - 1) + b"\n")
        self.assertEqual({"content": []}, call.get())
        self.assertIsNone(client.ending)

    def test_end_of_the_servers_output_ends_a_waiting_call(self):
        client, stub = make_client(self)
        open_session(self, client, stub)
        call = InThread(lambda: client.call_tool("slow"))
        stub.expect(self, "tools/call")
        stub.end_output()
        with self.assertRaises(McpSessionEnded) as caught:
            call.get()
        self.assertEqual(SERVER_GONE, caught.exception.code)

    def test_a_write_the_server_does_not_take_ends_the_session(self):
        client, stub = make_client(self)
        open_session(self, client, stub)
        stub.stall()
        client.begin_call("last-read")
        stub.expect(self, "tools/call")
        stub.shut_input()
        with self.assertRaises(McpSessionEnded) as caught:
            client.call_tool("unread")
        self.assertEqual(OUTPUT_FAILED, caught.exception.code)
        self.assertEqual(OUTPUT_FAILED, client.ending)

    def test_close_ends_a_waiting_call_and_shuts_the_servers_input(self):
        client, stub = make_client(self)
        open_session(self, client, stub)
        call = InThread(lambda: client.call_tool("slow"))
        stub.expect(self, "tools/call")
        client.close()
        with self.assertRaises(McpSessionEnded) as caught:
            call.get()
        self.assertEqual(CLIENT_SHUT, caught.exception.code)
        self.assertIsNone(stub.next(), "the server reads end of input")


if __name__ == "__main__":
    unittest.main()
