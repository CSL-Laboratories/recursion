"""A request of the ending kind is sent to the daemon at once and answered only by the daemon (lane REMOTE).

``test_relay_ending_kind`` holds that such a request (a Stop, an interrupt, a cancel, a denied approval)
is never counted against the window. Here: once a session has started, nothing on disk holds it back or
answers it in the daemon's place. A window file that cannot be read, a relay folder swapped, removed or
loosened while the relay runs, a FIFO or another program's lock at a file's name, a full disk: the Stop is
still sent, and its answer is the daemon's answer to it. Two at once each ask the daemon; none leaves a
thread, a descriptor or a file behind. Such a request refused for another reason (no right to it, too
large) is still recorded, without waiting for a record another program holds, and when it could not be
recorded the answer says so in one sentence. A timeout the platform cannot wait for is refused at the
start. Ordinary frames meet each of these changes refused with words, in time, never waiting on a FIFO.

Every test runs against the stand-in daemon in ``support`` in a scratch folder, with a test clock.
"""

from __future__ import annotations

import errno
import fcntl
import json
import math
import os
import shutil
import threading
import unittest
from unittest import mock

from recursion_core.remote import relay_store
from recursion_core.remote.relay import Relay
from recursion_core.remote.tests import support

POSIX = os.name != "nt"

ENDING = {
    "run.stop": ({"run_id": "r-1"}, "run"),
    "session.stop": ({"command_id": "cmd-stop-1", "session_id": "ses-1", "scope": "self"}, "run"),
    "session.interrupt": ({"command_id": "cmd-int-1", "session_id": "ses-1", "expected_turn_id": "turn-1"}, "run"),
    "queue.cancel": ({"command_id": "cmd-can-1", "item_id": "item-1", "expected_revision": 3}, "run"),
}
DENY = {"command_id": "cmd-ans-1", "approval_id": "apr-1", "digest": "0" * 64, "answer": "deny",
        "expected_revision": 1, "surface": "cli"}
# How many Stops the no-thread, no-descriptor, no-file test sends: a count of the test's own.
MANY_STOPS = 50


def control(method, n=None, params=None) -> dict:
    message = {"op": "control", "method": method, "params": {} if params is None else params}
    if n is not None:
        message["id"] = n
    return message


def stop(run_id="r-1", n=None) -> dict:
    return control("run.stop", n, {"run_id": run_id})


def within(test: unittest.TestCase, call, what: str):
    """``call()`` in a thread, failed (not hung) when it takes longer than the watchdog."""
    done, out = threading.Event(), []
    threading.Thread(target=lambda: (out.append(call()), done.set()), daemon=True).start()
    if not done.wait(support.WATCHDOG_S):
        test.fail(f"{what} did not come back")
    return out[0]


def open_descriptors() -> int:
    return len(os.listdir("/proc/self/fd")) if os.path.isdir("/proc/self/fd") else -1


