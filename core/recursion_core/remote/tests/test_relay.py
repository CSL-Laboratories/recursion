"""The relay (lane REMOTE): frames from stdin to the owner-only socket and back, and what it refuses.

Every test runs against a real ``UnixControlServer`` in a scratch runtime directory (the stand-in daemon in
``support``), never the real runtime folder. What these tests prove is the transport: that the relay is a
faithful pipe onto today's socket, and the relay's own lock and caps in front of it. They do not prove the
socket's own rights check, which is not built.
"""

from __future__ import annotations

import ast
import fcntl
import io
import json
import os
import select
import stat
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path

from recursion_core.control_transport import FrameError, InsecureSocketPath, RemoteBindRefused, UnixControlClient
from recursion_core.control_transport.framing import HEADER, MAX_FRAME_BYTES, encode_frame
from recursion_core.remote import codec
try:  # names this round adds; imported so that, without them, only the tests that use them fail
    from recursion_core.control.client import state_directory
    from recursion_core.remote import relay_store
except ImportError:  # pragma: no cover - the tree before this round
    state_directory = relay_store = None
from recursion_core.remote.relay import EXIT_OK, EXIT_REFUSED, EXIT_STREAM, RELAY_COMMAND, Relay, main
from recursion_core.remote.tests import support

POSIX = os.name != "nt"
REMOTE_DIR = Path(__file__).resolve().parents[1]


def frames(*messages) -> bytes:
    return b"".join(encode_frame(message) for message in messages)


def read_all(data: bytes) -> list[dict]:
    stream, out = io.BytesIO(data), []
    while (message := codec.read_message(stream)) is not None:
        out.append(message)
    return out


@unittest.skipUnless(POSIX, "the relay needs the AF_UNIX control socket (POSIX)")
class RelayForwardsTests(unittest.TestCase):
    def test_a_frame_reaches_the_socket_unchanged_and_its_answer_keeps_the_id(self) -> None:
        daemon = support.SocketDaemon(self)
        relay = Relay(environ=daemon.environ, rights=["read", "run"])  # a key whose forced command names run
        # A keyed method of the pinned method table, its params exactly as methods.v1 allows them.
        params = {"command_id": "cmd-4f1c2a9b7e3d5f60a8b1c2d3", "session_id": "ses-remote-1",
                  "expected_turn_id": "turn-7"}
        request = {"op": "control", "method": "session.interrupt", "params": params, "id": 41}
        reply = relay.handle(request)
        self.assertEqual(daemon.seen, [request])
        self.assertEqual(reply["id"], 41)
        self.assertEqual(reply["result"]["params"], params)

    def test_each_frame_gets_a_connection_of_its_own(self) -> None:
        daemon = support.SocketDaemon(self)
        relay = Relay(environ=daemon.environ)
        for n in range(3):
            relay.handle({"op": "control", "method": "run.list", "params": {}, "id": n})
        self.assertEqual(daemon.server.accepted, 3)

    def test_ping_is_answered_by_the_socket(self) -> None:
        daemon = support.SocketDaemon(self)
        self.assertEqual(Relay(environ=daemon.environ).handle({"op": "ping"}), {"ok": True, "op": "pong"})

    def test_stop_is_served_on_the_reserved_slot_while_the_socket_is_full(self) -> None:
        entered, release = threading.Event(), threading.Event()

        def handler(message, conn):
            if message.get("method") == "slow.call":
                entered.set()
                release.wait(support.WATCHDOG_S)
            return {"ok": True, "result": {"code": "ok"}}

        daemon = support.SocketDaemon(self, handler=handler, max_connections=1, reserved_connections=1,
                                      priority=lambda message: message.get("method") == "run.stop")
        holder = UnixControlClient(daemon.socket_path, timeout_s=support.WATCHDOG_S)
        self.addCleanup(holder.close)
        worker = threading.Thread(target=holder.request, args=({"op": "control", "method": "slow.call", "params": {}},))
        worker.start()
        self.addCleanup(lambda: (release.set(), worker.join(support.WATCHDOG_S)))  # release, then join: one cleanup
        self.assertTrue(entered.wait(support.WATCHDOG_S))
        relay = Relay(environ=daemon.environ)
        # Stop first: the socket's worker releases a reserved slot only after its reply is sent, so a Stop sent
        # right after another request that took the reserved slot can itself find it taken (a property of
        # control_transport, not of the relay). The ordinary request after the Stop is busy either way.
        stop = relay.handle({"op": "control", "method": "run.stop", "params": {"run_id": "r-2"}})
        self.assertEqual(stop, {"ok": True, "result": {"code": "ok"}})
        self.assertEqual(relay.handle({"op": "control", "method": "run.list", "params": {}}), codec.BUSY_MESSAGE)

    def test_the_sockets_not_admitted_frame_is_passed_back_exactly(self) -> None:
        daemon = support.SocketDaemon(self, admit=lambda cred: False)
        reply = Relay(environ=daemon.environ).handle({"op": "control", "method": "run.list", "params": {}})
        self.assertEqual(reply, codec.NOT_ADMITTED_MESSAGE)
        self.assertEqual(daemon.seen, [])

    def test_an_answer_that_claims_the_relay_layer_is_rewritten_as_delivered(self) -> None:
        def handler(message, conn):
            return {"ok": False, "error": {"code": "copied_text", "layer": "relay", "delivered": False}}

        daemon = support.SocketDaemon(self, handler=handler)
        reply = Relay(environ=daemon.environ).handle({"op": "control", "method": "run.list", "params": {}})
        self.assertEqual(reply["error"], {"code": "copied_text", "layer": "handler", "delivered": True})
        self.assertIsNone(codec.relay_error_of(reply))

    def test_relay_ops_are_answered_by_the_relay_and_never_forwarded(self) -> None:
        daemon = support.SocketDaemon(self)
        relay = Relay(environ=daemon.environ, key_label="laptop")
        hello = relay.handle({"op": "relay_hello", "relay_version": 1, "id": 3})
        self.assertEqual(hello["id"], 3)
        self.assertEqual(hello["relay"], {"version": 1, "transport": "stdio", "key_label": "laptop",
                                          "client_address": None})
        other = relay.handle({"op": "relay_shutdown", "id": 4})
        self.assertEqual(other["error"]["code"], "unknown_relay_op")
        self.assertIs(other["error"]["delivered"], False)
        self.assertEqual(daemon.seen, [])


