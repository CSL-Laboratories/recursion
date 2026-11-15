"""The SSH client transport (lane REMOTE): the command line, the host key pin, and the answers a surface sees.

The end-to-end tests run the client against the fake ssh in ``fake_ssh.py``, which runs the real relay
in a child process against a real ``UnixControlServer`` in a scratch runtime directory. No real host is
contacted and no real key, agent socket or known_hosts file is read: every host key is made up, and
every known_hosts file is written into the test's own scratch directory. A few deadline tests set short
deadlines on purpose; they check that a deadline ends a session, never how fast anything is.
"""

from __future__ import annotations

import base64
import io
import json
import os
import queue
import subprocess
import threading
import types
import unittest
from unittest import mock

from recursion_core.control import cli
from recursion_core.control.client import SocketAPI
from recursion_core.control_transport import UnixControlClient
from recursion_core.control_transport.framing import encode_frame
from recursion_core.remote import codec, ssh_transport
from recursion_core.remote.relay import RELAY_COMMAND
from recursion_core.remote.ssh_transport import (HARDENED_OPTIONS, HostKeyNotPinned, RelayAPI, RelayDeliveryUnknown,
                                                 RelayHandshakeFailed, RelayNotDelivered, SshRelayClient, SshTarget,
                                                 key_fingerprint, pinned_fingerprints, ssh_argv)
from recursion_core.remote.tests import support

POSIX = os.name != "nt"
# A hang detector for calls that end in well under a second: a broken guard fails here and the test still
# exits. Never a claim about speed.
SHORT_WATCHDOG_S = 15.0
TYPE, BLOB, FINGERPRINT = support.made_up_key("far-host")
_, OTHER_BLOB, OTHER_FINGERPRINT = support.made_up_key("someone-else")
# A keyed method of the pinned method table (#368 methods.v1), its params exactly as the schema allows them.
INTERRUPT = {"command_id": "cmd-4f1c2a9b7e3d5f60a8b1c2d3", "session_id": "ses-remote-1", "expected_turn_id": "turn-7"}


def target_for(pinned_file: str, **overrides) -> SshTarget:
    fields = {"host": "far.example", "known_hosts": pinned_file, "host_key_fingerprint": FINGERPRINT}
    fields.update(overrides)
    return SshTarget(**fields)


class ArgvTests(unittest.TestCase):
    def test_the_command_line_is_hardened_and_ends_with_the_relay_command(self) -> None:
        argv = ssh_argv(target_for("/srv/keys/far_known_hosts", user="owner", port=2222,
                                   identity_file="/srv/keys/relay_ed25519"))
        self.assertEqual(argv[:6], ["ssh", "-T", "-e", "none", "-F", "none"])
        options = [argv[i + 1] for i, value in enumerate(argv) if value == "-o"]
        self.assertIn("UserKnownHostsFile=/srv/keys/far_known_hosts", options)
        for option in HARDENED_OPTIONS:
            self.assertIn(option, options)
        # Written out here, not read from the module, so that weakening the module's list fails this test.
        for option in ("BatchMode=yes", "StrictHostKeyChecking=yes", "GlobalKnownHostsFile=none", "UpdateHostKeys=no",
                       "CheckHostIP=no", "VerifyHostKeyDNS=no", "ForwardAgent=no", "ForwardX11=no",
                       "ClearAllForwardings=yes", "PermitLocalCommand=no", "RequestTTY=no", "ControlMaster=no",
                       "ControlPath=none", "ConnectTimeout=15", "ServerAliveInterval=15", "ServerAliveCountMax=3",
                       "LogLevel=ERROR"):
            self.assertIn(option, options)
        self.assertIn("IdentitiesOnly=yes", options)
        self.assertEqual(argv[-3:], ["--", "far.example", RELAY_COMMAND])
        self.assertEqual(argv[argv.index("-p") + 1], "2222")
        self.assertEqual(argv[argv.index("-l") + 1], "owner")
        self.assertEqual(argv[argv.index("-i") + 1], "/srv/keys/relay_ed25519")
        self.assertEqual(argv.count("-F"), 1)

    def test_no_named_ssh_configuration_is_accepted(self) -> None:
        # A configuration could add a key source (KnownHostsCommand) or change the name looked up (HostKeyAlias).
        with self.assertRaises(TypeError):
            target_for("/srv/keys/far_known_hosts", ssh_config="/srv/keys/ssh_config")

    def test_no_field_can_carry_an_option_or_a_second_word(self) -> None:
        bad = [
            {"host": "-oProxyCommand=sh"}, {"host": "-v"}, {"host": "far.example extra"}, {"host": "owner@far.example"},
            {"host": ""}, {"user": "-l"}, {"user": "own er"}, {"port": 0}, {"port": 65536}, {"port": True},
            {"port": "22"}, {"known_hosts": "relative/known_hosts"}, {"known_hosts": "/a b/known_hosts"},
            {"known_hosts": "/srv/%h_known_hosts"}, {"known_hosts": "~/.ssh/known_hosts"},
            {"identity_file": "id_ed25519"}, {"host_key_fingerprint": "MD5:00:11"},
            {"host_key_fingerprint": FINGERPRINT + "x"},
            # A final newline passes a '$'-anchored match; each field is matched whole.
            {"host": "far.example\n"}, {"user": "owner\n"}, {"known_hosts": "/srv/keys/far_known_hosts\n"},
            {"identity_file": "/srv/keys/relay_ed25519\n"}, {"host_key_fingerprint": FINGERPRINT + "\n"},
        ]
        for change in bad:
            with self.subTest(change=change), self.assertRaises(ValueError):
                target_for("/srv/keys/far_known_hosts", **change)


class HostKeyPinTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = support.scratch_dir(self)

    def pins(self, lines, host="far.example", port=None):
        return pinned_fingerprints(support.write_known_hosts(self.dir, lines), host, port)

    def test_a_fingerprint_is_sha256_of_the_blob_unpadded_and_checks_the_key_type(self) -> None:
        self.assertEqual(key_fingerprint(TYPE, BLOB), FINGERPRINT)
        self.assertTrue(FINGERPRINT.startswith("SHA256:") and len(FINGERPRINT) == 50)
        self.assertIsNone(key_fingerprint("ssh-rsa", BLOB))
        self.assertIsNone(key_fingerprint(TYPE, "not base64!"))
        self.assertIsNone(key_fingerprint(TYPE, base64.b64encode(b"\x00\x00").decode()))  # shorter than its header

    def test_plain_hashed_and_port_entries_pin_exactly_their_host(self) -> None:
        self.assertEqual(self.pins([f"far.example {TYPE} {BLOB}"]), {FINGERPRINT})
        self.assertEqual(self.pins([f"FAR.example {TYPE} {BLOB}"]), {FINGERPRINT})
        self.assertEqual(self.pins([f"near.example {TYPE} {BLOB}"]), set())
        hashed = support.hashed_host("far.example", b"0123456789abcdefghij")
        self.assertEqual(self.pins([f"{hashed} {TYPE} {BLOB}"]), {FINGERPRINT})
        self.assertEqual(self.pins([f"[far.example]:2222 {TYPE} {BLOB}"], port=2222), {FINGERPRINT})
        self.assertEqual(self.pins([f"[far.example]:2222 {TYPE} {BLOB}"]), set())
        self.assertEqual(self.pins([f"far.example {TYPE} {BLOB}"], port=2222), set())

    def test_wildcards_negation_and_revocation_read_as_ssh_reads_them(self) -> None:
        self.assertEqual(self.pins([f"*.example {TYPE} {BLOB}"]), {FINGERPRINT})
        self.assertEqual(self.pins([f"*.example,!far.example {TYPE} {BLOB}"]), set())
        self.assertEqual(self.pins([f"far.example {TYPE} {BLOB}", f"@revoked * {TYPE} {BLOB}"]), set())
        self.assertEqual(self.pins(["# a comment", "", f"far.example {TYPE} {BLOB}"]), {FINGERPRINT})

    def test_a_certificate_authority_for_the_host_is_refused(self) -> None:
        hashed = support.hashed_host("far.example", b"salt-for-the-ca-line")
        for authority in (f"@cert-authority *.example {TYPE} {OTHER_BLOB}",
                          f"@cert-authority far.example {TYPE} {OTHER_BLOB}",
                          f"@cert-authority {hashed} {TYPE} {OTHER_BLOB}",
                          f"@cert-authority far.example {TYPE} not-a-key"):
            with self.subTest(authority=authority), self.assertRaises(HostKeyNotPinned):
                self.pins([f"far.example {TYPE} {BLOB}", authority])
        # An authority for other hosts only does not widen what this host may present.
        self.assertEqual(self.pins([f"far.example {TYPE} {BLOB}", f"@cert-authority *.other {TYPE} {OTHER_BLOB}"]),
                         {FINGERPRINT})
        self.assertEqual(self.pins([f"far.example {TYPE} {BLOB}",
                                    f"@cert-authority *.example,!far.example {TYPE} {OTHER_BLOB}"]), {FINGERPRINT})

    def test_a_marker_ssh_does_not_define_is_refused(self) -> None:
        with self.assertRaises(HostKeyNotPinned):
            self.pins([f"far.example {TYPE} {BLOB}", f"@trusted far.example {TYPE} {OTHER_BLOB}"])

    def test_the_client_starts_no_ssh_unless_exactly_the_expected_key_is_pinned(self) -> None:
        spawned: list[list[str]] = []

        def spawn(argv, **kwargs):
            spawned.append(argv)
            raise AssertionError("ssh must not start")

        for lines in ([f"far.example {TYPE} {OTHER_BLOB}"],
                      [f"far.example {TYPE} {BLOB}", f"far.example {TYPE} {OTHER_BLOB}"],
                      [f"far.example {TYPE} {BLOB}", f"@cert-authority *.example {TYPE} {OTHER_BLOB}"],
                      []):
            with self.subTest(lines=lines), self.assertRaises(HostKeyNotPinned):
                SshRelayClient(target_for(support.write_known_hosts(self.dir, lines)), spawn=spawn)
        self.assertEqual(spawned, [])


HELLO = {"ok": True, "op": "relay_hello", "relay": {"version": 1, "transport": "ssh"}}


