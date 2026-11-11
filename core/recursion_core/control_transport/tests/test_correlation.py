"""Close deadline and request-id correlation (CSL-719).

The close budget is one deadline for every handler, measured on an injected clock so
the suite does not sleep. Blocking handlers stand in for a Stop that has not returned.
Replies are delivered only for the request id that is still in flight.
"""

from __future__ import annotations

import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest

from recursion_core.control_transport import framing
from recursion_core.control_transport.errors import FrameError
from recursion_core.control_transport.correlation import (
    ACCEPTED,
    CANCELLED,
    CLOSING,
    DUPLICATE_ID,
    IGNORED,
    MISMATCH,
    MISSING_ID,
    REPLAYED_ID,
    UNKNOWN_ID,
    RequestTable,
)
from recursion_core.control_transport.unix import UnixControlClient, UnixControlServer

LINUX = sys.platform.startswith("linux")


def _pong(message, conn):
    return {"ok": True, "op": "pong"}


class FakeClock:
    """Monotonic stand-in. Tests advance it; nothing in here sleeps."""

    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class CloseDeadlineTests(unittest.TestCase):
    def test_join_budget_is_spent_once_across_handlers(self) -> None:
        clock = FakeClock()
        table = RequestTable(clock=clock, close_timeout_s=5.0)
        claims = [table.claim(f"h{i}") for i in range(4)]
        self.assertTrue(all(claim.accepted for claim in claims))
        self.assertEqual(table.begin_close(), 5.0)
        self.assertEqual(table.begin_close(), 5.0)  # a second close does not extend it

        spent = 0.0
        slots = []
        for _ in claims:
            left = table.remaining()
            slots.append(left)
            clock.advance(left)  # this blocked handler consumes whatever it was given
            spent += left
        self.assertEqual(slots, [5.0, 0.0, 0.0, 0.0])
        self.assertEqual(spent, 5.0)
        self.assertEqual(set(table.cancel_overdue()), {"h0", "h1", "h2", "h3"})
        for claim in claims:
            late = {"ok": True, "id": claim.request_id, "state": "stopped"}
            self.assertEqual(table.finish(claim, late), IGNORED)

    def test_time_a_handler_does_not_use_stays_for_the_others(self) -> None:
        clock = FakeClock()
        table = RequestTable(clock=clock, close_timeout_s=5.0)
        table.begin_close()
        self.assertEqual(table.remaining(), 5.0)
        clock.advance(1.0)
        self.assertEqual(table.remaining(), 4.0)
        self.assertEqual(table.cancel_overdue(), [])

    def test_blocked_handlers_are_cancelled_at_the_deadline_and_late_results_ignored(self) -> None:
        clock = FakeClock()
        table = RequestTable(clock=clock, close_timeout_s=5.0)
        claims = [table.claim(f"h{i}") for i in range(3)]
        entered = threading.Barrier(4)
        outcomes: list[str] = []

        def run(claim) -> None:
            entered.wait(timeout=2)
            claim.event.wait()
            outcomes.append(
                table.finish(claim, {"ok": True, "id": claim.request_id, "state": "stopped"})
            )

        threads = [threading.Thread(target=run, args=(claim,)) for claim in claims]
        for thread in threads:
            thread.start()
        entered.wait(timeout=2)
        self.assertEqual(table.begin_close(), 5.0)
        self.assertEqual(table.cancel_overdue(), [])
        self.assertTrue(all(not claim.event.is_set() for claim in claims))
        clock.advance(5.0)
        self.assertEqual(set(table.cancel_overdue()), {"h0", "h1", "h2"})
        for thread in threads:
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
        self.assertEqual(outcomes, [IGNORED, IGNORED, IGNORED])

    def test_a_result_that_arrives_before_the_deadline_is_kept(self) -> None:
        clock = FakeClock()
        table = RequestTable(clock=clock, close_timeout_s=5.0)
        claim = table.claim("s1")
        self.assertEqual(table.begin_close(), 5.0)
        clock.advance(1.0)
        self.assertFalse(table.past_deadline())
        self.assertEqual(table.finish(claim, {"ok": True, "id": "s1", "state": "stopped"}), ACCEPTED)
        clock.advance(10.0)
        self.assertEqual(table.cancel_overdue(), [])
        self.assertEqual(table.finish(claim, {"ok": True, "id": "s1", "late": True}), IGNORED)

    def test_claim_after_the_deadline_does_not_start_another_handler(self) -> None:
        clock = FakeClock()
        table = RequestTable(clock=clock, close_timeout_s=5.0)
        table.begin_close()
        clock.advance(5.0)
        refused = table.claim("late")
        self.assertFalse(refused.accepted)
        self.assertEqual(refused.code, CLOSING)


class RequestIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.table = RequestTable(clock=FakeClock(), close_timeout_s=5.0)

    def test_replies_match_the_request_that_is_still_in_flight(self) -> None:
        first = self.table.claim("a")
        second = self.table.claim("b")
        self.assertEqual(self.table.finish(second, {"ok": True, "id": "b", "state": "stopped"}), ACCEPTED)
        # b's body does not satisfy a, even though b was a real in-flight id.
        self.assertEqual(
            self.table.finish(first, {"ok": True, "id": "b", "state": "stopped"}),
            MISMATCH,
        )
        other = self.table.claim("a")
        self.assertFalse(other.accepted)
        self.assertEqual(other.code, REPLAYED_ID)

    def test_a_reply_that_omits_the_id_still_belongs_to_the_request(self) -> None:
        claim = self.table.claim("s1")
        self.assertEqual(self.table.finish(claim, {"ok": True, "state": "stopped"}), ACCEPTED)

    def test_duplicate_id_does_not_replace_the_in_flight_request(self) -> None:
        claim = self.table.claim("s1")
        duplicate = self.table.claim("s1")
        self.assertFalse(duplicate.accepted)
        self.assertEqual(duplicate.code, DUPLICATE_ID)
        self.assertEqual(self.table.finish(claim, {"ok": True, "id": "s1"}), ACCEPTED)
        self.assertEqual(self.table.finish(duplicate, {"ok": True, "id": "s1"}), IGNORED)

    def test_int_and_str_ids_are_distinct_and_bool_is_not_an_id(self) -> None:
        one = self.table.claim(1)
        self.assertTrue(one.accepted)
        self.assertTrue(self.table.claim("1").accepted)
        self.assertTrue(self.table.claim(0).accepted)
        # JSON true is not the integer 1, even though True == 1 in Python.
        self.assertEqual(self.table.finish(one, {"ok": True, "id": True}), MISMATCH)
        for bad in (None, True, False, "", 1.5, b"s1", {"id": "s1"}, ["s1"]):
            refused = self.table.claim(bad)
            self.assertFalse(refused.accepted, bad)
            self.assertEqual(refused.code, MISSING_ID, bad)

    def test_omitted_id_is_served_and_is_not_a_duplicate_of_another_omitted_id(self) -> None:
        first = self.table.claim_absent()
        second = self.table.claim_absent()
        self.assertTrue(first.accepted)
        self.assertTrue(second.accepted)
        self.assertIsNone(first.request_id)
        self.assertEqual(self.table.finish(first, {"ok": True, "op": "pong"}), ACCEPTED)
        self.assertEqual(self.table.finish(second, {"ok": True, "op": "pong"}), ACCEPTED)

    def test_another_connection_may_reuse_an_id(self) -> None:
        first = self.table.claim(0, scope="conn-a")
        second = self.table.claim(0, scope="conn-b")
        self.assertTrue(first.accepted)
        self.assertTrue(second.accepted)
        self.assertEqual(self.table.finish(first, {"ok": True, "id": 0}), ACCEPTED)
        again = self.table.claim(0, scope="conn-a")
        self.assertEqual(again.code, REPLAYED_ID)
        self.assertEqual(self.table.finish(second, {"ok": True, "id": 0}), ACCEPTED)

    def test_cancel_does_not_guess_when_two_connections_share_an_id(self) -> None:
        first = self.table.claim("s1", scope=1)
        second = self.table.claim("s1", scope=2)
        self.assertEqual(self.table.cancel("s1"), DUPLICATE_ID)
        self.assertFalse(first.event.is_set())
        self.assertFalse(second.event.is_set())
        self.assertEqual(self.table.finish(first, {"ok": True, "id": "s1"}), ACCEPTED)
        self.assertEqual(self.table.cancel("s1"), CANCELLED)
        self.assertTrue(second.event.is_set())

    def test_replay_of_a_finished_id_does_not_run_again(self) -> None:
        claim = self.table.claim("s1")
        self.assertEqual(self.table.finish(claim, {"ok": True, "id": "s1", "state": "stopped"}), ACCEPTED)
        replay = self.table.claim("s1")
        self.assertFalse(replay.accepted)
        self.assertEqual(replay.code, REPLAYED_ID)

    def test_replay_of_a_cancelled_id_does_not_run_again(self) -> None:
        claim = self.table.claim("s1")
        self.assertEqual(self.table.cancel("s1"), CANCELLED)
        self.assertTrue(claim.event.is_set())
        self.assertEqual(self.table.finish(claim, {"ok": True, "id": "s1", "state": "stopped"}), IGNORED)
        self.assertEqual(self.table.claim("s1").code, REPLAYED_ID)
        self.assertEqual(self.table.cancel("s1"), CANCELLED)

    def test_cancel_of_an_unknown_or_missing_id_is_deterministic(self) -> None:
        self.assertEqual(self.table.cancel("no-such"), UNKNOWN_ID)
        for bad in (None, True, False, ""):
            self.assertEqual(self.table.cancel(bad), MISSING_ID, bad)
        kept = self.table.claim("keep")
        self.assertEqual(self.table.cancel("no-such"), UNKNOWN_ID)
        self.assertEqual(self.table.finish(kept, {"ok": True, "id": "keep"}), ACCEPTED)
        self.assertEqual(self.table.cancel("keep"), REPLAYED_ID)

    def test_cancel_one_id_does_not_drop_another(self) -> None:
        first = self.table.claim("a")
        second = self.table.claim("b")
        self.assertEqual(self.table.cancel("a"), CANCELLED)
        self.assertEqual(self.table.finish(first, {"ok": True, "id": "a", "state": "stopped"}), IGNORED)
        self.assertEqual(self.table.finish(second, {"ok": True, "id": "b", "state": "stopped"}), ACCEPTED)

    def test_dropping_a_connection_cancels_a_request_that_has_no_id(self) -> None:
        """An omitted id belongs to its connection. Closing that connection cancels it.

        It is not filed under an id, so a second omitted id on the same connection
        is still served, and the settled-id cap does not grow.
        """
        table = RequestTable(clock=FakeClock(), close_timeout_s=5.0)
        first = table.claim_absent(scope="scope-1")
        second = table.claim_absent(scope="scope-1")
        other = table.claim_absent(scope="scope-2")
        named = table.claim("s1", scope="scope-1")
        self.assertTrue(first.accepted)
        self.assertTrue(second.accepted)
        self.assertTrue(named.accepted)
        table.drop_scope("scope-1")
        self.assertTrue(first.event.is_set())
        self.assertTrue(second.event.is_set())
        self.assertTrue(named.event.is_set())
        self.assertFalse(other.event.is_set())
        self.assertEqual(table.finish(first, {"ok": True, "op": "pong"}), IGNORED)
        self.assertEqual(table.finish(second, {"ok": True, "op": "pong"}), IGNORED)
        self.assertEqual(table.finish(named, {"ok": True, "id": "s1"}), IGNORED)
        self.assertEqual(table.settled_count("scope-1"), 0)
        self.assertEqual(table.settled_count(), 0)
        self.assertEqual(table.finish(other, {"ok": True, "op": "pong"}), ACCEPTED)

    def test_an_omitted_id_is_cancelled_with_everyone_else_at_the_deadline(self) -> None:
        clock = FakeClock()
        table = RequestTable(clock=clock, close_timeout_s=5.0)
        claim = table.claim_absent()
        table.begin_close()
        clock.advance(5.0)
        table.cancel_overdue()
        self.assertTrue(claim.event.is_set())
        self.assertEqual(table.finish(claim, {"ok": True, "op": "pong"}), IGNORED)


