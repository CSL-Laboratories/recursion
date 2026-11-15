"""Line framing, the writer, and JSON-RPC classification, below the session."""

from __future__ import annotations

import io
import json
import threading
import unittest

from recursion_core.control.mcp import framing, jsonrpc
from recursion_core.control.mcp.framing import NOT_JSON, NOT_UTF8, TOO_LARGE, Frame, FrameWriter, encode, read_frames
from recursion_core.control.mcp.tests.support import HANG_GUARD_S

SHORT = 0.2  # only ever used to show that something has not happened


def frames(data: bytes, **kwargs) -> list[Frame]:
    return list(read_frames(io.BytesIO(data), **kwargs))


class ReadFramesTest(unittest.TestCase):
    def test_a_line_at_the_limit_is_read_and_one_byte_more_is_refused(self) -> None:
        body = b'{"a":"' + b"x" * 10 + b'"}'
        at_limit = body + b"\n"
        self.assertEqual([Frame(message={"a": "x" * 10})], frames(at_limit, max_bytes=len(at_limit)))
        self.assertEqual([Frame(error=TOO_LARGE)], frames(at_limit, max_bytes=len(at_limit) - 1))

    def test_a_long_line_is_drained_and_the_next_line_is_read(self) -> None:
        data = b"y" * 300_000 + b"\n" + b'{"n":1}\n'
        self.assertEqual([Frame(error=TOO_LARGE), Frame(message={"n": 1})], frames(data, max_bytes=100))

    def test_a_long_last_line_without_newline_is_refused_once(self) -> None:
        self.assertEqual([Frame(error=TOO_LARGE)], frames(b"z" * 1000, max_bytes=100))

    def test_crlf_blank_lines_and_a_last_line_without_newline(self) -> None:
        data = b'{"n":1}\r\n\n  \r\n{"n":2}'
        self.assertEqual([Frame(message={"n": 1}), Frame(message={"n": 2})], frames(data))

    def test_a_cr_is_json_whitespace_outside_a_string_and_refused_inside_one(self) -> None:
        self.assertEqual([Frame(message={"n": 1})], frames(b'{"n":1}\r\r\n'))
        self.assertEqual([Frame(message={"n": 1})], frames(b'\r{"n":\r1}\r\n'))
        self.assertEqual([Frame(error=NOT_JSON)], frames(b'{"s":"a\rb"}\n'))

    def test_refusals(self) -> None:
        cases = {
            b"\xff\n": NOT_UTF8,
            b'{"a":"\xc3("}\n': NOT_UTF8,
            b"{nope}\n": NOT_JSON,
            b'{"x":NaN}\n': NOT_JSON,
            b"[Infinity]\n": NOT_JSON,
            b"-Infinity\n": NOT_JSON,
            b"[" * 200_000 + b"\n": NOT_JSON,
            b'{"a":1} {"b":2}\n': NOT_JSON,
        }
        for data, error in cases.items():
            with self.subTest(data=data[:20]):
                self.assertEqual([Frame(error=error)], frames(data))

    def test_an_integer_longer_than_the_limit_is_its_own_refusal(self) -> None:
        at_limit = "9" * framing.MAX_INT_DIGITS
        self.assertEqual([Frame(message={"id": int(at_limit)})], frames(f'{{"id":{at_limit}}}\n'.encode()))
        for digits in ("9" * (framing.MAX_INT_DIGITS + 1), "-" + "9" * (framing.MAX_INT_DIGITS + 1)):
            self.assertEqual([Frame(error=framing.NUMBER_TOO_LONG)], frames(f'{{"id":{digits}}}\n'.encode()))
        long_fraction = frames(b'{"x":1.' + b"5" * 5000 + b"}\n")  # a fraction is not an integer: read as it is
        self.assertIsNone(long_fraction[0].error)

    def test_any_json_value_is_a_frame_the_session_classifies(self) -> None:
        self.assertEqual([Frame(message=[1]), Frame(message="s"), Frame(message=None)], frames(b'[1]\n"s"\nnull\n'))

    def test_max_bytes_must_leave_room(self) -> None:
        with self.assertRaises(ValueError):
            frames(b"{}\n", max_bytes=1)


