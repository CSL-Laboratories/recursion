"""The frame vectors (fixtures/frames.json) are true to the control socket's own codec.

Every later codec of the wire (the desktop's Rust proxy first) is held to these vectors, so each one is
checked here against ``control_transport.framing`` and the transport's own frames at master.
"""

from __future__ import annotations

import json
import socket
import threading
import unittest
from pathlib import Path

from recursion_core.control_transport import unix
from recursion_core.control_transport.errors import FrameError
from recursion_core.control_transport.framing import HEADER, MAX_FRAME_BYTES, encode_frame, read_frame

VECTORS = json.loads((Path(__file__).resolve().parent / "fixtures" / "frames.json").read_text(encoding="utf-8"))
PREFIX = b'{"op":"control","method":"run.start","params":{"prompt":"'
SUFFIX = b'"}}'
CODES = {"frame_not_object", "frame_too_large", "frame_too_deep", "frame_not_json", "frame_truncated"}


def generated_body(size: int) -> bytes:
    body = PREFIX + b"a" * (size - len(PREFIX) - len(SUFFIX)) + SUFFIX
    assert len(body) == size
    return body


def read_bytes(data: bytes):
    """``read_frame`` on a socket that holds ``data`` and is then closed for writing."""
    left, right = socket.socketpair()
    with left, right:
        writer = threading.Thread(target=lambda: (left.sendall(data), left.shutdown(socket.SHUT_WR)))
        writer.start()
        try:
            return read_frame(right)
        finally:
            writer.join()


class EncodeTest(unittest.TestCase):
    def test_each_message_encodes_to_its_frame_and_reads_back(self) -> None:
        for vector in VECTORS["encode"]:
            with self.subTest(vector["name"]):
                frame = bytes.fromhex(vector["frame_hex"])
                self.assertEqual(encode_frame(vector["message"]), frame)
                self.assertEqual(HEADER.unpack(frame[:4])[0], len(frame) - 4)
                got = read_bytes(frame)
                self.assertEqual(got, vector["message"])
                self.assertEqual(list(got), list(vector["message"]))  # the key order survives the trip

    def test_a_body_at_the_cap_is_sent_and_one_byte_more_is_not(self) -> None:
        self.assertEqual(VECTORS["max_frame_bytes"], MAX_FRAME_BYTES)
        for vector in VECTORS["encode_generated"]:
            with self.subTest(vector["name"]):
                message = json.loads(generated_body(vector["body_bytes"]))
                if "error" in vector:
                    with self.assertRaises(FrameError) as caught:
                        encode_frame(message)
                    self.assertEqual(str(caught.exception), vector["error"])
                else:
                    frame = encode_frame(message)
                    self.assertEqual(len(frame), 4 + vector["body_bytes"])

    def test_what_is_not_an_object_is_refused(self) -> None:
        for vector in VECTORS["encode_refused"]:
            with self.subTest(vector["name"]):
                with self.assertRaises(FrameError) as caught:
                    encode_frame(vector["message"])
                self.assertEqual(str(caught.exception), vector["error"])


class DecodeTest(unittest.TestCase):
    def test_each_byte_string_reads_as_its_message_or_its_error(self) -> None:
        for vector in VECTORS["decode"]:
            with self.subTest(vector["name"]):
                data = bytes.fromhex(vector["bytes_hex"])
                if "error" in vector:
                    self.assertIn(vector["error"], CODES)
                    with self.assertRaises(FrameError) as caught:
                        read_bytes(data)
                    self.assertEqual(str(caught.exception), vector["error"])
                else:
                    self.assertEqual(read_bytes(data), vector["result"])

    def test_an_oversized_length_is_refused_before_any_body_byte_is_read(self) -> None:
        # The writer sends the header alone and closes: a reader that waited for the body would see
        # frame_truncated, so frame_too_large shows the length was refused first.
        for vector in [v for v in VECTORS["decode"] if v.get("error") == "frame_too_large"]:
            with self.subTest(vector["name"]):
                self.assertEqual(len(bytes.fromhex(vector["bytes_hex"])), 4)

    def test_generated_bodies(self) -> None:
        for vector in VECTORS["decode_generated"]:
            with self.subTest(vector["name"]):
                if "body_bytes" in vector:
                    body = generated_body(vector["body_bytes"])
                    got = read_bytes(HEADER.pack(len(body)) + body)
                    self.assertEqual(len(got["params"]["prompt"]), len(body) - len(PREFIX) - len(SUFFIX))
                else:
                    depth = vector["nested_lists"]
                    body = b"[" * depth + b"]" * depth
                    self.assertLessEqual(len(body), MAX_FRAME_BYTES)
                    with self.assertRaises(FrameError) as caught:
                        read_bytes(HEADER.pack(len(body)) + body)
                    self.assertEqual(str(caught.exception), vector["error"])


class TransportFramesTest(unittest.TestCase):
    def test_the_busy_and_not_admitted_frames_are_the_servers(self) -> None:
        frames = VECTORS["transport_frames"]
        self.assertEqual(bytes.fromhex(frames["busy"]["frame_hex"]), unix.BUSY_FRAME)
        self.assertEqual(bytes.fromhex(frames["not_admitted"]["frame_hex"]), unix.NOT_ADMITTED_FRAME)
        for name in ("busy", "not_admitted"):
            with self.subTest(name):
                self.assertEqual(read_bytes(bytes.fromhex(frames[name]["frame_hex"])), frames[name]["message"])
                self.assertIs(frames[name]["message"]["error"]["delivered"], False)


if __name__ == "__main__":
    unittest.main()
