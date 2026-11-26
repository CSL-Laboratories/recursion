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
# How much longer than its bound a frame may take in these tests (a loaded machine, the stand-in daemon).
MARGIN_S = 2.0
# The key's own sessions at once: how many, and how many frames each sends.
SESSIONS, FRAMES_EACH = 12, 100


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


def serve_within(test: unittest.TestCase, relay: Relay, frames: list[dict], *, within: float = support.WATCHDOG_S,
                 raw: bytes = b"") -> tuple[list[dict], float]:
    """One SSH session: ``frames`` (then ``raw`` bytes) on stdin, served by ``relay.serve`` in a thread,
    failed (not hung) past ``within`` seconds. Returns the replies in order and the seconds it took."""
    stdin = io.BytesIO(b"".join(encode_frame(frame) for frame in frames) + raw)
    stdout, done, out = io.BytesIO(), threading.Event(), []
    began = time.monotonic()
    threading.Thread(target=lambda: (out.append(relay.serve(stdin, stdout)), done.set()), daemon=True).start()
    if not done.wait(within):
        test.fail(f"the session did not come back within {within:g} seconds: a frame waited past its bound")
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
        self.assertIn("each time it asked", error["message"])
        self.assertIn("is still sent to the daemon at once and is never counted here", error["message"])
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

        def counted(fd, path, wait_s, **ceiling):
            waits.append(wait_s)
            return real(fd, path, wait_s, **ceiling)

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

    def test_a_bound_no_person_means_is_refused_at_the_start_with_the_relays_words(self) -> None:
        daemon = support.SocketDaemon(self)
        most = relay_module.RELAY_LOCK_WAIT_MOST_S
        for bad, words in ((most * 2, f"to {most:g}"), (1e9, f"to {most:g}"), (1e300, f"to {most:g}"),
                           (2 ** 1100, "characters"), (-0.0, "negative zero")):
            with self.subTest(lock_wait_s=bad), self.assertRaises(ValueError) as caught:
                Relay(environ=daemon.environ, lock_wait_s=bad)
            self.assertIn("lock_wait_s (--lock-wait-s)", str(caught.exception))
            self.assertIn(words, str(caught.exception))
            self.assertIn("no person means", str(caught.exception))
        self.assertEqual(Relay(environ=daemon.environ, lock_wait_s=most).lock_wait_s, most)
        for bad in (0, 0.0, -0.0, -1, math.inf, math.nan, most * 2, 2 ** 1100, True, None, "0.005"):
            with self.subTest(lock_try_s=bad), self.assertRaises(ValueError) as caught:
                Relay(environ=daemon.environ, lock_try_s=bad)
            self.assertIn("lock_try_s (--lock-try-s)", str(caught.exception))
            self.assertIn("pause between two tries", str(caught.exception))
        self.assertEqual(Relay(environ=daemon.environ).lock_try_s, relay_module.RELAY_LOCK_TRY_S)

    def test_the_forced_command_refuses_them_too(self) -> None:
        daemon = support.SocketDaemon(self)
        most = f"{relay_module.RELAY_LOCK_WAIT_MOST_S:g}"
        for option, text, words in (("--lock-wait-s", "7200", f"to {most}"), ("--lock-wait-s", "1e300", f"to {most}"),
                                    ("--lock-wait-s", "-0", "negative zero"), ("--lock-try-s", "0", "above 0"),
                                    ("--lock-try-s", "inf", "above 0"), ("--lock-try-s", "soon", "--lock-try-s is a")):
            with self.subTest(option=option, text=text):
                err = io.StringIO()
                rc = main(["relay", option, text], environ=daemon.environ, stdin=io.BytesIO(),
                          stdout=io.BytesIO(), stderr=err)
                self.assertEqual(rc, 2)
                self.assertIn(words, err.getvalue())
        out = io.BytesIO()
        rc = main(["relay", "--key-label", "laptop", "--lock-try-s", "0.001"], environ=daemon.environ,
                  stdin=io.BytesIO(encode_frame(control("run.status", 1))), stdout=out, stderr=io.StringIO())
        self.assertEqual(rc, 0)
        self.assertEqual(read_replies(out.getvalue())[0]["result"]["method"], "run.status")