class WriterTest(unittest.TestCase):
    def test_encode_is_one_sorted_ascii_line(self) -> None:
        data = encode({"b": "line\nbreak \u00e9 \u2028", "a": 1})
        self.assertTrue(data.isascii())
        self.assertEqual(1, data.count(b"\n"))
        self.assertTrue(data.endswith(b"\n"))
        self.assertEqual(b'{"a":1,"b":"line\\nbreak \\u00e9 \\u2028"}\n', data)

    def test_encode_refuses_what_json_cannot_hold(self) -> None:
        for bad in ({"x": float("nan")}, {"x": float("inf")}, {"x": object()}):
            with self.assertRaises((ValueError, TypeError)):
                encode(bad)

    def test_a_second_write_does_not_start_until_the_first_is_whole(self) -> None:
        first_inside, second_inside = threading.Event(), threading.Event()
        overlapped = []

        class Pausing:
            """The first write stops halfway and gives a second writer every chance to come in."""

            def __init__(self) -> None:
                self.data = bytearray()
                self.calls = 0

            def write(self, data):
                self.calls += 1
                if self.calls > 1:
                    second_inside.set()
                    self.data += data
                    return len(data)
                self.data += data[:5]
                first_inside.set()
                overlapped.append(second_inside.wait(SHORT))  # time only shows that it has NOT happened
                self.data += data[5:]
                return len(data)

            def flush(self):
                pass

        out = Pausing()
        writer = FrameWriter(out)
        first = threading.Thread(target=writer.write, args=({"n": 1, "pad": "a" * 20},))
        first.start()
        self.assertTrue(first_inside.wait(HANG_GUARD_S))
        second = threading.Thread(target=writer.write, args=({"n": 2, "pad": "b" * 20},))
        second.start()
        first.join()
        second.join()
        self.assertEqual([False], overlapped, "the second write entered the stream inside the first")
        self.assertEqual([1, 2], [json.loads(line)["n"] for line in bytes(out.data).splitlines()])

    def test_many_concurrent_writes_all_arrive_whole(self) -> None:
        out = io.BytesIO()
        writer = FrameWriter(out)
        start = threading.Barrier(6)

        def burst(n: int) -> None:
            start.wait()
            for i in range(50):
                writer.write({"writer": n, "i": i, "pad": "p" * 40})

        threads = [threading.Thread(target=burst, args=(n,)) for n in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        lines = out.getvalue().split(b"\n")
        self.assertEqual(b"", lines.pop())
        seen = sorted((m["writer"], m["i"]) for m in map(json.loads, lines))
        self.assertEqual(sorted((n, i) for n in range(6) for i in range(50)), seen)

    def test_a_failed_write_breaks_the_writer_for_good(self) -> None:
        class Failing:
            writes = 0

            def write(self, data):
                Failing.writes += 1
                raise BrokenPipeError()

            def flush(self):
                pass

        writer = FrameWriter(Failing())
        self.assertFalse(writer.write({"a": 1}))
        self.assertTrue(writer.broken)
        self.assertFalse(writer.write({"a": 2}))
        self.assertEqual(1, Failing.writes)

    def test_on_broken_is_called_once_outside_the_lock(self) -> None:
        calls = []

        class Failing(io.RawIOBase):
            def writable(self):
                return True

            def write(self, data):
                raise BrokenPipeError()

        writer = FrameWriter(Failing())

        def on_broken():
            calls.append(writer.broken)
            self.assertFalse(writer.write({"from": "the callback"}))  # would deadlock if the lock were held

        writer.on_broken = on_broken
        self.assertFalse(writer.write({"a": 1}))
        self.assertFalse(writer.write({"a": 2}))
        self.assertEqual([True], calls)

    def test_a_shut_stream_breaks_the_writer(self) -> None:
        stream = io.BytesIO()
        stream.close()
        writer = FrameWriter(stream)
        self.assertFalse(writer.write({"a": 1}))
        self.assertTrue(writer.broken)

    def test_a_stream_that_raises_anything_else_breaks_the_writer_too(self) -> None:
        # Round 6 of #395: a text stream given bytes (TypeError) or a wrapper that fails its own way
        # (RuntimeError) has failed like a pipe nobody reads: broken, on_broken once, nothing raised.
        class Raising(io.RawIOBase):
            def writable(self):
                return True

            def write(self, data):
                raise RuntimeError("the wrapper failed")

        for stream in (io.StringIO(), Raising()):
            with self.subTest(stream=type(stream).__name__):
                calls: list[bool] = []
                writer = FrameWriter(stream, on_broken=lambda calls=calls: calls.append(True))
                self.assertFalse(writer.write({"a": 1}))
                self.assertTrue(writer.broken)
                self.assertFalse(writer.write({"a": 2}))
                self.assertEqual([True], calls)

    def test_a_message_json_cannot_hold_raises_and_breaks_nothing(self) -> None:
        writer = FrameWriter(io.BytesIO())
        for value in (float("nan"), {"a set"}):
            with self.subTest(value=value), self.assertRaises((TypeError, ValueError)):
                writer.write({"a": value})
        self.assertFalse(writer.broken)
        self.assertTrue(writer.write({"a": 1}))

    def test_line_limit_leaves_room_for_a_full_control_frame(self) -> None:
        from recursion_core.control_transport.framing import MAX_FRAME_BYTES
        self.assertGreater(framing.MAX_LINE_BYTES, MAX_FRAME_BYTES)


class JsonRpcTest(unittest.TestCase):
    def test_valid_ids(self) -> None:
        for value in ("", "a", 0, -1, 2**70):
            self.assertTrue(jsonrpc.valid_id(value), value)
        for value in (None, True, False, 1.5, 1.0, float("nan"), float("inf"), [], {}, b"x"):
            self.assertFalse(jsonrpc.valid_id(value), value)

    def test_classify(self) -> None:
        self.assertEqual(jsonrpc.Request(1, "ping", {}), jsonrpc.classify({"jsonrpc": "2.0", "id": 1, "method": "ping"}))
        self.assertEqual(jsonrpc.Notification("n", {}), jsonrpc.classify({"jsonrpc": "2.0", "method": "n", "params": [1]}))
        self.assertEqual(jsonrpc.Response(), jsonrpc.classify({"jsonrpc": "2.0", "id": 1, "result": {}}))
        self.assertEqual(jsonrpc.Response(), jsonrpc.classify({"jsonrpc": "2.0", "id": 1, "error": {}}))
        invalid = jsonrpc.classify({"jsonrpc": "2.0", "id": False, "method": "ping"})
        self.assertIsInstance(invalid, jsonrpc.Invalid)
        self.assertEqual((None, jsonrpc.INVALID_REQUEST), (invalid.id, invalid.code))
        params = jsonrpc.classify({"jsonrpc": "2.0", "id": "q", "method": "ping", "params": 3})
        self.assertEqual(("q", jsonrpc.INVALID_PARAMS), (params.id, params.code))

    def test_error_carries_data_only_when_given(self) -> None:
        self.assertEqual({"jsonrpc": "2.0", "id": 1, "error": {"code": -1, "message": "m"}}, jsonrpc.error(1, -1, "m"))
        self.assertEqual({"jsonrpc": "2.0", "id": 1, "error": {"code": -1, "message": "m", "data": {"k": 0}}},
                         jsonrpc.error(1, -1, "m", {"k": 0}))


if __name__ == "__main__":
    unittest.main()