class SettledIdBoundTests(unittest.TestCase):
    def test_settled_ids_stay_bounded_and_a_live_replay_is_still_replayed(self) -> None:
        cap = 4
        table = RequestTable(clock=FakeClock(), close_timeout_s=5.0, settled_cap=cap)
        for n in range(40):
            claim = table.claim(n, scope="live")
            self.assertTrue(claim.accepted, n)
            self.assertEqual(table.finish(claim, {"ok": True, "id": n}), ACCEPTED)
            self.assertLessEqual(table.settled_count(), cap)
            self.assertLessEqual(table.settled_count("live"), cap)
        # The id just completed is still remembered on this live connection.
        self.assertEqual(table.claim(39, scope="live").code, REPLAYED_ID)
        self.assertLessEqual(table.settled_count("live"), cap)
        # An id the cap has forgotten can run again. That is the bound.
        forgotten = table.claim(0, scope="live")
        self.assertTrue(forgotten.accepted)

        for scope in range(100):
            claim = table.claim("s", scope=scope)
            self.assertEqual(table.finish(claim, {"ok": True, "id": "s"}), ACCEPTED)
            table.drop_scope(scope)
            self.assertLessEqual(table.settled_count(), cap)
        self.assertEqual(table.settled_count(), cap)
        table.drop_scope("live")
        self.assertEqual(table.settled_count(), 0)
        self.assertEqual(table.settled_count("live"), 0)
        # The connection's record is gone, so the id is not a replay anymore.
        self.assertTrue(table.claim(39, scope="live").accepted)
        self.assertTrue(table.claim(0, scope="live").accepted)


