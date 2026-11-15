"""The one-shot OpenCode drive end to end on today's route path (lane ENGINE-E3, slice 2, local).

``RouteService`` -> ``ConnectionBreaker`` -> ``ProfileRouteAdapter`` (the guard, ``guard_then_spawn``) ->
``opencode.route_argv`` and ``opencode.OneShot``, with a fake launcher whose process replays a SYNTHETIC fixture.
Nothing here edits or replaces the guard, the adapter or the service: they are today's code. Run by name:

    PYTHONPATH=core <python> -m unittest recursion_core.routes.engines.fixtures.opencode.test_opencode_route
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from recursion_core.routes import contract as c
from recursion_core.routes.breaker import ConnectionBreaker
from recursion_core.routes.engines import opencode as oc
from recursion_core.routes.launch import ReadTimeout
from recursion_core.routes.profile_route import profile_adapters
from recursion_core.routes.service import RouteService
from recursion_core.routes.tests.fakes import FakeClock, request

HERE = Path(__file__).resolve().parent
MODEL = "openai/gpt-5.5"
PROFILE = {"provider": "openai", "auth": "api_key", "credential_env": "OPENAI_API_KEY"}
HANG = object()


class RawProcess:
    """An engine process whose standard input is plain text (OpenCode reads the message there), not JSON lines."""

    def __init__(self, lines: list, clock: FakeClock) -> None:
        self.lines, self.clock = list(lines), clock
        self.sent: list[str] = []
        self.stdin_closed = False
        self.fenced: list[str] = []

    def send(self, line: str) -> None:
        assert not self.stdin_closed, "send after close_stdin"
        self.sent.append(line)

    def close_stdin(self) -> None:
        self.stdin_closed = True

    def read_line(self, timeout_s: float):
        assert self.stdin_closed, "read before the prompt was closed"
        if not self.lines:
            return None
        line = self.lines.pop(0)
        if line is HANG:
            self.clock.advance(timeout_s)
            raise ReadTimeout()
        return line

    def fence(self, reason: str) -> None:
        self.fenced.append(reason)


class Launcher:
    def __init__(self, clock: FakeClock) -> None:
        self.clock, self.calls, self.processes, self.next = clock, [], [], []

    def __call__(self, argv, **kwargs) -> RawProcess:
        self.calls.append({"argv": list(argv), "env": dict(kwargs["env"]), "shell": kwargs.get("shell")})
        lines = self.next.pop(0)
        if isinstance(lines, str):   # a fixture name: its lines as read_line gives them, without the break
            lines = (HERE / lines).read_text("utf-8").splitlines()
        process = RawProcess(lines, self.clock)
        self.processes.append(process)
        return process


@unittest.skipIf(os.name == "nt", "the guard's route-home checks are POSIX")
class RouteTests(unittest.TestCase):
    def setUp(self):
        root = Path(tempfile.mkdtemp(prefix="e3-route-"))
        self.addCleanup(shutil.rmtree, root, True)
        self.operator = root / "operator"
        self.operator.mkdir(mode=0o700)
        self.home = root / "route-home"
        self.home.mkdir(mode=0o700)
        self.clock = FakeClock()
        self.launcher = Launcher(self.clock)
        parent = {"PATH": "/usr/bin:/bin", "HOME": str(self.operator)}
        adapters = profile_adapters(self.launcher, self.clock, lambda cid, name: "sk-made-up-" + cid, parent,
                                    argv=oc.route_argv, driver=oc.OneShot(self.clock, wall=lambda: 1794614400.0),
                                    operator_home=str(self.operator))
        self.breaker = ConnectionBreaker(self.clock)
        self.service = RouteService(adapters, self.breaker, self.clock)

    def req(self, conn_id="k1", profile=None, credential_env="OPENAI_API_KEY", **kw):
        return request(engine="opencode", model=kw.pop("model", MODEL), kind="api_key", conn_id=conn_id,
                       credential_env=credential_env, profile=dict(profile or PROFILE), home=str(self.home), **kw)

    def run_fixture(self, name, **kw):
        self.launcher.next.append(name)
        return self.service.handle(self.req(**kw))

    def test_an_api_key_run_passes_the_guard_and_writes_the_prompt_on_standard_input(self):
        res = self.run_fixture("synthetic-v1-success.jsonl", prompt="-v --model claude-agent/x")
        self.assertEqual((res["status"], res["code"], res["text"]), ("ok", None, "The tests pass."))
        (call,) = self.launcher.calls
        self.assertEqual(call["argv"], ["opencode", "run", "--format", "json", "--model", MODEL])
        self.assertEqual(call["env"], {"PATH": "/usr/bin:/bin", "HOME": str(self.home),
                                       "OPENAI_API_KEY": "sk-made-up-k1"})
        self.assertIs(call["shell"], False)
        process = self.launcher.processes[0]
        self.assertEqual((process.sent, process.stdin_closed), (["-v --model claude-agent/x"], True))
        (usage,) = [e for e in res["events"] if e["kind"] == "usage"]
        self.assertEqual(usage["data"]["input_tokens"], {"value": 1200, "quality": "known", "provenance": "observed"})

    def test_a_replayed_429_opens_the_breaker_for_that_connection_only(self):
        for name in ("synthetic-v1-limit-429.jsonl", "synthetic-v2-rate-limit.jsonl"):
            with self.subTest(name):
                self.setUp()
                res = self.run_fixture(name)
                self.assertEqual((res["code"], res["breaker_effect"]), (c.PLAN_LIMIT, "opened"))
                again = self.service.handle(self.req())                 # the same connection: refused, no spawn
                self.assertEqual(again["code"], c.BREAKER_OPEN)
                self.assertEqual(len(self.launcher.calls), 1)
                other = self.run_fixture("synthetic-v1-success.jsonl", conn_id="k2")   # another connection runs
                self.assertEqual((other["code"], len(self.launcher.calls)), (None, 2))

    def test_a_plain_error_does_not_open_it(self):
        for name, code in (("synthetic-v1-error-no-status.jsonl", c.ENGINE_ERROR),
                           ("synthetic-v2-transport-error.jsonl", c.ENGINE_ERROR),
                           ("synthetic-v1-exit-1-no-output.jsonl", c.ENGINE_EXITED)):
            with self.subTest(name):
                self.setUp()
                res = self.run_fixture(name)
                self.assertEqual((res["code"], res["breaker_effect"]), (code, "none"))
                self.assertEqual(self.run_fixture("synthetic-v1-success.jsonl")["code"], None)

    def test_without_an_exit_status_the_last_step_decides(self):
        self.assertEqual(self.run_fixture("synthetic-v1-tool-use.jsonl")["code"], None)    # ends "stop"
        tool_calls_only = (HERE / "synthetic-v1-tool-use.jsonl").read_text("utf-8").splitlines()[:3]
        self.launcher.next.append(tool_calls_only)                                        # ends "tool-calls"
        res = self.service.handle(self.req())
        self.assertEqual((res["code"], res["detail"]), (c.ENGINE_EXITED, "no exit status and no finished step"))

    def test_refusals_come_before_any_spawn(self):
        cases = [
            ({"provider": "claude-agent", "auth": "subscription", "credential_env": "ANTHROPIC_API_KEY"},
             "ANTHROPIC_API_KEY", c.SUBSCRIPTION_ROUTE_REFUSED),          # OpenCode's Claude Agent provider
            ({"provider": "google", "auth": "subscription", "credential_env": "OPENAI_API_KEY"},
             "OPENAI_API_KEY", c.SUBSCRIPTION_ROUTE_REFUSED),             # a Google label
            ({"provider": "openai", "auth": "api_key", "credential_env": "CLAUDE_CODE_OAUTH_TOKEN"},
             "CLAUDE_CODE_OAUTH_TOKEN", c.LAUNCH_REFUSED),                # a Claude token for the child
        ]
        for profile, env_name, code in cases:
            with self.subTest(profile):
                res = self.service.handle(self.req(profile=profile, credential_env=env_name))
                self.assertEqual(res["code"], code)
                self.assertEqual(res["breaker_effect"], "none")
        self.assertEqual(self.launcher.calls, [])

    def test_a_hung_engine_is_fenced_at_the_deadline_for_this_request_only(self):
        self.launcher.next.append([HANG])
        res = self.service.handle(self.req(deadline_s=5.0))
        self.assertEqual((res["code"], res["breaker_effect"]), (c.DEADLINE_EXCEEDED, "none"))
        self.assertEqual(self.launcher.processes[0].fenced, ["deadline"])
        self.assertEqual(self.breaker.state("api_key:k1"), "closed")

    def test_an_invalid_model_is_a_typed_refusal(self):
        for model in ("gpt", "-x/y", "a b/c"):
            with self.subTest(model):
                with self.assertRaises(c.RouteError) as caught:
                    oc.route_argv(self.req(model=model))
                self.assertEqual((caught.exception.code, caught.exception.detail), (c.REQUEST_INVALID, "route.model"))
        self.assertEqual(oc.route_argv(self.req(), binary="/opt/oc/bin/opencode")[0], "/opt/oc/bin/opencode")

    def test_a_connection_the_parser_refuses_writes_nothing(self):
        process = RawProcess([], self.clock)
        req = self.req(conn_id="a\nb")
        res = oc.OneShot(self.clock)(process, req, self.clock() + 60, [])
        self.assertEqual((res["code"], res["detail"]), (c.REQUEST_INVALID, "route.connection_id"))
        self.assertEqual((process.sent, process.stdin_closed), ([], False))


if __name__ == "__main__":
    unittest.main()
