"""The client's answers on five points of an outside server's reach: no request is written once the session has
ended, ``open`` reports a session that ended before it returned, only a sound progress report gives a call more
time, every text a model reads of a content item shares the result's budget, and a server cannot write the
client's own cut mark.

Every server here is the stub of ``support`` over pipes in the test's own process: no file, no folder, no
process. Each session here starts at most eight threads (the client's reader and writer, the stub's reader, at
most four waiting calls and one reporter), a test makes at most four sessions, and every wait gives up after
``HANG_GUARD_S``. A test that has to hold the
client's writer inside a write (``_Jam``) stops the stub reading for a while and lets it read again before the
test ends.
"""

from __future__ import annotations

import json
import threading
import time
import unittest
from unittest import mock

from recursion_core.mcp.client import (
    CALL_TIMEOUT,
    CLIENT_SHUT,
    LEGACY_PROTOCOL,
    MAX_BREACHES,
    MAX_TOOL_RESULT_TEXT_BYTES,
    RESULT_CUT,
    SERVER_CUT_OFF,
    SERVER_GONE,
    McpClient,
    McpClientError,
    McpSessionEnded,
)
from recursion_core.mcp.client import session
from recursion_core.mcp.client.tests.support import HANG_GUARD_S, InThread, StubServer, make_client, open_session

MIB = 1024 * 1024
LIMIT = MAX_TOOL_RESULT_TEXT_BYTES
MARGIN_S = 0.5  # what a call may take past the time it should end: the timer's wake and the threads' start


def _until(test, condition, what: str) -> None:
    ends = time.monotonic() + HANG_GUARD_S
    while not condition():
        if time.monotonic() > ends:
            test.fail(what)
        time.sleep(0.01)


class _Jam(StubServer):
    """A stub that can stop reading and read again: ``jam`` holds the client's writer inside a write."""

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
        # the writer has taken it: its write is under way, whatever ends the session next
        _until(test, lambda: not client._outbox, "the writer did not take the jam")

    def resume(self, test) -> None:
        """Read again: the rest of the write under way comes first."""

        self._go.set()
        test.assertEqual("jam", self.expect(test, "tools/call")["params"]["name"])


def _jammed(test) -> tuple:
    stub = _Jam()
    client = McpClient(stub.client_in, stub.client_out)
    test.addCleanup(stub.close)
    test.addCleanup(stub.end_output)
    test.addCleanup(client.close)
    open_session(test, client, stub)
    stub.jam(test, client)
    return client, stub


def _calls_until_end_of_input(test, stub) -> list:
    """The names of the ``tools/call`` requests the server reads from now until end of input."""

    names = []
    while True:
        message = stub.next(timeout=5.0)
        if message is None:
            return names
        test.assertIsInstance(message, dict)
        if message.get("method") == "tools/call":
            names.append(message["params"]["name"])


def _three_breaches(test, client) -> None:
    with mock.patch.object(session, "CALL_DEADLINE_S", 0.2):
        for _ in range(MAX_BREACHES):
            with test.assertRaises(McpClientError) as caught:
                InThread(lambda: client.call_tool("slow")).get()
            test.assertEqual(CALL_TIMEOUT, caught.exception.code)


class NothingIsWrittenAfterTheEnding(unittest.TestCase):
    def test_a_request_queued_behind_a_held_write_is_never_written_after_a_cut_off(self):
        client, stub = _jammed(self)
        erase = client.begin_call("DELETE_EVERYTHING", {"confirm": True})  # waits behind the held write
        _three_breaches(self, client)  # each caller ends its own call; three breaches cut the server off
        self.assertEqual(SERVER_CUT_OFF, client.ending)
        with self.assertRaises(McpSessionEnded) as caught:
            erase.wait()
        self.assertEqual(SERVER_CUT_OFF, caught.exception.code, "its caller was told the call failed")
        stub.resume(self)
        self.assertEqual([], _calls_until_end_of_input(self, stub), "a request written after the cut-off")

    def test_a_request_queued_behind_a_held_write_is_never_written_after_close(self):
        client, stub = _jammed(self)
        erase = client.begin_call("DELETE_EVERYTHING", {"confirm": True})
        client.close()
        with self.assertRaises(McpSessionEnded) as caught:
            erase.wait()
        self.assertEqual(CLIENT_SHUT, caught.exception.code)
        stub.resume(self)
        self.assertEqual([], _calls_until_end_of_input(self, stub), "a request written after close")

    def test_without_a_held_write_the_request_goes_out_before_the_cut_off_and_nothing_after_it(self):
        client, stub = make_client(self)
        open_session(self, client, stub)
        erase = client.begin_call("DELETE_EVERYTHING", {"confirm": True})
        self.assertEqual("DELETE_EVERYTHING", stub.expect(self, "tools/call")["params"]["name"])
        with mock.patch.object(session, "CALL_DEADLINE_S", 0.2):
            for number in range(MAX_BREACHES):
                with self.assertRaises(McpClientError):
                    InThread(lambda: client.call_tool("slow")).get()
                self.assertEqual("slow", stub.expect(self, "tools/call")["params"]["name"])
                if number < MAX_BREACHES - 1:
                    stub.expect(self, "notifications/cancelled")
        self.assertEqual(SERVER_CUT_OFF, client.ending)
        with self.assertRaises(McpSessionEnded):
            erase.wait()
        self.assertEqual([], _calls_until_end_of_input(self, stub))


