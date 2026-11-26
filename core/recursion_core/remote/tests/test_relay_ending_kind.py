"""A request whose kind is to END something is never refused because of other traffic (lane REMOTE).

The relay counts ordinary frames against a window (``--max-frames`` in ``--window-s``, per key or per
session). A Stop, an interrupt, a cancel and a denied approval are of the ending kind
(``rights.ENDING_METHODS``, ``rights.ends_something``): they take no slot and read no window, so a key
whose own watching filled the window can still stop what it watches, and an unreadable window refuses
them neither. The key's rights are checked first, unchanged. When an ordinary frame IS refused, the
answer says what was limited and when it frees (``retry_after_s`` and ``message``).

Every test runs against the stand-in daemon in ``support`` in a scratch folder, with a test clock.
"""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path

from recursion_core.remote import rights
from recursion_core.remote.relay import Relay
from recursion_core.remote.tests import support

try:  # names this change adds; imported so that, without them, only the tests that use them fail
    from recursion_core.remote.rights import ENDING_METHODS, ends_something
except ImportError:  # pragma: no cover - the tree before this change
    ENDING_METHODS = ends_something = None

POSIX = os.name != "nt"
REMOTE_DIR = Path(__file__).resolve().parents[1]
PUBLISHED_TABLE = Path(__file__).resolve().parents[4] / "schemas" / "control" / "v1" / "methods.table.v1.json"

# The ending kind by the published method table (table version 1.0-draft.10), each with params of its row.
ENDING = {
    "run.stop": {"run_id": "r-1"},
    "session.stop": {"command_id": "cmd-stop-1", "session_id": "ses-1", "scope": "self"},
    "session.interrupt": {"command_id": "cmd-int-1", "session_id": "ses-1", "expected_turn_id": "turn-1"},
    "queue.cancel": {"command_id": "cmd-can-1", "item_id": "item-1", "expected_revision": 3},
}
ENDING_SUFFIXES = (".stop", ".interrupt", ".cancel")


def control(method, n=None, params=None) -> dict:
    message = {"op": "control", "method": method, "params": {} if params is None else params}
    if n is not None:
        message["id"] = n
    return message


def answer(word, n=None) -> dict:
    params = {"command_id": "cmd-ans-1", "approval_id": "apr-1", "digest": "0" * 64, "answer": word,
              "expected_revision": 1, "surface": "cli"}
    return control("approval.answer", n, params)


class CountingClock(support.FakeClock):
    """A test clock that counts how often it is read."""

    def __init__(self, now: float = 1000.0) -> None:
        super().__init__(now)
        self.reads = 0

    def __call__(self) -> float:
        self.reads += 1
        return self.now


def fill(test, relay, count, method="run.status") -> None:
    for n in range(count):
        test.assertIn("result", relay.handle(control(method, 100 + n)))


class EndingKindTableTests(unittest.TestCase):
    """The kind is one named set beside the relay's table, pinned against the published table. Pure."""

    def test_the_kind_names_exactly_the_tables_stop_interrupt_and_cancel_methods(self) -> None:
        self.assertIsNotNone(ENDING_METHODS, "rights.ENDING_METHODS is missing")
        self.assertEqual(set(ENDING_METHODS), set(ENDING))

    def test_each_member_is_a_mutating_row_of_the_published_table(self) -> None:
        with open(PUBLISHED_TABLE, encoding="utf-8") as source:
            rows = {row["name"]: row for row in json.load(source)["methods"]}
        for method in ENDING_METHODS:
            with self.subTest(method=method):
                self.assertIn(method, rows)
                self.assertIs(rows[method]["mutating"], True)
                self.assertIn(method, rights.METHOD_RIGHTS)

    def test_a_table_method_that_ends_stop_interrupt_or_cancel_is_of_the_kind(self) -> None:
        with open(PUBLISHED_TABLE, encoding="utf-8") as source:
            names = [row["name"] for row in json.load(source)["methods"]]
        ending = [name for name in names if name.endswith(ENDING_SUFFIXES)]
        self.assertTrue(ending)
        for name in ending:
            with self.subTest(method=name):
                self.assertIn(name, ENDING_METHODS)

    def test_which_frames_are_of_the_kind(self) -> None:
        for method, params in ENDING.items():
            with self.subTest(method=method):
                self.assertIs(ends_something(control(method, 1, params)), True)
        self.assertIs(ends_something(answer("deny")), True)
        for frame in (control("run.status"), control("run.start"), control("session.steer"), answer("approve"),
                      answer("Deny"), answer(None), answer(["deny"]), control("approval.answer"),
                      control("approval.answer", params=["deny"]), {"op": "ping"}, {"op": "run.stop"},
                      {"op": "control", "method": 7}, {"method": "run.stop", "params": {}},
                      {"op": "control", "method": type("Name", (str,), {})("run.stop"), "params": {}}):
            with self.subTest(frame=repr(frame)):
                self.assertIs(ends_something(frame), False)

    def test_the_readme_names_every_member_of_the_kind(self) -> None:
        text = (REMOTE_DIR / "README.md").read_text(encoding="utf-8")
        start = text.find("never counted")
        self.assertNotEqual(start, -1, "the README says which requests are never counted")
        sentence = text[start - 400:start + 400]
        for method in sorted(ENDING_METHODS):
            with self.subTest(method=method):
                self.assertIn(f"`{method}`", sentence)
        self.assertIn("`deny`", sentence)