class PipeProc:
    """A stand-in for the ssh child: its stdout is a real pipe that first carries ``hello`` and ``after``.

    It reads the frames written to it; for each whole request after the hello it writes the next of
    ``answers`` to the pipe, so that an answer never arrives before its request.
    ``stdin_fails``: every write after the hello raises ``BrokenPipeError`` (ssh stopped reading).
    ``block``: every write after the hello blocks until the child is killed (a far side that stalls).
    ``chunk``: each write takes at most this many bytes (a pipe that takes part of a write).
    ``hang_up``: the pipe is closed after the frames (the far side ended while idle).
    ``wait`` closes the pipe, as a child's exit does, so the client's reader thread ends.
    """

    def __init__(self, *, hello=HELLO, after=(), answers=(), stdin_fails=False, block=False, chunk=None,
                 hang_up=False, stderr=b"") -> None:
        read_end, self._write_end = os.pipe()
        for frame in ([hello] if hello is not None else []) + list(after):
            os.write(self._write_end, encode_frame(frame))
        if hang_up:
            os.close(self._write_end)
            self._write_end = None
        self.stdout = os.fdopen(read_end, "rb", buffering=0)
        self.stderr = io.BytesIO(stderr)
        self.stdin = self
        self.writes, self.written, self._unread, self.requests = 0, bytearray(), bytearray(), 0
        self.blocked, self._killed = threading.Event(), threading.Event()
        self._stdin_fails, self._block, self._chunk = stdin_fails, block, chunk
        self._answers, self._hello_seen = list(answers), False
        self._exit_lock = threading.Lock()
        self.returncode = None

    def write(self, data):
        self.writes += 1
        if self._hello_seen and self._stdin_fails:
            raise BrokenPipeError("ssh stopped reading")
        if self._hello_seen and self._block:
            self.blocked.set()
            self._killed.wait()
            raise BrokenPipeError("the child was killed")
        taken = len(data) if self._chunk is None else min(self._chunk, len(data))
        self.written += bytes(data[:taken])
        self._unread += bytes(data[:taken])
        while len(self._unread) >= 4 and len(self._unread) >= 4 + int.from_bytes(self._unread[:4], "big"):
            size = int.from_bytes(self._unread[:4], "big")
            frame = json.loads(bytes(self._unread[4:4 + size]))
            del self._unread[:4 + size]
            if frame.get("op") == "relay_hello":
                self._hello_seen = True
                continue
            self.requests += 1
            if self._answers:
                with self._exit_lock:
                    if self._write_end is not None:
                        os.write(self._write_end, encode_frame(self._answers.pop(0)))
        return taken

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass

    def wait(self, timeout=None) -> int:
        with self._exit_lock:  # two threads may end the child at once (close, and the request's own cleanup)
            if self._write_end is not None:
                os.close(self._write_end)
                self._write_end = None
            self.returncode = 0
        return 0

    def kill(self) -> None:
        self._killed.set()
        self.wait()


class FakeTime:
    """Stands in for the ``time`` module inside ssh_transport: a monotonic clock the test moves by hand."""

    def __init__(self, now: float = 100.0) -> None:
        self.now = now

    def monotonic(self) -> float:
        return self.now


def recording_queue_module(waits: list) -> types.SimpleNamespace:
    """Stands in for the ``queue`` module inside ssh_transport: every wait's timeout is written to ``waits``."""

    class RecordingQueue(queue.Queue):
        def get(self, block=True, timeout=None):
            waits.append(timeout)
            return super().get(block, timeout)

    return types.SimpleNamespace(Queue=RecordingQueue, Empty=queue.Empty, Full=queue.Full)


class ClockedPipeProc(PipeProc):
    """Each write moves the clock on: by ``hello_step`` while writing the hello, by ``request_step`` after."""

    def __init__(self, clock: FakeTime, *, hello_step: float = 0.0, request_step: float = 0.0, **options) -> None:
        super().__init__(**options)
        self.clock, self.steps = clock, (hello_step, request_step)

    def write(self, data):
        self.clock.now += self.steps[1] if self._hello_seen else self.steps[0]
        return super().write(data)


class LateFlush(PipeProc):
    """Takes every byte of a request; then its flush returns only when the child is killed."""

    def flush(self) -> None:
        if self.requests:
            self._killed.wait()


class StuckAfterWrite(PipeProc):
    """Takes every byte of a request, then does not return from that write until ``resume`` (a kill does not)."""

    def __init__(self, **options) -> None:
        super().__init__(**options)
        self.resume = threading.Event()

    def write(self, data):
        taken = super().write(data)
        if self.requests:
            self.resume.wait()
        return taken


class FlushFails(PipeProc):
    """Takes every byte of a request; then its flush fails, as a pipe closed under it would."""

    def flush(self) -> None:
        if self.requests:
            raise BrokenPipeError("the pipe closed after the write")


class EndsWithoutAnIOFailure(PipeProc):
    """Takes every byte of a request, then its write ends on an error that is not an I/O failure."""

    def write(self, data):
        taken = super().write(data)
        if self.requests:
            raise MemoryError("not an I/O failure")
        return taken