@unittest.skipUnless(LINUX, "Linux is the tested path")
class SocketCorrelationTests(unittest.TestCase):
    """The server uses the table: one close deadline, and ids answered on the socket."""

    def setUp(self) -> None:
        self.base = tempfile.mkdtemp(prefix="ct-", dir="/tmp" if os.path.isdir("/tmp") else None)
        self.addCleanup(self._rm)
        self.sock_dir = os.path.join(self.base, "run")

    def _rm(self) -> None:
        shutil.rmtree(self.base, ignore_errors=True)

    def server(self, handler=None, **kwargs) -> UnixControlServer:
        srv = UnixControlServer(self.sock_dir, handler or _pong, **kwargs)
        srv.start()
        self.addCleanup(srv.close)
        return srv

    def test_close_cancels_blocked_handlers_on_one_deadline(self) -> None:
        close_timeout_s = 0.5
        entered = threading.Barrier(4)
        flags: list[bool] = []

        def handler(message, conn):
            entered.wait(timeout=2)
            flags.append(conn.cancelled.wait(timeout=2))
            return {"ok": True, "op": "stop", "state": "stopped", "id": message.get("id")}

        srv = self.server(handler, close_timeout_s=close_timeout_s, idle_timeout_s=30.0, max_connections=4)
        errors: list[object] = []

        def call(i: int) -> None:
            try:
                with UnixControlClient(srv.path, timeout_s=3) as client:
                    errors.append(client.request({"op": "stop", "id": f"s{i}"}))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=call, args=(i,)) for i in range(3)]
        for thread in threads:
            thread.start()
        entered.wait(timeout=2)
        started = time.monotonic()
        srv.close()
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, close_timeout_s * 0.7)
        self.assertLess(elapsed, close_timeout_s * 2.3, f"close took {elapsed:.3f}s")
        for thread in threads:
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and (len(flags) < 3 or srv.late_dropped < 3):
            time.sleep(0.01)
        self.assertEqual(flags, [True, True, True])
        self.assertEqual(srv.late_dropped, 3)
        for item in errors:
            if isinstance(item, dict):
                self.assertNotEqual(item.get("state"), "stopped")
                self.assertNotIn("stopped", str(item.get("op")))

    def test_replay_missing_and_unknown_cancel_on_the_socket(self) -> None:
        entered = threading.Event()
        seen: list[object] = []

        def handler(message, conn):
            if message.get("op") == "ping":
                return {"ok": True, "op": "pong"}
            seen.append(message.get("id"))
            if message.get("id") == "hold":
                entered.set()
                conn.cancelled.wait(timeout=3)
            return {"ok": True, "op": "stop", "state": "stopped"}

        srv = self.server(handler, max_connections=4)
        held: dict[str, object] = {}

        def hold() -> None:
            client = UnixControlClient(srv.path, timeout_s=3)
            try:
                held["reply"] = client.request({"op": "stop", "id": "hold"})
                # Same connection, same id, after the first one settled: not run again.
                held["replay"] = client.request({"op": "stop", "id": "hold"})
            finally:
                client.close()

        worker = threading.Thread(target=hold)
        worker.start()
        self.assertTrue(entered.wait(timeout=2))
        with UnixControlClient(srv.path, timeout_s=2) as client:
            # Another connection may use the same id. Ids are not global.
            other = client.request({"op": "ping", "id": "hold"})
            unknown = client.request({"op": "cancel", "id": "no-such"})
            missing = client.request({"op": "cancel"})
            bad = client.request({"op": "stop", "id": None})
            cancelled = client.request({"op": "cancel", "id": "hold"})
        self.assertEqual(other, {"ok": True, "op": "pong", "id": "hold"})
        self.assertEqual(unknown["error"]["code"], UNKNOWN_ID)
        self.assertEqual(unknown["id"], "no-such")
        self.assertFalse(unknown["error"]["retryable"])
        self.assertEqual(missing["error"]["code"], MISSING_ID)
        self.assertNotIn("id", missing)
        self.assertEqual(bad["error"]["code"], MISSING_ID)
        self.assertIsNone(bad["id"])
        self.assertEqual(cancelled["op"], "cancel")
        self.assertEqual(cancelled["state"], "cancelled")
        self.assertEqual(cancelled["id"], "hold")
        self.assertTrue(cancelled["ok"])
        worker.join(timeout=3)
        self.assertFalse(worker.is_alive())
        reply = held["reply"]
        replay = held["replay"]
        self.assertIsInstance(reply, dict)
        self.assertIsInstance(replay, dict)
        assert isinstance(reply, dict)
        assert isinstance(replay, dict)
        self.assertEqual(reply["error"]["code"], CANCELLED)
        self.assertEqual(reply["id"], "hold")
        self.assertNotIn("state", reply)
        self.assertEqual(replay["error"]["code"], REPLAYED_ID)
        self.assertEqual(replay["id"], "hold")
        self.assertFalse(replay["error"]["retryable"])
        self.assertEqual(seen, ["hold"])
        with UnixControlClient(srv.path, timeout_s=2) as client:
            omitted = client.request({"op": "ping"})
        self.assertEqual(omitted.get("op"), "pong")
        self.assertNotIn("id", omitted)

    def test_settled_ids_on_a_live_connection_stay_bounded(self) -> None:
        """A long-lived connection remembers a bounded set, then drops it on close.

        The newest id is still a replay while the connection is open. After the
        connection closes the record is gone, so the server does not keep one
        entry per request for the process lifetime.
        """
        cap = 4
        seen: list[object] = []

        def handler(message, conn):
            seen.append(message.get("id"))
            return {"ok": True, "op": "stop", "state": "stopped"}

        srv = self.server(handler, settled_cap=cap)
        with UnixControlClient(srv.path, timeout_s=2) as client:
            for n in range(30):
                reply = client.request({"op": "stop", "id": f"n{n}"})
                self.assertEqual(reply["state"], "stopped")
                self.assertEqual(reply["id"], f"n{n}")
                self.assertLessEqual(srv.settled_count(), cap)
            replay = client.request({"op": "stop", "id": "n29"})
            self.assertEqual(replay["error"]["code"], REPLAYED_ID)
            self.assertEqual(replay["id"], "n29")
            self.assertFalse(replay["error"]["retryable"])
            self.assertLessEqual(srv.settled_count(), cap)
        self.assertEqual(seen, [f"n{n}" for n in range(30)])
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and srv.settled_count() != 0:
            time.sleep(0.01)
        self.assertEqual(srv.settled_count(), 0)

    def test_response_too_large_echoes_the_request_id(self) -> None:
        """A client that sent an id gets it back, so request() does not raise reply_id_mismatch."""

        def handler(message, conn):
            return {"ok": True, "id": message["id"], "pad": "x" * 200}

        srv = self.server(handler, max_frame_bytes=80)
        with UnixControlClient(srv.path, timeout_s=2) as client:
            reply = client.request({"op": "stop", "id": "s9"})
        self.assertEqual(reply["error"]["code"], "response_too_large")
        self.assertEqual(reply["id"], "s9")
        self.assertEqual(reply["ok"], False)

    def test_bad_frame_echoes_a_parseable_id_and_unparseable_frames_stay_idless(self) -> None:
        """An oversized frame whose JSON still yields an id comes back as bad_frame with that id.

        A frame that is not JSON has no id to echo. The reply stays id-less. That is
        the only bad_frame a client cannot correlate: there is no id to put on it,
        and a client that had attached one would raise reply_id_mismatch.
        """
        srv = self.server(max_frame_bytes=48)
        with UnixControlClient(srv.path, timeout_s=2, max_frame_bytes=1000) as client:
            reply = client.request({"id": "s9", "op": "stop", "pad": "y" * 40})
        self.assertEqual(reply["error"]["code"], "bad_frame")
        self.assertEqual(reply["error"]["detail"], "frame_too_large")
        self.assertEqual(reply["id"], "s9")
        # null is an id the client sent. Echoing it is what keeps request() from
        # raising reply_id_mismatch; omitting the key would.
        with UnixControlClient(srv.path, timeout_s=2, max_frame_bytes=1000) as client:
            null_id = client.request({"id": None, "op": "stop", "pad": "z" * 40})
        self.assertEqual(null_id["error"]["code"], "bad_frame")
        self.assertIn("id", null_id)
        self.assertIsNone(null_id["id"])

        raw = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        with raw:
            raw.settimeout(2)
            raw.connect(srv.path)
            raw.sendall(framing.HEADER.pack(5) + b"nope!")
            junk = framing.read_frame(raw)
        self.assertIsInstance(junk, dict)
        assert isinstance(junk, dict)
        self.assertEqual(junk["error"]["code"], "bad_frame")
        self.assertNotIn("id", junk)

    def test_a_follow_up_after_frame_too_large_is_not_held(self) -> None:
        """frame_too_large finishes the connection. The next request fails at once.

        The rest of an oversized body may still be in flight. That tail must not
        reset the socket before the bad_frame reply has been read.
        """
        srv = self.server(max_frame_bytes=64)
        with UnixControlClient(srv.path, timeout_s=5, max_frame_bytes=4000) as client:
            first = client.request({"id": "s9", "op": "stop", "pad": "y" * 80})
            self.assertEqual(first["error"]["code"], "bad_frame")
            self.assertEqual(first["error"]["detail"], "frame_too_large")
            self.assertEqual(first["id"], "s9")
            started = time.monotonic()
            with self.assertRaises(ConnectionError):
                client.request({"op": "ping", "id": "next"})
            waited = time.monotonic() - started
            self.assertLess(waited, 0.5, f"follow-up waited {waited:.3f}s")
        with UnixControlClient(srv.path, timeout_s=2) as other:
            self.assertEqual(other.request({"op": "ping", "id": "live"}), {"ok": True, "op": "pong", "id": "live"})

        raw = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(raw.close)
        raw.settimeout(2)
        raw.connect(srv.path)
        raw.sendall(framing.HEADER.pack((1 << 20) + 50))
        box: dict[str, object] = {}

        def reader() -> None:
            try:
                box["reply"] = framing.read_frame(raw)
            except Exception as exc:
                box["reply"] = exc

        reader_thread = threading.Thread(target=reader)
        reader_thread.start()
        for _ in range(4):
            try:
                raw.sendall(b"{")
            except OSError as exc:
                box["send"] = exc
                break
            time.sleep(0.05)
        reader_thread.join(1)
        self.assertFalse(reader_thread.is_alive())
        reply = box.get("reply")
        self.assertIsInstance(reply, dict, reply)
        assert isinstance(reply, dict)
        self.assertEqual(reply["error"]["code"], "bad_frame")
        self.assertEqual(reply["error"]["detail"], "frame_too_large")

    def test_a_deep_frame_with_a_leading_id_echoes_it(self) -> None:
        """The id is a shallow field at the front, so a too-deep body can still name it.

        A too-deep body that does not start with an id stays id-less. That case is
        test_deeply_nested_frame_is_a_frame_error_and_the_server_lives.
        """
        deep = b"[" * 200_000 + b"]" * 200_000
        body = b'{"id":"s9","op":' + deep + b"}"
        srv = self.server()
        raw = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        with raw:
            raw.settimeout(10)
            raw.connect(srv.path)
            raw.sendall(framing.HEADER.pack(len(body)) + body)
            reply = framing.read_frame(raw)
        self.assertEqual(reply["error"]["code"], "bad_frame")
        self.assertEqual(reply["error"]["detail"], "frame_too_deep")
        self.assertEqual(reply["id"], "s9")

    def test_a_handler_reply_with_the_wrong_id_is_not_forwarded(self) -> None:
        """A handler body that names another id is not this request's reply.

        The client receives an error for its own id, and the same connection
        still serves the next request. The foreign id is not on the wire.
        """
        seen: list[object] = []

        def handler(message, conn):
            seen.append(message.get("id"))
            if message.get("id") == "original-id":
                return {"ok": True, "op": "stop", "state": "stopped", "id": "mismatched-id"}
            return {"ok": True, "op": "pong"}

        srv = self.server(handler)
        with UnixControlClient(srv.path, timeout_s=2) as client:
            reply = client.request({"op": "stop", "id": "original-id"})
            self.assertEqual(reply["ok"], False)
            self.assertEqual(reply["error"]["code"], "internal_error")
            self.assertFalse(reply["error"]["retryable"])
            self.assertEqual(reply["id"], "original-id")
            self.assertNotIn("state", reply)
            self.assertNotIn("mismatched-id", str(reply))
            replay = client.request({"op": "stop", "id": "original-id"})
            self.assertEqual(replay["error"]["code"], REPLAYED_ID)
            self.assertEqual(replay["id"], "original-id")
            nxt = client.request({"op": "ping", "id": "next"})
        self.assertEqual(nxt, {"ok": True, "op": "pong", "id": "next"})
        self.assertEqual(seen, ["original-id", "next"])

    def test_bad_frame_omits_an_id_that_cannot_be_written_back(self) -> None:
        """Infinity and a lone surrogate are not echoed. A plain id still is.

        The worker must answer bad_frame and stay alive for the next connection.
        """
        crashes: list[str] = []
        previous = threading.excepthook

        def record(args: threading.ExceptHookArgs) -> None:
            crashes.append(args.exc_type.__name__)

        threading.excepthook = record
        self.addCleanup(lambda: setattr(threading, "excepthook", previous))
        srv = self.server()
        for body in (b'{"id": 1e9999} trailing', b'{"id": "\\ud800"} trailing'):
            raw = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            with raw:
                raw.settimeout(2)
                raw.connect(srv.path)
                raw.sendall(framing.HEADER.pack(len(body)) + body)
                reply = framing.read_frame(raw)
            self.assertIsInstance(reply, dict, (body, reply, crashes))
            self.assertEqual(reply["error"]["code"], "bad_frame")
            self.assertNotIn("id", reply)
        raw = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        with raw:
            raw.settimeout(2)
            raw.connect(srv.path)
            body = b'{"id": "s9"} trailing'
            raw.sendall(framing.HEADER.pack(len(body)) + body)
            reply = framing.read_frame(raw)
        self.assertEqual(reply["error"]["code"], "bad_frame")
        self.assertEqual(reply["id"], "s9")
        with UnixControlClient(srv.path, timeout_s=2) as client:
            self.assertEqual(client.request({"op": "ping"}), {"ok": True, "op": "pong"})
        self.assertEqual(crashes, [])

    def test_an_unencodable_reply_id_is_dropped_and_the_worker_lives(self) -> None:
        """A NaN id on the request or the handler reply is omitted, not fatal."""

        def handler(message, conn):
            if "nan" in message:
                return {"ok": True, "op": "pong", "id": float("nan")}
            return {"ok": True, "op": "pong"}

        crashes: list[str] = []
        previous = threading.excepthook

        def record(args: threading.ExceptHookArgs) -> None:
            crashes.append(args.exc_type.__name__)

        threading.excepthook = record
        self.addCleanup(lambda: setattr(threading, "excepthook", previous))
        srv = self.server(handler)
        raw = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        with raw:
            raw.settimeout(2)
            raw.connect(srv.path)
            body = b'{"op":"ping","id":NaN}'
            raw.sendall(framing.HEADER.pack(len(body)) + body)
            reply = framing.read_frame(raw)
        self.assertEqual(reply["error"]["code"], "missing_id")
        self.assertNotIn("id", reply)
        with UnixControlClient(srv.path, timeout_s=2) as client:
            pong = client.request({"op": "ping", "nan": True})
            self.assertEqual(pong["op"], "pong")
            self.assertNotIn("id", pong)
            self.assertEqual(client.request({"op": "ping", "id": "s9"}), {"ok": True, "op": "pong", "id": "s9"})
        self.assertEqual(crashes, [])

    def test_an_oversize_scan_does_not_restart_its_wait_on_each_byte(self) -> None:
        """One scan budget covers every byte. A trickle must not hold the worker."""
        left, right = socket.socketpair()
        self.addCleanup(left.close)
        self.addCleanup(right.close)
        right.sendall(framing.HEADER.pack((1 << 20) + 50))

        def trickle() -> None:
            for _ in range(8):
                try:
                    right.sendall(b"{")
                except OSError:
                    return
                time.sleep(0.15)

        worker = threading.Thread(target=trickle)
        worker.start()
        self.addCleanup(worker.join)
        started = time.monotonic()
        with self.assertRaises(FrameError) as caught:
            framing.read_frame(left, max_bytes=1 << 20, deadline=time.monotonic() + 30)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.8, f"scan ran for {elapsed:.3f}s")
        self.assertEqual(str(caught.exception), "frame_too_large")

    def test_a_handler_that_ignores_cancelled_cannot_write_after_close(self) -> None:
        """Close does not deliver a result from a handler that never watches cancelled.

        The handler blocks on its own event. Close still returns on the shared
        deadline, and the result that arrives afterwards is dropped rather than
        written as a completed stop.
        """
        entered = threading.Event()
        release = threading.Event()
        returned = threading.Event()

        def handler(message, conn):
            entered.set()
            release.wait(timeout=3)
            returned.set()
            return {"ok": True, "op": "stop", "state": "stopped", "id": message.get("id")}

        srv = self.server(handler, close_timeout_s=0.3, idle_timeout_s=30.0)
        caught: list[object] = []

        def call() -> None:
            try:
                with UnixControlClient(srv.path, timeout_s=3) as client:
                    caught.append(client.request({"op": "stop", "id": "s1"}))
            except Exception as exc:
                caught.append(exc)

        worker = threading.Thread(target=call)
        worker.start()
        self.assertTrue(entered.wait(timeout=2))
        closer = threading.Thread(target=srv.close)
        closer.start()
        closer.join(timeout=1.0)
        release.set()
        self.assertFalse(closer.is_alive(), "close waited on a handler that ignores cancelled")
        closer.join(timeout=2)
        self.assertTrue(returned.wait(timeout=2))
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and srv.late_dropped < 1:
            time.sleep(0.01)
        self.assertGreaterEqual(srv.late_dropped, 1)
        self.assertEqual(len(caught), 1)
        item = caught[0]
        if isinstance(item, dict):
            self.assertNotEqual(item.get("state"), "stopped")