@unittest.skipUnless(POSIX, "the relay needs the AF_UNIX control socket (POSIX)")
class RelayRefusesTests(unittest.TestCase):
    def test_a_network_shaped_socket_address_is_refused_at_start(self) -> None:
        for address in ("127.0.0.1:7000", "tcp://198.51.100.7:22", "ssh://far.example", "localhost", "[::1]:22",
                        "::1", ("198.51.100.7", 22), 7000):
            with self.subTest(address=address), self.assertRaises(RemoteBindRefused):
                Relay(address, environ={})

    def test_main_refuses_a_network_socket_and_writes_nothing_on_stdout(self) -> None:
        out, err = io.BytesIO(), io.StringIO()
        code = main(["relay", "--socket", "198.51.100.7:9"], environ={}, stdin=io.BytesIO(frames({"op": "ping"})),
                    stdout=out, stderr=err)
        self.assertEqual(code, EXIT_REFUSED)
        self.assertEqual(out.getvalue(), b"")
        self.assertIn("RemoteBindRefused", err.getvalue())

    def test_a_socket_directory_open_to_others_is_refused(self) -> None:
        daemon = support.SocketDaemon(self)
        os.chmod(daemon.directory, 0o750)
        self.addCleanup(os.chmod, daemon.directory, 0o700)
        with self.assertRaises(InsecureSocketPath):
            Relay(environ=daemon.environ)

    def test_a_socket_directory_of_any_mode_but_0700_is_refused(self) -> None:
        daemon = support.SocketDaemon(self)
        self.addCleanup(os.chmod, daemon.directory, 0o700)
        for mode in (0o500, 0o600, 0o300, 0o100, 0o1700):
            with self.subTest(mode=oct(mode)):
                os.chmod(daemon.directory, mode)
                with self.assertRaises(InsecureSocketPath):
                    Relay(environ=daemon.environ)
        os.chmod(daemon.directory, 0o700)
        self.assertEqual(Relay(environ=daemon.environ).handle({"op": "ping"}), {"ok": True, "op": "pong"})

    def test_a_file_where_the_socket_directory_should_be_is_refused(self) -> None:
        runtime = support.scratch_dir(self)
        path = os.path.join(runtime, "recursion")
        with open(path, "w", encoding="utf-8"):
            pass
        os.chmod(path, 0o700)
        with self.assertRaises(InsecureSocketPath):
            Relay(environ={"XDG_RUNTIME_DIR": runtime})

    def test_a_symlinked_socket_directory_is_refused(self) -> None:
        daemon = support.SocketDaemon(self)
        elsewhere = support.scratch_dir(self)
        os.symlink(daemon.directory, os.path.join(elsewhere, "recursion"))
        with self.assertRaises(InsecureSocketPath):
            Relay(environ={"XDG_RUNTIME_DIR": elsewhere})

    def test_a_socket_directory_of_another_uid_is_refused(self) -> None:
        daemon = support.SocketDaemon(self)
        with self.assertRaises(InsecureSocketPath):
            Relay(environ=daemon.environ, uid=os.geteuid() + 1)

    def test_a_directory_loosened_after_start_refuses_the_next_frame(self) -> None:
        daemon = support.SocketDaemon(self)
        relay = Relay(environ=daemon.environ)
        os.chmod(daemon.directory, 0o755)
        self.addCleanup(os.chmod, daemon.directory, 0o700)
        reply = relay.handle({"op": "control", "method": "run.list", "params": {}, "id": 9})
        self.assertEqual(reply, codec.relay_error(codec.UNAVAILABLE, delivered=False,
                                                  detail="connect:InsecureSocketPath", request={"id": 9}))
        self.assertEqual(daemon.seen, [])

    def test_no_daemon_running_means_not_delivered(self) -> None:
        runtime = support.scratch_dir(self)
        environ = {"XDG_RUNTIME_DIR": runtime, "XDG_STATE_HOME": os.path.join(runtime, "state")}
        reply = Relay(environ=environ).handle({"op": "control", "method": "run.list",
                                                                     "params": {}})
        self.assertEqual(reply["error"], {"code": codec.UNAVAILABLE, "layer": "relay", "delivered": False,
                                          "detail": "connect:FileNotFoundError"})

    def test_a_daemon_that_has_gone_means_not_delivered(self) -> None:
        daemon = support.SocketDaemon(self)
        relay = Relay(environ=daemon.environ)
        daemon.server.close()
        reply = relay.handle({"op": "control", "method": "run.list", "params": {}})
        self.assertIs(reply["error"]["delivered"], False)
        self.assertEqual(reply["error"]["detail"], "connect:FileNotFoundError")

    def test_an_answer_lost_after_the_write_is_delivery_unknown(self) -> None:
        daemon = support.SocketDaemon(self)

        class LostAnswer:
            def __init__(self, path, timeout_s):
                pass

            def request(self, message):
                raise ConnectionResetError("the daemon went away mid-answer")

            def close(self):
                pass

        reply = Relay(environ=daemon.environ, connect=LostAnswer).handle(
            {"op": "control", "method": "run.stop", "params": {"run_id": "r"}})
        self.assertEqual(reply["error"]["delivered"], "unknown")
        self.assertEqual(reply["error"]["detail"], "request:ConnectionResetError")

    def test_a_frame_that_cannot_be_framed_again_is_refused_unsent(self) -> None:
        daemon = support.SocketDaemon(self)
        relay = Relay(environ=daemon.environ)
        # json.loads accepts NaN, Infinity and a lone surrogate written as an escape; encode_frame does not.
        for body in (b'{"op":"control","method":"run.list","params":{"x":NaN},"id":1}',
                     b'{"op":"control","method":"run.list","params":{"x":-Infinity},"id":1}',
                     b'{"op":"control","method":"run.list","params":{"x":"\\ud800"},"id":1}'):
            with self.subTest(body=body):
                reply = relay.handle(codec.read_message(io.BytesIO(HEADER.pack(len(body)) + body)))
                self.assertEqual((reply["error"]["code"], reply["error"]["delivered"], reply["id"]),
                                 ("bad_frame", False, 1))
        # A caller handing the relay a value JSON has no form for (a set) is refused the same way.
        reply = relay.handle({"op": "control", "method": "run.list", "params": {"x": {1, 2}}, "id": 2})
        self.assertEqual((reply["error"]["code"], reply["error"]["delivered"], reply["error"]["detail"]),
                         ("bad_frame", False, "frame_not_encodable:TypeError"))
        self.assertEqual(daemon.seen, [])

    def test_a_daemon_answer_that_cannot_be_framed_again_is_typed_and_delivered(self) -> None:
        daemon = support.SocketDaemon(self)  # its socket directory; the answer comes from the client below

        class NotEncodable:
            def __init__(self, path, timeout_s):
                pass

            def request(self, message):
                return {"ok": True, "result": {"code": "ok", "value": float("nan")}}

            def close(self):
                pass

        out = io.BytesIO()
        code = Relay(environ=daemon.environ, connect=NotEncodable).serve(
            io.BytesIO(frames({"op": "control", "method": "run.list", "params": {}, "id": 7})), out)
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(read_all(out.getvalue()),
                         [codec.relay_error("response_not_encodable", delivered=True, request={"id": 7})])

    def test_the_relays_own_answers_can_always_be_written(self) -> None:
        daemon = support.SocketDaemon(self)
        out = io.BytesIO()
        bodies = (b'{"op":"relay_\\ud800x","id":1}', b'{"op":"relay_hello","relay_version":1,"id":"\\udfff"}',
                  b'{"op":"control","method":"run.list","params":{},"id":"\\ud800"}')
        stream = b"".join(HEADER.pack(len(body)) + body for body in bodies)
        self.assertEqual(Relay(environ=daemon.environ).serve(io.BytesIO(stream), out), EXIT_OK)
        unknown_op, hello, control = read_all(out.getvalue())
        self.assertEqual((unknown_op["error"]["code"], unknown_op["error"]["detail"], unknown_op["id"]),
                         ("unknown_relay_op", "relay_\\ud800x", 1))
        self.assertTrue(unknown_op["error"]["detail"].isascii())
        self.assertNotIn("id", hello)  # an id that cannot be framed again is left out
        self.assertEqual(hello["relay"]["version"], 1)
        self.assertEqual((control["error"]["code"], control["error"]["delivered"]), ("bad_frame", False))
        self.assertNotIn("id", control)
        self.assertEqual(daemon.seen, [])

    def test_bad_constructor_arguments_are_refused(self) -> None:
        daemon = support.SocketDaemon(self)
        for kwargs in ({"timeout_s": "x"}, {"timeout_s": 0}, {"timeout_s": True}, {"max_frame_bytes": 0},
                       {"max_frame_bytes": 1.5}, {"connect": 7}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                Relay(environ=daemon.environ, **kwargs)

    def test_a_request_too_large_for_a_frame_is_refused_before_connecting(self) -> None:
        daemon = support.SocketDaemon(self)
        connects: list[str] = []

        def connect(path, timeout_s):
            connects.append(path)
            raise AssertionError("must not connect")

        relay = Relay(environ=daemon.environ, connect=connect, max_frame_bytes=128, rights=["read", "run"])
        reply = relay.handle({"op": "control", "method": "run.start", "params": {"prompt": "x" * 512}})
        self.assertEqual(reply["error"]["code"], "bad_frame")
        self.assertIs(reply["error"]["delivered"], False)
        self.assertEqual(connects, [])

    def test_a_request_that_is_not_an_object_is_refused_unsent(self) -> None:
        # serve() hands handle() only objects; a direct caller may not, and nothing can have been sent.
        daemon = support.SocketDaemon(self)
        relay = Relay(environ=daemon.environ)
        for request in ([1, 2], "run.list", None, 7):
            with self.subTest(request=request):
                self.assertEqual(relay.handle(request),  # type: ignore[arg-type]
                                 codec.relay_error("bad_frame", delivered=False, detail="frame_not_object"))
        self.assertEqual(daemon.seen, [])


@unittest.skipUnless(POSIX, "the relay needs the AF_UNIX control socket (POSIX)")
class RelayStreamTests(unittest.TestCase):
    def test_frames_are_answered_in_order_and_end_of_input_ends_cleanly(self) -> None:
        daemon = support.SocketDaemon(self)
        out = io.BytesIO()
        code = Relay(environ=daemon.environ).serve(
            io.BytesIO(frames({"op": "ping", "id": 1}, {"op": "control", "method": "run.list", "params": {}, "id": 2})),
            out)
        self.assertEqual(code, EXIT_OK)
        replies = read_all(out.getvalue())
        self.assertEqual([reply["id"] for reply in replies], [1, 2])
        self.assertEqual(replies[0]["op"], "pong")

    def test_a_length_over_the_limit_ends_the_session_unread(self) -> None:
        daemon = support.SocketDaemon(self)
        out = io.BytesIO()
        code = Relay(environ=daemon.environ).serve(io.BytesIO(HEADER.pack(MAX_FRAME_BYTES + 1) + b"{}"), out)
        self.assertEqual(code, EXIT_STREAM)
        self.assertEqual(read_all(out.getvalue()),
                         [codec.relay_error("bad_frame", delivered=False, detail="frame_too_large")])
        self.assertEqual(daemon.seen, [])

    def test_a_truncated_frame_ends_the_session(self) -> None:
        daemon = support.SocketDaemon(self)
        out = io.BytesIO()
        code = Relay(environ=daemon.environ).serve(io.BytesIO(HEADER.pack(50) + b'{"op": "pi'), out)
        self.assertEqual(code, EXIT_STREAM)
        self.assertEqual(daemon.seen, [])

    def test_a_frame_that_is_not_json_ends_the_session(self) -> None:
        daemon = support.SocketDaemon(self)
        out = io.BytesIO()
        body = b"not json"
        code = Relay(environ=daemon.environ).serve(io.BytesIO(HEADER.pack(len(body)) + body), out)
        self.assertEqual(code, EXIT_STREAM)
        self.assertEqual(read_all(out.getvalue())[0]["error"]["detail"], "frame_not_json")

    def test_a_refused_frame_is_answered_and_the_next_sound_frame_is_still_served(self) -> None:
        # A whole frame holding a value the socket refuses on read (NaN, a number too large for a float) has a
        # sound boundary: it is answered "not delivered" with its id, nothing is sent, and the session goes on.
        daemon = support.SocketDaemon(self)
        out = io.BytesIO()
        bodies = (b'{"op":"control","method":"run.list","params":{"x":NaN},"id":1}',
                  b'{"op":"control","method":"run.list","params":{"x":1e999},"id":2}')
        stream = b"".join(HEADER.pack(len(body)) + body for body in bodies)
        stream += frames({"op": "control", "method": "run.list", "params": {}, "id": 3})
        code = Relay(environ=daemon.environ).serve(io.BytesIO(stream), out)
        self.assertEqual(code, EXIT_OK)
        first, second, sound = read_all(out.getvalue())
        for reply, request_id in ((first, 1), (second, 2)):
            self.assertEqual((reply["error"]["code"], reply["error"]["delivered"], reply["id"]),
                             ("bad_frame", False, request_id))
        self.assertEqual(sound["id"], 3)
        self.assertNotEqual(sound.get("error", {}).get("layer"), "relay")
        self.assertEqual([message["id"] for message in daemon.seen], [3])

    def test_a_relay_answer_too_large_for_the_frame_is_not_delivered(self) -> None:
        daemon = support.SocketDaemon(self)
        out = io.BytesIO()
        hello = {"op": "relay_hello", "relay_version": 1, "id": "i" * 400}  # fits 512 bytes; its answer does not
        code = Relay(environ=daemon.environ, max_frame_bytes=512).serve(io.BytesIO(frames(hello)), out)
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(read_all(out.getvalue()),
                         [codec.relay_error("response_too_large", delivered=False, request=hello)])
        self.assertEqual(daemon.seen, [])

    def test_an_answer_too_large_for_the_relays_frame_is_reported_as_delivered(self) -> None:
        daemon = support.SocketDaemon(self, handler=lambda message, conn: {"ok": True, "result": {"code": "ok",
                                                                                                    "blob": "y" * 2048}})
        out = io.BytesIO()
        code = Relay(environ=daemon.environ, max_frame_bytes=512).serve(
            io.BytesIO(frames({"op": "control", "method": "run.list", "params": {}, "id": 5})), out)
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(read_all(out.getvalue()),
                         [codec.relay_error("response_too_large", delivered=True, request={"id": 5})])
        self.assertEqual(len(daemon.seen), 1)


@unittest.skipUnless(POSIX, "the relay needs the AF_UNIX control socket (POSIX)")
class RelayMainTests(unittest.TestCase):
    def test_a_key_used_for_another_command_is_refused_before_any_frame(self) -> None:
        daemon = support.SocketDaemon(self)
        out, err = io.BytesIO(), io.StringIO()
        code = main(["relay"], environ={**daemon.environ, "SSH_ORIGINAL_COMMAND": "scp -t /tmp"},
                    stdin=io.BytesIO(frames({"op": "control", "method": "run.list", "params": {}})), stdout=out,
                    stderr=err)
        self.assertEqual(code, EXIT_REFUSED)
        self.assertEqual(out.getvalue(), b"")
        self.assertEqual(daemon.seen, [])

    def test_the_relay_command_is_served_and_labelled_from_sshd(self) -> None:
        daemon = support.SocketDaemon(self)
        out = io.BytesIO()
        environ = {**daemon.environ, "SSH_ORIGINAL_COMMAND": RELAY_COMMAND,
                   "SSH_CONNECTION": "198.51.100.7 50022 192.0.2.10 22"}
        code = main(["relay", "--key-label", "laptop"], environ=environ,
                    stdin=io.BytesIO(frames({"op": "relay_hello", "relay_version": 1},
                                            {"op": "control", "method": "run.list", "params": {}, "id": 1})),
                    stdout=out, stderr=io.StringIO())
        self.assertEqual(code, EXIT_OK)
        hello, answer = read_all(out.getvalue())
        self.assertEqual(hello["relay"], {"version": 1, "transport": "ssh", "key_label": "laptop",
                                          "client_address": "198.51.100.7"})
        self.assertEqual(answer["result"]["method"], "run.list")

    def test_a_bad_key_label_is_refused(self) -> None:
        daemon = support.SocketDaemon(self)
        for label in ("lap top;", "laptop\n", "-laptop", "x" * 65):
            with self.subTest(label=label):
                out = io.BytesIO()
                code = main(["relay", "--key-label", label], environ=daemon.environ, stdin=io.BytesIO(),
                            stdout=out, stderr=io.StringIO())
                self.assertEqual(code, EXIT_REFUSED)
                self.assertEqual(out.getvalue(), b"")

    def test_a_usage_error_writes_nothing_on_stdout(self) -> None:
        out, err = io.BytesIO(), io.StringIO()
        self.assertEqual(main(["shell"], environ={}, stdin=io.BytesIO(), stdout=out, stderr=err), EXIT_REFUSED)
        self.assertEqual(out.getvalue(), b"")
        self.assertIn("usage error", err.getvalue())

    def test_a_runtime_directory_that_is_a_file_is_refused_not_raised(self) -> None:
        runtime = os.path.join(support.scratch_dir(self), "runtime-file")
        with open(runtime, "w", encoding="ascii"):
            pass
        out, err = io.BytesIO(), io.StringIO()
        code = main(["relay"], environ={"XDG_RUNTIME_DIR": runtime}, stdin=io.BytesIO(frames({"op": "ping"})),
                    stdout=out, stderr=err)
        self.assertEqual(code, EXIT_REFUSED)
        self.assertEqual(out.getvalue(), b"")
        self.assertIn("recursion relay: refused: NotADirectoryError", err.getvalue())


class Trickle(io.RawIOBase):
    """A pipe that hands over one byte per read and takes one byte per write, as a pipe may."""

    def __init__(self, data: bytes = b"") -> None:
        self._data, self.taken = bytearray(data), bytearray()

    def readable(self) -> bool:
        return True

    def writable(self) -> bool:
        return True

    def read(self, n: int = -1) -> bytes:
        chunk, self._data[:1] = bytes(self._data[:1]), b""
        return chunk

    def write(self, data) -> int:
        self.taken += bytes(data)[:1]
        return 1


class CodecTests(unittest.TestCase):
    def test_a_frame_split_into_single_bytes_is_read_and_written_whole(self) -> None:
        message = {"op": "control", "method": "run.list", "params": {"after": None}, "id": 7}
        self.assertEqual(codec.read_message(Trickle(encode_frame(message))), message)
        out = Trickle()
        codec.write_message(out, message)
        self.assertEqual(bytes(out.taken), encode_frame(message))

    def test_end_of_input_inside_a_frame_is_truncated_and_between_frames_is_clean(self) -> None:
        self.assertIsNone(codec.read_message(io.BytesIO(b"")))
        with self.assertRaises(FrameError):
            codec.read_message(io.BytesIO(encode_frame({"op": "ping"})[:-1]))
        with self.assertRaises(FrameError):
            codec.read_message(io.BytesIO(b"\x00\x00"))

    def test_only_the_relays_own_errors_are_read_as_relay_errors(self) -> None:
        self.assertIsNotNone(codec.relay_error_of(codec.relay_error("x", delivered="unknown")))
        for reply in ({"ok": False, "error": {"code": "x", "layer": "relay", "delivered": 0}},
                      {"ok": False, "error": {"code": "x", "layer": "relay", "delivered": 1}},
                      {"ok": False, "error": {"code": "x", "layer": "relay", "delivered": "maybe"}},
                      {"ok": True, "error": {"code": "x", "layer": "relay", "delivered": False}},
                      {"ok": False, "error": {"code": "x", "layer": "handler", "delivered": False}}):
            with self.subTest(reply=reply):
                self.assertIsNone(codec.relay_error_of(reply))
        with self.assertRaises(ValueError):
            codec.relay_error("x", delivered=0)


class NoListenerTests(unittest.TestCase):
    """A guard over the package's own source: it opens no listener and makes no network socket."""

    FORBIDDEN_MODULES = {"socket", "socketserver", "http.server", "asyncio", "ssl", "selectors"}
    FORBIDDEN_CALLS = {"bind", "listen", "accept", "create_server", "create_connection", "start_server"}

    def test_remote_sources_open_no_listener_and_import_no_socket_module(self) -> None:
        sources = sorted(path for path in REMOTE_DIR.glob("*.py"))
        self.assertTrue(sources)
        for path in sources:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        self.assertNotIn(alias.name, self.FORBIDDEN_MODULES, f"{path.name}:{node.lineno}")
                if isinstance(node, ast.ImportFrom):
                    self.assertNotIn(node.module, self.FORBIDDEN_MODULES, f"{path.name}:{node.lineno}")
                if isinstance(node, ast.Attribute):
                    self.assertNotIn(node.attr, self.FORBIDDEN_CALLS, f"{path.name}:{node.lineno}")


DEVICE_REFUSED = {"config.set": "all", "plugins.install": "all", "daemon.shutdown": "all",
                  "credentials.read": "all", "run.start": "run", "approval.answer": "approve",
                  "workspace.publish": "manage"}


def control(method, n=None) -> dict:
    message = {"op": "control", "method": method, "params": {}}
    if n is not None:
        message["id"] = n
    return message


@unittest.skipUnless(POSIX, "the relay needs the AF_UNIX control socket (POSIX)")
class RelayRightsTests(unittest.TestCase):
    def test_a_device_with_the_default_rights_reaches_the_read_methods_and_stop(self) -> None:
        daemon = support.SocketDaemon(self)
        relay = Relay(environ=daemon.environ, device_id="dev_phone")
        self.assertEqual(relay.rights, ("read", "run.stop"))
        sent = [control(method, n) for n, method in enumerate(("run.list", "run.status", "events.read",
                                                               "session.list", "run.stop"))]
        for request in sent:
            with self.subTest(method=request["method"]):
                reply = relay.handle(request)
                self.assertEqual(reply["result"]["method"], request["method"])
        self.assertEqual(relay.handle({"op": "ping"}), {"ok": True, "op": "pong"})
        self.assertEqual(daemon.seen, sent + [{"op": "ping"}])

    def test_a_device_with_the_default_rights_is_refused_by_the_relay_before_any_connection(self) -> None:
        daemon = support.SocketDaemon(self)
        events: list[dict] = []
        relay = Relay(environ=daemon.environ, device_id="dev_phone", record=events.append)
        for n, (method, right) in enumerate(DEVICE_REFUSED.items()):
            with self.subTest(method=method):
                reply = relay.handle(control(method, n))
                self.assertEqual(reply["id"], n)
                self.assertEqual({key: reply["error"][key] for key in ("code", "layer", "delivered", "method",
                                                                       "needs")},
                                 {"code": "scope_refused", "layer": "relay", "delivered": False, "method": method,
                                  "needs": right})
                self.assertEqual(reply["error"]["detail"],
                                 "method_not_in_table" if right == "all" else f"needs_{right}")
        self.assertEqual(daemon.server.accepted, 0)
        self.assertEqual(daemon.seen, [])
        self.assertEqual([(event["refused"], event["method"], event["needs"]) for event in events],
                         [("scope_refused", method, right) for method, right in DEVICE_REFUSED.items()])

    def test_an_op_that_is_not_control_needs_all_four_rights(self) -> None:
        daemon = support.SocketDaemon(self)
        reply = Relay(environ=daemon.environ, device_id="dev_phone").handle({"op": "x", "id": 2})
        self.assertEqual((reply["error"]["code"], reply["error"]["needs"], reply["error"]["method"]),
                         ("scope_refused", "all", "op:x"))
        self.assertEqual(daemon.server.accepted, 0)

    def test_a_key_that_names_no_device_and_no_rights_holds_watch_and_stop(self) -> None:
        daemon = support.SocketDaemon(self)
        relay = Relay(environ=daemon.environ)
        self.assertEqual(relay.rights, ("read", "run.stop"))
        for n, method in enumerate(("config.set", "plugins.install", "daemon.shutdown", "credentials.read",
                                    "run.start", "workspace.publish")):
            with self.subTest(method=method):
                reply = relay.handle(control(method, n))
                self.assertEqual((reply["error"]["code"], reply["error"]["delivered"], reply["error"]["method"]),
                                 ("scope_refused", False, method))
        self.assertEqual(daemon.server.accepted, 0)
        self.assertEqual(daemon.seen, [])
        self.assertEqual(relay.handle(control("run.list", 10))["result"]["method"], "run.list")
        self.assertEqual(relay.handle(control("run.stop", 11))["result"]["method"], "run.stop")
        self.assertEqual(relay.grant()["rights"], ["read", "run.stop"])

    def test_the_relay_line_of_the_readme_holds_watch_and_stop(self) -> None:
        daemon = support.SocketDaemon(self)
        out, err = io.BytesIO(), io.StringIO()
        environ = {**daemon.environ, "SSH_ORIGINAL_COMMAND": RELAY_COMMAND,
                   "SSH_CONNECTION": "198.51.100.7 50022 192.0.2.10 22"}
        sent = [control(method, n) for n, method in enumerate(("config.set", "plugins.install", "daemon.shutdown",
                                                               "credentials.read"))]
        code = main(["relay", "--key-label", "laptop"], environ=environ,
                    stdin=io.BytesIO(frames({"op": "relay_hello", "relay_version": 1}, *sent)), stdout=out, stderr=err)
        self.assertEqual(code, EXIT_OK)
        hello, *replies = read_all(out.getvalue())
        self.assertEqual(hello["grant"]["rights"], ["read", "run.stop"])
        self.assertEqual([reply["error"]["code"] for reply in replies], ["scope_refused"] * 4)
        self.assertEqual(daemon.server.accepted, 0)
        self.assertEqual(daemon.seen, [])

    def test_full_reach_only_where_the_forced_command_names_all_four(self) -> None:
        daemon = support.SocketDaemon(self)
        relay = Relay(environ=daemon.environ, rights=["read", "run", "approve", "manage"])
        self.assertEqual(relay.rights, ("read", "run", "approve", "manage"))
        self.assertEqual(relay.handle(control("config.set", 1))["result"]["method"], "config.set")
        self.assertEqual(daemon.seen, [control("config.set", 1)])
        out = io.BytesIO()
        code = main(["relay", "--rights", "read,run,approve,manage"], environ=daemon.environ,
                    stdin=io.BytesIO(frames({"op": "relay_hello", "relay_version": 1}, control("daemon.shutdown", 2))),
                    stdout=out, stderr=io.StringIO())
        self.assertEqual(code, EXIT_OK)
        hello, answer = read_all(out.getvalue())
        self.assertEqual(hello["grant"]["rights"], ["read", "run", "approve", "manage"])
        self.assertEqual(answer["result"]["method"], "daemon.shutdown")

    def test_a_key_with_fewer_rights_named_by_the_owner_is_held_to_them(self) -> None:
        daemon = support.SocketDaemon(self)
        relay = Relay(environ=daemon.environ, rights=["read"])
        self.assertEqual(relay.handle(control("run.stop", 1))["error"]["needs"], "run")
        self.assertEqual(relay.handle(control("run.list", 2))["result"]["method"], "run.list")
        self.assertEqual([message["id"] for message in daemon.seen], [2])

    def test_a_device_is_held_to_what_pairing_allows(self) -> None:
        daemon = support.SocketDaemon(self)
        for words in (["manage"], ["read", "run"], ["approve"], ["stop"], ["session.stop"], "read", []):
            with self.subTest(words=words), self.assertRaises(ValueError):
                Relay(environ=daemon.environ, device_id="dev_phone", rights=words)
        self.assertEqual(Relay(environ=daemon.environ, device_id="dev_phone", rights=["run.list"]).rights,
                         ("run.list",))
        for device in ("", "dev phone", "x" * 65, 7, "dev;rm"):
            with self.subTest(device=device), self.assertRaises(ValueError):
                Relay(environ=daemon.environ, device_id=device)

    def test_hello_shows_the_keys_grant(self) -> None:
        daemon = support.SocketDaemon(self)
        hello = Relay(environ=daemon.environ, device_id="dev_phone", max_frames=5, window_s=2.5,
                      max_frame_bytes=None).handle({"op": "relay_hello", "relay_version": 1})
        self.assertEqual(hello["grant"], {"device": "dev_phone", "rights": ["read", "run.stop"],
                                          "caps": {"max_frames": 5, "window_s": 2.5, "max_frame_bytes": None}})


@unittest.skipUnless(POSIX, "the relay needs the AF_UNIX control socket (POSIX)")
class RelayCapsTests(unittest.TestCase):
    def test_over_the_frame_cap_the_relay_refuses_records_and_goes_on(self) -> None:
        daemon = support.SocketDaemon(self)
        clock, events = support.FakeClock(), []
        relay = Relay(environ=daemon.environ, max_frames=3, window_s=10.0, clock=clock, record=events.append)
        replies = [relay.handle(control("run.list", n)) for n in range(4)]
        self.assertEqual([reply.get("result", {}).get("method") for reply in replies[:3]], ["run.list"] * 3)
        self.assertEqual((replies[3]["error"]["code"], replies[3]["error"]["delivered"], replies[3]["id"]),
                         ("rate_limited", False, 3))
        self.assertEqual(daemon.server.accepted, 3)
        self.assertEqual(events, [{"refused": "rate_limited", "cap": "max_frames", "value": 3, "window_s": 10.0,
                                   "seen": 3}])
        clock.now += 9.5
        self.assertEqual(relay.handle(control("run.list", 4))["error"]["code"], "rate_limited")
        clock.now += 0.5  # the window has moved past the first three
        self.assertEqual(relay.handle(control("run.list", 5))["result"]["method"], "run.list")
        self.assertEqual([message["id"] for message in daemon.seen], [0, 1, 2, 5])

    def test_the_window_is_a_setting(self) -> None:
        daemon = support.SocketDaemon(self)
        clock = support.FakeClock()
        relay = Relay(environ=daemon.environ, max_frames=1, window_s=2.0, clock=clock)
        self.assertIn("result", relay.handle(control("run.list", 1)))
        clock.now += 1.0
        self.assertEqual(relay.handle(control("run.list", 2))["error"]["code"], "rate_limited")
        clock.now += 1.0
        self.assertIn("result", relay.handle(control("run.list", 3)))

    def test_the_first_value_of_the_frame_cap_is_used_when_none_is_given(self) -> None:
        from recursion_core.remote.relay import RELAY_MAX_FRAMES
        daemon = support.SocketDaemon(self)
        relay = Relay(environ=daemon.environ, clock=support.FakeClock())
        replies = [relay.handle({"op": "ping", "id": n}) for n in range(RELAY_MAX_FRAMES + 1)]
        self.assertTrue(all(reply.get("op") == "pong" for reply in replies[:-1]))
        self.assertEqual(replies[-1]["error"]["code"], "rate_limited")
        self.assertEqual(daemon.server.accepted, RELAY_MAX_FRAMES)

    def test_no_limit_is_a_value(self) -> None:
        from recursion_core.remote.relay import RELAY_MAX_FRAMES
        daemon = support.SocketDaemon(self)
        relay = Relay(environ=daemon.environ, max_frames=None, clock=support.FakeClock())
        count = RELAY_MAX_FRAMES + 50
        replies = [relay.handle({"op": "ping", "id": n}) for n in range(count)]
        self.assertTrue(all(reply.get("op") == "pong" for reply in replies))
        self.assertEqual(daemon.server.accepted, count)

    def test_relay_ops_and_refused_frames_are_not_counted(self) -> None:
        daemon = support.SocketDaemon(self)
        relay = Relay(environ=daemon.environ, device_id="dev_phone", max_frames=1, clock=support.FakeClock(),
                      max_frame_bytes=256)
        for n in range(3):
            self.assertEqual(relay.handle({"op": "relay_hello", "relay_version": 1, "id": n})["op"], "relay_hello")
        self.assertEqual(relay.handle(control("config.set", 3))["error"]["code"], "scope_refused")
        big = {**control("run.list", 4), "params": {"x": "y" * 512}}
        self.assertEqual(relay.handle(big)["error"]["code"], "bad_frame")
        self.assertIn("result", relay.handle(control("run.list", 5)))
        self.assertEqual(relay.handle(control("run.list", 6))["error"]["code"], "rate_limited")

    def test_a_frame_over_the_size_cap_is_refused_and_recorded(self) -> None:
        daemon = support.SocketDaemon(self)
        events: list[dict] = []
        relay = Relay(environ=daemon.environ, max_frame_bytes=128, record=events.append)
        reply = relay.handle({**control("run.list", 1), "params": {"x": "y" * 512}})
        self.assertEqual((reply["error"]["code"], reply["error"]["delivered"]), ("bad_frame", False))
        self.assertEqual(events, [{"refused": "bad_frame", "cap": "max_frame_bytes", "value": 128}])
        out = io.BytesIO()
        self.assertEqual(relay.serve(io.BytesIO(HEADER.pack(129) + b"{}"), out), EXIT_STREAM)
        self.assertEqual(events, [{"refused": "bad_frame", "cap": "max_frame_bytes", "value": 128}] * 2)
        self.assertEqual(daemon.server.accepted, 0)

    def test_no_size_limit_of_the_relays_own_is_a_value(self) -> None:
        daemon = support.SocketDaemon(self)
        relay = Relay(environ=daemon.environ, max_frame_bytes=None)
        reply = relay.handle({**control("run.list", 1), "params": {"x": "y" * 4096}})
        self.assertEqual(reply["result"]["params"], {"x": "y" * 4096})
        small = Relay(environ=daemon.environ, max_frame_bytes=1024)
        self.assertEqual(small.handle({**control("run.list", 2), "params": {"x": "y" * 4096}})["error"]["code"],
                         "bad_frame")

    def test_a_record_that_raises_changes_no_answer(self) -> None:
        daemon = support.SocketDaemon(self)

        def record(event):
            raise RuntimeError("full disk")

        relay = Relay(environ=daemon.environ, max_frames=0, record=record)
        self.assertEqual(relay.handle(control("run.list", 1))["error"]["code"], "rate_limited")

    def test_bad_cap_arguments_are_refused(self) -> None:
        daemon = support.SocketDaemon(self)
        for kwargs in ({"max_frames": -1}, {"max_frames": 1.5}, {"max_frames": True}, {"max_frames": "3"},
                       {"window_s": 0}, {"window_s": -1}, {"window_s": float("nan")}, {"window_s": float("inf")},
                       {"window_s": True}, {"window_s": "1"}, {"clock": 7}, {"record": 7}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                Relay(environ=daemon.environ, **kwargs)


@unittest.skipUnless(POSIX, "the relay needs the AF_UNIX control socket (POSIX)")
class RelayMainRightsAndCapsTests(unittest.TestCase):
    def run_main(self, args, daemon, *messages):
        out, err = io.BytesIO(), io.StringIO()
        code = main(["relay", *args], environ=daemon.environ, stdin=io.BytesIO(frames(*messages)), stdout=out,
                    stderr=err)
        return code, out.getvalue(), err.getvalue()

    def test_a_device_key_on_the_forced_command_is_held_to_watch_and_stop(self) -> None:
        daemon = support.SocketDaemon(self)
        code, out, err = self.run_main(["--device", "dev_phone"], daemon, control("run.list", 1),
                                       control("config.set", 2), control("run.stop", 3))
        self.assertEqual(code, EXIT_OK)
        listed, refused, stopped = read_all(out)
        self.assertEqual(listed["result"]["method"], "run.list")
        self.assertEqual((refused["id"], refused["error"]["code"]), (2, "scope_refused"))
        self.assertEqual(stopped["result"]["method"], "run.stop")
        self.assertEqual([message["id"] for message in daemon.seen], [1, 3])
        self.assertIn('"refused": "scope_refused"', err)

    def test_rights_named_on_the_forced_command(self) -> None:
        daemon = support.SocketDaemon(self)
        code, out, _ = self.run_main(["--device", "dev_phone", "--rights", "run.list"], daemon,
                                     control("run.list", 1), control("run.stop", 2))
        self.assertEqual(code, EXIT_OK)
        self.assertEqual([reply.get("error", {}).get("needs") for reply in read_all(out)], [None, "run"])
        for bad in ("manage", "read,run", "stop", "", "read,,run.stop"):
            with self.subTest(rights=bad):
                code, out, err = self.run_main(["--device", "dev_phone", "--rights", bad], daemon,
                                               control("run.list"))
                self.assertEqual(code, EXIT_REFUSED)
                self.assertEqual(out, b"")
                self.assertIn("recursion relay: refused", err)

    def test_the_frame_cap_on_the_command_line_refuses_records_on_stderr_and_frames_only_on_stdout(self) -> None:
        daemon = support.SocketDaemon(self)
        code, out, err = self.run_main(["--max-frames", "2", "--window-s", "3600"], daemon,
                                       *[control("run.list", n) for n in range(3)])
        self.assertEqual(code, EXIT_OK)
        replies = read_all(out)
        self.assertEqual([reply.get("error", {}).get("code") for reply in replies], [None, None, "rate_limited"])
        self.assertEqual(len(daemon.seen), 2)
        self.assertIn('recursion relay: record: {"cap": "max_frames"', err)

    def test_none_on_the_command_line_is_no_limit_and_is_said_once(self) -> None:
        daemon = support.SocketDaemon(self)
        code, out, err = self.run_main(["--max-frames", "none", "--max-frame-bytes", "none"], daemon,
                                       control("run.list", 1))
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(read_all(out)[0]["result"]["method"], "run.list")
        self.assertEqual(err.count("max_frames is none: no limit"), 1)
        self.assertEqual(err.count("max_frame_bytes is none: no limit"), 1)

    def test_a_bad_cap_on_the_command_line_is_refused_with_nothing_on_stdout(self) -> None:
        daemon = support.SocketDaemon(self)
        for args in (["--max-frames", "-1"], ["--max-frames", "x"], ["--max-frames", "1.5"], ["--max-frames", ""],
                     ["--window-s", "0"], ["--window-s", "nan"], ["--window-s", "abc"], ["--window-s", "inf"],
                     ["--max-frame-bytes", "0"], ["--max-frame-bytes", "-5"], ["--max-frame-bytes", "1e3"]):
            with self.subTest(args=args):
                code, out, err = self.run_main(args, daemon, control("run.list", 1))
                self.assertEqual(code, EXIT_REFUSED)
                self.assertEqual(out, b"")
                self.assertTrue(err.startswith("recursion relay: "))
        self.assertEqual(daemon.seen, [])


# Values the control socket refuses on read, each written before a later duplicate of its key.
HIDDEN_BY_A_DUPLICATE = (b"NaN", b"Infinity", b"1e999", b'"\\ud800"')


def hidden_body(value: bytes, request_id: bytes = b"7") -> bytes:
    return b'{"id":' + request_id + b',"op":"x","a":' + value + b',"a":1}'


def framed(body: bytes) -> bytes:
    return HEADER.pack(len(body)) + body


@unittest.skipUnless(POSIX, "the relay needs the AF_UNIX control socket (POSIX)")
class RelayRefusedBodyTests(unittest.TestCase):
    def test_a_refused_value_under_a_later_duplicate_key_is_never_forwarded(self) -> None:
        for value in HIDDEN_BY_A_DUPLICATE:
            with self.subTest(value=value):
                daemon = support.SocketDaemon(self)
                out = io.BytesIO()
                stream = framed(hidden_body(value)) + frames({"op": "control", "method": "run.list", "params": {},
                                                              "id": 8})
                self.assertEqual(Relay(environ=daemon.environ).serve(io.BytesIO(stream), out), EXIT_OK)
                refused, served = read_all(out.getvalue())
                self.assertEqual((refused["error"]["code"], refused["error"]["layer"], refused["error"]["delivered"],
                                  refused["id"]), ("bad_frame", "relay", False, 7))
                self.assertEqual((served["id"], served["result"]["method"]), (8, "run.list"))
                self.assertEqual(daemon.server.accepted, 1)
                self.assertEqual([message["id"] for message in daemon.seen], [8])

    def test_the_refused_body_keeps_only_its_id(self) -> None:
        for value in HIDDEN_BY_A_DUPLICATE:
            with self.subTest(value=value):
                message = codec.read_message(io.BytesIO(framed(hidden_body(value))))
                self.assertIsInstance(message, codec.RefusedFrame)
                self.assertEqual(message, {"id": 7})
        no_id = codec.read_message(io.BytesIO(framed(b'{"op":"x","a":NaN,"a":1}')))
        self.assertEqual(no_id, {})

    def test_a_refused_body_is_bad_frame_also_for_a_device_key(self) -> None:
        daemon = support.SocketDaemon(self)
        relay = Relay(environ=daemon.environ, device_id="dev_phone")
        reply = relay.handle(codec.read_message(io.BytesIO(framed(hidden_body(b"NaN")))))
        self.assertEqual((reply["error"]["code"], reply["error"]["delivered"], reply["id"]), ("bad_frame", False, 7))
        self.assertEqual(daemon.server.accepted, 0)

    def test_an_id_that_cannot_be_framed_again_is_left_out(self) -> None:
        daemon = support.SocketDaemon(self)
        out = io.BytesIO()
        body = b'{"id":"\\udfff","op":"x","a":NaN,"a":1}'
        self.assertEqual(Relay(environ=daemon.environ).serve(io.BytesIO(framed(body)), out), EXIT_OK)
        (reply,) = read_all(out.getvalue())
        self.assertEqual((reply["error"]["code"], reply["error"]["delivered"]), ("bad_frame", False))
        self.assertNotIn("id", reply)
        self.assertEqual(daemon.server.accepted, 0)

    def test_a_duplicate_key_alone_or_a_sound_surrogate_pair_is_forwarded_as_the_socket_reads_it(self) -> None:
        daemon = support.SocketDaemon(self)
        out = io.BytesIO()
        bodies = (b'{"id":1,"op":"control","method":"run.list","params":{"a":1,"a":2}}',
                  b'{"id":2,"op":"control","method":"run.list","params":{"a":"\\ud83d\\ude00","a":3}}',
                  b'{"id":3,"op":"control","method":"run.list","params":{"a":["\\ud83d\\ude00"]}}')
        stream = b"".join(framed(body) for body in bodies)
        self.assertEqual(Relay(environ=daemon.environ).serve(io.BytesIO(stream), out), EXIT_OK)
        self.assertEqual([reply["result"]["params"] for reply in read_all(out.getvalue())],
                         [{"a": 2}, {"a": 3}, {"a": ["\U0001f600"]}])
        self.assertEqual(daemon.server.accepted, 3)

    def test_a_lone_surrogate_in_a_list_or_a_key_under_a_duplicate_is_refused(self) -> None:
        daemon = support.SocketDaemon(self)
        out = io.BytesIO()
        bodies = (b'{"id":1,"op":"x","a":["\\ud800"],"a":1}', b'{"id":2,"op":"x","a":{"\\udc00":1},"a":1}')
        self.assertEqual(Relay(environ=daemon.environ).serve(io.BytesIO(b"".join(framed(b) for b in bodies)), out),
                         EXIT_OK)
        self.assertEqual([(reply["error"]["code"], reply["id"]) for reply in read_all(out.getvalue())],
                         [("bad_frame", 1), ("bad_frame", 2)])
        self.assertEqual(daemon.server.accepted, 0)


def run_session(daemon, args, *messages, environ=None):
    """One SSH session of the relay, as sshd starts it: its own main(), its own Relay."""
    out, err = io.BytesIO(), io.StringIO()
    code = main(["relay", *args], environ={**daemon.environ, "SSH_ORIGINAL_COMMAND": RELAY_COMMAND, **(environ or {})},
                stdin=io.BytesIO(frames(*messages)), stdout=out, stderr=err)
    return code, read_all(out.getvalue()), err.getvalue()


def relay_folder_of(daemon) -> str:
    return os.path.join(daemon.environ["XDG_STATE_HOME"], "recursion", "relay")


@unittest.skipUnless(POSIX, "the relay needs the AF_UNIX control socket (POSIX)")
class RelayKeyWindowTests(unittest.TestCase):
    def test_three_sessions_of_one_key_under_a_cap_of_two_forward_exactly_two(self) -> None:
        daemon = support.SocketDaemon(self)
        codes = []
        for session in range(3):
            code, replies, _ = run_session(daemon, ["--key-label", "laptop", "--max-frames", "2", "--window-s", "3600"],
                                           *[control("run.list", 10 * session + n) for n in range(3)])
            codes.append(code)
        self.assertEqual(codes, [EXIT_OK] * 3)
        self.assertEqual(len(daemon.seen), 2)
        self.assertEqual(daemon.server.accepted, 2)

    def test_the_window_of_a_device_key_is_shared_by_its_sessions(self) -> None:
        daemon = support.SocketDaemon(self)
        clock = support.FakeClock()
        first = Relay(environ=daemon.environ, device_id="dev_phone", max_frames=2, window_s=10.0, clock=clock)
        second = Relay(environ=daemon.environ, device_id="dev_phone", max_frames=2, window_s=10.0, clock=clock)
        other = Relay(environ=daemon.environ, device_id="dev_tablet", max_frames=2, window_s=10.0, clock=clock)
        self.assertIn("result", first.handle(control("run.list", 1)))
        self.assertIn("result", second.handle(control("run.list", 2)))
        refused = second.handle(control("run.list", 3))
        self.assertEqual((refused["error"]["code"], refused["error"]["delivered"]), ("rate_limited", False))
        self.assertEqual(first.handle(control("run.list", 4))["error"]["code"], "rate_limited")
        self.assertIn("result", other.handle(control("run.list", 5)))
        clock.now += 10.0  # the window has moved past both
        self.assertIn("result", first.handle(control("run.list", 6)))
        self.assertEqual([message["id"] for message in daemon.seen], [1, 2, 5, 6])

    def test_the_scope_of_the_cap_is_a_setting_and_session_gives_each_session_its_own(self) -> None:
        from recursion_core.remote.relay import RELAY_CAP_SCOPE
        self.assertEqual(RELAY_CAP_SCOPE, "key")
        daemon = support.SocketDaemon(self)
        for session in range(3):
            code, _, _ = run_session(daemon, ["--key-label", "laptop", "--max-frames", "2", "--window-s", "3600",
                                              "--cap-scope", "session"], *[control("run.list", n) for n in range(3)])
            self.assertEqual(code, EXIT_OK)
        self.assertEqual(len(daemon.seen), 6)
        self.assertEqual(Relay(environ=daemon.environ, cap_scope="session").cap_scope, "session")
        for bad in ("Key", "", None, 1, "all"):
            with self.subTest(cap_scope=bad), self.assertRaises(ValueError):
                Relay(environ=daemon.environ, cap_scope=bad)
        code, replies, err = run_session(daemon, ["--cap-scope", "node"], control("run.list", 1))
        self.assertEqual((code, replies), (EXIT_REFUSED, []))
        self.assertIn("recursion relay: usage error", err)

    def test_keys_with_other_names_have_windows_of_their_own(self) -> None:
        daemon = support.SocketDaemon(self)
        for label in ("laptop", "desk", None):
            args = ["--max-frames", "1", "--window-s", "3600"] + (["--key-label", label] if label else [])
            for n in range(2):
                run_session(daemon, args, control("run.list", n))
        self.assertEqual(len(daemon.seen), 3)
        self.assertEqual(sorted(os.listdir(relay_folder_of(daemon))),
                         ["refusals.jsonl", "window-label-desk.json", "window-label-laptop.json", "window-unnamed.json"])

    def test_a_session_waits_for_the_lock_of_the_keys_window(self) -> None:
        daemon = support.SocketDaemon(self)
        relay = Relay(environ=daemon.environ, key_label="laptop", max_frames=5)
        path = os.path.join(relay.state_folder, "window-label-laptop.json")
        done = threading.Event()
        with open(path, "a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)  # another session holds the window
            worker = threading.Thread(target=lambda: (relay.handle(control("run.list", 1)), done.set()), daemon=True)
            worker.start()
            self.assertFalse(done.wait(0.5))
            self.assertEqual(daemon.seen, [])
        self.assertTrue(done.wait(support.WATCHDOG_S))
        self.assertEqual(len(daemon.seen), 1)

    def test_a_time_later_than_now_is_dropped_from_the_window(self) -> None:
        daemon = support.SocketDaemon(self)
        clock = support.FakeClock(5.0)
        relay = Relay(environ=daemon.environ, key_label="laptop", max_frames=1, window_s=60.0, clock=clock)
        with open(os.path.join(relay.state_folder, "window-label-laptop.json"), "w", encoding="ascii") as handle:
            json.dump([900.0], handle)  # written before the clock started again
        self.assertIn("result", relay.handle(control("run.list", 1)))

    def test_a_window_that_cannot_be_read_refuses_the_frame_unsent(self) -> None:
        daemon = support.SocketDaemon(self)
        relay = Relay(environ=daemon.environ, key_label="laptop")
        os.mkdir(os.path.join(relay.state_folder, "window-label-laptop.json"))
        reply = relay.handle(control("run.list", 1))
        self.assertEqual((reply["error"]["code"], reply["error"]["delivered"], reply["id"]), ("rate_limited", False, 1))
        self.assertTrue(reply["error"]["detail"].startswith("window_unreadable:"))
        self.assertEqual(daemon.server.accepted, 0)

    def test_the_relay_folder_is_owner_only_and_a_loose_one_is_refused_at_start(self) -> None:
        daemon = support.SocketDaemon(self)
        relay = Relay(environ=daemon.environ)
        self.assertEqual(relay.state_folder, relay_folder_of(daemon))
        self.assertEqual(stat.S_IMODE(os.lstat(relay.state_folder).st_mode), 0o700)
        os.chmod(relay.state_folder, 0o755)
        with self.assertRaises(relay_store.RelayStateRefused):
            Relay(environ=daemon.environ)
        code, replies, err = run_session(daemon, [], control("run.list", 1))
        self.assertEqual((code, replies), (EXIT_REFUSED, []))
        self.assertIn("RelayStateRefused", err)
        self.assertEqual(daemon.seen, [])

    def test_the_state_folder_comes_from_the_one_rule_and_never_from_the_process_home(self) -> None:
        scratch = support.scratch_dir(self)
        self.assertEqual(state_directory({"XDG_STATE_HOME": scratch}), os.path.join(scratch, "recursion"))
        self.assertEqual(state_directory({"HOME": scratch}), os.path.join(scratch, ".local/state", "recursion"))
        with self.assertRaises(ValueError):
            state_directory({})
        daemon = support.SocketDaemon(self)
        bare = {"XDG_RUNTIME_DIR": daemon.environ["XDG_RUNTIME_DIR"]}
        with self.assertRaises(ValueError):
            Relay(environ=bare)
        relay = Relay(environ=bare, cap_scope="session", record_max=0)  # nothing to keep: no folder needed
        self.assertIsNone(relay.state_folder)
        self.assertEqual(relay.handle(control("run.list", 1))["result"]["method"], "run.list")

    def test_a_relay_folder_owned_by_another_uid_is_refused_at_start(self) -> None:
        daemon = support.SocketDaemon(self)
        Relay(environ=daemon.environ)  # the folder, made by this uid
        state = os.path.join(daemon.environ["XDG_STATE_HOME"], "recursion")
        other = os.geteuid() + 1
        with self.assertRaises(relay_store.RelayStateRefused) as caught:
            relay_store.relay_folder(state, uid=other)
        self.assertIn(f"is owned by uid {os.geteuid()}, not {other}", str(caught.exception))
        # No socket directory here (the daemon not running), so only the relay's own folder can refuse.
        bare = {"XDG_RUNTIME_DIR": support.scratch_dir(self), "XDG_STATE_HOME": daemon.environ["XDG_STATE_HOME"]}
        with self.assertRaises(relay_store.RelayStateRefused):
            Relay(environ=bare, uid=other)
        self.assertEqual(Relay(environ=bare).state_folder, relay_folder_of(daemon))  # its owner starts


# Sessions of one key started at the same time (RelayKeyWindowRaceTests). Each round starts RACE_SESSIONS
# sessions of a key of its own behind a barrier, each sending RACE_FRAMES frames under a cap of RACE_CAP
# in a window of an hour, and counts what the stand-in daemon saw. Every clock of these sessions sleeps
# RACE_PAUSE_S after it is read: a session held up between reading the clock and taking the window's
# lock, which is where a clock read outside the lock lets a frame through. RACE_ROUNDS rounds: with the
# clock read outside the lock, about three rounds in four forwarded more than the cap, on both
# interpreters, as processes and as threads, and at least 9 of 20 in every run, so that 20 rounds all at
# the cap by luck is a chance below one in a hundred thousand; with it read under the lock every round
# is at the cap. These are the test's own figures, not settings of the relay.
RACE_SESSIONS, RACE_FRAMES, RACE_CAP, RACE_ROUNDS, RACE_PAUSE_S = 8, 4, 3, 20, 0.002

RACE_CHILD = r"""
import io, json, sys, time
real = time.monotonic
def held_up():
    now = real()
    time.sleep(PAUSE)
    return now
time.monotonic = held_up  # before the relay is imported: its clock is this one
sys.path.insert(0, sys.argv[1])
from recursion_core.control_transport.framing import encode_frame
from recursion_core.remote.relay import main
environ, child, rounds, frames_each, cap = json.loads(sys.argv[2]), *map(int, sys.argv[3:7])
print("ready", flush=True)
for r in range(rounds):
    if sys.stdin.readline().strip() != f"go {r}":
        sys.exit(3)
    data = b"".join(encode_frame({"op": "control", "method": "run.list", "params": {}, "id": 1000 * r + 10 * child + n})
                    for n in range(frames_each))
    main(["relay", "--key-label", f"race-{r}", "--max-frames", str(cap), "--window-s", "3600"], environ=environ,
         stdin=io.BytesIO(data), stdout=io.BytesIO(), stderr=io.StringIO())
    print(f"done {r}", flush=True)
"""


def held_up_clock():
    def clock() -> float:
        now = time.monotonic()
        time.sleep(RACE_PAUSE_S)
        return now
    return clock


def forwarded_per_round(daemon) -> list[int]:
    counts = [0] * RACE_ROUNDS
    for message in daemon.seen:
        counts[message["id"] // 1000] += 1
    return counts


@unittest.skipUnless(POSIX, "the relay needs the AF_UNIX control socket (POSIX)")
class RelayKeyWindowRaceTests(unittest.TestCase):
    def test_sessions_of_one_key_at_the_same_time_as_processes_forward_exactly_the_cap(self) -> None:
        daemon = support.SocketDaemon(self)
        environ = {**daemon.environ, "SSH_ORIGINAL_COMMAND": RELAY_COMMAND}
        core = str(REMOTE_DIR.parents[1])
        code = RACE_CHILD.replace("PAUSE", repr(RACE_PAUSE_S))
        children = []
        for child in range(RACE_SESSIONS):  # a hard count of processes, each ended in the cleanup
            proc = subprocess.Popen([sys.executable, "-B", "-c", code, core, json.dumps(environ), str(child),
                                     str(RACE_ROUNDS), str(RACE_FRAMES), str(RACE_CAP)],
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            self.addCleanup(end_process, proc)
            children.append(proc)
        deadline = time.monotonic() + support.WATCHDOG_S * 3
        for proc in children:
            self.assertEqual(line_of(proc, deadline), "ready")
        for r in range(RACE_ROUNDS):
            for proc in children:  # every session is waiting on this line: the barrier
                proc.stdin.write(f"go {r}\n".encode("ascii"))
                proc.stdin.flush()
            for proc in children:
                self.assertEqual(line_of(proc, deadline), f"done {r}")
        self.assertEqual(forwarded_per_round(daemon), [RACE_CAP] * RACE_ROUNDS)

    def test_sessions_of_one_key_at_the_same_time_as_threads_forward_exactly_the_cap(self) -> None:
        daemon = support.SocketDaemon(self)
        for r in range(RACE_ROUNDS):
            relays = [Relay(environ=daemon.environ, key_label=f"race-{r}", max_frames=RACE_CAP, window_s=3600.0,
                            clock=held_up_clock()) for _ in range(RACE_SESSIONS)]
            barrier = threading.Barrier(RACE_SESSIONS, timeout=support.WATCHDOG_S)

            def session(relay, child, r=r, barrier=barrier):
                barrier.wait()
                for n in range(RACE_FRAMES):
                    relay.handle(control("run.list", 1000 * r + 10 * child + n))

            workers = [threading.Thread(target=session, args=(relay, child), daemon=True)
                       for child, relay in enumerate(relays)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(support.WATCHDOG_S)
            self.assertFalse(any(worker.is_alive() for worker in workers))
        self.assertEqual(forwarded_per_round(daemon), [RACE_CAP] * RACE_ROUNDS)


def line_of(proc, deadline: float) -> str:
    """The next line a child writes, waiting no longer than ``deadline`` (a hang fails, never waits for ever)."""
    remaining = deadline - time.monotonic()
    ready, _, _ = select.select([proc.stdout], [], [], max(remaining, 0.0))
    if not ready:
        raise AssertionError(f"child {proc.pid} wrote nothing in time")
    return proc.stdout.readline().decode("ascii").strip()


def end_process(proc) -> None:
    for stream in (proc.stdin, proc.stdout):
        try:
            stream.close()
        except OSError:
            pass
    if proc.poll() is None:
        proc.kill()
    proc.wait(support.WATCHDOG_S)


@unittest.skipUnless(POSIX, "the relay needs the AF_UNIX control socket (POSIX)")
class RelayRecordTests(unittest.TestCase):
    def test_each_refusal_of_the_rights_and_the_caps_is_recorded_on_the_daemons_machine(self) -> None:
        daemon = support.SocketDaemon(self)
        environ = {"SSH_CONNECTION": "198.51.100.7 50022 192.0.2.10 22"}
        code, replies, _ = run_session(daemon, ["--device", "dev_phone", "--max-frames", "1", "--window-s", "3600"],
                                       control("config.set", 1), control("run.list", 2), control("run.list", 3),
                                       environ=environ)
        self.assertEqual(code, EXIT_OK)
        self.assertEqual([reply.get("error", {}).get("code") for reply in replies], ["scope_refused", None, "rate_limited"])
        record = relay_store.read_refusals(relay_folder_of(daemon))
        self.assertEqual([(entry["refused"], entry["key"], entry["device"], entry["client_address"]) for entry in record],
                         [("scope_refused", "device-dev_phone", "dev_phone", "198.51.100.7"),
                          ("rate_limited", "device-dev_phone", "dev_phone", "198.51.100.7")])
        self.assertEqual((record[0]["method"], record[0]["needs"]), ("config.set", "all"))
        self.assertEqual((record[1]["cap"], record[1]["value"]), ("max_frames", 1))
        self.assertTrue(all(isinstance(entry["at"], float) for entry in record))
        mode = stat.S_IMODE(os.lstat(os.path.join(relay_folder_of(daemon), "refusals.jsonl")).st_mode)
        self.assertEqual(mode, 0o600)

    def test_a_frame_over_the_size_cap_is_recorded_on_the_daemons_machine(self) -> None:
        daemon = support.SocketDaemon(self)
        relay = Relay(environ=daemon.environ, max_frame_bytes=128)
        relay.handle({**control("run.list", 1), "params": {"x": "y" * 512}})
        relay.serve(io.BytesIO(HEADER.pack(129) + b"{}"), io.BytesIO())
        self.assertEqual([(entry["refused"], entry["cap"]) for entry in relay_store.read_refusals(relay.state_folder)],
                         [("bad_frame", "max_frame_bytes")] * 2)

    def test_a_flood_of_refusals_keeps_at_most_the_records_setting_newest_last(self) -> None:
        from recursion_core.remote.relay import RELAY_RECORD_MAX
        self.assertEqual(RELAY_RECORD_MAX, 1000)
        daemon = support.SocketDaemon(self)
        relay = Relay(environ=daemon.environ, device_id="dev_phone", record_max=50)
        for n in range(120):
            relay.handle(control(f"m{n}", n))
        record = relay_store.read_refusals(relay.state_folder)
        self.assertEqual([entry["method"] for entry in record], [f"m{n}" for n in range(70, 120)])
        code, _, _ = run_session(daemon, ["--device", "dev_phone", "--record-max", "3"],
                                 *[control("config.set", n) for n in range(5)])
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(len(relay_store.read_refusals(relay.state_folder)), 3)

    def test_no_limit_and_none_kept_are_values_of_the_record(self) -> None:
        daemon = support.SocketDaemon(self)
        everything = Relay(environ=daemon.environ, device_id="dev_phone", record_max=None)
        for n in range(1100):
            everything.handle(control("config.set", n))
        self.assertEqual(len(relay_store.read_refusals(everything.state_folder)), 1100)
        other = support.SocketDaemon(self)
        nothing = Relay(environ=other.environ, device_id="dev_phone", record_max=0)
        nothing.handle(control("config.set", 1))
        self.assertEqual(relay_store.read_refusals(relay_folder_of(other)), [])
        relay_store.RefusalRecord(relay_folder_of(other), 0).add({"refused": "scope_refused"})
        self.assertEqual(relay_store.read_refusals(relay_folder_of(other)), [])
        for bad in (-1, 1.5, True, "3"):
            with self.subTest(record_max=bad), self.assertRaises(ValueError):
                Relay(environ=daemon.environ, record_max=bad)
        code, replies, err = run_session(daemon, ["--record-max", "x"], control("run.list", 1))
        self.assertEqual((code, replies), (EXIT_REFUSED, []))
        code, _, err = run_session(daemon, ["--record-max", "none"], control("run.list", 1))
        self.assertEqual((code, err.count("record_max is none: no limit")), (EXIT_OK, 1))

    def test_a_record_that_cannot_be_written_changes_no_answer(self) -> None:
        daemon = support.SocketDaemon(self)
        relay = Relay(environ=daemon.environ, device_id="dev_phone")
        os.mkdir(os.path.join(relay.state_folder, "refusals.jsonl"))
        reply = relay.handle(control("config.set", 1))
        self.assertEqual((reply["error"]["code"], reply["id"]), ("scope_refused", 1))


class CountingStream(io.RawIOBase):
    """A stream that counts every byte read from it."""

    def __init__(self, data: bytes) -> None:
        self._data, self.read_bytes = io.BytesIO(data), 0

    def readable(self) -> bool:
        return True

    def read(self, n=-1) -> bytes:
        chunk = self._data.read(n)
        self.read_bytes += len(chunk)
        return chunk


@unittest.skipUnless(POSIX, "the relay needs the AF_UNIX control socket (POSIX)")
class RelaySizeCapUnreadTests(unittest.TestCase):
    def test_a_length_over_the_size_cap_is_refused_before_one_byte_of_its_body_is_read(self) -> None:
        daemon = support.SocketDaemon(self)
        for cap, length in ((1000, 1_000_000), (None, 0xFFFFFFFF), (None, MAX_FRAME_BYTES + 1),
                            ("first", 2_000_000)):
            with self.subTest(cap=cap, length=length):
                kwargs = {} if cap == "first" else {"max_frame_bytes": cap}
                stream, out = CountingStream(HEADER.pack(length) + b"x" * 1_000_000), io.BytesIO()
                self.assertEqual(Relay(environ=daemon.environ, **kwargs).serve(stream, out), EXIT_STREAM)
                self.assertEqual(stream.read_bytes, HEADER.size)
                self.assertEqual(read_all(out.getvalue())[0]["error"]["detail"], "frame_too_large")
        self.assertEqual(daemon.server.accepted, 0)


if __name__ == "__main__":
    unittest.main()
