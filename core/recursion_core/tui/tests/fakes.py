"""Fakes for the TUI suite: a recording API, a fake engine driver and a manual clock.

Nothing here starts a process, opens a terminal or calls a provider (hard rule 8). The driver's
``model_calls`` counts launches through the control API's starter: a model call can only begin there.
"""

from __future__ import annotations

import json
import pathlib
import threading
import time

from recursion_core.control.api import ControlAPI
from recursion_core.control.registry import EMPTY, InMemoryRunRegistry
from recursion_core.control.tests.fakes import FakeKillTree

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
TRANSCRIPTS = pathlib.Path(__file__).parent / "transcripts"


def fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class RecordingAPI:
    """Wraps any ``call(method, params)`` API and records each request with its monotonic time."""

    def __init__(self, inner) -> None:
        self.inner, self.calls, self.times = inner, [], []
        self._lock = threading.Lock()

    def call(self, method: str, params: dict | None = None) -> dict:
        with self._lock:
            self.calls.append((method, dict(params or {})))
            self.times.append((method, time.monotonic()))
        return self.inner.call(method, params)


class ScriptedAPI:
    """Answers from a fixed table keyed by method, recording requests; for canned-answer fixtures."""

    def __init__(self, answers: dict) -> None:
        self.answers, self.calls = answers, []

    def call(self, method: str, params: dict | None = None) -> dict:
        self.calls.append((method, dict(params or {})))
        answer = self.answers.get(method, {"code": "method_unknown", "detail": method})
        if isinstance(answer, Exception):
            raise answer
        return json.loads(json.dumps(answer))


class FakeDriver:
    """The engine side: a real ControlAPI over fakes, plus an output emitter at a fixed rate."""

    def __init__(self, confirm_answers=(EMPTY,), new_run_id=None) -> None:
        self.registry, self.kill = InMemoryRunRegistry(), FakeKillTree(list(confirm_answers))
        self.model_calls = 0
        self._ids = 0
        self.api = ControlAPI(self.registry, self.kill, self._start, clock=lambda: 1000.0, new_run_id=new_run_id or self._new_id)
        self._stop = threading.Event()
        self.emitted = 0

    def _new_id(self) -> str:  # the API picks the run id (#327); sequential here so goldens stay stable
        self._ids += 1
        return f"run-{self._ids}"

    def _start(self, params: dict) -> dict:
        self.model_calls += 1
        return {"cgroup": f"/sys/fs/cgroup/fake/{params['run_id']}"}

    def start_run(self) -> str:
        return self.api.call("run.start", {"tenant": "t1", "engine": "codex", "prompt": "p"})["run"]["run_id"]

    def emit(self, sink, run_id: str, rate_per_s: int, batch: int = 200, text: str = "x" * 100) -> threading.Thread:
        """Emit ``rate_per_s`` events per second in batches until ``halt``; returns the thread."""

        period = batch / rate_per_s

        def go() -> None:
            nxt = time.monotonic()
            while not self._stop.is_set():
                for _ in range(batch):
                    self.emitted += 1
                    sink({"run_id": run_id, "seq": self.emitted, "text": f"{self.emitted:08d} {text}\n"})
                nxt += period
                delay = nxt - time.monotonic()
                if delay > 0:
                    time.sleep(delay)

        t = threading.Thread(target=go, daemon=True)
        t.start()
        return t

    def halt(self) -> None:
        self._stop.set()
