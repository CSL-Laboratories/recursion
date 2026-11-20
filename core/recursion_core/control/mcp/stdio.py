"""The process's own stdin and stdout, read and written at the file-descriptor level.

``python -m recursion_core.control.mcp`` reads fd 0 and writes fd 1 through these two classes, not through
``sys.stdin`` and ``sys.stdout``:

* ``FdWriter`` writes each message with ``os.write`` and buffers nothing. When the client stops reading,
  the write fails at once with ``BrokenPipeError`` (Python ignores SIGPIPE), the server stops, and nothing
  is left in ``sys.stdout``'s buffer for the interpreter's final flush to fail on (a failing final flush
  makes the process exit 120 with a traceback on stderr).
* ``FdReader`` reads with ``os.read`` into a buffer of its own. The reader thread blocked in it holds no
  lock of ``sys.stdin``, so the interpreter can end while it waits (a daemon thread holding the lock of a
  buffered ``sys.stdin`` can make the interpreter abort at shutdown).

``readline(limit)`` has the meaning ``framing.read_frames`` uses: at most ``limit`` bytes, ending at the
first newline when there is one within them; ``b""`` only at end of input.
"""

from __future__ import annotations

import os

_CHUNK = 64 * 1024


class FdReader:
    def __init__(self, fd: int) -> None:
        self._fd = fd
        self._buffer = bytearray()
        self._eof = False

    def readline(self, limit: int = -1) -> bytes:
        while True:
            end = self._buffer.find(b"\n")
            if end >= 0 and (limit < 0 or end < limit):
                return self._take(end + 1)
            if 0 <= limit <= len(self._buffer):
                return self._take(limit)
            if self._eof:
                return self._take(len(self._buffer))
            chunk = os.read(self._fd, _CHUNK)
            if chunk:
                self._buffer += chunk
            else:
                self._eof = True

    def _take(self, count: int) -> bytes:
        data = bytes(self._buffer[:count])
        del self._buffer[:count]
        return data


class FdWriter:
    def __init__(self, fd: int) -> None:
        self._fd = fd

    def write(self, data: bytes) -> int:
        view = memoryview(data)
        while view:
            view = view[os.write(self._fd, view):]
        return len(data)

    def flush(self) -> None:
        pass
