"""Test helpers for the Control MCP server: a scripted control API, a gated one, a captured stdout and the
fixture replayer. Nothing here opens a socket or starts a process.

Hang guard: every wait below gives up after ``HANG_GUARD_S`` so that a server that never answers fails
the test instead of hanging the suite. No test passes or fails by timing; the guard only ends a hang.
(``SHORT`` waits in the test modules only ever show that something has NOT happened.)
"""

from __future__ import annotations

import copy
import json
import os
import queue
import threading
from pathlib import Path
from typing import Any

from recursion_core.control.mcp import server
from recursion_core.control.mcp.framing import encode

# A mutation run may shorten it (MCP_TEST_HANG_GUARD_S); a broken mutant then fails sooner, never a correct run.
HANG_GUARD_S = float(os.environ.get("MCP_TEST_HANG_GUARD_S", "60"))
FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "legacy"


def canon(value: Any) -> Any:
    """A JSON value in a form where ``true`` is not ``1`` and ``1`` is not ``1.0`` (Python's == merges them)."""

    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, int):
        return ("int", value)
    if isinstance(value, float):
        return ("float", value)
    if isinstance(value, str):
        return ("str", value)
    if value is None:
        return ("null",)
    if isinstance(value, list):
        return ("list", tuple(canon(v) for v in value))
    if isinstance(value, dict):
        return ("object", tuple(sorted((k, canon(v)) for k, v in value.items())))
    raise TypeError(f"not a JSON value: {type(value).__name__}")


def assert_json_equal(test, want: Any, got: Any, where: str = "") -> None:
    if canon(want) != canon(got):
        test.fail(f"{where}\nwant: {json.dumps(want, sort_keys=True)}\n got: {json.dumps(got, sort_keys=True)}")


class ScriptedAPI:
    """Answers each call from a script, in order, and records every call and every mismatch."""

    def __init__(self, script=()) -> None:
        self._script = [copy.deepcopy(entry) for entry in script]
        self._lock = threading.Lock()
        self.calls: list[tuple[str, Any]] = []
        self.problems: list[str] = []

    def call(self, method: str, params: dict | None = None) -> dict:
        with self._lock:
            self.calls.append((method, copy.deepcopy(params)))
            if not self._script:
                self.problems.append(f"unexpected call {method} {json.dumps(params, sort_keys=True)}")
                return {"code": "fixture_unexpected_call"}
            want = self._script.pop(0)
        if (want["method"], canon(want["params"])) != (method, canon(params)):
            self.problems.append(f"call {method} {json.dumps(params, sort_keys=True)}, fixture expected "
                                 f"{want['method']} {json.dumps(want['params'], sort_keys=True)}")
        return copy.deepcopy(want["answer"])

    def assert_done(self, test, where: str = "") -> None:
        test.assertEqual([], self.problems, where)
        test.assertEqual([], self._script, f"{where}: calls the fixture expected were not made")


class GateAPI:
    """Every call waits until the test releases it. ``entered`` yields ``(method, params, release)``."""

    def __init__(self, answer=None) -> None:
        self._answer = answer or (lambda method, params: {"code": "ok"})
        self.entered: queue.Queue = queue.Queue()
        self.calls: list[tuple[str, Any]] = []
        self._lock = threading.Lock()

    def call(self, method: str, params: dict | None = None) -> dict:
        release = threading.Event()
        with self._lock:
            self.calls.append((method, copy.deepcopy(params)))
        self.entered.put((method, copy.deepcopy(params), release))
        if not release.wait(HANG_GUARD_S):
            raise AssertionError(f"{method} was never released")
        return self._answer(method, params)

    def next_entered(self) -> tuple[str, Any, threading.Event]:
        return self.entered.get(timeout=HANG_GUARD_S)

    def assert_nothing_entered(self, test) -> None:
        test.assertTrue(self.entered.empty(), "a call reached the control API")


class Capture:
    """A binary stdout: keeps the raw bytes and hands each complete line, parsed, to a queue."""

    def __init__(self) -> None:
        self.raw = bytearray()
        self.lines: queue.Queue = queue.Queue()
        self._pending = b""
        self._lock = threading.Lock()

    def write(self, data: bytes) -> int:
        with self._lock:
            self.raw += data
            self._pending += data
            while b"\n" in self._pending:
                line, self._pending = self._pending.split(b"\n", 1)
                self.lines.put(json.loads(line))
        return len(data)

    def flush(self) -> None:
        pass

    def next(self) -> Any:
        return self.lines.get(timeout=HANG_GUARD_S)

    def drain(self) -> list:
        out = []
        while True:
            try:
                out.append(self.lines.get_nowait())
            except queue.Empty:
                return out