@unittest.skipUnless(POSIX, "the relay needs the AF_UNIX control socket (POSIX)")
class StopThroughAFullWindowTests(unittest.TestCase):
    def test_a_stop_after_the_keys_own_reads_filled_the_window_is_forwarded(self) -> None:
        daemon = support.SocketDaemon(self)
        events: list[dict] = []
        relay = Relay(environ=daemon.environ, device_id="dev_phone", max_frames=3, window_s=60.0,
                      clock=support.FakeClock(), record=events.append)
        fill(self, relay, 3)
        self.assertEqual(relay.handle(control("run.status", 7))["error"]["code"], "rate_limited")
        stop = relay.handle(control("run.stop", 8, ENDING["run.stop"]))
        self.assertNotIn("error", stop)
        self.assertEqual((stop["id"], stop["result"]["method"], stop["result"]["params"]),
                         (8, "run.stop", ENDING["run.stop"]))
        self.assertEqual(daemon.seen[-1]["method"], "run.stop")
        self.assertEqual([event["refused"] for event in events], ["rate_limited"])  # the Stop is not refused

    def test_two_sessions_of_one_key_share_the_window_and_the_stop_still_goes(self) -> None:
        daemon = support.SocketDaemon(self)
        clock = support.FakeClock()
        watching = Relay(environ=daemon.environ, key_label="phone", max_frames=4, window_s=60.0, clock=clock)
        stopping = Relay(environ=daemon.environ, key_label="phone", max_frames=4, window_s=60.0, clock=clock)
        self.assertEqual(watching.cap_scope, "key")
        fill(self, watching, 4, "events.read")
        self.assertEqual(stopping.handle(control("run.status", 9))["error"]["code"], "rate_limited")
        self.assertEqual(stopping.handle(control("run.stop", 10, ENDING["run.stop"]))["result"]["method"], "run.stop")

    def test_with_a_window_per_session_the_stop_still_goes(self) -> None:
        daemon = support.SocketDaemon(self)
        relay = Relay(environ=daemon.environ, key_label="phone", max_frames=2, window_s=60.0,
                      clock=support.FakeClock(), cap_scope="session")
        fill(self, relay, 2)
        self.assertEqual(relay.handle(control("run.status", 9))["error"]["code"], "rate_limited")
        self.assertEqual(relay.handle(control("run.stop", 10, ENDING["run.stop"]))["result"]["method"], "run.stop")

    def test_every_member_of_the_kind_goes_through_a_full_window(self) -> None:
        for scope in ("key", "session"):
            daemon = support.SocketDaemon(self)
            relay = Relay(environ=daemon.environ, key_label="laptop", rights=["read", "run", "approve"],
                          max_frames=2, window_s=60.0, clock=support.FakeClock(), cap_scope=scope)
            fill(self, relay, 2)
            self.assertEqual(relay.handle(control("run.status", 9))["error"]["code"], "rate_limited")
            for n, (method, params) in enumerate(ENDING.items()):
                with self.subTest(scope=scope, method=method):
                    reply = relay.handle(control(method, n, params))
                    self.assertNotIn("error", reply)
                    self.assertEqual((reply["result"]["method"], reply["result"]["params"]), (method, params))
            with self.subTest(scope=scope, method="approval.answer deny"):
                self.assertEqual(relay.handle(answer("deny", 20))["result"]["params"]["answer"], "deny")
            # An ordinary frame of the same methods' neighbours is still held, and so is an approval.
            for frame in (control("run.start", 30), control("session.steer", 31), answer("approve", 32),
                          answer("DENY", 33)):
                with self.subTest(scope=scope, frame=frame["method"]):
                    self.assertEqual(relay.handle(frame)["error"]["code"], "rate_limited")

    def test_a_window_that_cannot_be_read_never_refuses_a_stop(self) -> None:
        daemon = support.SocketDaemon(self)
        relay = Relay(environ=daemon.environ, key_label="laptop", rights=["read", "run"])
        os.mkdir(os.path.join(relay.state_folder, "window-label-laptop.json"))  # a folder where the file goes
        for n, (method, params) in enumerate(ENDING.items()):
            with self.subTest(method=method):
                self.assertEqual(relay.handle(control(method, n, params))["result"]["method"], method)
        refused = relay.handle(control("run.status", 9))["error"]
        self.assertEqual((refused["code"], refused["delivered"]), ("rate_limited", False))
        self.assertTrue(refused["detail"].startswith("window_unreadable:"))
        self.assertIsNone(refused["retry_after_s"])
        self.assertIn("could not read", refused["message"])
        self.assertIn("A Stop", refused["message"])
        self.assertIn("still goes", refused["message"])

    def test_a_key_without_the_right_to_stop_is_still_refused_and_no_window_is_read(self) -> None:
        daemon = support.SocketDaemon(self)
        clock = CountingClock()
        relay = Relay(environ=daemon.environ, key_label="viewer", rights=["read"], max_frames=1, clock=clock)
        for n, (method, params) in enumerate(ENDING.items()):
            with self.subTest(method=method):
                reply = relay.handle(control(method, n, params))
                self.assertEqual((reply["error"]["code"], reply["error"]["needs"]), ("scope_refused", "run"))
        self.assertEqual(relay.handle(answer("deny", 9))["error"]["code"], "scope_refused")
        self.assertEqual(clock.reads, 0)
        self.assertFalse(os.path.exists(os.path.join(relay.state_folder, "window-label-viewer.json")))
        self.assertEqual(daemon.seen, [])

    def test_a_stop_reads_no_window_and_takes_no_slot(self) -> None:
        daemon = support.SocketDaemon(self)
        clock = CountingClock()
        relay = Relay(environ=daemon.environ, key_label="phone", max_frames=1, window_s=60.0, clock=clock)
        for n in range(3):
            self.assertEqual(relay.handle(control("run.stop", n, ENDING["run.stop"]))["result"]["method"], "run.stop")
        self.assertEqual(clock.reads, 0)
        self.assertFalse(os.path.exists(os.path.join(relay.state_folder, "window-label-phone.json")))
        self.assertIn("result", relay.handle(control("run.status", 5)))  # the one slot is still there
        self.assertEqual(relay.handle(control("run.status", 6))["error"]["code"], "rate_limited")

    def test_a_stop_in_a_full_window_moves_nothing_the_next_ordinary_frame_waits_for(self) -> None:
        for scope in ("key", "session"):
            for with_stop in (False, True):
                with self.subTest(scope=scope, with_stop=with_stop):
                    daemon = support.SocketDaemon(self)
                    clock = support.FakeClock(1000.0)
                    relay = Relay(environ=daemon.environ, key_label=f"k{int(with_stop)}{scope}", max_frames=2,
                                  window_s=10.0, clock=clock, cap_scope=scope)
                    self.assertIn("result", relay.handle(control("run.status", 1)))
                    clock.now = 1001.0
                    self.assertIn("result", relay.handle(control("run.status", 2)))
                    if with_stop:
                        clock.now = 1002.0
                        self.assertIn("result", relay.handle(control("run.stop", 3, ENDING["run.stop"])))
                    clock.now = 1005.0
                    early = relay.handle(control("run.status", 4))["error"]
                    self.assertEqual((early["code"], early["retry_after_s"]), ("rate_limited", 5.0))
                    clock.now = 1009.999
                    self.assertEqual(relay.handle(control("run.status", 5))["error"]["code"], "rate_limited")
                    clock.now = 1010.0
                    self.assertIn("result", relay.handle(control("run.status", 6)))
                    clock.now = 1010.5
                    late = relay.handle(control("run.status", 7))["error"]
                    self.assertEqual((late["code"], late["retry_after_s"]), ("rate_limited", 0.5))
                    clock.now = 1011.0
                    self.assertIn("result", relay.handle(control("run.status", 8)))


