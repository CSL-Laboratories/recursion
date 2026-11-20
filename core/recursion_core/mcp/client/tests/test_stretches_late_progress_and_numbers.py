"""The client's fourth answer round on an outside server: a stretch of dropped answers is one pause of the server
(it ends only when an answer is taken while none is held), a progress report gives no time to a call whose
deadline has already passed, and only a line that can spell a number that is not finite has its message walked.

Every server here is the stub of ``support`` over pipes in the test's own process: no file, no folder, no
process. The client's writes are held, or let through one at a time, by the stand-in below, and the reader's
progress is followed through a progress callback, so no count here depends on how fast a thread runs.
"""

from __future__ import annotations

import json
import threading
import time
import unittest
from unittest import mock

from recursion_core.control.mcp.framing import encode
from recursion_core.mcp.client import (
    ANSWERS_DROPPED,
    BAD_ANSWER,
    CALL_TIMEOUT,
    SERVER_CUT_OFF,
    McpClient,
    McpClientError,
    McpSessionEnded,
)
from recursion_core.mcp.client import session
from recursion_core.mcp.client.tests.support import HANG_GUARD_S, SHORT, InThread, StubServer, make_client, open_session


class _Records:
    """An ``on_record`` that keeps every record."""

    def __init__(self) -> None:
        self.records: list = []
        self._lock = threading.Lock()

    def __call__(self, record: dict) -> None:
        with self._lock:
            self.records.append(record)

    def named(self, code: str, key: str | None = None) -> list:
        with self._lock:
            return [r for r in self.records if r.get("record") == code and (key is None or key in r)]