class QueueWriter:
    """The ``write`` callable of an in-process ``LegacySession``: each message goes to a queue."""

    def __init__(self) -> None:
        self.messages: queue.Queue = queue.Queue()

    def __call__(self, message: dict) -> bool:
        encode(message)  # what the real writer would refuse, this one refuses too
        self.messages.put(copy.deepcopy(message))
        return True

    def next(self) -> dict:
        return self.messages.get(timeout=HANG_GUARD_S)

    def drain(self) -> list:
        out = []
        while True:
            try:
                out.append(self.messages.get_nowait())
            except queue.Empty:
                return out


def write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


class Stalling(Capture):
    """A ``Capture`` whose write of a message with an id in ``stall_ids`` blocks until ``release`` is set
    (a client that stopped reading stdout); with ``fail``, that write then fails as a broken pipe."""

    def __init__(self, stall_ids, *, fail: bool = False) -> None:
        super().__init__()
        self.stall_ids = set(stall_ids)
        self.fail = fail
        self.stalled = threading.Event()
        self.release = threading.Event()

    def write(self, data: bytes) -> int:
        if json.loads(data).get("id") in self.stall_ids:
            self.stalled.set()
            if not self.release.wait(HANG_GUARD_S):
                raise AssertionError("stdout was never released")
            if self.fail:
                raise BrokenPipeError("the client went away")
        return super().write(data)


class Served:
    """``server.serve`` on a thread, reading a real pipe and writing to a ``Capture`` (or to ``out``)."""

    def __init__(self, api: Any, out: Capture | None = None, **kwargs: Any) -> None:
        read_fd, self._write_fd = os.pipe()
        self._instream = os.fdopen(read_fd, "rb")
        self.out = out if out is not None else Capture()
        self.events: list[dict] = []
        self.status: int | None = None
        self._thread = threading.Thread(target=self._serve, args=(api, kwargs), name="mcp-test-serve", daemon=True)
        self._thread.start()

    def _serve(self, api: Any, kwargs: dict) -> None:
        # stdin is not closed here: after stdout fails, serve returns while its reader still waits on
        # stdin, and closing a buffered stream another thread is reading waits for that read.
        self.status = server.serve(api, self._instream, self.out, log=self.events.append, **kwargs)

    def send(self, message: Any) -> None:
        self.send_bytes(encode(message) if isinstance(message, dict) else message)

    def send_bytes(self, data: bytes) -> None:
        write_all(self._write_fd, data)

    def end_input(self) -> None:
        if self._write_fd is not None:
            os.close(self._write_fd)
            self._write_fd = None

    def join(self, timeout: float = HANG_GUARD_S) -> bool:
        """``True`` when ``serve`` has returned within ``timeout``. Once it has and input has ended, stdin
        is closed (a reader still running, when serve ended on an output failure, has end of input)."""

        self._thread.join(timeout)
        returned = not self._thread.is_alive()
        if returned and self._write_fd is None:
            self._instream.close()
        return returned

    def finish(self, test) -> list:
        """End input, wait for ``serve`` to return 0, and return what was written after the last read."""

        self.end_input()
        test.assertTrue(self.join(), "serve did not return after end of input")
        test.assertEqual(0, self.status)
        return self.out.drain()


def step_bytes(step: dict) -> bytes:
    if "send" in step:
        return encode(step["send"]) if isinstance(step["send"], dict) else (json.dumps(step["send"]) + "\n").encode()
    if "send_text" in step:
        return step["send_text"].encode("utf-8")
    return bytes.fromhex(step["send_hex"])


def fixture_files() -> list[Path]:
    return sorted(FIXTURES.glob("*/*.json"))


def replay(test, path: Path) -> None:
    """Replay one fixture through ``serve``: each step's bytes, then exactly its expected answers, in order."""

    doc = json.loads(path.read_text(encoding="ascii"))
    name = doc["name"]
    test.assertEqual("mcp-stdio-fixture/1", doc["format"], name)
    test.assertEqual(server.LEGACY_PROTOCOL, doc["protocol"], name)
    api = ScriptedAPI(doc["api"])
    served = Served(api)
    try:
        for index, step in enumerate(doc["steps"]):
            served.send_bytes(step_bytes(step))
            for want in step["expect"]:
                assert_json_equal(test, want, served.out.next(), f"{name} step {index}")
    finally:
        rest = served.finish(test)
    assert_json_equal(test, doc["after_end_of_input"], rest, f"{name} after end of input")
    api.assert_done(test, name)