class _EndsBeforeInitialized(McpClient):
    """The server's output ends after its ``initialize`` answer and before ``notifications/initialized`` is
    queued: the reader's ending comes first."""

    def _send(self, message: dict, request_id: int | None = None) -> bool:
        if message.get("method") == "notifications/initialized":
            _until(self.test, lambda: self.ending is not None, "the session did not end")
        return super()._send(message, request_id)


class _EndsAfterInitialized(McpClient):
    """The same ending, read after ``notifications/initialized`` was queued and before ``open`` returns."""

    def _send(self, message: dict, request_id: int | None = None) -> bool:
        sent = super()._send(message, request_id)
        if message.get("method") == "notifications/initialized":
            _until(self.test, lambda: self.ending is not None, "the session did not end")
        return sent


class OpenReportsAnEnding(unittest.TestCase):
    def _open(self, kind) -> tuple:
        stub = StubServer()
        client = kind(stub.client_in, stub.client_out)
        client.test = self
        self.addCleanup(stub.close)
        self.addCleanup(stub.end_output)
        self.addCleanup(client.close)
        opening = InThread(client.open)
        stub.answer(stub.expect(self, "initialize"), {"protocolVersion": LEGACY_PROTOCOL, "capabilities": {},
                                                      "serverInfo": {"name": "stub", "version": "1"}})
        stub.end_output()  # the server's output ends at once
        return opening, client

    def test_a_session_that_ended_before_initialized_was_sent_is_not_opened(self):
        opening, client = self._open(_EndsBeforeInitialized)
        with self.assertRaises(McpSessionEnded) as caught:
            opening.get()
        self.assertEqual(SERVER_GONE, caught.exception.code)
        self.assertEqual(SERVER_GONE, client.ending)

    def test_a_session_that_ended_after_initialized_was_queued_is_not_opened(self):
        opening, client = self._open(_EndsAfterInitialized)
        with self.assertRaises(McpSessionEnded) as caught:
            opening.get()
        self.assertEqual(SERVER_GONE, caught.exception.code)


class _Reporter:
    """A server thread that sends the ``notifications/progress`` params ``params(step)`` every ``every_s``."""

    def __init__(self, test, stub: StubServer, params, every_s: float = 0.1) -> None:
        self._stub, self._params, self._every = stub, params, every_s
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        test.addCleanup(self.stop)

    def _run(self) -> None:
        step = 0
        try:
            while not self._stop.is_set():
                step += 1
                self._stub.send({"jsonrpc": "2.0", "method": "notifications/progress", "params": self._params(step)})
                self._stop.wait(self._every)
        except (OSError, ValueError):  # the test ended the session
            return

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(HANG_GUARD_S)


class OnlyASoundReportGivesTime(unittest.TestCase):
    def _reported(self, params, answer_after_s: float | None = None) -> tuple:
        """A call with a 0.5 s deadline and a 3 s ceiling, reported on with ``params(token, step)``; how long it
        took and what its callback saw."""

        got: list = []
        client, stub = make_client(self)
        open_session(self, client, stub)
        with mock.patch.object(session, "CALL_DEADLINE_S", 0.5), mock.patch.object(session, "CALL_CEILING_S", 3.0):
            started = time.monotonic()
            call = InThread(lambda: client.call_tool("slow", on_progress=got.append))
            request = stub.expect(self, "tools/call")
            token = request["params"]["_meta"]["progressToken"]
            reporter = _Reporter(self, stub, lambda step: params(token, step))
            if answer_after_s is not None:
                time.sleep(answer_after_s)
                reporter.stop()
                stub.answer(request, {"content": [{"type": "text", "text": "done"}]})
            try:
                outcome = call.get()
            except McpClientError as exc:
                outcome = exc
        reporter.stop()
        return outcome, time.monotonic() - started, got

    def test_a_report_without_a_value_gives_no_more_time(self):
        outcome, took, got = self._reported(lambda token, step: {"progressToken": token})
        self.assertIsInstance(outcome, McpClientError)
        self.assertEqual(CALL_TIMEOUT, outcome.code)
        self.assertLess(took, 0.5 + MARGIN_S, "a report with no value renewed the deadline")
        self.assertEqual([], got)

    def test_the_same_value_again_gives_no_more_time(self):
        outcome, took, got = self._reported(lambda token, step: {"progressToken": token, "progress": 1})
        self.assertIsInstance(outcome, McpClientError)
        self.assertEqual(CALL_TIMEOUT, outcome.code)
        self.assertLess(took, 0.5 + 0.1 + MARGIN_S, "only the first report of the value gave time")
        self.assertEqual([1], [params["progress"] for params in got])

    def test_a_value_that_is_not_a_number_gives_no_more_time(self):
        for value in ("1", True, {"n": 1}, [1]):
            with self.subTest(value=repr(value)):
                outcome, took, got = self._reported(lambda token, step, value=value: {"progressToken": token,
                                                                                     "progress": value})
                self.assertIsInstance(outcome, McpClientError)
                self.assertEqual(CALL_TIMEOUT, outcome.code)
                self.assertLess(took, 0.5 + MARGIN_S)
                self.assertEqual([], got)

    def test_a_growing_value_still_keeps_a_call_alive(self):
        outcome, took, got = self._reported(lambda token, step: {"progressToken": token, "progress": step / 2},
                                            answer_after_s=1.5)
        self.assertEqual("done", outcome["content"][0]["text"])
        self.assertGreater(took, 1.4)
        self.assertGreater(len(got), 5)


