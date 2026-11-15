# Written by Claude Opus 5.5 (claude-opus-5-5), builder lane ENGINE-E6, for Lucas.
"""A fake Cursor ``agent`` for both drives, for tests only. It starts no process and reads no login.

Both fakes are ``EngineProcess`` doubles (``routes/launch.py``: ``send``, ``close_stdin``, ``read_line``,
``fence``) handed out by ``FakeLauncher``, which records every launch's argv, environment and options, so a
test can hold argv to this lane's rules (``cursor.check_argv``) without running anything.

* ``OneShotProcess`` replays a one-shot stream fixture (``oneshot/*.jsonl``) line by line, then end of file.
* ``AcpProcess`` plays one side of an ACP transcript (``acp/*.jsonl``, lines ``{"dir", "msg"}``): each message
  the client sends must be the transcript's next client message (the same method, or for an answer to the
  agent's own request the same id); the agent messages after it are then readable. The ids of the client's
  requests are the client's own: an agent answer carries the id the client actually used.
  ``read_line`` raises ``ReadTimeout`` while the agent is waiting for the client, and gives end of file
  when the transcript is done.

A fixture's first line, ``{"type": "_fixture", ...}``, names who wrote it and that it is synthetic; the
fakes never replay it, so what a parser sees is what an engine would print.
"""

from __future__ import annotations

import json
from collections import deque
from pathlib import Path

from recursion_core.routes.launch import ReadTimeout

HERE = Path(__file__).resolve().parent


class FakeScriptError(AssertionError):
    """The client did something the transcript does not have at this point."""


def fixture_lines(name: str) -> list[str]:
    """The lines of a fixture, without its ``_fixture`` header, each without its line end."""
    lines = (HERE / name).read_text(encoding="utf-8").splitlines()
    return [line for line in lines if not _is_header(line)]


def fixture_header(name: str) -> dict:
    """A fixture's header line."""
    first = (HERE / name).read_text(encoding="utf-8").splitlines()[0]
    if not _is_header(first):
        raise FakeScriptError(f"{name} has no _fixture header")
    return json.loads(first)


def _is_header(line: str) -> bool:
    try:
        value = json.loads(line)
    except ValueError:
        return False
    return isinstance(value, dict) and value.get("type") == "_fixture"


class _Process:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.stdin_closed = False
        self.fenced: list[str] = []

    def send(self, line: str) -> None:
        if self.stdin_closed:
            raise BrokenPipeError("stdin is closed")
        self.sent.append(line)
        self._received(line)

    def close_stdin(self) -> None:
        self.stdin_closed = True

    def fence(self, reason: str) -> None:
        self.fenced.append(reason)

    def _received(self, line: str) -> None:
        pass


class OneShotProcess(_Process):
    def __init__(self, lines: list[str]) -> None:
        super().__init__()
        self._out = deque(lines)

    def read_line(self, timeout_s: float) -> str | None:
        return self._out.popleft() if self._out else None


class AcpProcess(_Process):
    def __init__(self, entries: list[dict]) -> None:
        super().__init__()
        self._script = deque(entries)
        self._out: deque[str] = deque()
        self._ids: dict = {}   # transcript id of a client request -> the id the client used
        self._release()

    def _release(self) -> None:
        """Make readable every agent message up to the next client message."""
        while self._script and self._script[0]["dir"] == "agent":
            msg = dict(self._script.popleft()["msg"])
            if "method" not in msg and msg.get("id") in self._ids:   # an answer to a client request
                msg["id"] = self._ids.pop(msg["id"])
            self._out.append(json.dumps(msg, separators=(",", ":")))

    def _received(self, line: str) -> None:
        got = json.loads(line)
        if not self._script:
            raise FakeScriptError(f"the transcript is done; the client sent {line!r}")
        want = self._script.popleft()["msg"]
        if "method" in want:
            if got.get("method") != want["method"]:
                raise FakeScriptError(f"expected {want['method']}, got {got.get('method')!r}")
            if "id" in want:
                if "id" not in got:
                    raise FakeScriptError(f"{want['method']} must be a request")
                self._ids[want["id"]] = got["id"]
            elif "id" in got:
                raise FakeScriptError(f"{want['method']} must be a notification")
        elif got.get("id") != want.get("id") or "method" in got:
            raise FakeScriptError(f"expected the answer to request {want.get('id')!r}")
        self._release()

    def read_line(self, timeout_s: float) -> str | None:
        if self._out:
            return self._out.popleft()
        if self._script:
            raise ReadTimeout()   # the agent waits for the client
        return None


class FakeLauncher:
    """``launcher(argv, *, env, shell=False, **options)``: records the launch and hands out the fake."""

    def __init__(self, fixture: str) -> None:
        self.fixture = fixture
        self.launches: list[dict] = []
        self.processes: list[_Process] = []

    def __call__(self, argv, *, env, shell=False, **options):
        self.launches.append({"argv": list(argv), "env": dict(env), "shell": shell, "options": dict(options)})
        lines = fixture_lines(self.fixture)
        if self.fixture.startswith("acp/"):
            process: _Process = AcpProcess([json.loads(line) for line in lines])
        else:
            process = OneShotProcess(lines)
        self.processes.append(process)
        return process
