"""Two more limits on an outside server (``limits.py``): the answers the client owes a server that does not read
are bounded (``MAX_UNSENT_ANSWER_BYTES``), and one listing of tools is bounded in pages (``MAX_LIST_PAGES``) and
by one deadline for the whole listing.

The stub server runs in the test's own process (``support``); a server that never ends its pages is answered by
a thread of the test, and every listing is ended by the test itself, so no run waits for ever.
"""

from __future__ import annotations

import queue
import threading
import time
import unittest
from unittest import mock

from recursion_core.control.mcp.framing import encode
from recursion_core.mcp.client import (
    ANSWERS_DROPPED,
    CALL_TIMEOUT,
    LIST_CUT_PAGES,
    LIST_CUT_TIME,
    MAX_LIST_PAGES,
    MAX_UNSENT_ANSWER_BYTES,
    SERVER_CUT_OFF,
    SERVER_REQUEST_REFUSED,
    TOOL_LIST_CUT,
    McpClient,
    McpClientError,
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
MARGIN_S = 0.5  # what a listing may take past its deadline: the last page's own wait and the thread's start
LIST_CHANGED = {"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}


def _unsent_answers(client: McpClient) -> tuple[int, int]:
    """(count, encoded bytes) of the answers to the server's requests still in the client's outbox."""

    with client._cond:
        messages = [entry[0] if isinstance(entry, tuple) else entry for entry in client._outbox]
    answers = [m for m in messages if "method" not in m]
    return len(answers), sum(len(encode(m)) for m in answers)


class _Marker:
    """An ``on_record`` that keeps every record and wakes a waiter when a refused request names ``method``."""

    def __init__(self) -> None:
        self.records: list = []
        self._seen: dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    def __call__(self, record: dict) -> None:
        self.records.append(record)
        if record.get("record") == SERVER_REQUEST_REFUSED:
            self._event(record.get("method")).set()

    def _event(self, method) -> threading.Event:
        with self._lock:
            return self._seen.setdefault(method, threading.Event())

    def wait(self, method: str) -> bool:
        return self._event(method).wait(HANG_GUARD_S)

    def named(self, code: str) -> list:
        return [r for r in self.records if r.get("record") == code]


class _GatedOutput:
    """The server's input as the client writes it, held while the gate is shut: a server that does not read."""

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


class UnsentAnswers(unittest.TestCase):
    def test_the_bound_is_one_named_constant_in_the_limits_module(self):
        self.assertEqual(1024 * 1024, MAX_UNSENT_ANSWER_BYTES)
        self.assertEqual(limits.MAX_UNSENT_ANSWER_BYTES, session.MAX_UNSENT_ANSWER_BYTES)

    def test_a_server_that_does_not_read_cannot_grow_the_answers_it_is_owed(self):
        marker = _Marker()
        client, stub = make_client(self, on_record=marker)
        open_session(self, client, stub)
        stub.stall()  # it reads one more line and then never reads again
        for number in range(50_000):
            stub.send({"jsonrpc": "2.0", "id": number, "method": "roots/list"})
        stub.send({"jsonrpc": "2.0", "id": "end-1", "method": "vendor/end-1"})
        self.assertTrue(marker.wait("vendor/end-1"))
        count, size = _unsent_answers(client)
        self.assertLessEqual(size, MAX_UNSENT_ANSWER_BYTES,
                             f"after 50,000 roots/list: answers the client holds unsent: {count}, {size} bytes")
        for number in range(60):
            stub.send({"jsonrpc": "2.0", "id": f"{number:08d}" + "p" * MIB, "method": "ping"})
        stub.send({"jsonrpc": "2.0", "id": "end-2", "method": "vendor/end-2"})
        self.assertTrue(marker.wait("vendor/end-2"))
        count, size = _unsent_answers(client)
        self.assertLessEqual(size, MAX_UNSENT_ANSWER_BYTES,
                             f"after 60 pings of a 1 MiB id: answers the client holds unsent: {count}, {size} bytes")
        dropped = marker.named(ANSWERS_DROPPED)
        self.assertEqual(1, len(dropped), "one record when dropping starts, none while it goes on")
        self.assertEqual({"record", "pending_bytes", "limit_bytes"}, set(dropped[0]))
        self.assertEqual(MAX_UNSENT_ANSWER_BYTES, dropped[0]["limit_bytes"])
        self.assertLessEqual(dropped[0]["pending_bytes"], MAX_UNSENT_ANSWER_BYTES)
        self.assertIsNone(client.ending, "one breach, not three")

    def test_a_request_the_caller_sends_is_never_dropped_by_this_bound(self):
        marker = _Marker()
        client, stub = make_client(self, on_record=marker)
        open_session(self, client, stub)
        stub.stall()
        for number in range(30_000):
            stub.send({"jsonrpc": "2.0", "id": number, "method": "roots/list"})
        stub.send({"jsonrpc": "2.0", "id": "end", "method": "vendor/end"})
        self.assertTrue(marker.wait("vendor/end"))
        self.assertEqual(1, len(marker.named(ANSWERS_DROPPED)))
        call = InThread(lambda: client.call_tool("still-sent", deadline_s=0.2))
        with self.assertRaises(McpClientError) as caught:
            call.get()
        self.assertEqual(CALL_TIMEOUT, caught.exception.code, "the caller's request keeps its own deadline")
        with client._cond:
            methods = [(entry[0] if isinstance(entry, tuple) else entry).get("method") for entry in client._outbox]
        self.assertNotIn("tools/call", methods, "its caller gave up before it was written: it is never sent")
        self.assertNotIn("notifications/cancelled", methods, "the server never saw it: no cancel")

    def _stretch(self, client, stub, gated, marker, tag: str, wait_for: int) -> None:
        """One stretch of a server that does not read: the gate shut, ten refused requests."""

        written_by = time.monotonic() + HANG_GUARD_S
        while client._unsent_answer_bytes and time.monotonic() < written_by:  # the last answer counts until written
            time.sleep(0.01)
        gated.gate.clear()
        for number in range(10):
            stub.send({"jsonrpc": "2.0", "id": f"{tag}-{number}", "method": f"vendor/{tag}-{number}"})
        self.assertTrue(marker.wait(f"vendor/{tag}-{wait_for}"))

    def test_room_again_gives_one_record_with_the_count_and_a_third_stretch_cuts_the_server_off(self):
        stub = StubServer()
        gated = _GatedOutput(stub.client_out)
        marker = _Marker()
        client = McpClient(stub.client_in, gated, on_record=marker)
        self.addCleanup(stub.close)
        self.addCleanup(stub.end_output)
        self.addCleanup(client.close)
        self.addCleanup(gated.gate.set)
        open_session(self, client, stub)
        answer = len(encode({"jsonrpc": "2.0", "id": "s0-0", "error": {"code": -32601, "message": "Method not found"}}))
        with mock.patch.object(session, "MAX_UNSENT_ANSWER_BYTES", 3 * answer):  # three answers held, seven dropped
            for stretch in range(2):
                self._stretch(client, stub, gated, marker, f"s{stretch}", 9)
                started = [r for r in marker.named(ANSWERS_DROPPED) if "limit_bytes" in r]
                self.assertEqual(stretch + 1, len(started), "one record when dropping starts")
                self.assertEqual(3 * answer, started[-1]["pending_bytes"])
                self.assertIsNone(client.ending)
                gated.gate.set()  # the server reads again: the three answers held are written
                self.assertEqual([f"s{stretch}-{n}" for n in range(3)], [stub.next()["id"] for _ in range(3)])
                stub.send({"jsonrpc": "2.0", "id": f"r{stretch}", "method": f"vendor/r{stretch}"})
                self.assertTrue(marker.wait(f"vendor/r{stretch}"))
                self.assertEqual(f"r{stretch}", stub.next()["id"], "the dropped answers are never written")
                resumed = [r for r in marker.named(ANSWERS_DROPPED) if "dropped" in r]
                self.assertEqual([{"record": ANSWERS_DROPPED, "dropped": 7}] * (stretch + 1), resumed)
            self._stretch(client, stub, gated, marker, "s2", 3)
        self.assertEqual(SERVER_CUT_OFF, client.ending, "the third stretch is the third breach")
        self.assertEqual([{"record": SERVER_CUT_OFF, "breaches": 3}], marker.named(SERVER_CUT_OFF))


class _Pages:
    """A server thread answering ``tools/list``: ``page(number)`` gives the page (``None``: never answered)."""

    def __init__(self, test, stub: StubServer, page, delay_s: float = 0.0) -> None:
        self.asked = 0
        self._stub, self._page, self._delay = stub, page, delay_s
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        test.addCleanup(self.stop)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                message = self._stub.next(timeout=0.1)
            except queue.Empty:
                continue
            if message is None:
                return
            if message.get("method") != "tools/list":
                continue
            self.asked += 1
            result = self._page(self.asked)
            if self._delay:
                time.sleep(self._delay)
            if result is not None and not self._stop.is_set():
                try:
                    self._stub.answer(message, result)
                except (OSError, ValueError):
                    return

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(HANG_GUARD_S)


def _empty_page(number: int) -> dict:
    return {"tools": [], "nextCursor": f"c{number}"}


class OneListing(unittest.TestCase):
    def test_the_page_bound_is_one_named_constant_in_the_limits_module(self):
        self.assertEqual(64, MAX_LIST_PAGES)
        self.assertEqual(limits.MAX_LIST_PAGES, session.MAX_LIST_PAGES)

    def _listing(self, client, deadline_s: float):
        started = time.monotonic()
        listing = InThread(lambda: client.list_tools(deadline_s=deadline_s))
        try:
            tools = listing.get(timeout=3.0)
        except AssertionError:
            client.close()  # the run never hangs: the test ends the session itself
            self.fail(f"list_tools(deadline_s={deadline_s}) after 3 s: STILL WAITING")
        return tools, time.monotonic() - started

    def test_pages_with_no_tool_end_at_the_listings_deadline(self):
        records: list = []
        client, stub = make_client(self, on_record=records.append)
        open_session(self, client, stub)
        with mock.patch.object(session, "MAX_LIST_PAGES", 10 ** 9):  # only the deadline can end it here
            pages = _Pages(self, stub, _empty_page)
            tools, took = self._listing(client, 1.0)
            asked = pages.asked
            time.sleep(SHORT)
            self.assertLessEqual(pages.asked, asked + 1, "no page is asked for after the bound")
        self.assertLess(took, 1.0 + MARGIN_S)
        self.assertEqual([], tools)
        self.assertEqual((TOOL_LIST_CUT, LIST_CUT_TIME), (tools.cut, tools.reason))
        cut = [r for r in records if r["record"] == TOOL_LIST_CUT]
        self.assertEqual(1, len(cut))
        self.assertEqual(LIST_CUT_TIME, cut[0]["reason"])
        self.assertIsNone(client.ending, "one breach")

    def test_pages_with_no_tool_end_at_the_page_bound(self):
        records: list = []
        client, stub = make_client(self, on_record=records.append)
        open_session(self, client, stub)
        pages = _Pages(self, stub, _empty_page)
        tools, took = self._listing(client, 1.0)
        self.assertEqual((TOOL_LIST_CUT, LIST_CUT_PAGES), (tools.cut, tools.reason))
        time.sleep(SHORT)
        self.assertEqual(MAX_LIST_PAGES, pages.asked, "no page is asked for after the bound")
        self.assertEqual([{"record": TOOL_LIST_CUT, "reason": LIST_CUT_PAGES, "limit": MAX_LIST_PAGES,
                           "listed": 0}], [r for r in records if r["record"] == TOOL_LIST_CUT])

    def test_65_pages_of_one_tool_inside_the_deadline_are_listed_to_64_pages(self):
        client, stub = make_client(self)
        open_session(self, client, stub)

        def page(number):
            result = {"tools": [{"name": f"t{number}"}]}
            if number < 65:
                result["nextCursor"] = f"c{number}"
            return result

        pages = _Pages(self, stub, page)
        tools, _took = self._listing(client, 2.5)
        self.assertEqual([{"name": f"t{n}"} for n in range(1, 65)], list(tools))
        self.assertEqual((TOOL_LIST_CUT, LIST_CUT_PAGES), (tools.cut, tools.reason))
        self.assertEqual(64, pages.asked)

    def test_512_pages_of_one_tool_then_empty_pages_are_cut_at_the_page_bound(self):
        client, stub = make_client(self)
        open_session(self, client, stub)
        pages = _Pages(self, stub, lambda n: {"tools": [{"name": f"t{n}"}] if n <= 512 else [],
                                             "nextCursor": f"c{n}"})
        tools, _took = self._listing(client, 2.5)
        self.assertEqual(MAX_LIST_PAGES, len(tools))
        self.assertEqual(LIST_CUT_PAGES, tools.reason)
        self.assertEqual(MAX_LIST_PAGES, pages.asked)

    def test_one_deadline_covers_the_whole_listing_not_each_page(self):
        records: list = []
        client, stub = make_client(self, on_record=records.append)
        open_session(self, client, stub)
        _Pages(self, stub, lambda n: {"tools": [{"name": f"t{n}"}], **({"nextCursor": f"c{n}"} if n < 6 else {})},
               delay_s=0.3)
        tools, took = self._listing(client, 0.5)
        self.assertLess(took, 0.5 + MARGIN_S, "six pages of 0.3 s each: the listing's deadline is 0.5 s")
        self.assertEqual([{"name": "t1"}], list(tools))
        self.assertEqual((TOOL_LIST_CUT, LIST_CUT_TIME), (tools.cut, tools.reason))
        self.assertEqual([LIST_CUT_TIME], [r["reason"] for r in records if r["record"] == TOOL_LIST_CUT])

    def test_a_whole_list_of_pages_is_not_cut(self):
        client, stub = make_client(self)
        open_session(self, client, stub)
        _Pages(self, stub, lambda n: {"tools": [{"name": f"t{n}"}], **({"nextCursor": f"c{n}"} if n < 64 else {})})
        tools, _took = self._listing(client, 2.5)
        self.assertEqual(64, len(tools))
        self.assertEqual((None, None), (tools.cut, tools.reason))

    def test_long_cursors_are_kept_as_digests_and_a_repeat_is_still_a_bad_answer(self):
        client, stub = make_client(self)
        open_session(self, client, stub)
        long = "x" * (64 * 1024)
        _Pages(self, stub, lambda n: {"tools": [], "nextCursor": long + ("again" if n >= 3 else str(n))})
        listing = InThread(lambda: client.list_tools(deadline_s=2.5))
        with self.assertRaises(McpClientError) as caught:
            listing.get(timeout=3.0)
        self.assertEqual("mcp_bad_answer", caught.exception.code)


class Relisting(unittest.TestCase):
    def _relist_ends(self, records: list, done: threading.Event, pages: _Pages, client) -> None:
        if not done.wait(3.0):
            asked = pages.asked
            client.close()
            self.fail(f"the relist thread after one change notice, 3 s: STILL LISTING | pages {asked}")

    def test_a_change_notice_then_pages_with_no_tool_end_at_the_page_bound_with_no_caller(self):
        records: list = []
        done = threading.Event()

        def on_record(record):
            records.append(record)
            if record.get("record") == TOOL_LIST_CUT:
                done.set()

        client, stub = make_client(self, on_record=on_record)
        open_session(self, client, stub)
        pages = _Pages(self, stub, _empty_page)
        stub.send(LIST_CHANGED)
        self._relist_ends(records, done, pages, client)
        time.sleep(SHORT)
        self.assertEqual(MAX_LIST_PAGES, pages.asked)
        self.assertEqual([LIST_CUT_PAGES], [r["reason"] for r in records if r["record"] == TOOL_LIST_CUT])
        self.assertIsNone(client.ending)

    def test_a_change_notice_then_pages_with_no_tool_end_at_the_listings_deadline(self):
        records: list = []
        done = threading.Event()

        def on_record(record):
            records.append(record)
            if record.get("record") == TOOL_LIST_CUT:
                done.set()

        client, stub = make_client(self, on_record=on_record)
        open_session(self, client, stub)
        with mock.patch.object(session, "MAX_LIST_PAGES", 10 ** 9), \
                mock.patch.object(session, "CALL_DEADLINE_S", 1.0):
            pages = _Pages(self, stub, _empty_page)
            started = time.monotonic()
            stub.send(LIST_CHANGED)
            self._relist_ends(records, done, pages, client)
            took = time.monotonic() - started
        self.assertLess(took, 1.0 + MARGIN_S)
        self.assertEqual([LIST_CUT_TIME], [r["reason"] for r in records if r["record"] == TOOL_LIST_CUT])


if __name__ == "__main__":
    unittest.main()
