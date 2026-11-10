"""Close deadline and request-id correlation (CSL-719).

The close budget is one deadline for every handler, measured on an injected clock so
the suite does not sleep. Blocking handlers stand in for a Stop that has not returned.
Replies are delivered only for the request id that is still in flight.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

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

    def test_an_omitted_id_is_cancelled_with_everyone_else_at_the_deadline(self) -> None:
        clock = FakeClock()
        table = RequestTable(clock=clock, close_timeout_s=5.0)
        claim = table.claim_absent()
        table.begin_close()
        clock.advance(5.0)
        table.cancel_overdue()
        self.assertTrue(claim.event.is_set())
        self.assertEqual(table.finish(claim, {"ok": True, "op": "pong"}), IGNORED)


@unittest.skipUnless(LINUX, "Linux is the tested path")
class SocketCorrelationTests(unittest.TestCase):
    """The server uses the table: one close deadline, and ids answered on the socket."""

    def setUp(self) -> None:
        self.base = tempfile.mkdtemp(prefix="ct-", dir="/tmp" if os.path.isdir("/tmp") else None)
        self.addCleanup(self._rm)
        self.sock_dir = os.path.join(self.base, "run")

    def _rm(self) -> None:
        shutil.rmtree(self.base, ignore_errors=True)

    def server(self, handler, **kwargs) -> UnixControlServer:
        srv = UnixControlServer(self.sock_dir, handler, **kwargs)
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


if __name__ == "__main__":
    unittest.main()
