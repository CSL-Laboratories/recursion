"""The client's third answer round on an outside server: what counts toward cutting a server off and over what
window, a progress report that gives a ``tools/call`` more time up to a ceiling, one budget for all a model
reads of a result, and three points on the session itself (it holds each call's deadline, no request goes out
before ``open`` has finished, and the writer never dies without an ending). A request whose deadline passed
before it was written is never sent.

Every server here is the stub of ``support`` over pipes in the test's own process: no file, no folder, no
process. Waits that show something has NOT happened are short; a timing bound carries its margin by name.
"""

from __future__ import annotations

import json
import threading
import time
import unittest
from unittest import mock

from recursion_core.mcp.client import (
    BAD_ANSWER,
    CALL_TIMEOUT,
    CLIENT_SHUT,
    MAX_MESSAGE_BYTES,
    MAX_TOOL_RESULT_TEXT_BYTES,
    MAX_TOOLS,
    OUTPUT_FAILED,
    RESULT_CUT,
    SERVER_CUT_OFF,
    TOOL_LIST_CUT,
    McpClient,
    McpClientError,
    McpSessionEnded,
)
from recursion_core.mcp.client import limits, session
from recursion_core.mcp.client.tests.support import HANG_GUARD_S, SHORT, InThread, StubServer, make_client, open_session

