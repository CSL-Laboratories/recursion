"""The fd-level stdin reader and stdout writer the process entry uses (``stdio.py``), over real pipes."""

from __future__ import annotations

import os
import threading
import unittest
from unittest import mock

from recursion_core.control.mcp import stdio
from recursion_core.control.mcp.framing import Frame, read_frames
from recursion_core.control.mcp.stdio import FdReader, FdWriter


class FdReaderTest(unittest.TestCase):
    def pipe_with(self, data: bytes) -> FdReader:
        read_fd, write_fd = os.pipe()
        self.addCleanup(os.close, read_fd)
        os.write(write_fd, data)
        os.close(write_fd)
        return FdReader(read_fd)

    def test_lines_limits_and_end_of_input(self) -> None:
        reader = self.pipe_with(b"one\ntwo-is-long\nthree")
        self.assertEqual(b"one\n", reader.readline(100))
        self.assertEqual(b"two-", reader.readline(4))        # no newline within the limit
        self.assertEqual(b"is-long\n", reader.readline(100))
        self.assertEqual(b"three", reader.readline(100))      # the last line, unterminated
        self.assertEqual(b"", reader.readline(100))
        self.assertEqual(b"", reader.readline(100))

    def test_no_limit_and_a_newline_exactly_at_the_limit(self) -> None:
        reader = self.pipe_with(b"abc\nxyz\n")
        self.assertEqual(b"abc\n", reader.readline(4))
        self.assertEqual(b"xyz\n", reader.readline())

    def test_read_frames_over_it_matches_a_buffered_stream(self) -> None:
        data = b'{"n":1}\r\n\n' + b"x" * 300 + b'\n{"n":2}'
        self.assertEqual([Frame(message={"n": 1}), Frame(error="too_large"), Frame(message={"n": 2})],
                         list(read_frames(self.pipe_with(data), max_bytes=100)))

    def test_reads_that_split_lines_anywhere(self) -> None:
        chunks = iter([b"ab", b"c\ndef", b"\n", b""])
        with mock.patch.object(stdio.os, "read", side_effect=lambda fd, count: next(chunks)):
            reader = FdReader(123)
            self.assertEqual(b"a", reader.readline(1))
            self.assertEqual(b"bc\n", reader.readline(3))
            self.assertEqual(b"def\n", reader.readline(4))
            self.assertEqual(b"", reader.readline())

    def test_a_line_longer_than_one_read(self) -> None:
        big = b"y" * 200_000
        read_fd, write_fd = os.pipe()
        self.addCleanup(os.close, read_fd)
        reader = FdReader(read_fd)
        writer = threading.Thread(target=lambda: (os.write(write_fd, big + b"\n"), os.close(write_fd)))
        writer.start()
        self.assertEqual(big + b"\n", reader.readline(300_000))
        writer.join()


class FdWriterTest(unittest.TestCase):
    def test_writes_everything_and_flush_is_a_no_op(self) -> None:
        read_fd, write_fd = os.pipe()
        self.addCleanup(os.close, read_fd)
        writer = FdWriter(write_fd)
        self.assertEqual(5, writer.write(b"hello"))
        writer.flush()
        os.close(write_fd)
        self.assertEqual(b"hello", os.read(read_fd, 100))

    def test_a_short_write_is_finished(self) -> None:
        written = bytearray()

        def two_bytes_at_a_time(fd, data):
            piece = bytes(data[:2])
            written.extend(piece)
            return len(piece)

        with mock.patch.object(stdio.os, "write", side_effect=two_bytes_at_a_time):
            self.assertEqual(7, FdWriter(123).write(b"answer\n"))
        self.assertEqual(b"answer\n", bytes(written))

    @unittest.skipIf(os.name == "nt", "a pipe with no reader raises another error on Windows")
    def test_a_pipe_nobody_reads_raises_broken_pipe(self) -> None:
        read_fd, write_fd = os.pipe()
        self.addCleanup(os.close, write_fd)
        os.close(read_fd)
        with self.assertRaises(BrokenPipeError):
            FdWriter(write_fd).write(b"x")


if __name__ == "__main__":
    unittest.main()
