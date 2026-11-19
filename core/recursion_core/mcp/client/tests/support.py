"""Test helpers for the MCP client: a scripted stub server and a call run on a thread, both in the test's own
process. Nothing here starts a process or opens a socket; the streams are pipes made with ``os.pipe``.

Hang guard: every wait gives up after ``HANG_GUARD_S`` so that a client that never answers fails the test
instead of hanging the suite. No test passes or fails by timing; ``SHORT`` waits only show that something
has NOT happened.
"""

from __future__ import annotations

import json
import os
import queue
import threading
from typing import Any, Callable

from recursion_core.mcp.client import LEGACY_PROTOCOL, McpClient

# A cut run may shorten it (MCP_CLIENT_TEST_HANG_GUARD_S); a broken cut then fails sooner, never a correct run.
HANG_GUARD_S = float(os.environ.get("MCP_CLIENT_TEST_HANG_GUARD_S", "30"))
SHORT = 0.3


class StubServer:
    """The server's side of two pipes: ``next`` is the client's next message, ``send`` writes one line."""

    def __init__(self) -> None:
        c2s_r, c2s_w = os.pipe()
        s2c_r, s2c_w = os.pipe()
        self.client_in = os.fdopen(s2c_r, "rb")   # the client reads the server's output here
        self.client_out = os.fdopen(c2s_w, "wb")  # the client writes the server's input here
        self._from_client = os.fdopen(c2s_r, "rb")
        self._to_client = os.fdopen(s2c_w, "wb")
        self._lines: queue.Queue = queue.Queue()
        self._stalled = threading.Event()
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def _read(self) -> None:
        for line in self._from_client:
            self._lines.put(json.loads(line))
            if self._stalled.is_set():
                return  # a server that stops reading its input (the pipe fills)
        self._lines.put(None)  # end of input: the client shut the server's input

    def stall(self) -> None:
        self._stalled.set()

    def shut_input(self) -> None:
        """After ``stall`` and one more line: shut the server's input, so the client's next write fails."""

        self._reader.join(HANG_GUARD_S)
        self._from_client.close()

    def next(self, timeout: float = HANG_GUARD_S) -> Any:
        return self._lines.get(timeout=timeout)

    def expect(self, test, method: str) -> dict:
        message = self.next()
        test.assertIsInstance(message, dict, f"waiting for {method}")
        test.assertEqual(method, message.get("method"), message)
        return message

    def send(self, message: dict) -> None:
        self.send_bytes((json.dumps(message) + "\n").encode())

    def send_bytes(self, data: bytes) -> None:
        self._to_client.write(data)
        self._to_client.flush()

    def answer(self, request: dict, result: dict) -> None:
        self.send({"jsonrpc": "2.0", "id": request["id"], "result": result})

    def end_output(self) -> None:
        self._to_client.close()

    def close(self) -> None:
        """Shut every stream without waiting on a lock another thread holds: while the stub still reads, the
        client's output is shut first (the stub reads end of input); once the stub has stopped reading, its
        input is shut first (a client writer blocked on the full pipe then fails)."""

        reading = self._reader.is_alive()
        order = ((self._to_client, self.client_out, "join", self._from_client, self.client_in) if reading
                 else (self._to_client, self._from_client, self.client_out, self.client_in))
        for stream in order:
            if stream == "join":
                self._reader.join(HANG_GUARD_S)
                continue
            try:
                stream.close()
            except Exception:  # noqa: BLE001 - already shut by the client or the test
                pass


class InThread:
    """``fn()`` run on a thread of its own; ``get`` gives its value or raises what it raised."""

    def __init__(self, fn: Callable[[], Any]) -> None:
        self._done = threading.Event()
        self._value: Any = None
        self._error: BaseException | None = None
        threading.Thread(target=self._run, args=(fn,), daemon=True).start()

    def _run(self, fn: Callable[[], Any]) -> None:
        try:
            self._value = fn()
        except BaseException as exc:  # noqa: BLE001 - handed to get()
            self._error = exc
        finally:
            self._done.set()

    def finished(self, timeout: float = SHORT) -> bool:
        return self._done.wait(timeout)

    def get(self, timeout: float = HANG_GUARD_S) -> Any:
        if not self._done.wait(timeout):
            raise AssertionError("the call did not finish within the hang guard")
        if self._error is not None:
            raise self._error
        return self._value


def make_client(test, **kwargs: Any) -> tuple[McpClient, StubServer]:
    stub = StubServer()
    client = McpClient(stub.client_in, stub.client_out, **kwargs)
    test.addCleanup(stub.close)
    test.addCleanup(stub.end_output)
    test.addCleanup(client.close)
    return client, stub


def open_session(test, client: McpClient, stub: StubServer, version: str = LEGACY_PROTOCOL,
                 capabilities: dict | None = None) -> dict:
    """``open`` answered with ``version``; checks that ``notifications/initialized`` follows."""

    opening = InThread(client.open)
    request = stub.expect(test, "initialize")
    stub.answer(request, {"protocolVersion": version, "capabilities": capabilities or {"tools": {}},
                          "serverInfo": {"name": "stub", "version": "1"}})
    stub.expect(test, "notifications/initialized")
    return opening.get()
