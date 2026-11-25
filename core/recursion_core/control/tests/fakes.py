"""Fake cleanup layer and starter. Nothing here touches a real process, cgroup or signal."""

from __future__ import annotations

from recursion_core.control.api import ControlAPI
from recursion_core.control.registry import EMPTY, InMemoryRunRegistry


class FakeKillTree:
    """``answers`` is the sequence ``confirm`` returns (an Exception instance is raised)."""

    def __init__(self, answers=(EMPTY,), request_error: Exception | None = None) -> None:
        self.answers, self.request_error = list(answers), request_error
        self.requests: list[str] = []
        self.confirms: list[str] = []

    def request_stop(self, cgroup: str) -> None:
        self.requests.append(cgroup)
        if self.request_error is not None:
            raise self.request_error

    def confirm(self, cgroup: str, timeout_s: float) -> str:
        self.confirms.append(cgroup)
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer


class FakeStarter:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, params: dict) -> dict:
        self.calls.append(params)
        return {"cgroup": f"/sys/fs/cgroup/fake/{params['run_id']}"}


def run_ids():
    """Deterministic ``new_run_id``: run-1, run-2, ..."""
    n = iter(range(1, 1_000_000))
    return lambda: f"run-{next(n)}"


class AdmitsAsAsked:
    """A stand-in for the routine store, for tests of the channel and the start path rather than of the
    store (control/tests/test_routine_*.py test the store): every trigger start is admitted with the values
    it was asked with, nothing is charged and nothing is kept."""

    def admit_start(self, origin, asked=None, *, charge=True, reread=False, launching=None, key=None):
        answer = {"code": "ok", "start": dict(asked or {})}
        if launching is not None:
            answer["run_id"] = launching()
        return answer

    def launched(self, origin, run_id):
        return True

    def recorded(self, origin, run_id):
        return True

    def give_back(self, origin, run_id):
        return True

    def settle(self, recorded):
        return []


def make_api(kill: FakeKillTree | None = None, registry=None, starter=None, routines=None):
    """A control API over fakes. A trigger start needs a routine store; ``routines`` defaults to the
    stand-in above."""
    registry, kill, starter = registry or InMemoryRunRegistry(), kill or FakeKillTree(), starter or FakeStarter()
    api = ControlAPI(registry, kill, starter, clock=lambda: 1000.0, new_run_id=run_ids(),
                     routines=AdmitsAsAsked() if routines is None else routines)
    return api, registry, kill, starter


def started(api) -> str:
    return api.call("run.start", {"tenant": "t1", "engine": "codex", "prompt": "p"})["run"]["run_id"]