@unittest.skipUnless(POSIX, "the relay needs the AF_UNIX control socket (POSIX)")
class RefusedForAnotherReasonTests(unittest.TestCase):
    """A request of the ending kind is never counted, and is still recorded when refused for another reason."""

    def test_a_request_of_the_kind_without_its_right_is_refused_and_recorded(self) -> None:
        daemon = support.SocketDaemon(self)
        events: list[dict] = []
        relay = Relay(environ=daemon.environ, key_label="viewer", rights=["read"], record=events.append)
        frames = [(m, control(m, n, p), r) for n, (m, (p, r)) in enumerate(ENDING.items())]
        frames.append(("approval.answer", control("approval.answer", 9, DENY), "approve"))
        for method, frame, right in frames:
            with self.subTest(method=method):
                error = relay.handle(frame)["error"]
                self.assertEqual((error["code"], error["delivered"], error["needs"]), ("scope_refused", False, right))
                self.assertNotIn("record_notice", error)
        wanted = [{"refused": "scope_refused", "method": method, "needs": right} for method, _, right in frames]
        self.assertEqual(events, wanted)
        record = relay_store.read_refusals(relay.state_folder)
        self.assertEqual([{name: entry[name] for name in ("refused", "method", "needs")} for entry in record], wanted)
        self.assertEqual({entry["key"] for entry in record}, {"label-viewer"})
        self.assertEqual(daemon.seen, [])

    def test_a_stop_over_the_frame_size_is_refused_and_recorded(self) -> None:
        from recursion_core.remote import relay as relay_module
        daemon = support.SocketDaemon(self)
        events: list[dict] = []
        limit = relay_module.RELAY_LEAST_FRAME_BYTES + 40
        relay = Relay(environ=daemon.environ, key_label="phone", max_frame_bytes=limit, record=events.append)
        error = relay.handle(control("run.stop", 1, {"run_id": "r-" + "x" * limit}))["error"]
        self.assertEqual((error["code"], error["delivered"], error["detail"]), ("bad_frame", False, "frame_too_large"))
        wanted = {"refused": "bad_frame", "cap": "max_frame_bytes", "value": limit}
        self.assertEqual(events, [wanted])
        record = relay_store.read_refusals(relay.state_folder)
        self.assertEqual([{name: entry[name] for name in wanted} for entry in record], [wanted])
        self.assertEqual(daemon.seen, [])

    def test_a_refused_stop_never_waits_for_a_record_another_program_holds_and_says_it_is_not_in_it(self) -> None:
        daemon = support.SocketDaemon(self)
        events: list[dict] = []
        relay = Relay(environ=daemon.environ, key_label="viewer", rights=["read"], record=events.append)
        path = os.path.join(relay.state_folder, relay_store.RECORD_FILE)
        holder = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, holder)
        fcntl.flock(holder, fcntl.LOCK_EX)  # another open file description: a lock the relay cannot take
        error = within(self, lambda: relay.handle(stop(n=1)), "a refused Stop with the record held")["error"]
        self.assertEqual((error["code"], error["delivered"]), ("scope_refused", False))
        self.assertIn("not in the relay's record", error["record_notice"])
        self.assertIn("BlockingIOError", error["record_notice"])
        self.assertEqual(events, [{"refused": "scope_refused", "method": "run.stop", "needs": "run"}])
        self.assertEqual(daemon.seen, [])

    def test_a_refused_stop_on_a_full_disk_says_it_is_not_in_the_record(self) -> None:
        daemon = support.SocketDaemon(self)
        relay = Relay(environ=daemon.environ, key_label="viewer", rights=["read"])

        def full(fd, data):
            raise OSError(errno.ENOSPC, "No space left on device")

        with mock.patch.object(relay_store, "_write_at_end", full):
            error = relay.handle(stop(n=1))["error"]
            ordinary = relay.handle(control("config.set", 2))["error"]
        self.assertEqual(error["code"], "scope_refused")
        self.assertIn("(OSError)", error["record_notice"])
        self.assertEqual(ordinary["code"], "scope_refused")
        self.assertNotIn("record_notice", ordinary)  # an ordinary refusal is answered as before


