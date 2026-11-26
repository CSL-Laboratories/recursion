"""Nothing in a relay session waits without end on a file (lane REMOTE).

An ordinary frame waits for the lock of its key's window and of the record of refusals at most
``lock_wait_s`` seconds in all. A lock another program still holds then refuses that frame with words
(``rate_limited``, ``window_busy``), or leaves its refusal out of the record, and the session reads its next
frame: a Stop, an interrupt or any request behind it in the same session is reached within that bound and
sent at once. The bound is a setting (``--lock-wait-s``) with a first value; a value with no end is refused
at the start.

Every test runs against the stand-in daemon in ``support`` in a scratch folder. A lock "another program
holds" is taken on a second open of the same file in the test process (another open file description).
"""

from __future__ import annotations

import fcntl
import io
import math
import os
import threading
import time
import unittest
from unittest import mock

from recursion_core.control_transport.framing import encode_frame
from recursion_core.remote import relay as relay_module
from recursion_core.remote import relay_store
from recursion_core.remote.relay import Relay, main
from recursion_core.remote.tests import support

POSIX = os.name != "nt"
INTERRUPT = {"command_id": "cmd-int-1", "session_id": "ses-1", "expected_turn_id": "turn-1"}
# A bound of the tests' own, short so that a test waits little; the first value is the relay's.
SHORT_S = 0.3


def control(method, n, params=None) -> dict:
    return {"op": "control", "method": method, "params": {} if params is None else params, "id": n}


def read_replies(data: bytes) -> list[dict]:
    from recursion_core.remote.codec import read_message
    stream, replies = io.BytesIO(data), []
    while (reply := read_message(stream, max_bytes=relay_module.RELAY_MAX_FRAME_BYTES)) is not None:
        replies.append(reply)
    return replies


def held(test: unittest.TestCase, path: str) -> None:
    """Hold ``path``'s lock as another program would, until the test ends."""
    holder = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    test.addCleanup(os.close, holder)
    fcntl.flock(holder, fcntl.LOCK_EX)


def serve_within(test: unittest.TestCase, relay: Relay, frames: list[dict]) -> tuple[list[dict], float]:
    """One SSH session: ``frames`` on stdin, served by ``relay.serve`` in a thread, failed (not hung) past
    the watchdog. Returns the replies in order and the seconds the session took."""
    stdin = io.BytesIO(b"".join(encode_frame(frame) for frame in frames))
    stdout, done, out = io.BytesIO(), threading.Event(), []
    began = time.monotonic()
    threading.Thread(target=lambda: (out.append(relay.serve(stdin, stdout)), done.set()), daemon=True).start()
    if not done.wait(support.WATCHDOG_S):
        test.fail("the session did not come back: a frame waited without end")
    return read_replies(stdout.getvalue()), time.monotonic() - began