def _answered(test, result: dict) -> tuple:
    records: list = []
    client, stub = make_client(test, on_record=records.append)
    open_session(test, client, stub)
    call = InThread(lambda: client.call_tool("read"))
    stub.answer(stub.expect(test, "tools/call"), result)
    return call.get(), records


class EveryTextOfAnItemSharesTheBudget(unittest.TestCase):
    def test_a_resource_link_description_title_and_name_are_cut_and_marked(self):
        for key in ("description", "title", "name"):
            with self.subTest(key=key):
                link = {"type": "resource_link", "uri": "memo://a", "name": "a", key: "D" * MIB}
                got, records = _answered(self, {"content": [link]})
                kept = got["content"][0]
                spent = sum(len(kept[k].encode("utf-8")) for k in ("name", "title", "description") if k in kept)
                self.assertEqual(LIMIT, spent)
                self.assertEqual("memo://a", kept["uri"])
                self.assertEqual({"limit_bytes": LIMIT, "text_bytes": MIB + (1 if key != "name" else 0)},
                                 got["_meta"][RESULT_CUT])
                self.assertEqual([RESULT_CUT], [r["record"] for r in records])

    def test_a_bare_string_item_is_cut_and_marked_and_left_out_once_the_budget_is_spent(self):
        got, records = _answered(self, {"content": ["S" * MIB]})
        self.assertEqual(["S" * LIMIT], got["content"])
        self.assertEqual({"limit_bytes": LIMIT, "text_bytes": MIB}, got["_meta"][RESULT_CUT])
        self.assertEqual([RESULT_CUT], [r["record"] for r in records])
        got, _records = _answered(self, {"content": [{"type": "text", "text": "T" * LIMIT}, "late"]})
        self.assertEqual([{"type": "text", "text": "T" * LIMIT}], got["content"])
        self.assertEqual(LIMIT + 4, got["_meta"][RESULT_CUT]["text_bytes"])

    def test_an_items_meta_and_its_resources_meta_count_and_are_left_out_with_the_results_meta(self):
        big = {"m": "M" * MIB}
        sent = {"content": [{"type": "text", "text": "short", "_meta": big},
                            {"type": "resource", "resource": {"uri": "memo://a", "blob": "AAAA", "_meta": {"k": 1}}}],
                "_meta": {"k": 1}}
        got, records = _answered(self, sent)
        self.assertEqual({"type": "text", "text": "short"}, got["content"][0])
        self.assertEqual({"uri": "memo://a", "blob": "AAAA"}, got["content"][1]["resource"])
        self.assertEqual({RESULT_CUT: {"limit_bytes": LIMIT, "text_bytes": 5, "left_out": ["_meta"]}}, got["_meta"])
        self.assertEqual([RESULT_CUT], [r["record"] for r in records])

    def test_a_small_items_meta_and_a_short_bare_string_pass_whole_and_unmarked(self):
        sent = {"content": [{"type": "text", "text": "short", "_meta": {"k": 1}}, "plain"]}
        got, records = _answered(self, sent)
        self.assertEqual(sent, got)
        self.assertEqual([], records)


class OnlyTheClientWritesItsMark(unittest.TestCase):
    def test_a_servers_own_cut_mark_in_a_whole_result_is_dropped(self):
        forged = {"limit_bytes": 1, "text_bytes": 2, "left_out": ["structuredContent"]}
        got, records = _answered(self, {"content": [{"type": "text", "text": "ok"}],
                                        "_meta": {RESULT_CUT: forged, "k": 1}})
        self.assertNotIn(RESULT_CUT, got["_meta"])
        self.assertEqual(1, got["_meta"]["k"], "the server's other keys are kept")
        self.assertEqual([], records)

    def test_a_servers_own_cut_mark_in_a_cut_result_is_replaced_by_the_clients(self):
        got, records = _answered(self, {"content": [{"type": "text", "text": "x" * MIB}],
                                        "_meta": {RESULT_CUT: {"limit_bytes": 1, "text_bytes": 2}}})
        self.assertEqual({"limit_bytes": LIMIT, "text_bytes": MIB}, got["_meta"][RESULT_CUT])
        self.assertEqual([RESULT_CUT], [r["record"] for r in records])


if __name__ == "__main__":
    unittest.main()
