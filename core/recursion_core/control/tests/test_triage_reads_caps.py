"""Witnesses for the operator-read reply cap and a local timeout that the platform cannot hold.

Synthetic sockets only. The cap-plus-one cases and the timeout case fail before the product edit.
"""

from __future__ import annotations

import json
import os
import socket
import unittest
from unittest import mock

from recursion_core.control.operator_reads import OperatorReads, refusal
from recursion_core.control import operator_reads_socket as transport

METHOD = "operator.positions.inspect"
CAN_SERVE = all((hasattr(socket, "AF_UNIX"), hasattr(socket, "SO_PEERCRED"), hasattr(os, "getuid")))
CAP = transport.MAX_REPLY_BYTES


def _framed(obj: dict) -> bytes:
    return json.dumps(obj, allow_nan=False, separators=(",", ":")).encode("ascii") + b"\n"


def _fallback_frame(profile: dict | None) -> bytes:
    body = refusal(transport.REPLY_TOO_LARGE, profile=profile)
    return json.dumps(body, separators=(",", ":")).encode("ascii") + b"\n"


def _profile_of_length(nbytes: int) -> dict:
    """A profile whose ``reply_too_large`` frame, newline included, is ``nbytes`` long."""

    bare = len(_fallback_frame({"profile_id": ""}))
    profile = {"profile_id": "p" * (nbytes - bare)}
    framed = _fallback_frame(profile)
    if len(framed) != nbytes:
        raise AssertionError(f"profile frame is {len(framed)}, want {nbytes}")
    return profile


class _ScriptedSocket:
    """In-memory recv stream. Honours ``recv(n)`` and sends nothing."""

    def __init__(self, payload: bytes):
        self._payload = payload
        self.offset = 0

    def settimeout(self, value):
        pass

    def connect(self, path):
        pass

    def sendall(self, data):
        pass

    def recv(self, maximum):
        chunk = self._payload[self.offset:self.offset + maximum]
        self.offset += len(chunk)
        return chunk

    def close(self):
        pass


def _client_reply(payload: bytes) -> dict:
    scripted = _ScriptedSocket(payload)
    with mock.patch.object(transport.socket, "socket", return_value=scripted), \
            mock.patch.object(transport, "_peer_uid", return_value=os.getuid()):
        return transport.OperatorReadsClient("unused.sock").call(METHOD)


def _json_line(total: int) -> bytes:
    head = b'{"code":"ok"}'
    payload = head + b" " * (total - len(head) - 1) + b"\n"
    if len(payload) != total:
        raise AssertionError(f"line is {len(payload)}, want {total}")
    return payload


class ReplyCapTests(unittest.TestCase):
    def test_f05_reply_too_large_fallback_itself_obeys_cap(self):
        reads = OperatorReads(profile={"profile_id": "p" * CAP})
        server = transport.OperatorReadsServer(reads)
        encoded = server._encode(refusal("busy", profile=reads.profile))
        self.assertLessEqual(len(encoded), CAP)
        self.assertEqual(json.loads(encoded)["code"], "reply_too_large")

    def test_server_fallback_boundary_is_cap_and_one_byte_over(self):
        over = {"code": "ok", "pad": "x" * CAP}

        at_cap = _profile_of_length(CAP)
        server = transport.OperatorReadsServer(OperatorReads(profile=at_cap))
        encoded = server._encode(over)
        self.assertEqual(len(encoded), CAP)
        self.assertEqual(json.loads(encoded)["code"], "reply_too_large")

        one_over = _profile_of_length(CAP + 1)
        server = transport.OperatorReadsServer(OperatorReads(profile=one_over))
        encoded = server._encode(over)
        self.assertLessEqual(len(encoded), CAP)
        self.assertEqual(json.loads(encoded)["code"], "reply_too_large")
        self.assertNotIn(one_over["profile_id"].encode("ascii"), encoded)

    def test_server_keeps_a_reply_at_the_cap_and_replaces_one_byte_over(self):
        pad_at = CAP - len(_framed({"k": ""}))
        at = {"k": "x" * pad_at}
        framed = _framed(at)
        self.assertEqual(len(framed), CAP)
        server = transport.OperatorReadsServer(OperatorReads())
        self.assertEqual(server._encode(at), framed)

        over = {"k": "x" * (pad_at + 1)}
        self.assertEqual(len(_framed(over)), CAP + 1)
        encoded = server._encode(over)
        self.assertLessEqual(len(encoded), CAP)
        self.assertEqual(json.loads(encoded)["code"], "reply_too_large")

    @unittest.skipUnless(CAN_SERVE, "Linux client required")
    def test_f06_complete_reply_one_byte_over_cap_is_refused(self):
        head = b'{"code":"ok"}'
        payload = head + b" " * (CAP - len(head)) + b"\n"
        self.assertEqual(len(payload), CAP + 1)
        reply = _client_reply(payload)
        self.assertEqual(reply.get("code"), "control_api_unavailable")
        self.assertEqual((reply.get("delivered"), reply.get("detail")), ("unknown", "reply_too_large"))

    @unittest.skipUnless(CAN_SERVE, "Linux client required")
    def test_client_accepts_a_reply_of_exactly_the_cap(self):
        reply = _client_reply(_json_line(CAP))
        self.assertEqual(reply.get("code"), "ok")
        self.assertNotIn("delivered", reply)

    @unittest.skipUnless(CAN_SERVE, "Linux client required")
    def test_f09_local_timeout_overflow_is_not_delivered_unknown(self):
        reply = transport.OperatorReadsClient("unused.sock", timeout_s=1e20).call(METHOD)
        self.assertEqual(reply["code"], "control_api_unavailable")
        self.assertIs(reply["delivered"], False)


if __name__ == "__main__":
    unittest.main()
