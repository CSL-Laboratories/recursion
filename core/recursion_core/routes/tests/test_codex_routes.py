"""Codex route behaviour through ``RouteService.handle`` (plain dicts in, plain dicts out).

Split out of the former ``test_routes.py`` with the same classes and methods; the shared
harness (``PARENT_ENV``, ``KEYS``, ``credentials``, ``Harness``) is copied verbatim into each part.
Lane DRIVER-CX keeps this file once the split has merged.
"""

from __future__ import annotations

import json
import unittest

from recursion_core.routes import contract as c
from recursion_core.routes.breaker import ConnectionBreaker
from recursion_core.routes.claude_code import ClaudeCodeAdapter
from recursion_core.routes.codex import CodexAdapter
from recursion_core.routes.service import RouteService
from recursion_core.routes.tests.fakes import FakeClock, FakeLauncher, codex_script, request, runs_adapter

PARENT_ENV = {
    "PATH": "/usr/bin", "HOME": "/home/op", "LANG": "C.UTF-8", "SECRET_THING": "x",
    "ANTHROPIC_API_KEY": "parent-anthropic", "CLAUDE_CODE_OAUTH_TOKEN": "parent-oauth",
    "OPENAI_API_KEY": "parent-openai", "CODEX_API_KEY": "parent-codex", "CODEX_ACCESS_TOKEN": "parent-access",
    "CODEX_HOME": "/home/op/.codex-route",
}
KEYS = {("key-1", "OPENAI_API_KEY"): "route-openai", ("key-2", "ANTHROPIC_API_KEY"): "route-anthropic",
        ("key-3", "CODEX_ACCESS_TOKEN"): "custody-access-token"}


def credentials(conn_id: str, name: str) -> str:
    return KEYS[(conn_id, name)]


class Harness:
    def __init__(self, scripts) -> None:
        self.clock = FakeClock()
        self.launcher = FakeLauncher(scripts, self.clock)
        args = (self.launcher, self.clock, credentials, PARENT_ENV)
        self.breaker = ConnectionBreaker(self.clock)
        self.service = RouteService(
            {"claude_code": ClaudeCodeAdapter(*args, wall_clock=self.clock),
             "codex": CodexAdapter(*args, wall_clock=self.clock)},
            self.breaker, self.clock)

    def handle(self, req: dict) -> dict:
        res = self.service.handle(json.loads(json.dumps(req)))  # requests cross the boundary as JSON
        json.dumps(res)  # results must too
        return res


