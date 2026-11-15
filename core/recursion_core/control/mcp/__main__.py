"""``python -m recursion_core.control.mcp``: the Control MCP stdio server against this user's running daemon.

An MCP client spawns this process and speaks MCP 2025-06-18 over its stdin and stdout. It takes no
arguments. Before reading anything from stdin it runs the socket check (``socket_check``): if the check
fails it writes one JSON line to stderr, ``{"code": "socket_check_failed", "reason": ..., "detail": ...}``,
writes nothing to stdout and exits 1. Otherwise it serves until the client ends stdin, or until stdout
fails, and exits 0. It reads fd 0 and writes fd 1 directly (``stdio.py``), so a client that stops reading
ends the process cleanly instead of leaving a buffered stdout for the interpreter's final flush to fail on.
Every line it writes to stderr goes to fd 2 directly too, never through ``sys.stderr``: a client that
closes stderr leaves nothing buffered for the final flush to fail on (exit 120), and the diagnostics
thread (``server.py``, DIAGNOSTICS) holds no lock of ``sys.stderr`` while it waits on a stderr nobody reads
(a daemon thread holding that lock keeps the interpreter from ending, or makes it abort, after an
interrupt too). Written through ``sys.stderr`` instead, four process tests in ``tests/test_stdio.py`` fail:
with stderr closed, exit 120 instead of 0 and 2 (``ClosedStderrTest``); with stderr full, a process that
does not end, or that aborts after an interrupt.

Exit statuses: 0 end of input or stdout failed; 1 the socket check failed; 2 usage (an argument was given);
3 an unexpected error while serving (one JSON line on stderr, ``{"code": "internal_error", ...}``); 130
interrupted. This is a client entry, not a kernel one: it registers nothing and starts no daemon service.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable, Mapping
from typing import Any

from . import server, socket_check
from .stdio import FdReader, FdWriter

EXIT_OK, EXIT_REFUSED, EXIT_USAGE, EXIT_INTERNAL, EXIT_INTERRUPTED = 0, 1, 2, 3, 130


def _stderr_line(stream: Any, event: dict) -> None:
    try:
        stream.write(json.dumps(event, sort_keys=True, ensure_ascii=True) + "\n")
        stream.flush()
    except (OSError, ValueError):
        pass


def _fd_line(writer: FdWriter, event: dict) -> None:
    try:
        writer.write((json.dumps(event, sort_keys=True, ensure_ascii=True) + "\n").encode("ascii"))
    except OSError:
        pass


def _line_writer(stderr: Any) -> Callable[[dict], None]:
    """One JSON line per event: to fd 2 for the process's own stderr (``stderr`` left out), else to the
    text stream given (the tests' in-process entry)."""

    if stderr is None:
        errors = FdWriter(2)
        return lambda event: _fd_line(errors, event)
    return lambda event: _stderr_line(stderr, event)


def main(argv: list[str] | None = None, *, environ: Mapping[str, str] | None = None, stdin: Any = None,
         stdout: Any = None, stderr: Any = None, check=socket_check.check) -> int:
    argv = sys.argv[1:] if argv is None else argv
    line = _line_writer(stderr)
    if argv:
        line({"code": "usage", "detail": "python -m recursion_core.control.mcp takes no arguments"})
        return EXIT_USAGE
    try:
        api = check(environ)
    except socket_check.SocketCheckFailed as exc:
        line({"code": "socket_check_failed", "reason": exc.reason, "detail": exc.detail})
        return EXIT_REFUSED
    instream = FdReader(0) if stdin is None else stdin
    outstream = FdWriter(1) if stdout is None else stdout
    line({"event": "serving", "protocol": server.LEGACY_PROTOCOL})
    try:
        return server.serve(api, instream, outstream, log=line)
    except KeyboardInterrupt:  # the client interrupted us: calls already sent to the daemon go on there
        return EXIT_INTERRUPTED
    except Exception as exc:  # noqa: BLE001 - one diagnostic line, an exit status of its own
        line({"code": "internal_error", "detail": f"{type(exc).__name__}: {exc}"})
        return EXIT_INTERNAL


if __name__ == "__main__":
    sys.exit(main())