class _Metered:
    """The server's input as the client writes it. After ``hold`` each write waits for a permit (``allow``):
    a server that reads one answer at a time, or none; ``release`` lets every write through again."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self._free = threading.Event()
        self._free.set()
        self._permits = threading.Semaphore(0)

    def hold(self) -> None:
        self._free.clear()

    def allow(self, count: int = 1) -> None:
        for _ in range(count):
            self._permits.release()

    def release(self) -> None:
        self._free.set()

    def write(self, data):
        while not self._free.is_set() and not self._permits.acquire(timeout=0.01):
            pass
        return self._inner.write(data)

    def flush(self):
        self._inner.flush()

    def close(self):
        self._free.set()
        self._inner.close()


def _until(test, condition, what: str) -> None:
    ends = time.monotonic() + HANG_GUARD_S
    while not condition():
        test.assertLess(time.monotonic(), ends, what)
        time.sleep(0.005)


class _Session:
    """A session over a metered writer, with a call in flight whose progress callback shows how far the reader
    has read: ``mark`` sends one report and returns once the reader has handed it over (or the session ended)."""

    def __init__(self, test, records: _Records) -> None:
        self.test = test
        self.stub = StubServer()
        self.out = _Metered(self.stub.client_out)
        self.client = McpClient(self.stub.client_in, self.out, on_record=records)
        test.addCleanup(self.stub.close)
        test.addCleanup(self.stub.end_output)
        test.addCleanup(self.client.close)
        test.addCleanup(self.out.release)
        open_session(test, self.client, self.stub)
        self._seen: list = []
        self._step = 0
        self.client.begin_call("marker", on_progress=self._seen.append)
        self._token = self.stub.expect(test, "tools/call")["params"]["_meta"]["progressToken"]

    def ping(self, request_id: str) -> None:
        self.stub.send({"jsonrpc": "2.0", "id": request_id, "method": "ping"})

    def mark(self) -> None:
        self._step += 1
        step = self._step
        self.stub.send({"jsonrpc": "2.0", "method": "notifications/progress",
                        "params": {"progressToken": self._token, "progress": step}})
        _until(self.test, lambda: any(p.get("progress") == step for p in list(self._seen))
               or self.client.ending is not None, "the reader did not reach the report")

    def held(self) -> int:
        with self.client._cond:
            return self.client._unsent_answer_bytes


class OneStretchIsOnePause(unittest.TestCase):
    def test_a_server_that_reads_one_answer_between_drops_is_counted_once_and_not_cut_off(self):
        records = _Records()
        s = _Session(self, records)
        size = len(encode({"jsonrpc": "2.0", "id": "p0", "result": {}}))
        with mock.patch.object(session, "MAX_UNSENT_ANSWER_BYTES", 2 * size):  # two answers held, a third dropped
            s.out.hold()
            s.ping("p0")  # taken: the writer holds it in its write
            s.ping("p1")  # taken: two held
            s.ping("p2")  # dropped: the stretch starts, one breach
            s.mark()
            for taken, dropped in (("p3", "p4"), ("p5", "p6")):
                s.out.allow(1)  # the server reads one answer: one is still held
                _until(self, lambda: s.held() == size, "the server's read did not free one answer")
                s.ping(taken)  # taken while one is still held: the stretch goes on
                s.ping(dropped)  # dropped again: the same stretch, no new breach
                s.mark()
                self.assertIsNone(s.client.ending, f"cut off at {dropped}: one pause was counted more than once")
            self.assertEqual(1, len(records.named(ANSWERS_DROPPED, "limit_bytes")), "one breach for the stretch")
            self.assertEqual([], records.named(ANSWERS_DROPPED, "dropped"), "the stretch has not ended")
            s.out.release()  # the server reads all it is owed
            _until(self, lambda: s.held() == 0, "the held answers were not written")
            s.ping("p7")  # taken with none held: the stretch ends here
            s.mark()
        self.assertEqual([{"record": ANSWERS_DROPPED, "dropped": 3}], records.named(ANSWERS_DROPPED, "dropped"))
        ids = []
        while "p7" not in ids:
            message = s.stub.next(timeout=5.0)
            self.assertIsNotNone(message, "the client shut the server's input")
            if "result" in message:
                ids.append(message["id"])
        self.assertEqual(["p0", "p1", "p3", "p5", "p7"], ids, "the dropped answers are never written")
        self.assertEqual([], records.named(SERVER_CUT_OFF))
        self.assertIsNone(s.client.ending)

    def test_a_server_that_never_reads_is_counted_once_however_the_sizes_of_its_answers_mix(self):
        records = _Records()
        s = _Session(self, records)
        short = len(encode({"jsonrpc": "2.0", "id": "s0", "result": {}}))
        long = len(encode({"jsonrpc": "2.0", "id": "L0" + "x" * 200, "result": {}}))
        self.assertGreater(long, 2 * short)
        with mock.patch.object(session, "MAX_UNSENT_ANSWER_BYTES", long + 3 * short):
            s.out.hold()  # and never a permit: the server reads nothing more
            s.ping("s0")  # taken (held in the writer's write)
            s.ping("L1" + "x" * 200)  # taken
            for number in (2, 3, 4):
                s.ping(f"L{number}" + "x" * 200)  # dropped: it does not fit
                if number < 4:
                    s.ping(f"s{number - 1}")  # a short answer still fits: taken, but answers are still held
                s.mark()
                self.assertIsNone(s.client.ending, f"cut off at L{number}: one pause was counted more than once")
        self.assertEqual(1, len(records.named(ANSWERS_DROPPED, "limit_bytes")), "one breach for the stretch")
        self.assertEqual([], records.named(ANSWERS_DROPPED, "dropped"), "nothing was taken with none held")
        self.assertEqual(3, s.client._answers_dropped)
        self.assertEqual(long + 3 * short, s.held())


class ProgressAfterTheDeadline(unittest.TestCase):
    def test_a_report_for_a_call_past_its_deadline_gives_it_no_time_and_its_late_answer_is_dropped(self):
        records, seen = _Records(), []
        stub = StubServer()
        out = _Metered(stub.client_out)
        client = McpClient(stub.client_in, out, on_record=records)
        self.addCleanup(stub.close)
        self.addCleanup(stub.end_output)
        self.addCleanup(client.close)
        self.addCleanup(out.release)
        open_session(self, client, stub)
        handle = client.begin_call("slow", on_progress=seen.append, deadline_s=0.3)
        request = stub.expect(self, "tools/call")
        out.hold()
        client.begin_call("jam")  # the writer, the session's timer, is held inside this write
        time.sleep(0.3 + SHORT)  # the call's deadline passes; nobody waits on it and the timer is held
        stub.send({"jsonrpc": "2.0", "method": "notifications/progress",
                   "params": {"progressToken": request["params"]["_meta"]["progressToken"], "progress": 1}})
        stub.answer(request, {"content": [{"type": "text", "text": "late"}]})
        _until(self, lambda: request["id"] not in client._pending, "the reader did not settle the call")
        with self.assertRaises(McpClientError) as caught:
            InThread(handle.wait).get(timeout=2.0)
        self.assertEqual(CALL_TIMEOUT, caught.exception.code, "the late answer was taken")
        self.assertNotIsInstance(caught.exception, McpSessionEnded)
        self.assertEqual([], seen, "a report for a call already over is not handed to its callback")
        self.assertEqual([], records.named(CALL_TIMEOUT), "a shorter deadline the caller asked for is not counted")
        self.assertIsNone(client.ending)


def _raw_call(test, client, stub, result_json: str):
    call = InThread(lambda: client.call_tool("number"))
    request = stub.expect(test, "tools/call")
    stub.send_bytes(('{"jsonrpc":"2.0","id":%d,"result":%s}\n' % (request["id"], result_json)).encode())
    return call


class OnlyALineThatCanSpellOneIsWalked(unittest.TestCase):
    def test_an_ordinary_line_and_a_nested_one_are_not_walked_and_a_line_with_an_exponent_is(self):
        client, stub = make_client(self)
        open_session(self, client, stub)
        nested: list = []
        for _ in range(100):
            nested = [nested, 1.5]
        ordinary = {"content": [{"type": "text", "text": "12 and 0.25, e9"}], "structuredContent": {"n": [1.5, -12]}}
        with mock.patch.object(session, "_finite", wraps=session._finite) as walk:
            for result in (ordinary, {"content": [], "structuredContent": {"deep": nested}}):
                self.assertEqual(result, _raw_call(self, client, stub, json.dumps(result)).get())
            self.assertEqual(0, walk.call_count, "a line that cannot spell an infinite number is not walked")
            got = _raw_call(self, client, stub, '{"content":[],"structuredContent":{"n":15e2}}').get()
            self.assertEqual(1500.0, got["structuredContent"]["n"])
            self.assertEqual(1, walk.call_count, "a line with an exponent is walked")
        self.assertIsNone(client.ending)

    def test_an_infinite_number_in_every_spelling_is_still_refused_and_a_long_finite_one_is_taken(self):
        client, stub = make_client(self)
        open_session(self, client, stub)
        for spelling in ("1e999", "-1E+999", "2e308", "1" + "0" * 309 + ".5", "-" + "9" * 400 + ".0"):
            with self.subTest(number=spelling[:12]):
                call = _raw_call(self, client, stub, '{"content":[],"structuredContent":{"n":[1,%s]}}' % spelling)
                with self.assertRaises(McpClientError) as caught:
                    call.get()
                self.assertEqual(BAD_ANSWER, caught.exception.code)
                self.assertNotIsInstance(caught.exception, McpSessionEnded)
        small = "0." + "0" * 400 + "1"  # a run of 401 digits that is finite (it reads as 0.0): walked, then taken
        got = _raw_call(self, client, stub, '{"content":[],"structuredContent":{"n":%s}}' % small).get()
        self.assertEqual(0.0, got["structuredContent"]["n"])
        self.assertIsNone(client.ending)


if __name__ == "__main__":
    unittest.main()
