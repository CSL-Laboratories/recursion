"""The client's second answer round on an outside server's input: every text of a tool result is cut, a cut
list keeps its mark, the start limit and a cut result are settled before any record is handed over, no line
is handled after the session's ending, what is written is what the caller sent at the call, and a number
that is not finite is refused.

Every server here is the stub of ``support`` over pipes in the test's own process: no file, no folder, no
process. A test that has to hold the client's writer inside a write (``_Jam``) stops the stub reading for a
while and lets it read again before the test ends.
"""

from __future__ import annotations

import ast
import json
import math
import pathlib
import threading
import time
import unittest
from unittest import mock

from recursion_core.control.mcp.framing import NOT_JSON, decode_line
from recursion_core.mcp.client import (
    BAD_ANSWER,
    CLIENT_SHUT,
    LEGACY_PROTOCOL,
    MAX_TOOL_RESULT_TEXT_BYTES,
    MAX_TOOLS,
    RESULT_CUT,
    SERVER_CUT_OFF,
    SERVER_REQUEST_REFUSED,
    SERVER_START_TIMEOUT,
    TOOL_LIST_CUT,
    McpClientError,
    McpSessionEnded,
)
from recursion_core.mcp.client import limits, session
from recursion_core.mcp.client.tests.support import (
    HANG_GUARD_S,
    SHORT,
    InThread,
    StubServer,
    make_client,
    open_session,
)

MIB = 1024 * 1024
LIMIT = MAX_TOOL_RESULT_TEXT_BYTES
PING = {"jsonrpc": "2.0", "id": "p1", "method": "ping"}
PONG = {"jsonrpc": "2.0", "id": "p1", "result": {}}


def _answered(test, records: list | None = None, result: dict | None = None) -> dict:
    """One ``tools/call`` answered with ``result`` on a fresh open session; the result the caller got."""

    client, stub = make_client(test, **({} if records is None else {"on_record": records.append}))
    open_session(test, client, stub)
    call = InThread(lambda: client.call_tool("read"))
    stub.answer(stub.expect(test, "tools/call"), result)
    return call.get()