KIB = 1024
MIB = 1024 * 1024
LIMIT = MAX_TOOL_RESULT_TEXT_BYTES
MARGIN_S = 0.5  # what a call may take past the time it should end: the timer's wake and the threads' start
PING = {"jsonrpc": "2.0", "id": "p1", "method": "ping"}
PONG = {"jsonrpc": "2.0", "id": "p1", "result": {}}
LIST_CHANGED = {"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}


class _Records:
    """An ``on_record`` that keeps every record and lets a test wait until ``count`` records name ``code``."""

    def __init__(self) -> None:
        self.records: list = []
        self._cond = threading.Condition()

    def __call__(self, record: dict) -> None:
        with self._cond:
            self.records.append(record)
            self._cond.notify_all()

    def named(self, code: str) -> list:
        with self._cond:
            return [r for r in self.records if r.get("record") == code]

    def wait(self, code: str, count: int) -> bool:
        with self._cond:
            return self._cond.wait_for(lambda: len([r for r in self.records if r.get("record") == code]) >= count,
                                       HANG_GUARD_S)


def _until_pong(test, stub) -> list:
    """Every line the server reads before the answer to one ``ping``: what the client wrote before it."""

    stub.send(PING)
    seen = []
    while True:
        message = stub.next(timeout=5.0)
        if message == PONG:
            return seen
        test.assertIsNotNone(message, "the client shut the server's input before the answer to ping")
        seen.append(message)


def _call(test, client, stub, result: dict) -> dict:
    call = InThread(lambda: client.call_tool("read"))
    stub.answer(stub.expect(test, "tools/call"), result)
    return call.get()


def _answered(test, result: dict) -> tuple[dict, _Records, McpClient]:
    records = _Records()
    client, stub = make_client(test, on_record=records)
    open_session(test, client, stub)
    return _call(test, client, stub, result), records, client


class _Reporter:
    """A server thread that sends ``notifications/progress`` for ``request`` every ``every_s`` seconds, and after
    ``for_s`` seconds (``None``: never) answers it with ``answer``."""

    def __init__(self, test, stub: StubServer, request: dict, every_s: float = 0.1, for_s: float | None = None,
                 answer: dict | None = None) -> None:
        self._stub, self._token = stub, request["params"]["_meta"]["progressToken"]
        self._request, self._every, self._for, self._answer = request, every_s, for_s, answer
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        test.addCleanup(self.stop)

    def _run(self) -> None:
        started, step = time.monotonic(), 0
        try:
            while not self._stop.is_set():
                if self._for is not None and time.monotonic() - started >= self._for:
                    if self._answer is not None:
                        self._stub.answer(self._request, self._answer)
                    return
                step += 1
                self._stub.send({"jsonrpc": "2.0", "method": "notifications/progress",
                                 "params": {"progressToken": self._token, "progress": step}})
                self._stop.wait(self._every)
        except (OSError, ValueError):  # the test ended the session
            return

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(HANG_GUARD_S)


class _Gated:
    """The server's input as the client writes it, held while the gate is shut: a writer blocked in a write."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.gate = threading.Event()
        self.gate.set()

    def write(self, data):
        self.gate.wait()
        return self._inner.write(data)

    def flush(self):
        self._inner.flush()

    def close(self):
        self.gate.set()
        self._inner.close()


class _Raising:
    """An output stream that takes the first ``ok`` writes and then raises ``exc``, as a stream object can."""

    def __init__(self, inner, ok: int, exc: BaseException) -> None:
        self._inner, self._ok, self._exc, self.closed = inner, ok, exc, False

    def write(self, data):
        if self._ok <= 0:
            raise self._exc
        self._ok -= 1
        return self._inner.write(data)

    def flush(self):
        self._inner.flush()

    def close(self):
        self.closed = True
        self._inner.close()


def _client_on(test, stub: StubServer, out, **kwargs) -> McpClient:
    client = McpClient(stub.client_in, out, **kwargs)
    test.addCleanup(stub.close)
    test.addCleanup(stub.end_output)
    test.addCleanup(client.close)
    return client


def _timeout(test, client, **kwargs) -> float:
    """One ``call_tool`` that nobody answers; how long it took to end ``CALL_TIMEOUT``."""

    started = time.monotonic()
    with test.assertRaises(McpClientError) as caught:
        InThread(lambda: client.call_tool("slow", **kwargs)).get()
    test.assertEqual(CALL_TIMEOUT, caught.exception.code)
    return time.monotonic() - started


class FirstValues(unittest.TestCase):
    def test_the_window_and_the_ceiling_are_first_values_with_one_home(self):
        self.assertEqual(600.0, limits.BREACH_WINDOW_S)
        self.assertEqual(1800.0, limits.CALL_CEILING_S)
        for name in ("BREACH_WINDOW_S", "CALL_CEILING_S"):
            with self.subTest(name=name):
                self.assertEqual(getattr(limits, name), getattr(session, name))


class WhatCounts(unittest.TestCase):
    def test_an_honest_server_of_513_tools_relisted_three_times_is_not_cut_off(self):
        records = _Records()
        client, stub = make_client(self, on_record=records)
        open_session(self, client, stub, capabilities={"tools": {"listChanged": True}})
        tools = [{"name": f"t{n}"} for n in range(MAX_TOOLS + 1)]
        for number in range(1, 4):
            stub.send(LIST_CHANGED)
            stub.answer(stub.expect(self, "tools/list"), {"tools": tools})
            self.assertTrue(records.wait(TOOL_LIST_CUT, number))
        self.assertEqual([{"record": TOOL_LIST_CUT, "limit": MAX_TOOLS, "listed": MAX_TOOLS + 1}] * 3,
                         records.named(TOOL_LIST_CUT))
        self.assertEqual((MAX_TOOLS, TOOL_LIST_CUT), (len(client.tools), client.tools.cut))
        self.assertEqual([], records.named(SERVER_CUT_OFF))
        self.assertIsNone(client.ending, "a list cut at MAX_TOOLS is marked and recorded, not counted")

    def test_twenty_normal_answers_between_three_results_over_the_budget_do_not_cut_the_server_off(self):
        records = _Records()
        client, stub = make_client(self, on_record=records)
        open_session(self, client, stub)
        for _ in range(3):
            for _ in range(20):
                self.assertEqual({"content": [{"type": "text", "text": "ok"}]},
                                 _call(self, client, stub, {"content": [{"type": "text", "text": "ok"}]}))
            big = _call(self, client, stub, {"content": [{"type": "text", "text": "x" * (LIMIT + 1)}]})
            self.assertEqual({"limit_bytes": LIMIT, "text_bytes": LIMIT + 1}, big["_meta"][RESULT_CUT])
        self.assertEqual(3, len(records.named(RESULT_CUT)))
        self.assertEqual([], records.named(SERVER_CUT_OFF))
        self.assertIsNone(client.ending, "a cut result is marked and recorded, not counted")

    def test_three_calls_past_their_deadline_within_the_window_still_cut_the_server_off(self):
        records = _Records()
        client, stub = make_client(self, on_record=records)
        open_session(self, client, stub)
        with mock.patch.object(session, "CALL_DEADLINE_S", 0.1):
            for _ in range(3):
                _timeout(self, client)
        self.assertEqual(SERVER_CUT_OFF, client.ending)
        self.assertEqual([{"record": SERVER_CUT_OFF, "breaches": 3}], records.named(SERVER_CUT_OFF))

    def test_a_breach_older_than_the_window_no_longer_counts(self):
        with self.subTest(apart="more than the window"):
            client, stub = make_client(self)
            open_session(self, client, stub)
            with mock.patch.object(session, "CALL_DEADLINE_S", 0.1), mock.patch.object(session, "BREACH_WINDOW_S", 1.0):
                for number in range(3):
                    if number:
                        time.sleep(1.2)  # the breach before is older than the window
                    _timeout(self, client)
            self.assertIsNone(client.ending)
            self.assertLessEqual(len(client._breach_times), session.MAX_BREACHES)
        with self.subTest(apart="inside the window"):
            client, stub = make_client(self)
            open_session(self, client, stub)
            with mock.patch.object(session, "CALL_DEADLINE_S", 0.1), mock.patch.object(session, "BREACH_WINDOW_S", 1.0):
                for _ in range(3):
                    _timeout(self, client)
            self.assertEqual(SERVER_CUT_OFF, client.ending)


class Progress(unittest.TestCase):
    def _slow_tool(self, on_progress) -> tuple:
        records = _Records()
        client, stub = make_client(self, on_record=records)
        open_session(self, client, stub)
        with mock.patch.object(session, "CALL_DEADLINE_S", 0.5):
            call = InThread(lambda: client.call_tool("slow", on_progress=on_progress))
            request = stub.expect(self, "tools/call")
        _Reporter(self, stub, request, every_s=0.1, for_s=2.0, answer={"content": [{"type": "text", "text": "done"}]})
        return call, records, client

    def test_a_tool_that_reports_progress_lives_past_its_deadline(self):
        got: list = []
        call, records, client = self._slow_tool(got.append)
        self.assertEqual("done", call.get()["content"][0]["text"])
        self.assertGreater(len(got), 5, "the caller saw the reports")
        self.assertEqual([], records.named(CALL_TIMEOUT))
        self.assertIsNone(client.ending)

    def test_progress_extends_a_call_whose_caller_asked_to_see_none(self):
        call, records, client = self._slow_tool(None)
        self.assertEqual("done", call.get()["content"][0]["text"])
        self.assertEqual([], records.named(CALL_TIMEOUT))
        self.assertIsNone(client.ending)

    def test_a_call_with_no_progress_still_ends_at_its_deadline(self):
        client, stub = make_client(self)
        open_session(self, client, stub)
        with mock.patch.object(session, "CALL_DEADLINE_S", 0.5):
            took = _timeout(self, client)
        self.assertGreaterEqual(took, 0.45)
        self.assertLess(took, 0.5 + MARGIN_S)

    def test_progress_for_ever_meets_the_ceiling_typed_once_counted_and_named(self):
        records = _Records()
        client, stub = make_client(self, on_record=records)
        open_session(self, client, stub)
        with mock.patch.object(session, "CALL_DEADLINE_S", 0.5), mock.patch.object(session, "CALL_CEILING_S", 1.5):
            started = time.monotonic()
            call = InThread(lambda: client.call_tool("for-ever"))
            request = stub.expect(self, "tools/call")
            reporter = _Reporter(self, stub, request, every_s=0.1)
            with self.assertRaises(McpClientError) as caught:
                call.get()
            took = time.monotonic() - started
        reporter.stop()
        self.assertEqual(CALL_TIMEOUT, caught.exception.code)
        self.assertGreaterEqual(took, 1.45, "the reports kept it past its 0.5 s deadline")
        self.assertLess(took, 1.5 + MARGIN_S, "the ceiling of 1.5 s ended it")
        self.assertEqual([{"record": CALL_TIMEOUT, "request": request["id"], "reason": session.CALL_CUT_CEILING}],
                         records.named(CALL_TIMEOUT))
        self.assertIsNone(client.ending, "one breach")

    def test_a_shorter_deadline_the_caller_asked_for_is_given_again_and_its_timeout_is_not_counted(self):
        records = _Records()
        client, stub = make_client(self, on_record=records)
        open_session(self, client, stub)
        started = time.monotonic()
        call = InThread(lambda: client.call_tool("slow", deadline_s=0.3))
        _Reporter(self, stub, stub.expect(self, "tools/call"), every_s=0.1, for_s=0.9)  # then silent, no answer
        with self.assertRaises(McpClientError) as caught:
            call.get()
        took = time.monotonic() - started
        self.assertEqual(CALL_TIMEOUT, caught.exception.code)
        self.assertGreater(took, 0.9, "each report gave the call its 0.3 s again")
        self.assertLess(took, 0.9 + 0.3 + MARGIN_S, "not 60 s: the caller asked for less")
        self.assertEqual([], records.named(CALL_TIMEOUT))

    def test_each_call_carries_its_own_token_and_a_report_for_an_ended_call_changes_nothing(self):
        got: list = []
        client, stub = make_client(self)
        open_session(self, client, stub)
        first = InThread(lambda: client.call_tool("one", on_progress=got.append))
        one = stub.expect(self, "tools/call")
        stub.answer(one, {"content": []})
        first.get()
        with mock.patch.object(session, "CALL_DEADLINE_S", 0.5):
            started = time.monotonic()
            second = InThread(lambda: client.call_tool("two"))
            two = stub.expect(self, "tools/call")
            self.assertNotEqual(one["params"]["_meta"]["progressToken"], two["params"]["_meta"]["progressToken"])
            _Reporter(self, stub, one, every_s=0.1)  # reports naming the ended call, for ever
            with self.assertRaises(McpClientError) as caught:
                second.get()
        self.assertEqual(CALL_TIMEOUT, caught.exception.code)
        self.assertLess(time.monotonic() - started, 0.5 + MARGIN_S, "the other call was given no more time")
        self.assertEqual([], got, "the ended call's callback saw nothing")

    def test_a_listing_is_not_extended_by_progress(self):
        client, stub = make_client(self)
        open_session(self, client, stub)
        with mock.patch.object(session, "CALL_DEADLINE_S", 0.5):
            started = time.monotonic()
            listing = InThread(client.list_tools)
            request = stub.expect(self, "tools/list")
            _Reporter(self, stub, {**request, "params": {"_meta": {"progressToken": request["id"]}}}, every_s=0.1)
            with self.assertRaises(McpClientError) as caught:
                listing.get()
        self.assertEqual(CALL_TIMEOUT, caught.exception.code)
        self.assertLess(time.monotonic() - started, 0.5 + MARGIN_S)


class OneBudget(unittest.TestCase):
    def _left_out(self, result: dict, part: str) -> dict:
        got, records, client = _answered(self, result)
        mark = got["_meta"][RESULT_CUT]
        self.assertEqual({"limit_bytes": LIMIT, "text_bytes": 5, "left_out": [part]}, mark)
        self.assertEqual([{"record": RESULT_CUT, **mark}], records.named(RESULT_CUT))
        self.assertIsNone(client.ending, "a part left out is recorded once and not counted")
        self.assertEqual("short", got["content"][0]["text"], "the text is whole")
        return got

    def test_one_mebibyte_of_structured_content_is_left_out_whole_and_named(self):
        got = self._left_out({"content": [{"type": "text", "text": "short"}], "structuredContent": {"s": "S" * MIB}},
                             "structuredContent")
        self.assertNotIn("structuredContent", got)

    def test_one_mebibyte_of_meta_is_left_out_and_only_the_clients_mark_is_kept(self):
        got = self._left_out({"content": [{"type": "text", "text": "short"}], "_meta": {"m": "M" * MIB}}, "_meta")
        self.assertEqual([RESULT_CUT], list(got["_meta"]))

    def test_one_mebibyte_of_annotations_is_left_out_from_every_item_together(self):
        resource = {"uri": "memo://a", "blob": "AAAA", "annotations": {"note": "B" * KIB}}
        got = self._left_out({"content": [{"type": "text", "text": "short", "annotations": {"note": "A" * MIB}},
                                          {"type": "resource", "resource": resource}]}, "annotations")
        self.assertNotIn("annotations", got["content"][0])
        self.assertEqual({"uri": "memo://a", "blob": "AAAA"}, got["content"][1]["resource"])

    def test_the_parts_are_kept_in_their_order_until_the_budget_ends(self):
        got, _records, _client = _answered(self, {"content": [{"type": "text", "text": "short"}],
                                                  "structuredContent": {"s": "S" * (200 * KIB)},
                                                  "_meta": {"m": "M" * (100 * KIB)}})
        self.assertEqual({"s": "S" * (200 * KIB)}, got["structuredContent"])
        self.assertEqual(["_meta"], got["_meta"][RESULT_CUT]["left_out"])

    def test_100_kib_of_structured_content_with_short_text_passes_whole_and_unmarked(self):
        sent = {"content": [{"type": "text", "text": "short"}], "structuredContent": {"s": "S" * (100 * KIB)}}
        got, records, _client = _answered(self, sent)
        self.assertEqual(sent, got)
        self.assertEqual([], records.records)

    def test_an_image_the_size_of_the_message_limit_passes_whole(self):
        image = {"type": "image", "data": "", "mimeType": "image/png"}
        envelope = {"jsonrpc": "2.0", "id": 2, "result": {"content": [image]}}  # id 2: the first call's
        image["data"] = "A" * (MAX_MESSAGE_BYTES - len(json.dumps(envelope)) - 1)
        got, records, _client = _answered(self, {"content": [image]})
        self.assertEqual(MAX_MESSAGE_BYTES, len(json.dumps({**envelope, "result": got})) + 1)
        self.assertEqual({"content": [image]}, got)
        self.assertEqual([], records.records)


class ContentThatIsNotAList(unittest.TestCase):
    def test_a_string_under_the_budget_is_kept_whole(self):
        got, records, _client = _answered(self, {"content": "a short answer"})
        self.assertEqual({"content": "a short answer"}, got)
        self.assertEqual([], records.records)

    def test_a_string_over_the_budget_is_cut_and_marked_like_any_text(self):
        got, records, client = _answered(self, {"content": "x" * MIB})
        self.assertEqual("x" * LIMIT, got["content"])
        self.assertEqual({"limit_bytes": LIMIT, "text_bytes": MIB}, got["_meta"][RESULT_CUT])
        self.assertEqual(1, len(records.named(RESULT_CUT)))
        self.assertIsNone(client.ending)

    def test_a_content_that_is_neither_a_list_nor_a_string_is_a_bad_answer(self):
        client, stub = make_client(self)
        open_session(self, client, stub)
        for content in (42, {"text": "an object"}, None):
            with self.subTest(content=content):
                call = InThread(lambda: client.call_tool("odd"))
                stub.answer(stub.expect(self, "tools/call"), {"content": content})
                with self.assertRaises(McpClientError) as caught:
                    call.get()
                self.assertEqual(BAD_ANSWER, caught.exception.code)
                self.assertNotIsInstance(caught.exception, McpSessionEnded)
        self.assertIsNone(client.ending)


class TheSessionHoldsTheDeadline(unittest.TestCase):
    def test_a_late_answer_to_a_call_nobody_waited_on_is_not_taken(self):
        client, stub = make_client(self)
        open_session(self, client, stub)
        handle = client.begin_call("slow", deadline_s=0.05)
        request = stub.expect(self, "tools/call")
        self.assertEqual(request["id"], stub.expect(self, "notifications/cancelled")["params"]["requestId"])
        stub.answer(request, {"content": [{"type": "text", "text": "late"}]})
        self.assertEqual([], _until_pong(self, stub), "one cancel only")
        with self.assertRaises(McpClientError) as caught:
            handle.wait()
        self.assertEqual(CALL_TIMEOUT, caught.exception.code)

    def test_a_call_nobody_waits_on_is_cancelled_at_its_deadline(self):
        client, stub = make_client(self)
        open_session(self, client, stub)
        client.begin_call("never-answered", deadline_s=0.05)
        request = stub.expect(self, "tools/call")
        cancelled = stub.next(timeout=5.0)  # before anything else, with nobody waiting
        self.assertEqual("notifications/cancelled", cancelled.get("method"), cancelled)
        self.assertEqual({"requestId": request["id"], "reason": CALL_TIMEOUT}, cancelled["params"])
        self.assertEqual({}, client._pending, "no call is left in flight")

    def test_an_answer_read_after_the_deadline_while_the_writer_is_held_is_not_taken(self):
        stub = StubServer()
        gated = _Gated(stub.client_out)
        client = _client_on(self, stub, gated)
        self.addCleanup(gated.gate.set)
        open_session(self, client, stub)
        handle = client.begin_call("slow", deadline_s=0.3)
        request = stub.expect(self, "tools/call")
        gated.gate.clear()
        client.begin_call("jam")  # the writer, the session's timer, is held inside this write
        time.sleep(0.3 + SHORT)
        stub.answer(request, {"content": [{"type": "text", "text": "late"}]})
        with self.assertRaises(McpClientError) as caught:
            InThread(handle.wait).get(timeout=2.0)
        self.assertEqual(CALL_TIMEOUT, caught.exception.code)


class NothingBeforeOpen(unittest.TestCase):
    def test_a_request_before_open_is_refused_and_nothing_goes_before_initialize(self):
        client, stub = make_client(self)
        for name, request in (("begin_call", lambda: client.begin_call("too-early")),
                              ("call_tool", lambda: client.call_tool("too-early")),
                              ("list_tools", client.list_tools)):
            with self.subTest(request=name), self.assertRaises(McpClientError) as caught:
                InThread(request).get(timeout=2.0)
            self.assertEqual(session.SESSION_NOT_OPEN, caught.exception.code)
            self.assertNotIsInstance(caught.exception, McpSessionEnded)
        open_session(self, client, stub)  # the first line the server reads is initialize
        self.assertEqual([], _until_pong(self, stub))

    def test_a_request_while_open_has_not_finished_is_refused(self):
        client, stub = make_client(self)
        opening = InThread(client.open)
        request = stub.expect(self, "initialize")
        with self.assertRaises(McpClientError) as caught:
            InThread(client.list_tools).get(timeout=2.0)
        self.assertEqual(session.SESSION_NOT_OPEN, caught.exception.code)
        stub.answer(request, {"protocolVersion": "2025-06-18", "capabilities": {}})
        stub.expect(self, "notifications/initialized")
        opening.get()
        self.assertEqual([], _until_pong(self, stub))

    def test_a_request_after_the_session_ended_raises_its_ending(self):
        client, _stub = make_client(self)
        client.close()
        with self.assertRaises(McpSessionEnded) as caught:
            client.call_tool("after")
        self.assertEqual(CLIENT_SHUT, caught.exception.code)


class TheWriterNeverDiesSilently(unittest.TestCase):
    def _raising(self, exc: BaseException) -> tuple:
        died: list = []
        self.raised = threading.Event()
        old = threading.excepthook

        def hook(args) -> None:  # what the interpreter is shown of a thread that ended by an exception
            died.append(args.exc_type)
            self.raised.set()

        threading.excepthook = hook
        self.addCleanup(setattr, threading, "excepthook", old)
        stub = StubServer()
        out = _Raising(stub.client_out, 2, exc)  # initialize and notifications/initialized pass
        client = _client_on(self, stub, out)
        open_session(self, client, stub)
        with self.assertRaises(McpSessionEnded) as caught:
            InThread(lambda: client.call_tool("x", deadline_s=5.0)).get(timeout=2.0)
        return caught.exception.code, client.ending, out.closed, died

    def test_an_interrupt_from_the_stream_ends_the_session_typed_and_is_raised_again(self):
        code, ending, closed, died = self._raising(KeyboardInterrupt())
        self.assertEqual((OUTPUT_FAILED, OUTPUT_FAILED, True), (code, ending, closed))
        self.assertTrue(self.raised.wait(HANG_GUARD_S), "the writer's thread raised it again")
        self.assertIn(KeyboardInterrupt, died, "the interpreter still sees it")

    def test_an_exit_from_the_stream_ends_the_session_typed(self):
        code, ending, closed, _died = self._raising(SystemExit(3))
        self.assertEqual((OUTPUT_FAILED, OUTPUT_FAILED, True), (code, ending, closed))

    def test_control_an_error_from_the_stream_ends_the_session_typed(self):
        code, ending, closed, died = self._raising(BrokenPipeError("control"))
        self.assertEqual((OUTPUT_FAILED, OUTPUT_FAILED, True), (code, ending, closed))
        self.assertNotIn(BrokenPipeError, died, "an Exception from the stream is an ending, not raised again")


class AbandonedBeforeWritten(unittest.TestCase):
    def _gated(self) -> tuple:
        stub = StubServer()
        gated = _Gated(stub.client_out)
        client = _client_on(self, stub, gated)
        self.addCleanup(gated.gate.set)
        open_session(self, client, stub)
        gated.gate.clear()
        client.begin_call("first")  # the writer takes it and is held inside its write
        return client, stub, gated

    def test_a_request_whose_caller_gave_up_before_it_was_written_is_never_sent(self):
        client, stub, gated = self._gated()
        with self.assertRaises(McpClientError) as caught:
            InThread(lambda: client.call_tool("given-up", deadline_s=0.2)).get()
        self.assertEqual(CALL_TIMEOUT, caught.exception.code)
        gated.gate.set()
        self.assertEqual("first", stub.expect(self, "tools/call")["params"]["name"])
        self.assertEqual([], _until_pong(self, stub), "neither the request nor a cancel for it was sent")

    def test_the_same_when_nobody_waits_on_it(self):
        client, stub, gated = self._gated()
        handle = client.begin_call("given-up", deadline_s=0.2)
        time.sleep(0.2 + SHORT)  # its deadline passes while the writer is held
        gated.gate.set()
        self.assertEqual("first", stub.expect(self, "tools/call")["params"]["name"])
        self.assertEqual([], _until_pong(self, stub), "neither the request nor a cancel for it was sent")
        with self.assertRaises(McpClientError) as caught:
            handle.wait()
        self.assertEqual(CALL_TIMEOUT, caught.exception.code)


if __name__ == "__main__":
    unittest.main()
