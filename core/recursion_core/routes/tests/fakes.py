"""Fake clock, process and launcher doubles. No real process, engine or network is ever used."""

from __future__ import annotations

import json
import threading

from recursion_core.routes import contract as c
from recursion_core.routes.launch import ReadTimeout

HANG = object()  # read_line waits out its whole timeout


class FakeClock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now
        self._lock = threading.Lock()

    def __call__(self) -> float:
        with self._lock:
            return self.now

    def advance(self, seconds: float) -> None:
        with self._lock:
            self.now += seconds


class FakeProcess:
    def __init__(self, script, clock: FakeClock | None = None) -> None:
        self.script = [json.dumps(item) if isinstance(item, dict) else item for item in script]
        self.clock = clock
        self.sent: list[dict] = []
        self.stdin_closed = False
        self.fenced: list[str] = []

    def send(self, line: str) -> None:
        assert not self.stdin_closed, "send after close_stdin"
        self.sent.append(json.loads(line))

    def close_stdin(self) -> None:
        self.stdin_closed = True

    def read_line(self, timeout_s: float):
        if not self.script:
            return None
        item = self.script.pop(0)
        if item is HANG:
            if self.clock is not None:
                self.clock.advance(timeout_s)
            raise ReadTimeout()
        return item

    def fence(self, reason: str) -> None:
        self.fenced.append(reason)


class FakeLauncher:
    """Records every spawn. ``scripts`` is a list consumed one per spawn, or a callable(argv, env)."""

    def __init__(self, scripts=None, clock: FakeClock | None = None) -> None:
        self.scripts, self.clock = scripts if scripts is not None else [], clock
        self.calls: list[dict] = []
        self.processes: list[FakeProcess] = []

    def __call__(self, argv, **kwargs) -> FakeProcess:
        assert isinstance(argv, list) and all(isinstance(a, str) for a in argv)
        self.calls.append({"argv": list(argv), "env": dict(kwargs["env"]), "kwargs": kwargs})
        script = self.scripts(argv, kwargs["env"]) if callable(self.scripts) else self.scripts.pop(0)
        proc = FakeProcess(script, self.clock)
        self.processes.append(proc)
        return proc


def request(rid="r1", tenant="t1", engine="claude_code", model="claude-sonnet", conn_id="login-A",
            kind="subscription", credential_env=None, deadline_s=60.0, prompt="hello", **route_extra) -> dict:
    conn = {"kind": kind, "id": conn_id}
    if credential_env:
        conn["credential_env"] = credential_env
    return {"contract": c.CONTRACT_VERSION, "request_id": rid, "tenant": tenant, "prompt": prompt,
            "deadline_s": deadline_s, "route": {"engine": engine, "model": model, "connection": conn, **route_extra}}


# -- Claude Code stream-json scripts ---------------------------------------------------------
def claude_ok(text="answer"):
    return [{"type": "system", "subtype": "init"},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}},
            {"type": "result", "subtype": "success", "is_error": False, "result": text}]


def claude_limit(reset_at=None):
    info = {"status": "rejected", "rateLimitType": "five_hour"}
    if reset_at is not None:
        info["resetsAt"] = reset_at
    return [{"type": "system", "subtype": "init"},
            {"type": "rate_limit_event", "rate_limit_info": info},
            {"type": "result", "subtype": "error_during_execution", "is_error": True, "result": "limit"}]


def claude_error(text="something broke"):
    return [{"type": "result", "subtype": "error_during_execution", "is_error": True, "result": text}]


# -- Codex app-server scripts ----------------------------------------------------------------
def codex_script(text="answer", limits=None, notes=(), turn=None):
    return [{"id": 1, "result": {"userAgent": "fake"}},
            {"id": 2, "result": {"rateLimits": limits or {"limitId": "codex", "primary": {"usedPercent": 10}}}},
            {"id": 3, "result": {"thread": {"id": "th-1"}}},
            {"id": 4, "result": {"turn": {"id": "tu-1"}}},
            *notes,
            {"method": "item/completed", "params": {"item": {"type": "agentMessage", "text": text}}},
            {"method": "turn/completed", "params": {"turn": turn or {"id": "tu-1", "status": "completed"}}}]