class StuckOnHello(PipeProc):
    """Takes every byte of the hello, then does not return from that write until ``resume`` (a kill does not)."""

    def __init__(self, **options) -> None:
        super().__init__(**options)
        self.resume = threading.Event()

    def write(self, data):
        taken = super().write(data)
        if self._hello_seen and not self.requests:
            self.resume.wait()
        return taken


class SessionEdgeTests(unittest.TestCase):
    REQUEST = {"op": "control", "method": "run.stop", "params": {"run_id": "r"}, "id": 1}

    def setUp(self) -> None:
        directory = support.scratch_dir(self)
        self.target = target_for(support.write_known_hosts(directory, [f"far.example {TYPE} {BLOB}"]))

    def client(self, proc: PipeProc, **options) -> SshRelayClient:
        client = SshRelayClient(self.target, spawn=lambda argv, **kwargs: proc, **options)
        self.addCleanup(client.close)
        return client

    def test_a_write_that_fails_is_not_delivered_and_ends_the_session(self) -> None:
        client = self.client(PipeProc(stdin_fails=True))
        with self.assertRaises(RelayNotDelivered) as caught:
            client.request(self.REQUEST)
        self.assertEqual(caught.exception.detail, "request:write:BrokenPipeError")
        self.assertTrue(client.closed)

    def test_a_pipe_that_takes_part_of_each_write_gets_the_whole_frame(self) -> None:
        proc = PipeProc(chunk=3, answers=[{"ok": True, "result": {"code": "stopped"}, "id": 1}])
        client = self.client(proc)
        self.assertEqual(client.request(self.REQUEST)["result"], {"code": "stopped"})
        self.assertEqual(bytes(proc.written), encode_frame({"op": "relay_hello", "relay_version": 1})
                         + encode_frame(self.REQUEST))

    def test_a_session_that_ended_while_idle_takes_no_request(self) -> None:
        proc = PipeProc(hang_up=True)
        client = self.client(proc)
        client._reader.join(support.WATCHDOG_S)  # the reader has seen the end of the stream
        with self.assertRaises(RelayNotDelivered) as caught:
            client.request(self.REQUEST)
        self.assertEqual(caught.exception.detail, "session_ended")
        self.assertEqual(proc.writes, 1)  # the hello only: the request was never written

    def test_frames_with_no_request_outstanding_are_bounded_and_end_the_session(self) -> None:
        proc = PipeProc(after=[{"ok": True, "result": {"code": "stray"}}] * 6)
        client = self.client(proc)
        self.assertTrue(client._out_of_step.wait(support.WATCHDOG_S))
        self.assertLessEqual(client._replies.qsize(), 2)
        with self.assertRaises(RelayNotDelivered) as caught:
            client.request(self.REQUEST)
        self.assertEqual(caught.exception.detail, "session_ended")
        self.assertEqual(proc.writes, 1)

    def bounded(self, call, watchdog: float = support.WATCHDOG_S):
        """Run ``call`` in a daemon thread; a hang fails the test at the watchdog and the process still exits."""
        outcome = []

        def run() -> None:
            try:
                outcome.append(call())
            except BaseException as exc:  # noqa: BLE001 - handed back to the test
                outcome.append(exc)

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        worker.join(watchdog)
        self.assertFalse(worker.is_alive(), "the call outlived its deadline")
        return outcome[0]

    def test_each_wait_gets_only_what_is_left_of_its_deadline(self) -> None:
        clock, waits = FakeTime(), []
        proc = ClockedPipeProc(clock, hello_step=1.0, request_step=1.5,
                               answers=[{"ok": True, "result": {"code": "stopped"}, "id": 1}])
        with mock.patch.object(ssh_transport, "time", clock), \
                mock.patch.object(ssh_transport, "queue", recording_queue_module(waits)):
            client = self.client(proc, hello_timeout_s=1.25, timeout_s=2.0)
            self.assertEqual(client.request(self.REQUEST)["result"], {"code": "stopped"})
        # The hello's write took 1.0 of its 1.25; the request's write took 1.5 of its 2.0.
        self.assertEqual(waits, [0.25, 0.5])

    def test_a_write_that_completes_at_the_deadline_is_never_reported_unsent(self) -> None:
        client = self.client(LateFlush(), timeout_s=0.2)
        failure = self.bounded(lambda: client.request(self.REQUEST))
        self.assertIsInstance(failure, RelayDeliveryUnknown)
        # The deadline's kill also ends the child's output, so the wait that follows may see the end of the
        # stream or nothing at all: either way the whole frame went out, and it is never called unsent.
        self.assertIn(failure.detail, ("request:timeout", "request:eof"))
        self.assertTrue(client.closed)

    def test_a_write_whose_outcome_is_not_known_is_never_reported_unsent(self) -> None:
        proc = StuckAfterWrite()
        client = self.client(proc, timeout_s=0.2)
        self.addCleanup(proc.resume.set)
        with mock.patch.object(ssh_transport, "CLOSE_GRACE_S", 0.2):
            failure = self.bounded(lambda: client.request(self.REQUEST))
        self.assertIsInstance(failure, RelayDeliveryUnknown)
        self.assertEqual(failure.detail, "request:write_unknown")
        self.assertTrue(client.closed)

    def test_a_full_write_then_a_failed_flush_is_never_reported_unsent(self) -> None:
        # The answer the far side already sent is the answer.
        answered = self.client(FlushFails(answers=[{"ok": True, "result": {"code": "stopped"}, "id": 1}]))
        self.assertEqual(answered.request(self.REQUEST)["result"], {"code": "stopped"})
        # With no answer, the request may have been carried out: unknown, never unsent.
        silent = self.client(FlushFails(), timeout_s=0.2)
        failure = self.bounded(lambda: silent.request(self.REQUEST))
        self.assertIsInstance(failure, RelayDeliveryUnknown)
        self.assertEqual(failure.detail, "request:timeout")

    def test_a_writer_that_ends_without_an_io_failure_is_unknown(self) -> None:
        with mock.patch.object(threading, "excepthook", lambda args: None):  # the writer's error is expected
            client = self.client(EndsWithoutAnIOFailure())
            failure = self.bounded(lambda: client.request(self.REQUEST))
        self.assertIsInstance(failure, RelayDeliveryUnknown)
        self.assertEqual(failure.detail, "request:write_unknown")
        self.assertTrue(client.closed)

    def test_a_hello_whose_write_never_returns_is_a_failed_handshake(self) -> None:
        proc = StuckOnHello()
        self.addCleanup(proc.resume.set)
        with mock.patch.object(ssh_transport, "CLOSE_GRACE_S", 0.2):
            failure = self.bounded(lambda: self.client(proc, hello_timeout_s=0.2))
        self.assertIsInstance(failure, RelayHandshakeFailed)
        self.assertEqual(failure.detail, "hello_timeout")

    def test_a_request_that_cannot_be_serialised_is_refused_before_it_is_written(self) -> None:
        proc = PipeProc()
        client = self.client(proc)
        with self.assertRaises(RelayNotDelivered) as caught:
            client.request({"op": "control", "method": "run.list", "params": {"x": {1, 2}}, "id": 1})
        self.assertEqual(caught.exception.detail, "request:frame_not_encodable:TypeError")
        self.assertEqual(proc.writes, 1)  # the hello only
        self.assertFalse(client.closed)

    def test_an_answer_that_never_comes_ends_the_session_as_delivery_unknown(self) -> None:
        client = self.client(PipeProc(), timeout_s=0.2)
        failure = self.bounded(lambda: client.request(self.REQUEST))
        self.assertIsInstance(failure, RelayDeliveryUnknown)
        self.assertEqual(failure.detail, "request:timeout")
        self.assertTrue(client.closed)

    def test_a_hello_that_never_comes_is_a_failed_handshake(self) -> None:
        failure = self.bounded(lambda: self.client(PipeProc(hello=None), hello_timeout_s=0.2))
        self.assertIsInstance(failure, RelayHandshakeFailed)
        self.assertEqual(failure.detail, "hello_timeout")

    def test_deadlines_must_be_positive_numbers(self) -> None:
        for options in ({"timeout_s": 0}, {"timeout_s": "x"}, {"hello_timeout_s": -1}, {"hello_timeout_s": True}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                SshRelayClient(self.target, spawn=lambda argv, **kwargs: self.fail("ssh must not start"), **options)

    def test_a_hello_must_be_the_relays_with_an_integer_version(self) -> None:
        cases = {"relay_version": {**HELLO, "relay": {"version": True, "transport": "ssh"}},
                 "relay_hello_invalid": {"ok": True, "op": "pong", "relay": {"version": 1}}}
        for detail, hello in cases.items():
            with self.subTest(detail=detail), self.assertRaises(RelayHandshakeFailed) as caught:
                self.client(PipeProc(hello=hello))
            self.assertEqual(caught.exception.detail, detail)

    def test_the_relays_own_refusal_without_an_id_is_not_delivered(self) -> None:
        refusal = codec.relay_error("bad_frame", delivered=False, detail="frame_too_large")
        client = self.client(PipeProc(answers=[refusal]))
        with self.assertRaises(RelayNotDelivered) as caught:
            client.request(self.REQUEST)
        self.assertEqual(caught.exception.detail, "relay:frame_too_large")
        self.assertTrue(client.closed)

    def test_close_ends_a_request_blocked_in_its_write(self) -> None:
        proc = PipeProc(block=True)
        client = self.client(proc)
        outcome = []

        def ask() -> None:
            try:
                client.request(self.REQUEST)
            except RelayNotDelivered as exc:
                outcome.append(exc.detail)

        worker = threading.Thread(target=ask, daemon=True)
        worker.start()
        self.addCleanup(lambda: (proc.kill(), worker.join(SHORT_WATCHDOG_S)))  # release, then join: one cleanup
        self.assertTrue(proc.blocked.wait(support.WATCHDOG_S))
        self.bounded(client.close, SHORT_WATCHDOG_S)  # ending the session must not wait for the blocked request
        worker.join(SHORT_WATCHDOG_S)
        self.assertFalse(worker.is_alive())
        self.assertEqual(outcome, ["request:write:BrokenPipeError"])
        self.assertTrue(client.closed)

    def test_stderr_is_shown_as_printable_ascii_only(self) -> None:
        client = self.client(PipeProc(stderr=b"caf\xc3\xa9 \x1b[31m\nok\n"))
        client._drain.join(support.WATCHDOG_S)
        self.assertEqual(client.last_stderr(), "caf?? ?[31m\nok\n")


class FakeSession:
    """A session for RelayAPI's own decisions, with no process behind it."""

    def __init__(self, answers, label=None) -> None:
        self.answers, self.label, self.closed, self.asked = list(answers), label, False, []

    def request(self, message):
        self.asked.append(message)
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            if isinstance(answer, RelayNotDelivered) and answer.detail == "session_ended":
                self.closed = True
            raise answer
        return dict(answer)

    def close(self) -> None:
        self.closed = True


class BlockingSession(FakeSession):
    """A session whose one call waits until released."""

    def __init__(self) -> None:
        super().__init__([{"ok": True, "result": {"code": "ok"}}])
        self.entered, self.release = threading.Event(), threading.Event()

    def request(self, message):
        self.entered.set()
        self.release.wait(support.WATCHDOG_S)
        return super().request(message)


class RelayAPIDecisionTests(unittest.TestCase):
    def test_a_call_sends_the_socket_clients_frame_and_nothing_more(self) -> None:
        session = FakeSession([{"ok": True, "result": {"code": "ok"}}])
        RelayAPI(connect=lambda: session).call("run.list")
        self.assertEqual(session.asked, [{"op": "control", "method": "run.list", "params": {}}])

    def test_a_session_that_ended_while_idle_is_reopened_and_asked_once(self) -> None:
        sessions = [FakeSession([{"ok": True, "result": {"code": "ok"}}, RelayNotDelivered("session_ended")]),
                    FakeSession([{"ok": True, "result": {"code": "ok", "n": 2}}])]
        opened = []
        api = RelayAPI(persistent=True, connect=lambda: opened.append(1) or sessions[len(opened) - 1])
        self.assertEqual(api.call("run.list"), {"code": "ok"})
        self.assertEqual(api.call("run.list"), {"code": "ok", "n": 2})
        self.assertEqual(len(opened), 2)
        self.assertEqual([len(session.asked) for session in sessions], [2, 1])

    def test_a_stop_does_not_wait_for_the_shared_session(self) -> None:
        shared = BlockingSession()
        sessions = iter([shared, FakeSession([{"ok": True, "result": {"code": "stopped"}}])])
        api = RelayAPI(persistent=True, connect=lambda: next(sessions))
        polling = threading.Thread(target=api.call, args=("run.list",))
        polling.start()
        self.addCleanup(lambda: (shared.release.set(), polling.join(support.WATCHDOG_S)))
        self.assertTrue(shared.entered.wait(support.WATCHDOG_S))
        answers, done = [], threading.Event()
        stopping = threading.Thread(target=lambda: (answers.append(api.call("run.stop", {"run_id": "r"})), done.set()))
        stopping.start()
        self.addCleanup(stopping.join, support.WATCHDOG_S)
        self.assertTrue(done.wait(support.WATCHDOG_S), "the Stop waited for the shared session")
        self.assertEqual(answers, [{"code": "stopped"}])
        self.assertFalse(shared.release.is_set())  # the polling call was still inside the shared session

    def test_an_answer_the_relay_could_not_pass_back_is_delivered(self) -> None:
        answer = codec.relay_error("response_too_large", delivered=True)
        api = RelayAPI(connect=lambda: FakeSession([answer]))
        self.assertEqual(api.call("run.list"), {"code": codec.UNAVAILABLE, "delivered": True,
                                                "detail": "relay:response_too_large"})

    def test_a_connect_that_fails_is_not_delivered(self) -> None:
        def connect():
            raise FileNotFoundError("no ssh")

        self.assertEqual(RelayAPI(connect=connect).call("run.stop", {"run_id": "r"}),
                         {"code": codec.UNAVAILABLE, "delivered": False, "detail": "connect:FileNotFoundError"})

    def test_a_connect_that_is_not_a_function_is_refused_at_once(self) -> None:
        with self.assertRaises(TypeError):
            RelayAPI(connect=7)


@unittest.skipUnless(POSIX, "the end-to-end path needs the AF_UNIX control socket (POSIX)")
class EndToEndTests(unittest.TestCase):
    """Client -> fake ssh (a child process) -> the real relay -> a real owner-only socket server."""

    def setUp(self) -> None:
        self.keys = support.scratch_dir(self)
        self.known_hosts = support.write_known_hosts(self.keys, [f"far.example {TYPE} {BLOB}"])
        self.target = target_for(self.known_hosts)
        self.log = os.path.join(self.keys, "fake-ssh.log")

    def options(self, daemon, mode="relay") -> dict:
        return {"ssh": support.fake_ssh_prefix(), "env": support.fake_ssh_env(daemon.runtime, mode=mode, log=self.log)}

    def api(self, daemon, *, mode="relay", **options) -> RelayAPI:
        api = RelayAPI(self.target, **self.options(daemon, mode), **options)
        self.addCleanup(api.close)
        return api

    def client(self, daemon, *, mode="relay") -> SshRelayClient:
        client = SshRelayClient(self.target, **self.options(daemon, mode))
        self.addCleanup(client.close)
        return client

    def starts(self) -> list[dict]:
        if not os.path.exists(self.log):
            return []
        with open(self.log, encoding="utf-8") as handle:
            return [json.loads(line) for line in handle]

    def test_a_call_reaches_the_far_daemon_with_its_params_unchanged(self) -> None:
        daemon = support.SocketDaemon(self)
        answer = self.api(daemon).call("session.interrupt", INTERRUPT)
        self.assertEqual(answer, {"code": "ok", "method": "session.interrupt", "params": INTERRUPT})
        self.assertEqual(daemon.seen, [{"op": "control", "method": "session.interrupt", "params": INTERRUPT}])

    def test_the_far_daemon_sees_exactly_the_frame_the_socket_client_sends(self) -> None:
        daemon = support.SocketDaemon(self)
        SocketAPI(daemon.socket_path).call("run.stop", {"run_id": "r-1"})
        self.api(daemon).call("run.stop", {"run_id": "r-1"})
        self.assertEqual(len(daemon.seen), 2)
        self.assertEqual(daemon.seen[0], daemon.seen[1])

    def test_the_fake_ssh_was_given_exactly_the_hardened_command_line(self) -> None:
        daemon = support.SocketDaemon(self)
        self.api(daemon).call("run.list")
        self.assertEqual([start["argv"] for start in self.starts()], [ssh_argv(self.target, ssh=())])

    def test_the_cli_drives_the_far_daemon(self) -> None:
        daemon = support.SocketDaemon(self)
        out = io.StringIO()
        code = cli.main(["status", "r-9"], self.api(daemon), out=out, err=io.StringIO())
        self.assertEqual(code, cli.EXIT_OK)
        self.assertEqual(json.loads(out.getvalue())["params"], {"run_id": "r-9"})

    def test_the_answers_are_the_socket_clients_for_success_refusal_and_not_admitted(self) -> None:
        def handler(message, conn):
            if message.get("method") == "run.status":
                return {"ok": False, "error": {"code": "request_invalid"}}
            if message.get("method") == "run.list":
                return {"ok": True, "result": {"code": "ok"}, "id": "the-handlers-own"}
            return {"ok": True, "result": {"code": "ok"}}

        daemon = support.SocketDaemon(self, handler=handler)
        local, far = SocketAPI(daemon.socket_path), self.api(daemon)
        for method in ("run.list", "run.status", "run.stop"):
            with self.subTest(method=method):
                self.assertEqual(far.call(method, {"run_id": "r"}), local.call(method, {"run_id": "r"}))
        refusing = support.SocketDaemon(self, admit=lambda cred: False)
        self.assertEqual(self.api(refusing).call("run.list"), SocketAPI(refusing.socket_path).call("run.list"))
        self.assertEqual(self.api(refusing).call("run.list"),
                         {"code": codec.UNAVAILABLE, "delivered": False, "detail": "daemon:scope_refused"})

    def test_a_full_socket_answers_busy_as_the_socket_client_does(self) -> None:
        entered, release = threading.Event(), threading.Event()

        def handler(message, conn):
            if message.get("method") == "slow.call":
                entered.set()
                release.wait(support.WATCHDOG_S)
            return {"ok": True, "result": {"code": "ok"}}

        daemon = support.SocketDaemon(self, handler=handler, max_connections=1)
        holder = UnixControlClient(daemon.socket_path, timeout_s=support.WATCHDOG_S)
        self.addCleanup(holder.close)
        worker = threading.Thread(target=holder.request, args=({"op": "control", "method": "slow.call", "params": {}},))
        worker.start()
        self.addCleanup(lambda: (release.set(), worker.join(support.WATCHDOG_S)))
        self.assertTrue(entered.wait(support.WATCHDOG_S))
        busy = {"code": codec.UNAVAILABLE, "delivered": False, "detail": "busy"}
        self.assertEqual(SocketAPI(daemon.socket_path).call("run.list"), busy)
        self.assertEqual(self.api(daemon).call("run.list"), busy)

    def test_no_daemon_behind_the_relay_is_not_delivered(self) -> None:
        daemon = support.SocketDaemon(self)
        daemon.server.close()
        self.assertEqual(self.api(daemon).call("run.stop", {"run_id": "r"}),
                         {"code": codec.UNAVAILABLE, "delivered": False, "detail": "relay:connect:FileNotFoundError"})

    def test_ssh_that_cannot_log_in_is_not_delivered(self) -> None:
        daemon = support.SocketDaemon(self)
        self.assertEqual(self.api(daemon, mode="refuse").call("run.stop", {"run_id": "r"}),
                         {"code": codec.UNAVAILABLE, "delivered": False, "detail": "connect:ssh_exit_255"})
        self.assertEqual(daemon.seen, [])

    def test_a_shell_that_prints_fails_the_handshake_not_a_request(self) -> None:
        daemon = support.SocketDaemon(self)
        answer = self.api(daemon, mode="banner").call("run.stop", {"run_id": "r"})
        self.assertEqual(answer, {"code": codec.UNAVAILABLE, "delivered": False,
                                  "detail": "connect:stream:frame_too_large"})
        self.assertEqual(daemon.seen, [])

    def test_a_session_that_dies_after_the_request_is_delivery_unknown(self) -> None:
        daemon = support.SocketDaemon(self)
        self.assertEqual(self.api(daemon, mode="hello_then_die").call("run.stop", {"run_id": "r"}),
                         {"code": codec.UNAVAILABLE, "delivered": "unknown", "detail": "request:eof"})

    def test_an_answer_with_another_id_is_delivery_unknown(self) -> None:
        daemon = support.SocketDaemon(self)
        client = self.client(daemon, mode="wrong_id")
        with self.assertRaises(RelayDeliveryUnknown) as caught:
            client.request({"op": "control", "method": "run.list", "params": {}, "id": 7})
        self.assertEqual(caught.exception.detail, "request:reply_id_mismatch")
        self.assertTrue(client.closed)

    def test_a_relay_of_another_version_is_refused_at_the_handshake(self) -> None:
        daemon = support.SocketDaemon(self)
        self.assertEqual(self.api(daemon, mode="old_relay").call("run.list"),
                         {"code": codec.UNAVAILABLE, "delivered": False, "detail": "connect:relay_version"})

    def test_a_key_asked_for_another_command_reaches_nothing(self) -> None:
        daemon = support.SocketDaemon(self)
        self.assertEqual(self.api(daemon, mode="misused_key").call("run.list"),
                         {"code": codec.UNAVAILABLE, "delivered": False, "detail": "connect:ssh_exit_2"})
        self.assertEqual(daemon.seen, [])

    def test_a_request_too_large_for_a_frame_is_refused_before_it_is_written(self) -> None:
        daemon = support.SocketDaemon(self)
        answer = self.api(daemon).call("run.start", {"prompt": "x" * (1 << 20)})
        self.assertEqual(answer, {"code": codec.UNAVAILABLE, "delivered": False, "detail": "request:frame_too_large"})
        self.assertEqual(daemon.seen, [])

    def test_a_frame_the_relay_refuses_unread_is_not_delivered_and_ends_the_session(self) -> None:
        daemon = support.SocketDaemon(self)
        request = {"op": "control", "method": "run.start", "params": {"prompt": "x" * 512}, "id": 3}
        client = self.client(daemon, mode="small_relay")
        with self.assertRaises(RelayNotDelivered) as caught:
            client.request(request)
        self.assertEqual(caught.exception.detail, "relay:frame_too_large")
        self.assertTrue(client.closed)
        self.assertEqual(self.api(daemon, mode="small_relay").call("run.start", {"prompt": "x" * 512}),
                         {"code": codec.UNAVAILABLE, "delivered": False, "detail": "relay:frame_too_large"})
        self.assertEqual(daemon.seen, [])

    def test_a_far_side_that_stops_reading_cannot_hold_a_request_past_its_deadline(self) -> None:
        daemon = support.SocketDaemon(self)
        children = []

        def spawn(argv, **kwargs):
            child = subprocess.Popen(argv, **kwargs)
            children.append(child)
            return child

        api = self.api(daemon, mode="stall", timeout_s=0.5, spawn=spawn)
        answers = []
        worker = threading.Thread(target=lambda: answers.append(api.call("run.start", {"prompt": "x" * 900_000})),
                                  daemon=True)
        worker.start()
        try:
            worker.join(SHORT_WATCHDOG_S)
            self.assertFalse(worker.is_alive(), "the request outlived its deadline")
        finally:  # a broken deadline must fail this test and still leave no child or thread behind
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.wait()
            worker.join(SHORT_WATCHDOG_S)
        self.assertEqual(answers, [{"code": codec.UNAVAILABLE, "delivered": False, "detail": "request:write_timeout"}])
        self.assertEqual(daemon.seen, [])

    def test_ssh_that_is_not_installed_is_not_delivered(self) -> None:
        daemon = support.SocketDaemon(self)
        api = RelayAPI(self.target, ssh=(os.path.join(self.keys, "no-such-ssh"),),
                       env=support.fake_ssh_env(daemon.runtime))
        self.assertEqual(api.call("run.list"),
                         {"code": codec.UNAVAILABLE, "delivered": False, "detail": "connect:FileNotFoundError"})

    def test_a_persistent_session_is_reused_and_a_stop_gets_its_own(self) -> None:
        daemon = support.SocketDaemon(self)
        api = self.api(daemon, persistent=True)
        self.assertEqual(api.call("run.list")["code"], "ok")
        self.assertEqual(api.call("run.list")["code"], "ok")
        self.assertEqual(len(self.starts()), 1)
        self.assertEqual(api.call("run.stop", {"run_id": "r"})["code"], "ok")
        self.assertEqual(len(self.starts()), 2)

    def test_the_label_names_the_pinned_key_and_what_the_relay_reported(self) -> None:
        daemon = support.SocketDaemon(self)
        api = self.api(daemon)
        api.call("run.list")
        self.assertEqual(api.label, {
            "transport": "ssh", "host": "far.example", "port": 22, "host_key_fingerprint": FINGERPRINT,
            "relay": {"version": 1, "transport": "ssh", "key_label": "laptop", "client_address": "198.51.100.7"},
        })


if __name__ == "__main__":
    unittest.main()