class ResultText(unittest.TestCase):
    def test_an_embedded_text_resource_is_cut_and_marked(self):
        records: list = []
        resource = {"uri": "memo://notes", "mimeType": "text/plain", "text": "A" * MIB}
        result = _answered(self, records, {"content": [{"type": "resource", "resource": resource}]})
        kept = result["content"][0]["resource"]
        self.assertEqual(LIMIT, len(kept["text"].encode("utf-8")))
        self.assertEqual("memo://notes", kept["uri"])
        self.assertEqual({"limit_bytes": LIMIT, "text_bytes": MIB}, result["_meta"][RESULT_CUT])
        self.assertEqual([RESULT_CUT], [r["record"] for r in records])

    def test_an_item_whose_type_is_spelled_otherwise_is_cut_and_marked(self):
        records: list = []
        result = _answered(self, records, {"content": [{"type": "Text", "text": "A" * MIB}]})
        self.assertEqual(LIMIT, len(result["content"][0]["text"]))
        self.assertEqual({"limit_bytes": LIMIT, "text_bytes": MIB}, result["_meta"][RESULT_CUT])
        self.assertEqual([RESULT_CUT], [r["record"] for r in records])

    def test_a_text_with_no_type_is_cut_and_marked(self):
        records: list = []
        result = _answered(self, records, {"content": [{"text": "A" * MIB}]})
        self.assertEqual(LIMIT, len(result["content"][0]["text"]))
        self.assertEqual({"limit_bytes": LIMIT, "text_bytes": MIB}, result["_meta"][RESULT_CUT])
        self.assertEqual([RESULT_CUT], [r["record"] for r in records])

    def test_texts_of_every_shape_share_one_budget(self):
        half = "B" * (LIMIT // 2)
        result = _answered(self, None, {"content": [
            {"type": "text", "text": half},
            {"type": "resource", "resource": {"uri": "memo://a", "text": half}},
            {"text": "C" * 10},
        ]})
        self.assertEqual([half, half], [result["content"][0]["text"], result["content"][1]["resource"]["text"]])
        self.assertEqual(2, len(result["content"]), "the text that found the budget spent is left out")
        self.assertEqual(LIMIT + 10, result["_meta"][RESULT_CUT]["text_bytes"])

    def test_what_is_not_text_is_not_cut_here(self):
        records: list = []
        image = {"type": "image", "data": "A" * (2 * LIMIT), "mimeType": "image/png"}
        sent = {"content": [image], "structuredContent": {"blob": "S" * (LIMIT // 2)}}  # the image is not counted
        self.assertEqual(sent, _answered(self, records, sent))
        self.assertEqual([], records)

    def test_the_servers_own_meta_is_kept_beside_the_mark(self):
        result = _answered(self, None, {"content": [{"type": "text", "text": "short"}],
                                        "structuredContent": {"s": "S" * MIB}, "_meta": {"k": 1}})
        self.assertEqual(1, result["_meta"]["k"])
        self.assertIn(RESULT_CUT, result["_meta"])


class CutListMark(unittest.TestCase):
    def test_a_list_cut_on_a_change_notice_keeps_its_mark(self):
        got: list = []
        handed = threading.Event()

        def on_tools(tools):
            got.append(tools)
            handed.set()

        client, stub = make_client(self, on_tools=on_tools)
        open_session(self, client, stub, capabilities={"tools": {"listChanged": True}})
        stub.send({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})
        stub.answer(stub.expect(self, "tools/list"), {"tools": [{"name": f"t{n}"} for n in range(600)]})
        self.assertTrue(handed.wait(HANG_GUARD_S))
        self.assertEqual(MAX_TOOLS, len(got[0]))
        self.assertEqual(TOOL_LIST_CUT, getattr(got[0], "cut", "no mark"), "what on_tools got says it was cut")
        self.assertEqual(TOOL_LIST_CUT, getattr(client.tools, "cut", "no mark"), "the tools property says so too")


class StartSettled(unittest.TestCase):
    def test_an_answer_that_comes_while_the_start_record_is_handed_over_is_not_returned(self):
        holder: dict = {}

        def on_record(record):
            if record["record"] == SERVER_START_TIMEOUT:
                holder["stub"].answer(holder["request"], {"protocolVersion": LEGACY_PROTOCOL, "capabilities": {}})
                time.sleep(SHORT)  # the reader has the answer meanwhile

        client, stub = make_client(self, on_record=on_record)
        holder["stub"] = stub
        with mock.patch.object(session, "INITIALIZE_DEADLINE_S", 0.5):
            opening = InThread(client.open)
            holder["request"] = stub.expect(self, "initialize")
            with self.assertRaises(McpSessionEnded) as caught:
                opening.get()
        self.assertEqual(SERVER_START_TIMEOUT, caught.exception.code)
        self.assertEqual(SERVER_START_TIMEOUT, client.ending)
        self.assertIsNone(client.protocol_version)

    def test_the_second_initialize_request_gets_no_fresh_budget(self):
        client, stub = make_client(self)
        with mock.patch.object(session, "INITIALIZE_DEADLINE_S", 1.0):
            started = time.monotonic()
            opening = InThread(client.open)
            first = stub.expect(self, "initialize")
            refusal = {"jsonrpc": "2.0", "id": first["id"], "error": {"code": -32602, "message": "no"}}
            late = threading.Timer(0.7, stub.send, (refusal,))
            late.daemon = True
            late.start()
            self.addCleanup(late.cancel)
            stub.expect(self, "initialize")  # never answered
            with self.assertRaises(McpSessionEnded) as caught:
                opening.get()
            elapsed = time.monotonic() - started
        self.assertEqual(SERVER_START_TIMEOUT, caught.exception.code)
        self.assertLess(elapsed, 1.45, "both requests share one start budget of 1.0 s (a fresh one ends at 1.7 s)")


class AfterTheEnding(unittest.TestCase):
    def test_a_line_read_after_the_ending_is_not_handled(self):
        records: list = []
        client, stub = make_client(self, on_record=records.append)
        open_session(self, client, stub)
        client.close()
        self.assertIsNone(stub.next(), "the client shut the server's input")
        stub.send({"jsonrpc": "2.0", "id": "s1", "method": "roots/list"})
        time.sleep(SHORT)  # shows only that nothing happens
        self.assertNotIn(SERVER_REQUEST_REFUSED, [r["record"] for r in records])

    def test_a_breach_after_the_ending_does_not_count(self):
        records: list = []
        client, stub = make_client(self, on_record=records.append)
        open_session(self, client, stub)
        client.close()
        with mock.patch.object(session, "MAX_BREACHES", 1):
            client._breach(RESULT_CUT, {})
        self.assertEqual(CLIENT_SHUT, client.ending)
        self.assertNotIn(SERVER_CUT_OFF, [r["record"] for r in records])


class SlowRecord(unittest.TestCase):
    def test_a_slow_on_record_does_not_hold_a_caller_whose_result_was_cut(self):
        release, seen = threading.Event(), threading.Event()

        def on_record(record):
            if record["record"] == RESULT_CUT:
                seen.set()
                release.wait(HANG_GUARD_S)

        client, stub = make_client(self, on_record=on_record)
        self.addCleanup(release.set)
        open_session(self, client, stub)
        call = InThread(lambda: client.call_tool("big", deadline_s=0.2))
        stub.answer(stub.expect(self, "tools/call"), {"content": [{"type": "text", "text": "x" * (LIMIT + 1)}]})
        self.assertTrue(seen.wait(HANG_GUARD_S))
        result = call.get(timeout=2.0)  # while on_record still holds the reader's thread
        self.assertIn(RESULT_CUT, result["_meta"])


class _Jam(StubServer):
    """A stub that can stop reading for a while, so the client's writer stays inside one large write."""

    def __init__(self) -> None:
        self._go = threading.Event()
        self._go.set()
        super().__init__()

    def _read(self) -> None:
        while True:
            self._go.wait(HANG_GUARD_S)
            line = self._from_client.readline()
            if not line:
                self._lines.put(None)
                return
            self._lines.put(json.loads(line))

    def jam(self, test, client) -> None:
        """Stop reading; one small call is read, then a call of 1 MiB fills the pipe and holds the writer."""

        self._go.clear()
        test.addCleanup(self._go.set)
        client.begin_call("first")
        test.assertEqual("first", self.expect(test, "tools/call")["params"]["name"])
        client.begin_call("jam", {"blob": "x" * MIB})

    def resume(self, test) -> None:
        self._go.set()
        test.assertEqual("jam", self.expect(test, "tools/call")["params"]["name"])


def _jammed(test) -> tuple:
    stub = _Jam()
    client = session.McpClient(stub.client_in, stub.client_out)
    test.addCleanup(stub.close)
    test.addCleanup(stub.end_output)
    test.addCleanup(client.close)
    open_session(test, client, stub)
    stub.jam(test, client)
    return client, stub


def _until_pong(test, stub) -> list:
    stub.send(PING)
    seen = []
    while True:
        message = stub.next(timeout=5.0)
        if message == PONG:
            return seen
        test.assertIsNotNone(message, "the client shut the server's input before the answer to ping")
        seen.append(message)


class Outbox(unittest.TestCase):
    def test_what_is_sent_is_what_the_caller_sent_at_the_call(self):
        client, stub = _jammed(self)
        arguments = {"path": "as sent"}
        client.begin_call("write_file", arguments)
        arguments["path"] = "changed after begin_call returned"
        stub.resume(self)
        self.assertEqual({"path": "as sent"}, stub.next(timeout=5.0)["params"]["arguments"])

    def test_a_value_changed_to_one_json_cannot_hold_after_the_call_leaves_the_writer_alive(self):
        client, stub = _jammed(self)
        arguments = {"path": "as sent"}
        client.begin_call("write_file", arguments)
        arguments["path"] = object()
        stub.resume(self)
        self.assertEqual({"path": "as sent"}, stub.next(timeout=5.0)["params"]["arguments"])
        self.assertEqual([], _until_pong(self, stub), "the writer goes on writing")
        self.assertIsNone(client.ending)

    def test_a_value_json_cannot_hold_refuses_at_the_call_and_nothing_is_sent(self):
        client, stub = make_client(self)
        open_session(self, client, stub)
        for value in (object(), math.nan):
            with self.subTest(value=repr(value)), self.assertRaises((TypeError, ValueError)):
                client.begin_call("write_file", {"value": value})
        self.assertEqual({}, client._pending)
        self.assertEqual([], _until_pong(self, stub))
        self.assertIsNone(client.ending)


class NotFinite(unittest.TestCase):
    def test_a_result_holding_a_number_that_is_not_finite_is_refused_and_the_caller_gets_no_value(self):
        client, stub = make_client(self)
        open_session(self, client, stub)
        for spelling in ("1e999", "-1e999"):
            with self.subTest(number=spelling):
                call = InThread(lambda: client.call_tool("number"))
                request = stub.expect(self, "tools/call")
                stub.send_bytes(('{"jsonrpc":"2.0","id":%d,"result":{"content":[{"type":"text","text":"n"}],'
                                 '"structuredContent":{"n":[1,%s]}}}\n' % (request["id"], spelling)).encode())
                with self.assertRaises(McpClientError) as caught:
                    call.get()
                self.assertEqual(BAD_ANSWER, caught.exception.code)
                self.assertNotIsInstance(caught.exception, McpSessionEnded)
        self.assertEqual([], _until_pong(self, stub))
        self.assertIsNone(client.ending)

    def test_a_progress_notice_holding_a_number_that_is_not_finite_is_dropped(self):
        got: list = []
        client, stub = make_client(self)
        open_session(self, client, stub)
        call = InThread(lambda: client.call_tool("slow", on_progress=got.append))
        request = stub.expect(self, "tools/call")
        token = request["params"]["_meta"]["progressToken"]
        stub.send_bytes(('{"jsonrpc":"2.0","method":"notifications/progress","params":'
                         '{"progressToken":%d,"progress":1e999}}\n' % token).encode())
        stub.send({"jsonrpc": "2.0", "method": "notifications/progress", "params": {"progressToken": token,
                                                                                    "progress": 1}})
        stub.answer(request, {"content": []})
        call.get()
        self.assertEqual([1], [params["progress"] for params in got])

    def test_the_decoder_refuses_the_words_and_reads_1e999_as_infinity(self):
        for word in (b"NaN", b"Infinity", b"-Infinity"):
            with self.subTest(word=word):
                self.assertEqual(NOT_JSON, decode_line(b'{"n":' + word + b"}").error)
        self.assertEqual(math.inf, decode_line(b'{"n":1e999}').message["n"], "the hole the client's check covers")


class OneHome(unittest.TestCase):
    def test_no_other_module_of_the_client_holds_a_second_copy_of_a_limit(self):
        values = {name: getattr(limits, name) for name in dir(limits) if name.isupper()}
        numbers = {value for value in values.values() if isinstance(value, (int, float))}
        package = pathlib.Path(session.__file__).parent
        for path in sorted(package.glob("*.py")):
            if path.name == "limits.py":
                continue
            for node in ast.parse(path.read_text(encoding="utf-8")).body:
                if isinstance(node, ast.Assign):
                    targets, value = node.targets, node.value
                elif isinstance(node, ast.AnnAssign) and node.value is not None:
                    targets, value = [node.target], node.value
                else:
                    continue
                for target in targets:
                    if isinstance(target, ast.Name):
                        self.assertNotIn(target.id, values, f"{path.name} binds {target.id} a second time")
                if all(isinstance(part, (ast.Constant, ast.BinOp, ast.UnaryOp, ast.operator, ast.unaryop))
                       for part in ast.walk(value)):
                    number = eval(compile(ast.Expression(value), path.name, "eval"), {"__builtins__": {}})
                    if isinstance(number, (int, float)) and not isinstance(number, bool):
                        self.assertNotIn(number, numbers, f"{path.name} holds a limit's value {number} again")


if __name__ == "__main__":
    unittest.main()