class ParseableIdTests(unittest.TestCase):
    def test_a_leading_id_is_readable_and_unparseable_bytes_are_not(self) -> None:
        """An id the server can read is echoed. Bytes that are not a frame have no id.

        A too-deep body still yields an id when that id is a shallow value at the
        start of the object. A body with no such value stays unknown, and the
        reply omits id.
        """
        from recursion_core.control_transport.framing import parseable_request_id

        self.assertEqual(parseable_request_id(b'{"id":"s9","op":"stop"}'), (True, "s9"))
        self.assertEqual(parseable_request_id(b'{"id":7}'), (True, 7))
        self.assertEqual(parseable_request_id(b'{"id":null}'), (True, None))
        self.assertEqual(parseable_request_id(b"nope!"), (False, None))
        self.assertEqual(parseable_request_id(b"[]"), (False, None))
        self.assertEqual(parseable_request_id(b'{"op":"ping"}'), (False, None))
        deep = b"[" * 200_000 + b"]" * 200_000
        self.assertEqual(parseable_request_id(b'{"id":"s9","op":' + deep + b"}"), (True, "s9"))
        self.assertEqual(parseable_request_id(b'{"op":' + deep + b"}"), (False, None))
        # A non-finite number and a lone surrogate can be read, but they cannot be
        # written back. They are not ids we echo.
        self.assertEqual(parseable_request_id(b'{"id": 1e9999} trailing'), (False, None))
        self.assertEqual(parseable_request_id(b'{"id": "\\ud800"} trailing'), (False, None))
        self.assertEqual(parseable_request_id(b'{"id": "s9"} trailing'), (True, "s9"))


if __name__ == "__main__":
    unittest.main()