@unittest.skipUnless(POSIX, "the relay needs the AF_UNIX control socket (POSIX)")
class TheKeysOwnSessionsTests(unittest.TestCase):
    def test_sessions_of_one_key_at_once_neither_refuse_nor_hold_each_other(self) -> None:
        # No other program holds anything: only the key's own sessions, each its own Relay with its own open
        # files, taking the key's window at once, with the relay's first values.
        daemon = support.SocketDaemon(self)
        relays = [Relay(environ=daemon.environ, key_label="laptop", max_frames=10 ** 7) for _ in range(SESSIONS)]
        refused, slowest, guard, start = [], [0.0], threading.Lock(), threading.Barrier(SESSIONS)

        def session(relay: Relay) -> None:
            start.wait()
            for n in range(FRAMES_EACH):
                began = time.monotonic()
                reply = relay.handle(control("run.status", n))
                took = time.monotonic() - began
                with guard:
                    slowest[0] = max(slowest[0], took)
                    if "error" in reply:
                        refused.append(reply["error"].get("detail"))

        threads = [threading.Thread(target=session, args=(relay,), daemon=True) for relay in relays]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(support.WATCHDOG_S)
        self.assertFalse(any(thread.is_alive() for thread in threads), "a session did not come back")
        self.assertEqual(refused, [])  # no frame answered window_busy (or anything else)
        self.assertLess(slowest[0], relay_module.RELAY_LOCK_WAIT_S / 2,
                        "a frame waited near the bound for a lock only the key's own sessions held")


@unittest.skipUnless(POSIX, "the relay needs the AF_UNIX control socket (POSIX)")
class EveryRoadOfARefusalIsBoundedTests(unittest.TestCase):
    """An ordinary frame refused for another reason, with the RECORD held by another open file: answered
    within the bound plus a margin, and the Stop behind it taken by the daemon."""

    def relay_with_the_record_held(self, **caps) -> tuple[support.SocketDaemon, Relay]:
        daemon = support.SocketDaemon(self)
        relay = Relay(environ=daemon.environ, key_label="laptop", rights=["read", "run"], lock_wait_s=SHORT_S,
                      **caps)
        held(self, os.path.join(relay.state_folder, relay_store.RECORD_FILE))
        return daemon, relay

    def a_stop_follows(self, daemon, relay, frames, detail_starts: str) -> None:
        replies, took = serve_within(self, relay, frames + [control("run.stop", 9, {"run_id": "r-1"})],
                                     within=SHORT_S + MARGIN_S)
        error = replies[0]["error"]
        self.assertEqual((error["code"], error["delivered"]), ("rate_limited", False))
        self.assertTrue(error["detail"].startswith(detail_starts), error["detail"])
        self.assertEqual(replies[1]["result"]["method"], "run.stop")
        self.assertEqual([m["method"] for m in daemon.seen], ["run.stop"])
        self.assertLess(took, SHORT_S + MARGIN_S)
        self.assertEqual(relay_store.read_refusals(relay.state_folder), [])  # left out; its line still went

    def test_a_full_window(self) -> None:
        daemon, relay = self.relay_with_the_record_held(max_frames=0)
        self.a_stop_follows(daemon, relay, [control("run.status", 1)], "max_frames:0:")

    def test_an_unreadable_window(self) -> None:
        daemon, relay = self.relay_with_the_record_held()
        os.mkdir(os.path.join(relay.state_folder, "window-label-laptop.json"))  # a folder at the file's name
        self.a_stop_follows(daemon, relay, [control("run.status", 1)], "window_unreadable:")

    def test_a_frame_too_large_to_read(self) -> None:
        daemon, relay = self.relay_with_the_record_held(max_frame_bytes=4096)
        big = encode_frame(control("run.status", 1, {"pad": "x" * 8192}))
        replies, took = serve_within(self, relay, [], raw=big + encode_frame(control("run.stop", 2, {"run_id": "r"})),
                                     within=SHORT_S + MARGIN_S)
        self.assertEqual([(r["error"]["code"], r["error"]["detail"]) for r in replies],
                         [("bad_frame", "frame_too_large")])  # answered once; the session then ends
        self.assertLess(took, SHORT_S + MARGIN_S)
        self.assertEqual(daemon.seen, [])
        self.assertEqual(relay_store.read_refusals(relay.state_folder), [])


if __name__ == "__main__":
    unittest.main()
