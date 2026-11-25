"""Codec errors, the frame deadline and the smallest frame cap.

The red tests of the s7 triage of the transport sweep (sol lists), entries D01, D15 and D19, from
``sweeps/20261115/s7-triage-sol-lists-claude-opus-5-5-evidence/patches/transport/D{01,15,19}-red.patch``;
their bodies are kept as written there, in one module, with two tests of this draft's own after them.
All filesystem writes stay beneath TMPDIR.
"""

from __future__ import annotations

import os
import socket
import sys
import tempfile
import threading
import unittest
from unittest import mock

from recursion_core import control_transport as ct
from recursion_core.control_transport import framing, unix


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux is the tested transport path")
class ScratchCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="s7-", dir=os.environ.get("TMPDIR"))
        self.addCleanup(self.tmp.cleanup)
        self.directory = os.path.join(self.tmp.name, "run")
        self.worker_errors = []
        old_hook = threading.excepthook
        threading.excepthook = lambda args: self.worker_errors.append(args.exc_type.__name__)
        self.addCleanup(setattr, threading, "excepthook", old_hook)

    def server(self, handler=None, **kwargs):
        srv = ct.UnixControlServer(self.directory, handler or ct.with_ping_health(), **kwargs)
        self.addCleanup(srv.close)
        srv.start()
        return srv


class EncodeErrorTests(ScratchCase):
    def test_f01_encoder_refuses_non_json_values_with_frame_error(self):
        """s7 D01: bytes, NaN, a lone surrogate and a cycle escaped encode_frame as TypeError or ValueError."""
        cyclic = {}
        cyclic["self"] = cyclic
        for label, value in (("bytes", b"x"), ("nan", float("nan")),
                             ("surrogate", "\ud800"), ("cycle", cyclic)):
            with self.subTest(label=label), self.assertRaises(ct.FrameError):
                framing.encode_frame({"value": value})

    def test_f01_handler_serialization_error_is_a_sanitized_reply(self):
        """s7 D01, the server half: a reply holding bytes is internal_error, and no worker dies."""
        srv = self.server(lambda message, conn: {"value": b"x"})
        with ct.UnixControlClient(srv.path, timeout_s=1) as client:
            reply = client.request({"op": "x"})
        self.assertEqual(reply, {"ok": False, "error": {"code": "internal_error"}})
        self.assertEqual(self.worker_errors, [])


class FrameDeadlineTests(ScratchCase):
    def test_final_frame_bytes_returning_after_deadline_are_refused(self):
        """s7 D15: a body whose last recv returned after the frame deadline was still returned."""
        from types import SimpleNamespace
        now = [0.0]

        class Sock:
            def __init__(self):
                self.chunks = iter((framing.HEADER.pack(2), b"{}"))

            def settimeout(self, seconds):
                pass

            def recv(self, size):
                chunk = next(self.chunks)
                if chunk == b"{}":
                    now[0] = 2.0
                return chunk

        with mock.patch.object(framing, "time", SimpleNamespace(monotonic=lambda: now[0])):
            with self.assertRaises(TimeoutError):
                framing.read_frame(Sock(), deadline=1.0)


class SmallestCapTests(ScratchCase):
    def test_fallback_respects_even_a_small_configured_frame_cap(self):
        """s7 D19: at a cap of 32 the 50-byte fallback was written anyway."""
        # A request body of 10 bytes fits cap=32. Master's fallback body is 50 bytes.
        try:
            srv = self.server(lambda m, c: {"blob": "x" * 200}, max_frame_bytes=32)
        except (ValueError, ct.TransportRefused):
            return  # Refusing an unusably small cap before startup is also safe.
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as raw:
            raw.settimeout(2)
            raw.connect(srv.path)
            framing.write_frame(raw, {"op": "x"}, max_bytes=32)
            header = framing._recv_exact(raw, 4)
            self.assertIsNotNone(header)
            (size,) = framing.HEADER.unpack(header)
            self.assertLessEqual(size, 32, f"fallback body {size} exceeds configured cap 32")

    def test_the_smallest_cap_is_refused_below_and_carries_the_fallback_at_it(self):
        """This draft's floor: below it construction refuses; at it the id-less fallback arrives whole."""
        floor = unix._MIN_FRAME_BYTES
        for cap in (floor - 1, 0, -1, True, 64.0, "64", None):
            with self.subTest(cap=repr(cap)), self.assertRaises(ValueError):
                ct.UnixControlServer(self.directory, ct.with_ping_health(), max_frame_bytes=cap)
        srv = self.server(lambda m, c: {"blob": "x" * 200}, max_frame_bytes=floor)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as raw:
            raw.settimeout(2)
            raw.connect(srv.path)
            framing.write_frame(raw, {"op": "x"}, max_bytes=floor)
            self.assertEqual(framing.read_frame(raw, max_bytes=floor),
                             {"ok": False, "error": {"code": "response_too_large"}})

    def test_a_reply_too_deep_to_encode_is_internal_error_with_its_id(self):
        """A reply encode_frame refuses for a reason other than size is internal_error, never response_too_large."""
        deep = {}
        node = deep
        for _ in range(100000):
            node["n"] = {}
            node = node["n"]
        srv = self.server(lambda m, c: deep)
        with ct.UnixControlClient(srv.path, timeout_s=2) as client:
            reply = client.request({"op": "x", "id": "req-deep"})
        self.assertEqual(reply, {"ok": False, "error": {"code": "internal_error"}, "id": "req-deep"})
        self.assertEqual(self.worker_errors, [])


if __name__ == "__main__":
    unittest.main()