@unittest.skipUnless(POSIX, "the relay needs the AF_UNIX control socket (POSIX)")
class LimitedWordsTests(unittest.TestCase):
    def test_the_answer_says_the_cap_the_window_and_when_it_frees_at_two_times(self) -> None:
        for scope in ("key", "session"):
            with self.subTest(scope=scope):
                daemon = support.SocketDaemon(self)
                clock = support.FakeClock(1000.0)
                relay = Relay(environ=daemon.environ, key_label="phone", max_frames=3, window_s=60.0, clock=clock,
                              cap_scope=scope)
                for n, now in enumerate((1000.0, 1010.0, 1020.0)):
                    clock.now = now
                    self.assertIn("result", relay.handle(control("run.status", n)))
                clock.now = 1030.0
                first = relay.handle(control("run.status", 3))
                error = first["error"]
                self.assertEqual((error["code"], error["delivered"], first["id"]), ("rate_limited", False, 3))
                self.assertEqual((error["cap"], error["max_frames"], error["window_s"], error["cap_scope"]),
                                 ("max_frames", 3, 60.0, scope))
                self.assertEqual(error["retry_after_s"], 30.0)  # the oldest kept time, 1000, leaves at 1060
                self.assertEqual(error["detail"], "max_frames:3:window_s:60")
                words = error["message"]
                self.assertIn("3 requests in any 60 seconds", words)
                self.assertIn("in 30 seconds", words)
                self.assertIn("--max-frames", words)
                self.assertIn("never held by this limit", words)
                self.assertIn("this key" if scope == "key" else "this session", words)
                self.assertEqual(words, words.encode("ascii").decode("ascii"))
                clock.now = 1045.0
                second = relay.handle(control("run.status", 4))["error"]
                self.assertEqual(second["retry_after_s"], 15.0)
                self.assertIn("in 15 seconds", second["message"])
                clock.now = 1060.0
                self.assertIn("result", relay.handle(control("run.status", 5)))

    def test_a_part_of_a_second_is_told_rounded_up_never_down(self) -> None:
        daemon = support.SocketDaemon(self)
        clock = support.FakeClock(1000.0)
        relay = Relay(environ=daemon.environ, key_label="phone", max_frames=1, window_s=10.0, clock=clock)
        self.assertIn("result", relay.handle(control("run.status", 1)))
        clock.now = 1002.34
        error = relay.handle(control("run.status", 2))["error"]
        self.assertAlmostEqual(error["retry_after_s"], 7.66, places=6)
        self.assertIn("in 7.7 seconds", error["message"])

    def test_a_cap_of_zero_frees_never_and_says_who_can_raise_it(self) -> None:
        daemon = support.SocketDaemon(self)
        relay = Relay(environ=daemon.environ, key_label="phone", max_frames=0, clock=support.FakeClock())
        error = relay.handle(control("run.status", 1))["error"]
        self.assertEqual((error["code"], error["max_frames"], error["retry_after_s"]), ("rate_limited", 0, None))
        self.assertIn("0 requests", error["message"])
        self.assertIn("--max-frames", error["message"])
        self.assertEqual(relay.handle(control("run.stop", 2, ENDING["run.stop"]))["result"]["method"], "run.stop")

    def test_the_clock_is_read_while_the_keys_window_is_locked(self) -> None:
        import fcntl
        daemon = support.SocketDaemon(self)
        holder: dict = {}
        unlocked: list[float] = []
        fake = support.FakeClock(1000.0)

        def clock() -> float:
            with open(holder["path"], "a+b") as other:  # an open of its own: held only if the window's lock is
                try:
                    fcntl.flock(other.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return fake()
                unlocked.append(fake())
                return fake()

        relay = Relay(environ=daemon.environ, key_label="phone", max_frames=1, window_s=10.0, clock=clock)
        holder["path"] = os.path.join(relay.state_folder, "window-label-phone.json")
        self.assertIn("result", relay.handle(control("run.status", 1)))
        fake.now = 1004.0
        self.assertEqual(relay.handle(control("run.status", 2))["error"]["retry_after_s"], 6.0)
        self.assertEqual(unlocked, [])

    def test_the_record_of_a_refusal_keeps_its_shape(self) -> None:
        daemon = support.SocketDaemon(self)
        events: list[dict] = []
        relay = Relay(environ=daemon.environ, max_frames=1, window_s=10.0, clock=support.FakeClock(),
                      record=events.append)
        relay.handle(control("run.status", 1))
        relay.handle(control("run.status", 2))
        relay.handle(control("run.stop", 3, ENDING["run.stop"]))
        self.assertEqual(events, [{"refused": "rate_limited", "cap": "max_frames", "value": 1, "window_s": 10.0,
                                   "seen": 1}])


if __name__ == "__main__":
    unittest.main()