class CodexTest(unittest.TestCase):
    def codex(self, scripts, **kw) -> tuple[Harness, dict]:
        h = Harness(scripts)
        res = h.handle(request(engine="codex", model="gpt-5", **kw))
        return h, res

    @runs_adapter
    def test_jsonrpc_sequence_and_answer(self) -> None:
        h, res = self.codex([codex_script("done")])
        self.assertEqual((res["status"], res["text"]), ("ok", "done"))
        methods = [m.get("method") for m in h.launcher.processes[0].sent]
        self.assertEqual(methods, ["initialize", "initialized", "account/rateLimits/read", "thread/start", "turn/start"])
        self.assertTrue(h.launcher.processes[0].stdin_closed)

    @runs_adapter
    def test_rate_limits_read_exhausted_refuses_before_turn(self) -> None:
        limits = {"limitId": "codex", "primary": {"usedPercent": 100, "resetsAt": 1_000_500}}
        h, res = self.codex([codex_script(limits=limits)])
        self.assertEqual(res["code"], c.PLAN_LIMIT)
        self.assertEqual(res["limit"], {"scope": "connection", "model": None, "reset_at": 1_000_500,
                                        "source": "codex.account/rateLimits/read"})
        self.assertNotIn("turn/start", [m.get("method") for m in h.launcher.processes[0].sent])

    @runs_adapter
    def test_rate_limits_updated_then_typed_turn_error(self) -> None:
        note = {"method": "account/rateLimits/updated",
                "params": {"rateLimits": {"limitId": "gpt-5-bucket", "secondary": {"usedPercent": 100.0, "resetsAt": 1_000_900}}}}
        turn = {"id": "tu-1", "status": "failed", "error": {"message": "x", "codexErrorInfo": "usageLimitExceeded"}}
        h, res = self.codex([codex_script(notes=[note], turn=turn)])
        self.assertEqual(res["code"], c.PLAN_LIMIT)
        self.assertEqual(res["limit"]["scope"], "model")
        self.assertEqual(res["limit"]["model"], "gpt-5-bucket")
        # The vendor bucket is the key. The route model is not fenced just because this turn asked for it.
        self.assertEqual(h.breaker.state("subscription:login-A", "gpt-5"), "closed")
        self.assertEqual(h.breaker.state("subscription:login-A", "gpt-5-bucket"), "open")
        self.assertEqual(h.breaker.state("subscription:login-A"), "closed")

    @runs_adapter
    def test_failed_turn_after_an_exhausted_bucket_opens_that_bucket(self) -> None:
        note = {"method": "account/rateLimits/updated",
                "params": {"rateLimits": {"limitId": "gpt-5-bucket",
                                           "primary": {"usedPercent": 100, "resetsAt": 1_000_900}}}}
        turn = {"id": "tu-1", "status": "failed", "error": {"message": "upstream error"}}
        h, res = self.codex([codex_script(notes=[note], turn=turn), codex_script("next")])
        self.assertEqual(res["code"], c.PLAN_LIMIT)
        self.assertEqual(res["limit"]["model"], "gpt-5-bucket")
        self.assertEqual(h.breaker.state("subscription:login-A", "gpt-5"), "closed")
        self.assertEqual(h.handle(request(rid="other", engine="codex", model="gpt-5"))["text"], "next")

    @runs_adapter
    def test_api_key_route_ignores_a_chatgpt_rate_limit_read_error(self) -> None:
        script = [{"id": 1, "result": {"userAgent": "fake"}},
                  {"id": 2, "error": {"code": -32000,
                                      "message": "chatgpt authentication required to read rate limits"}},
                  {"id": 3, "result": {"thread": {"id": "th-1"}}},
                  {"id": 4, "result": {"turn": {"id": "tu-1"}}},
                  {"method": "item/completed", "params": {"item": {"type": "agentMessage", "text": "keyed"}}},
                  {"method": "turn/completed", "params": {"turn": {"id": "tu-1", "status": "completed"}}}]
        h, res = self.codex([script], kind="api_key", conn_id="key-1", credential_env="OPENAI_API_KEY")
        self.assertEqual((res["status"], res["text"]), ("ok", "keyed"))
        self.assertEqual(h.breaker.state("api_key:key-1"), "closed")
        methods = [m.get("method") for m in h.launcher.processes[0].sent]
        self.assertIn("turn/start", methods)

    @runs_adapter
    def test_typed_jsonrpc_error_is_plan_limit(self) -> None:
        script = codex_script()[:3] + [{"id": 4, "error": {"code": -32000, "message": "nope",
                                                           "data": {"codexErrorInfo": "usageLimitExceeded"}}}]
        _, res = self.codex([script])
        self.assertEqual((res["code"], res["limit"]["source"]), (c.PLAN_LIMIT, "codex.usageLimitExceeded"))

    @runs_adapter
    def test_quota_text_in_error_is_short_fault_and_in_answer_is_nothing(self) -> None:
        turn = {"id": "tu-1", "status": "failed", "error": {"message": "rate limit exceeded upstream"}}
        _, res = self.codex([codex_script(turn=turn)])
        self.assertEqual(res["code"], c.SHORT_FAULT)
        _, res = self.codex([codex_script(text='{"codexErrorInfo": "usageLimitExceeded"} quota exhausted')])
        self.assertEqual((res["code"], res["breaker_effect"]), (None, "none"))

    @runs_adapter
    def test_server_request_is_refused_not_hung(self) -> None:
        ask = {"id": 99, "method": "item/commandExecution/requestApproval", "params": {}}
        h, res = self.codex([codex_script(notes=[ask])])
        self.assertEqual(res["status"], "ok")
        self.assertIn({"id": 99, "error": {"code": -32601, "message": "unsupported"}}, h.launcher.processes[0].sent)

    @runs_adapter
    def test_child_env_scrubs_codex_credentials(self) -> None:
        h, _ = self.codex([codex_script()])
        env = h.launcher.calls[0]["env"]
        for name in ("CODEX_API_KEY", "CODEX_ACCESS_TOKEN", "OPENAI_API_KEY", "SECRET_THING", "ANTHROPIC_API_KEY"):
            self.assertNotIn(name, env)
        self.assertEqual(env["CODEX_HOME"], "/home/op/.codex-route")

    @runs_adapter
    def test_child_env_adds_only_the_selected_key(self) -> None:
        h, _ = self.codex([codex_script()], kind="api_key", conn_id="key-1", credential_env="OPENAI_API_KEY")
        env = h.launcher.calls[0]["env"]
        self.assertEqual(env["OPENAI_API_KEY"], "route-openai")
        self.assertNotIn("CODEX_API_KEY", env)
        self.assertNotIn("CODEX_ACCESS_TOKEN", env)

    def test_codex_access_token_is_never_selectable(self) -> None:
        # A sign-in token, not an API key: Codex uses its own CLI login only (Nov 8; 3.3, O61). Even when
        # custody would answer for it, the route refuses before asking and nothing launches.
        h, res = self.codex([], kind="api_key", conn_id="key-3", credential_env="CODEX_ACCESS_TOKEN")
        self.assertEqual(res["code"], c.LAUNCH_REFUSED)
        self.assertEqual(h.launcher.calls, [])

    def test_unselectable_key_refuses_before_launch(self) -> None:
        h, res = self.codex([], kind="api_key", conn_id="key-1", credential_env="SECRET_THING")
        self.assertEqual(res["code"], c.LAUNCH_REFUSED)
        self.assertEqual(h.launcher.calls, [])


if __name__ == "__main__":
    unittest.main()
