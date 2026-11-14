"""The event-stream reader: line ends, split reads, comments, UTF-8."""

from __future__ import annotations

import unittest

from recursion_core.loop.providers.sse import SSEError, SSEEvent, iter_events


def reader(data: bytes, piece: int):
    chunks = [data[i:i + piece] for i in range(0, len(data), piece)]

    def read(_n: int) -> bytes:
        return chunks.pop(0) if chunks else b""

    return read


def events(data: bytes, piece: int = 1) -> list[SSEEvent]:
    return list(iter_events(reader(data, piece)))


class Reader(unittest.TestCase):
    def test_every_line_end_and_every_split(self) -> None:
        for sep in (b"\n", b"\r\n", b"\r"):
            data = sep.join([b": comment", b"event: a", b"data: one", b"data:two", b"", b"data: three", b"", b""])
            for piece in (1, 2, 3, 7, len(data)):
                with self.subTest(sep=sep, piece=piece):
                    self.assertEqual(events(data, piece), [SSEEvent("a", "one\ntwo"), SSEEvent("message", "three")])

    def test_unicode_line_separators_inside_data_are_data(self) -> None:
        text = "data: {\"t\":\"a\u2028b\u0085c\"}\n\n".encode("utf-8")
        self.assertEqual(events(text, 1), [SSEEvent("message", "{\"t\":\"a\u2028b\u0085c\"}")])

    def test_utf8_split_across_reads(self) -> None:
        data = "data: \u2713\u201c\n\n".encode("utf-8")
        self.assertEqual(events(data, 1)[0].data, "\u2713\u201c")

    def test_an_event_the_stream_ends_in_is_not_dispatched(self) -> None:
        self.assertEqual(events(b"data: x\n\ndata: y"), [SSEEvent("message", "x")])
        self.assertEqual(events(b"data: x\n\ndata: y\n"), [SSEEvent("message", "x")])

    def test_fields_without_data_do_not_dispatch(self) -> None:
        self.assertEqual(events(b"event: ping\n\nid: 3\nretry: 10\n\n"), [])

    def test_bytes_that_are_not_utf8_are_refused(self) -> None:
        with self.assertRaises(SSEError):
            events(b"data: \xff\xfe\n\n")


if __name__ == "__main__":
    unittest.main()