@unittest.skipUnless(POSIX, "the relay needs the AF_UNIX control socket (POSIX)")
class TheDaemonAnswersEveryStopTests(unittest.TestCase):
    """The answer to a request that ends something is always the daemon's answer to THAT request."""

    def test_a_stop_asked_again_after_pending_reaches_the_daemon_each_time(self) -> None:
        answers = [{"code": "pending", "run_id": "r-1", "state": "pending", "reason": "member_alive"},
                   {"code": "pending", "run_id": "r-1", "state": "pending", "reason": "confirm_timeout"},
                   {"code": "stopped", "run_id": "r-1", "state": "stopped"}]

        def handler(message, conn):
            return {"ok": True, "result": answers[min(len(daemon.seen), len(answers)) - 1]}

        daemon = support.SocketDaemon(self, handler=handler)
        first_session = Relay(environ=daemon.environ, device_id="dev_phone")
        first = first_session.handle(stop("r-1", 1))
        again = first_session.handle(stop("r-1", 2))
        third = Relay(environ=daemon.environ, device_id="dev_phone").handle(stop("r-1", 3))
        self.assertEqual([m["params"] for m in daemon.seen], [{"run_id": "r-1"}] * 3)
        self.assertEqual([(m["id"], m["result"]["code"]) for m in (first, again, third)],
                         [(1, "pending"), (2, "pending"), (3, "stopped")])
        self.assertEqual(again["result"]["reason"], "confirm_timeout")
        self.assertEqual(first_session.handle(stop("r-1", 4))["result"]["code"], "stopped")
        self.assertEqual(len(daemon.seen), 4)

    def test_a_stop_the_daemon_refused_is_asked_again(self) -> None:
        def handler(message, conn):
            return {"ok": False, "error": {"code": "run_not_found", "layer": "handler"}}

        daemon = support.SocketDaemon(self, handler=handler)
        relay = Relay(environ=daemon.environ, device_id="dev_phone")
        for n in range(2):
            self.assertEqual(relay.handle(stop("r-9", n))["error"]["code"], "run_not_found")
        self.assertEqual(len(daemon.seen), 2)

    def test_files_planted_in_the_folder_change_no_answer(self) -> None:
        daemon = support.SocketDaemon(self)
        relay = Relay(environ=daemon.environ, device_id="dev_phone")
        lie = {"ok": True, "result": {"code": "stopped", "run_id": "r-1"}}
        names = ("ending-device-dev_phone.json", "stop-device-dev_phone.json", "answer.json", "turn-device-dev_phone")
        for name in names:
            with open(os.path.join(relay.state_folder, name), "w", encoding="ascii") as handle:
                json.dump({"same": json.dumps(stop("r-1"), sort_keys=True), "reply": lie}, handle)
        os.mkdir(os.path.join(relay.state_folder, "window-device-dev_phone.json"))  # a folder where a file goes
        for n in range(3):
            reply = relay.handle(stop("r-1", n))
            self.assertEqual(reply["result"], support.SocketDaemon.answer(stop("r-1"), None)["result"])
        self.assertEqual(len(daemon.seen), 3)

    def test_two_stops_at_once_each_ask_the_daemon(self) -> None:
        both = threading.Barrier(2, timeout=support.WATCHDOG_S)

        def handler(message, conn):
            both.wait()  # neither is answered until the daemon holds both
            return support.SocketDaemon.answer(message, conn)

        daemon = support.SocketDaemon(self, handler=handler)
        relays = [Relay(environ=daemon.environ, key_label="laptop", rights=["read", "run"]) for _ in range(2)]
        replies: list[dict] = [None, None]
        threads = [threading.Thread(target=lambda i=i: replies.__setitem__(i, relays[i].handle(
            control("session.stop", i, ENDING["session.stop"][0])))) for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(support.WATCHDOG_S)
        self.assertEqual([r["result"]["method"] for r in replies], ["session.stop", "session.stop"])
        self.assertEqual(sorted(r["id"] for r in replies), [0, 1])
        self.assertEqual(len(daemon.seen), 2)

    def test_stops_leave_no_thread_no_descriptor_and_no_file_behind(self) -> None:
        daemon = support.SocketDaemon(self)
        relay = Relay(environ=daemon.environ, key_label="phone", max_frames=1, clock=support.FakeClock())
        relay.handle(control("run.status", 0))
        files = sorted(os.listdir(relay.state_folder))
        relay.handle(stop(n=1))
        threads, descriptors = threading.active_count(), open_descriptors()
        for n in range(MANY_STOPS):
            self.assertEqual(relay.handle(stop(n=n))["result"]["method"], "run.stop")
        self.assertEqual((threading.active_count(), open_descriptors()), (threads, descriptors))
        self.assertEqual(sorted(os.listdir(relay.state_folder)), files)
        self.assertEqual(len(daemon.seen), MANY_STOPS + 2)


@unittest.skipUnless(POSIX, "the relay needs the AF_UNIX control socket (POSIX)")
class AfterTheStartNothingOnDiskHoldsAStopTests(unittest.TestCase):
    """Once a session has started, the window's files never decide whether a Stop is sent."""

    def relay(self, daemon, **options) -> Relay:
        options.setdefault("max_frames", 5)
        return Relay(environ=daemon.environ, key_label="laptop", rights=["read", "run", "approve"],
                     clock=support.FakeClock(), **options)

    def every_ending_request_goes(self, relay, daemon) -> None:
        seen = len(daemon.seen)
        for n, (method, (params, _)) in enumerate(ENDING.items()):
            with self.subTest(method=method):
                reply = within(self, lambda: relay.handle(control(method, n, params)), method)
                self.assertEqual((reply["result"]["method"], reply["result"]["params"]), (method, params))
        self.assertEqual(within(self, lambda: relay.handle(control("approval.answer", 9, DENY)), "deny")
                         ["result"]["params"]["answer"], "deny")
        self.assertEqual(len(daemon.seen), seen + len(ENDING) + 1)

    def ordinary_is_refused_with_words(self, relay, exception: str) -> None:
        error = within(self, lambda: relay.handle(control("run.status", 50)), "an ordinary frame")["error"]
        self.assertEqual((error["code"], error["delivered"], error["detail"]),
                         ("rate_limited", False, f"window_unreadable:{exception}"))
        self.assertIn("could not read", error["message"])
        self.assertIn("still goes", error["message"])

    def test_the_relay_folder_swapped_for_a_link_while_running(self) -> None:
        daemon = support.SocketDaemon(self)
        relay = self.relay(daemon)
        self.assertIn("result", relay.handle(control("run.status", 1)))
        elsewhere = support.scratch_dir(self)
        os.chmod(elsewhere, 0o700)
        os.rename(relay.state_folder, relay.state_folder + ".old")
        os.symlink(elsewhere, relay.state_folder)
        self.every_ending_request_goes(relay, daemon)
        self.ordinary_is_refused_with_words(relay, "RelayFileRefused")
        self.assertEqual(os.listdir(elsewhere), [])  # nothing written through the link

    def test_the_relay_folder_removed_while_running(self) -> None:
        daemon = support.SocketDaemon(self)
        relay = self.relay(daemon)
        shutil.rmtree(relay.state_folder)
        self.every_ending_request_goes(relay, daemon)
        self.ordinary_is_refused_with_words(relay, "FileNotFoundError")

    def test_the_relay_folder_loosened_while_running(self) -> None:
        daemon = support.SocketDaemon(self)
        relay = self.relay(daemon)
        os.chmod(relay.state_folder, 0o755)
        self.every_ending_request_goes(relay, daemon)
        self.ordinary_is_refused_with_words(relay, "RelayFileRefused")

    def test_a_fifo_at_the_window_files_name(self) -> None:
        daemon = support.SocketDaemon(self)
        relay = self.relay(daemon)
        os.mkfifo(os.path.join(relay.state_folder, "window-label-laptop.json"), 0o600)
        self.every_ending_request_goes(relay, daemon)
        self.ordinary_is_refused_with_words(relay, "RelayFileRefused")

    def test_a_window_file_another_program_holds(self) -> None:
        daemon = support.SocketDaemon(self)
        relay = self.relay(daemon)
        path = os.path.join(relay.state_folder, "window-label-laptop.json")
        holder = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, holder)
        fcntl.flock(holder, fcntl.LOCK_EX)
        self.every_ending_request_goes(relay, daemon)

    def test_a_full_disk(self) -> None:
        daemon = support.SocketDaemon(self)
        relay = self.relay(daemon)

        def full(fd, data):
            raise OSError(errno.ENOSPC, "No space left on device")

        with mock.patch.object(relay_store, "_write_at_end", full):
            self.every_ending_request_goes(relay, daemon)
            self.ordinary_is_refused_with_words(relay, "OSError")


@unittest.skipUnless(POSIX, "the relay needs the AF_UNIX control socket (POSIX)")
class TimeoutTheSystemCanWaitTests(unittest.TestCase):
    def test_a_timeout_longer_than_the_system_can_wait_is_refused_at_the_start(self) -> None:
        daemon = support.SocketDaemon(self)
        for bad in (threading.TIMEOUT_MAX * 2, math.inf, math.nan, 1e300):
            with self.subTest(timeout_s=bad), self.assertRaises(ValueError) as caught:
                Relay(environ=daemon.environ, timeout_s=bad)
            if not math.isnan(bad):
                self.assertIn("longer than this system can wait", str(caught.exception))
        relay = Relay(environ=daemon.environ, timeout_s=threading.TIMEOUT_MAX)
        self.assertEqual(relay.handle(stop(n=1))["result"]["method"], "run.stop")


if __name__ == "__main__":
    unittest.main()
