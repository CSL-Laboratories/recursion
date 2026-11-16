"""Shared pieces for the loop's tests: private temporary folders, a loopback HTTP server that replays recorded
provider streams, and a collector for the loop's output lines. No test here reaches the network."""

from __future__ import annotations

import io
import json
import os
import queue
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from recursion_core.loop import protocol as p
from recursion_core.loop.providers import base as b
from recursion_core.loop.tools import read, search
from recursion_core.loop.tools.registry import ToolContext, ToolRegistry
from recursion_core.loop.tools.workspace import SpillStore, Workspace

FIXTURES = Path(__file__).resolve().parent / "fixtures"
POSIX = os.name == "posix"
FAKE_KEY = "sk-ant-test-0000-made-up-key-for-tests-only-0000"


def private_dir(case, prefix: str = "loop-") -> Path:
    folder = Path(tempfile.mkdtemp(prefix=prefix))
    os.chmod(folder, 0o700)

    def cleanup() -> None:
        import shutil
        shutil.rmtree(folder, ignore_errors=True)

    case.addCleanup(cleanup)
    return folder


def workspace(case, files: dict[str, str | bytes] | None = None) -> Workspace:
    root = private_dir(case, "loop-ws-")
    home = private_dir(case, "loop-home-")
    for rel, content in (files or {}).items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            target.write_bytes(content)
        else:
            target.write_bytes(content.encode("utf-8"))  # not text mode: Windows would turn each LF into CRLF
    return Workspace(root, SpillStore(home))


def registry(**kw) -> ToolRegistry:
    return ToolRegistry((read.TOOL, search.TOOL), **kw)


def context(ws: Workspace, scrub=lambda s: s, cancel: b.CancelToken | None = None) -> ToolContext:
    return ToolContext(ws, cancel or b.CancelToken(), scrub)


class Lines:
    """Collects what a Writer writes, as parsed objects."""

    def __init__(self, secrets=()) -> None:
        self.buffer = io.StringIO()
        self.redactor = p.Redactor(secrets)
        self.writer = p.Writer(self.buffer, self.redactor)

    def raw(self) -> str:
        return self.buffer.getvalue()

    def all(self) -> list[dict]:
        return [json.loads(line) for line in self.raw().splitlines()]

    def of(self, kind: str) -> list[dict]:
        return [d for d in self.all() if d["type"] == kind]


class Canned:
    """One answer the loopback server gives: status, headers, and the body in pieces (sent as HTTP chunks)."""

    def __init__(self, status: int = 200, body: bytes | list[bytes] = b"", headers: dict | None = None,
                 hold_after: int | None = None) -> None:
        self.status = status
        self.pieces = body if isinstance(body, list) else [body]
        self.headers = headers or {}
        self.hold_after = hold_after  # stop after this many pieces until the test releases the server
        self.release = threading.Event()
        self.held = threading.Event()


def sse_pieces(path: Path, piece: int = 37) -> list[bytes]:
    """A recorded stream cut into small pieces, so lines, events and UTF-8 characters arrive split."""

    data = path.read_bytes()
    return [data[i:i + piece] for i in range(0, len(data), piece)]


def sse(name: str, piece: int = 37) -> Canned:
    return Canned(200, sse_pieces(FIXTURES / "anthropic" / name, piece), {"content-type": "text/event-stream",
                                                                          "request-id": "req_fixture"})


class Loopback:
    """A local HTTP server on 127.0.0.1 that answers each POST with the next Canned answer and keeps what it got."""

    def __init__(self, case, answers: list[Canned]) -> None:
        self.answers: queue.Queue = queue.Queue()
        for a in answers:
            self.answers.put(a)
        self.requests: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args) -> None:  # keep test output clean
                pass

            def do_POST(self) -> None:
                length = int(self.headers.get("content-length", "0"))
                body = self.rfile.read(length)
                outer.requests.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()},
                                       "body": json.loads(body.decode("utf-8"))})
                answer: Canned = outer.answers.get_nowait()
                self.send_response(answer.status)
                for k, v in answer.headers.items():
                    self.send_header(k, v)
                self.send_header("transfer-encoding", "chunked")
                self.send_header("connection", "close")
                self.end_headers()
                try:
                    for i, piece in enumerate(answer.pieces):
                        if answer.hold_after is not None and i == answer.hold_after:
                            answer.held.set()
                            answer.release.wait()
                        self.wfile.write(f"{len(piece):x}\r\n".encode() + piece + b"\r\n")
                        self.wfile.flush()
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                except OSError:
                    pass
                self.close_connection = True

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()
        case.addCleanup(self.close)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self) -> None:
        while not self.answers.empty():
            try:
                self.answers.get_nowait().release.set()
            except queue.Empty:
                break
        self.server.shutdown()
        self.server.server_close()
