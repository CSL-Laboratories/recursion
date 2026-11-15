"""The SSH client transport (lane REMOTE): the command line, the host key pin, and the answers a surface sees.

The end-to-end tests run the client against the fake ssh in ``fake_ssh.py``, which runs the real relay
in a child process against a real ``UnixControlServer`` in a scratch runtime directory. No real host is
contacted and no real key, agent socket or known_hosts file is read: every host key is made up, and
every known_hosts file is written into the test's own scratch directory.
"""

from __future__ import annotations

import io
import json
import os
import threading
import unittest

from recursion_core.control import cli
from recursion_core.control.client import SocketAPI
from recursion_core.control_transport import UnixControlClient
from recursion_core.control_transport.framing import encode_frame
from recursion_core.remote import codec
from recursion_core.remote.relay import RELAY_COMMAND
from recursion_core.remote.ssh_transport import (HARDENED_OPTIONS, HostKeyNotPinned, RelayAPI, RelayNotDelivered,
                                                 SshRelayClient, SshTarget, key_fingerprint, pinned_fingerprints,
                                                 ssh_argv)
from recursion_core.remote.tests import support

POSIX = os.name != "nt"
TYPE, BLOB, FINGERPRINT = support.made_up_key("far-host")
_, OTHER_BLOB, OTHER_FINGERPRINT = support.made_up_key("someone-else")


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
                       "ForwardAgent=no", "ForwardX11=no", "ClearAllForwardings=yes", "PermitLocalCommand=no",
                       "RequestTTY=no", "ControlMaster=no", "ControlPath=none"):
            self.assertIn(option, options)
        self.assertIn("IdentitiesOnly=yes", options)
        self.assertEqual(argv[-3:], ["--", "far.example", RELAY_COMMAND])
        self.assertEqual(argv[argv.index("-p") + 1], "2222")
        self.assertEqual(argv[argv.index("-l") + 1], "owner")
        self.assertEqual(argv[argv.index("-i") + 1], "/srv/keys/relay_ed25519")

    def test_no_field_can_carry_an_option_or_a_second_word(self) -> None:
        bad = [
            {"host": "-oProxyCommand=sh"}, {"host": "-v"}, {"host": "far.example extra"}, {"host": "owner@far.example"},
            {"host": ""}, {"user": "-l"}, {"user": "own er"}, {"port": 0}, {"port": 65536}, {"port": True},
            {"port": "22"}, {"known_hosts": "relative/known_hosts"}, {"known_hosts": "/a b/known_hosts"},
            {"known_hosts": "/srv/%h_known_hosts"}, {"known_hosts": "~/.ssh/known_hosts"},
            {"identity_file": "id_ed25519"}, {"ssh_config": "relative"}, {"host_key_fingerprint": "MD5:00:11"},
            {"host_key_fingerprint": FINGERPRINT + "x"},
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

    def test_plain_hashed_and_port_entries_pin_exactly_their_host(self) -> None:
        self.assertEqual(self.pins([f"far.example {TYPE} {BLOB}"]), {FINGERPRINT})
        self.assertEqual(self.pins([f"FAR.example {TYPE} {BLOB}"]), {FINGERPRINT})
        self.assertEqual(self.pins([f"near.example {TYPE} {BLOB}"]), set())
        hashed = support.hashed_host("far.example", b"0123456789abcdefghij")
        self.assertEqual(self.pins([f"{hashed} {TYPE} {BLOB}"]), {FINGERPRINT})
        self.assertEqual(self.pins([f"[far.example]:2222 {TYPE} {BLOB}"], port=2222), {FINGERPRINT})
        self.assertEqual(self.pins([f"[far.example]:2222 {TYPE} {BLOB}"]), set())
        self.assertEqual(self.pins([f"far.example {TYPE} {BLOB}"], port=2222), set())

    def test_wildcards_negation_revocation_and_authorities_read_as_ssh_reads_them(self) -> None:
        self.assertEqual(self.pins([f"*.example {TYPE} {BLOB}"]), {FINGERPRINT})
        self.assertEqual(self.pins([f"*.example,!far.example {TYPE} {BLOB}"]), set())
        self.assertEqual(self.pins([f"far.example {TYPE} {BLOB}", f"@revoked * {TYPE} {BLOB}"]), set())
        self.assertEqual(self.pins([f"@cert-authority *.example {TYPE} {BLOB}"]), set())
        self.assertEqual(self.pins(["# a comment", "", f"far.example {TYPE} {BLOB}"]), {FINGERPRINT})

    def test_the_client_starts_no_ssh_unless_exactly_the_expected_key_is_pinned(self) -> None:
        spawned: list[list[str]] = []

        def spawn(argv, **kwargs):
            spawned.append(argv)
            raise AssertionError("ssh must not start")

        for lines in ([f"far.example {TYPE} {OTHER_BLOB}"],
                      [f"far.example {TYPE} {BLOB}", f"far.example {TYPE} {OTHER_BLOB}"],
                      []):
            with self.subTest(lines=lines), self.assertRaises(HostKeyNotPinned):
                SshRelayClient(target_for(support.write_known_hosts(self.dir, lines)), spawn=spawn)
        self.assertEqual(spawned, [])


class PipeProc:
    """A stand-in for the ssh child: its stdout is a real pipe that first carries the relay's hello.

    ``stdin_fails`` makes every write after the hello raise ``BrokenPipeError`` (ssh stopped reading).
    ``hang_up`` closes the pipe after the hello (the far side ended while idle). ``wait`` closes the pipe,
    as a child's exit does, so the client's reader thread ends.
    """

    def __init__(self, *, stdin_fails: bool = False, hang_up: bool = False) -> None:
        read_end, self._write_end = os.pipe()
        os.write(self._write_end, encode_frame({"ok": True, "op": "relay_hello",
                                                 "relay": {"version": 1, "transport": "ssh"}}))
        if hang_up:
            os.close(self._write_end)
            self._write_end = None
        self.stdout = os.fdopen(read_end, "rb", buffering=0)
        self.stderr = io.BytesIO()
        self.stdin = self
        self.writes = 0
        self._stdin_fails = stdin_fails
        self.returncode = None

    def write(self, data):
        self.writes += 1
        if self._stdin_fails and self.writes > 1:
            raise BrokenPipeError("ssh stopped reading")
        return len(data)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass

    def wait(self, timeout=None) -> int:
        if self._write_end is not None:
            os.close(self._write_end)
            self._write_end = None
        self.returncode = 0
        return 0

    def kill(self) -> None:
        self.wait()


class SessionEdgeTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = support.scratch_dir(self)
        self.target = target_for(support.write_known_hosts(directory, [f"far.example {TYPE} {BLOB}"]))

    def client(self, proc: PipeProc) -> SshRelayClient:
        client = SshRelayClient(self.target, spawn=lambda argv, **kwargs: proc)
        self.addCleanup(client.close)
        return client

    def test_a_write_that_fails_is_not_delivered_and_ends_the_session(self) -> None:
        client = self.client(PipeProc(stdin_fails=True))
        with self.assertRaises(RelayNotDelivered) as caught:
            client.request({"op": "control", "method": "run.stop", "params": {"run_id": "r"}, "id": 1})
        self.assertEqual(caught.exception.detail, "request:write:BrokenPipeError")
        self.assertTrue(client.closed)

    def test_a_session_that_ended_while_idle_takes_no_request(self) -> None:
        proc = PipeProc(hang_up=True)
        client = self.client(proc)
        client._reader.join(support.WATCHDOG_S)  # the reader has seen the end of the stream
        with self.assertRaises(RelayNotDelivered) as caught:
            client.request({"op": "control", "method": "run.stop", "params": {"run_id": "r"}, "id": 1})
        self.assertEqual(caught.exception.detail, "session_ended")
        self.assertEqual(proc.writes, 1)  # the hello only: the request was never written


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
        return {**answer, "id": message["id"]}

    def close(self) -> None:
        self.closed = True


class RelayAPIDecisionTests(unittest.TestCase):
    def test_a_session_that_ended_while_idle_is_reopened_and_asked_once(self) -> None:
        sessions = [FakeSession([{"ok": True, "result": {"code": "ok"}}, RelayNotDelivered("session_ended")]),
                    FakeSession([{"ok": True, "result": {"code": "ok", "n": 2}}])]
        opened = []
        api = RelayAPI(persistent=True, connect=lambda: opened.append(1) or sessions[len(opened) - 1])
        self.assertEqual(api.call("run.list"), {"code": "ok"})
        self.assertEqual(api.call("run.list"), {"code": "ok", "n": 2})
        self.assertEqual(len(opened), 2)
        self.assertEqual([len(session.asked) for session in sessions], [2, 1])

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


@unittest.skipUnless(POSIX, "the end-to-end path needs the AF_UNIX control socket (POSIX)")
class EndToEndTests(unittest.TestCase):
    """Client -> fake ssh (a child process) -> the real relay -> a real owner-only socket server."""

    def setUp(self) -> None:
        self.keys = support.scratch_dir(self)
        self.known_hosts = support.write_known_hosts(self.keys, [f"far.example {TYPE} {BLOB}"])
        self.target = target_for(self.known_hosts)
        self.log = os.path.join(self.keys, "fake-ssh.log")

    def api(self, daemon, *, mode="relay", **options) -> RelayAPI:
        api = RelayAPI(self.target, ssh=support.fake_ssh_prefix(),
                       env=support.fake_ssh_env(daemon.runtime, mode=mode, log=self.log), **options)
        self.addCleanup(api.close)
        return api

    def starts(self) -> list[dict]:
        if not os.path.exists(self.log):
            return []
        with open(self.log, encoding="utf-8") as handle:
            return [json.loads(line) for line in handle]

    def test_a_call_reaches_the_far_daemon_with_its_params_unchanged(self) -> None:
        daemon = support.SocketDaemon(self)
        answer = self.api(daemon).call("run.stop", {"run_id": "r-1", "command_id": "cmd-9"})
        self.assertEqual(answer, {"code": "ok", "method": "run.stop", "params": {"run_id": "r-1", "command_id": "cmd-9"}})
        self.assertEqual(daemon.seen, [{"op": "control", "method": "run.stop",
                                        "params": {"run_id": "r-1", "command_id": "cmd-9"}, "id": 1}])

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
            return {"ok": True, "result": {"code": "ok"}}

        daemon = support.SocketDaemon(self, handler=handler)
        local, far = SocketAPI(daemon.socket_path), self.api(daemon)
        for method in ("run.list", "run.status"):
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
        self.assertEqual(self.api(daemon, mode="wrong_id").call("run.list"),
                         {"code": codec.UNAVAILABLE, "delivered": "unknown", "detail": "request:reply_id_mismatch"})

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