@unittest.skipUnless(POSIX, "the relay needs the AF_UNIX control socket (POSIX)")
class AStopInTheSameSessionIsReachedTests(unittest.TestCase):
    def test_a_stop_behind_an_ordinary_frame_whose_window_another_program_holds(self) -> None:
        # The relay's first value of the bound: at e56648f0 this session waited without end.
        daemon = support.SocketDaemon(self)
        events: list[dict] = []
        relay = Relay(environ=daemon.environ, key_label="laptop", rights=["read", "run"], record=events.append)
        held(self, os.path.join(relay.state_folder, "window-label-laptop.json"))
        frames = [control("run.status", 1), control("session.interrupt", 2, INTERRUPT),
                  control("run.stop", 3, {"run_id": "r-1"})]
        replies, took = serve_within(self, relay, frames)
        self.assertEqual([reply["id"] for reply in replies], [1, 2, 3])  # in order, one answer each
        error = replies[0]["error"]
        self.assertEqual((error["code"], error["delivered"], error["detail"]),
                         ("rate_limited", False, f"window_busy:lock_wait_s:{relay_module.RELAY_LOCK_WAIT_S:g}"))
        self.assertIn("--lock-wait-s", error["message"])
        self.assertIn("never waits longer on a file", error["message"])
        self.assertIn("still goes", error["message"])
        self.assertIsNone(error["retry_after_s"])
        self.assertEqual([reply["result"]["method"] for reply in replies[1:]], ["session.interrupt", "run.stop"])
        self.assertEqual([m["method"] for m in daemon.seen], ["session.interrupt", "run.stop"])
        self.assertGreaterEqual(took, relay_module.RELAY_LOCK_WAIT_S)  # it did wait, as long as the bound
        self.assertEqual(events, [{"refused": "rate_limited", "cap": "max_frames", "window": "busy",
                                   "lock_wait_s": relay_module.RELAY_LOCK_WAIT_S}])

    def test_a_stop_behind_an_ordinary_refusal_whose_record_another_program_holds(self) -> None:
        daemon = support.SocketDaemon(self)
        events: list[dict] = []
        relay = Relay(environ=daemon.environ, key_label="laptop", rights=["read", "run"], record=events.append,
                      lock_wait_s=SHORT_S)
        held(self, os.path.join(relay.state_folder, relay_store.RECORD_FILE))
        frames = [control("config.set", 1), control("run.stop", 2, {"run_id": "r-1"})]
        replies, _ = serve_within(self, relay, frames)
        self.assertEqual((replies[0]["error"]["code"], replies[0]["error"]["delivered"]), ("scope_refused", False))
        self.assertEqual(replies[1]["result"]["method"], "run.stop")
        self.assertEqual(events, [{"refused": "scope_refused", "method": "config.set",
                                   "needs": replies[0]["error"]["needs"]}])
        self.assertEqual(relay_store.read_refusals(relay.state_folder), [])  # left out; its line still went
        self.assertEqual([m["method"] for m in daemon.seen], ["run.stop"])

    def test_the_window_and_the_record_share_one_bound(self) -> None:
        daemon = support.SocketDaemon(self)
        relay = Relay(environ=daemon.environ, key_label="laptop", lock_wait_s=SHORT_S)
        held(self, os.path.join(relay.state_folder, "window-label-laptop.json"))
        held(self, os.path.join(relay.state_folder, relay_store.RECORD_FILE))
        waits: list[float] = []
        real = relay_store._flock_within

        def counted(fd, path, wait_s):
            waits.append(wait_s)
            return real(fd, path, wait_s)

        with mock.patch.object(relay_store, "_flock_within", counted):
            replies, _ = serve_within(self, relay, [control("run.status", 1)])
        self.assertTrue(replies[0]["error"]["detail"].startswith("window_busy"))
        self.assertEqual(len(waits), 2)  # the window, then the record of its refusal
        self.assertLessEqual(waits[0], SHORT_S)
        self.assertLessEqual(sum(waits), SHORT_S + 1e-6)  # the record had only what the window left

    def test_a_lock_freed_within_the_bound_lets_the_frame_through(self) -> None:
        daemon = support.SocketDaemon(self)
        relay = Relay(environ=daemon.environ, key_label="laptop", lock_wait_s=support.WATCHDOG_S)
        holder = os.open(os.path.join(relay.state_folder, "window-label-laptop.json"), os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(holder, fcntl.LOCK_EX)
        threading.Timer(SHORT_S, os.close, (holder,)).start()  # the other program lets go
        replies, _ = serve_within(self, relay, [control("run.status", 1)])
        self.assertEqual(replies[0]["result"]["method"], "run.status")

    def test_a_bound_of_0_asks_once(self) -> None:
        daemon = support.SocketDaemon(self)
        relay = Relay(environ=daemon.environ, key_label="laptop", lock_wait_s=0)
        held(self, os.path.join(relay.state_folder, "window-label-laptop.json"))
        replies, _ = serve_within(self, relay, [control("run.status", 1), control("run.stop", 2, {"run_id": "r"})])
        self.assertEqual(replies[0]["error"]["detail"], "window_busy:lock_wait_s:0")
        self.assertIn("0 seconds", replies[0]["error"]["message"])
        self.assertEqual(replies[1]["result"]["method"], "run.stop")


@unittest.skipUnless(POSIX, "the relay needs the AF_UNIX control socket (POSIX)")
class TheBoundIsASettingTests(unittest.TestCase):
    def test_a_bound_with_no_end_or_not_a_number_is_refused_at_the_start(self) -> None:
        daemon = support.SocketDaemon(self)
        for bad in (math.inf, math.nan, -1, -0.5, None, True, "1"):
            with self.subTest(lock_wait_s=bad), self.assertRaises(ValueError) as caught:
                Relay(environ=daemon.environ, lock_wait_s=bad)
            self.assertIn("never waits without end on a file", str(caught.exception))
        self.assertEqual(Relay(environ=daemon.environ, lock_wait_s=0).lock_wait_s, 0.0)
        self.assertEqual(Relay(environ=daemon.environ).lock_wait_s, relay_module.RELAY_LOCK_WAIT_S)

    def test_the_forced_command_sets_it_and_refuses_what_cannot_be_used(self) -> None:
        daemon = support.SocketDaemon(self)
        for text, code, words in (("inf", 2, "never waits without end"), ("none", 2, "--lock-wait-s is a number"),
                                  ("-1", 2, "never waits without end"), ("soon", 2, "--lock-wait-s is a number")):
            with self.subTest(text=text):
                err = io.StringIO()
                rc = main(["relay", "--lock-wait-s", text], environ=daemon.environ, stdin=io.BytesIO(),
                          stdout=io.BytesIO(), stderr=err)
                self.assertEqual(rc, code)
                self.assertIn(words, err.getvalue())
        out = io.BytesIO()
        rc = main(["relay", "--key-label", "laptop", "--lock-wait-s", "0.5"], environ=daemon.environ,
                  stdin=io.BytesIO(encode_frame(control("run.status", 1))), stdout=out, stderr=io.StringIO())
        self.assertEqual(rc, 0)
        self.assertEqual(read_replies(out.getvalue())[0]["result"]["method"], "run.status")


if __name__ == "__main__":
    unittest.main()
