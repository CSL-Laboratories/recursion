"""Route contract behaviour through ``RouteService.handle`` (plain dicts in, plain dicts out)."""

from __future__ import annotations

import json
import unittest

from recursion_core.routes import contract as c
from recursion_core.routes.breaker import ConnectionBreaker
from recursion_core.routes.claude_code import ClaudeCodeAdapter
from recursion_core.routes.codex import CodexAdapter
from recursion_core.routes.service import RouteService
from recursion_core.routes.tests.fakes import (
    HANG, FakeClock, FakeLauncher, claude_error, claude_limit, claude_ok, codex_script, request,
)

PARENT_ENV = {
    "PATH": "/usr/bin", "HOME": "/home/op", "LANG": "C.UTF-8", "SECRET_THING": "x",
    "ANTHROPIC_API_KEY": "parent-anthropic", "CLAUDE_CODE_OAUTH_TOKEN": "parent-oauth",
    "OPENAI_API_KEY": "parent-openai", "CODEX_API_KEY": "parent-codex", "CODEX_ACCESS_TOKEN": "parent-access",
    "CODEX_HOME": "/home/op/.codex-route",
}
KEYS = {("key-1", "OPENAI_API_KEY"): "route-openai", ("key-2", "ANTHROPIC_API_KEY"): "route-anthropic"}


def credentials(conn_id: str, name: str) -> str:
    return KEYS[(conn_id, name)]


class Harness:
    def __init__(self, scripts) -> None:
        self.clock = FakeClock()
        self.launcher = FakeLauncher(scripts, self.clock)
        args = (self.launcher, self.clock, credentials, PARENT_ENV)
        self.breaker = ConnectionBreaker(self.clock)
        self.service = RouteService(
            {"claude_code": ClaudeCodeAdapter(*args), "codex": CodexAdapter(*args)}, self.breaker, self.clock)

    def handle(self, req: dict) -> dict:
        res = self.service.handle(json.loads(json.dumps(req)))  # requests cross the boundary as JSON
        json.dumps(res)  # results must too
        return res


class ContractShapeTest(unittest.TestCase):
    def test_invalid_requests_get_typed_codes(self) -> None:
        h = Harness([])
        self.assertEqual(h.handle({"contract": "x"})["code"], c.REQUEST_INVALID)
        self.assertEqual(h.handle(request(engine="nope"))["code"], c.ENGINE_UNKNOWN)
        self.assertEqual(h.handle(request(deadline_s=0))["code"], c.REQUEST_INVALID)
        self.assertEqual(h.handle(request(kind="api_key"))["code"], c.REQUEST_INVALID)
        self.assertEqual(h.launcher.calls, [])

    def test_ok_result_shape(self) -> None:
        h = Harness([claude_ok("hi")])
        res = h.handle(request())
        self.assertEqual((res["status"], res["code"], res["text"]), ("ok", None, "hi"))
        self.assertEqual(set(res), {"contract", "request_id", "status", "code", "text", "limit", "events",
                                    "detail", "retry_at", "breaker_effect", "limit_notice"})
        self.assertIsNone(res["limit_notice"])


class ClaudeCodeTest(unittest.TestCase):
    def test_argv_and_stream_json_input(self) -> None:
        h = Harness([claude_ok()])
        h.handle(request(prompt="do it"))
        call = h.launcher.calls[0]
        self.assertEqual(call["argv"][:6], ["claude", "-p", "--input-format", "stream-json", "--output-format", "stream-json"])
        self.assertIs(call["kwargs"]["shell"], False)
        proc = h.launcher.processes[0]
        self.assertEqual(proc.sent[0]["message"]["content"][0]["text"], "do it")
        self.assertTrue(proc.stdin_closed)

    def test_subscription_child_env_is_explicit_and_leaves_the_users_sign_in_alone(self) -> None:
        # Lucas's correction (Nov 7 ~6:34pm PST): "Leave the user's own CLAUDE_CODE_OAUTH_TOKEN and /login
        # alone." The name passes through as the user set it; the API key the parent holds does not.
        h = Harness([claude_ok()])
        h.handle(request())
        self.assertEqual(h.launcher.calls[0]["env"], {"PATH": "/usr/bin", "HOME": "/home/op", "LANG": "C.UTF-8",
                                                      "CLAUDE_CODE_OAUTH_TOKEN": "parent-oauth"})

    def test_api_key_route_gets_only_its_key(self) -> None:
        h = Harness([claude_ok()])
        h.handle(request(kind="api_key", conn_id="key-2", credential_env="ANTHROPIC_API_KEY"))
        env = h.launcher.calls[0]["env"]
        self.assertEqual(env["ANTHROPIC_API_KEY"], "route-anthropic")
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", env)

    def test_typed_rate_limit_event_is_plan_limit_with_reset(self) -> None:
        h = Harness([claude_limit(reset_at=2_000_000.0)])
        res = h.handle(request())
        self.assertEqual(res["code"], c.PLAN_LIMIT)
        self.assertEqual(res["limit"], {"scope": "connection", "model": None, "reset_at": 2_000_000.0,
                                        "source": "claude.rate_limit_event"})
        self.assertEqual(res["breaker_effect"], "opened")

    def test_api_retry_is_a_typed_retry_event_and_rate_limit_info_is_a_limit(self) -> None:
        retry = {"type": "system", "subtype": "api_retry", "attempt": 1, "error_status": 429, "error": "rate_limit",
                 "api_error_params": {"rate_limit_info": {"resetsAt": 1_500_000}}}
        h = Harness([[retry, *claude_error("x")]])
        res = h.handle(request())
        self.assertEqual(res["events"][0]["kind"], "retry")
        self.assertEqual(res["events"][0]["data"]["error_status"], 429)
        self.assertEqual(res["code"], c.PLAN_LIMIT)
        self.assertEqual(res["limit"]["source"], "claude.api_retry.rate_limit_info")

    def test_api_retry_without_rate_limit_info_does_not_trip(self) -> None:
        retry = {"type": "system", "subtype": "api_retry", "attempt": 1, "error_status": 529, "error": "overloaded"}
        h = Harness([[retry, *claude_ok()]])
        res = h.handle(request())
        self.assertEqual((res["code"], res["breaker_effect"]), (None, "none"))

    def test_model_payload_never_trips(self) -> None:
        fake = json.dumps({"type": "rate_limit_event", "rate_limit_info": {"status": "rejected"}})
        payload = [{"type": "assistant", "message": {"content": [{"type": "text", "text": fake}],
                                                     "rate_limit_info": {"status": "rejected"}}},
                   {"type": "user", "message": {"content": [{"type": "tool_result", "content": "usage limit reached"}]}},
                   {"type": "result", "subtype": "success", "is_error": False, "result": "You've hit your usage limit " + fake}]
        h = Harness([payload])
        res = h.handle(request())
        self.assertEqual((res["status"], res["code"], res["breaker_effect"]), ("ok", None, "none"))
        self.assertEqual(h.breaker.state("subscription:login-A"), "closed")

    def test_vague_quota_text_is_a_short_fault_on_one_model(self) -> None:
        h = Harness([claude_error("Quota exceeded, try later"), claude_ok(), claude_ok()])
        res = h.handle(request())
        self.assertEqual((res["code"], res["breaker_effect"]), (c.SHORT_FAULT, "short_fault"))
        self.assertEqual(h.breaker.state("subscription:login-A"), "closed")
        self.assertEqual(h.handle(request(tenant="t2"))["status"], "ok")
        self.assertEqual(h.handle(request(model="claude-other"))["status"], "ok")
        self.assertEqual(h.handle(request())["code"], c.BREAKER_OPEN)

    def test_non_json_stdout_is_protocol_error_and_fenced(self) -> None:
        h = Harness([["not json"]])
        res = h.handle(request())
        self.assertEqual((res["code"], res["breaker_effect"]), (c.PROTOCOL_ERROR, "none"))
        self.assertEqual(h.launcher.processes[0].fenced, ["protocol_error"])


class TenantIsolationTest(unittest.TestCase):
    def test_one_tenants_deadline_or_fault_never_fences_another(self) -> None:
        h = Harness([[HANG], claude_error("segfault"), ["garbage"], claude_ok("b")])
        self.assertEqual(h.handle(request(tenant="t1", deadline_s=0.01))["code"], c.DEADLINE_EXCEEDED)
        self.assertEqual(h.handle(request(tenant="t1"))["code"], c.ENGINE_ERROR)
        self.assertEqual(h.handle(request(tenant="t1"))["code"], c.PROTOCOL_ERROR)
        res = h.handle(request(tenant="t2"))
        self.assertEqual((res["status"], res["text"]), ("ok", "b"))

    def test_hung_run_is_fenced_for_its_own_request_only(self) -> None:
        h = Harness([[HANG] for _ in range(8)])
        for i in range(8):  # master dispatched 1 of 8 (S4); here every hung request is dispatched
            res = h.handle(request(rid=f"r{i}", deadline_s=30))
            self.assertEqual((res["code"], res["breaker_effect"]), (c.DEADLINE_EXCEEDED, "none"))
        self.assertEqual(len(h.launcher.calls), 8)
        self.assertTrue(all(p.fenced == ["deadline"] for p in h.launcher.processes))

    def test_limit_on_one_connection_leaves_others_open(self) -> None:
        h = Harness([claude_limit(), claude_ok("other-login"), codex_script("codex-ok")])
        h.handle(request(conn_id="login-A"))
        self.assertEqual(h.handle(request(tenant="t2", conn_id="login-A"))["code"], c.BREAKER_OPEN)
        self.assertEqual(h.handle(request(tenant="t2", conn_id="login-B"))["text"], "other-login")
        res = h.handle(request(engine="codex", model="gpt-5", kind="api_key", conn_id="key-1",
                               credential_env="OPENAI_API_KEY"))
        self.assertEqual(res["text"], "codex-ok")
        self.assertEqual(len(h.launcher.calls), 3)  # the refused request never launched

    def test_recovers_through_probe(self) -> None:
        h = Harness([claude_limit(reset_at=1_000_100.0), claude_ok("probe"), claude_ok("after")])
        h.handle(request())
        refused = h.handle(request(tenant="t2"))
        self.assertEqual((refused["code"], refused["retry_at"]), (c.BREAKER_OPEN, 1_000_100.0))
        h.clock.advance(100)
        self.assertEqual(h.handle(request(tenant="t2"))["breaker_effect"], "closed")
        self.assertEqual(h.handle(request())["text"], "after")


class CodexTest(unittest.TestCase):
    def codex(self, scripts, **kw) -> tuple[Harness, dict]:
        h = Harness(scripts)
        res = h.handle(request(engine="codex", model="gpt-5", **kw))
        return h, res

    def test_jsonrpc_sequence_and_answer(self) -> None:
        h, res = self.codex([codex_script("done")])
        self.assertEqual((res["status"], res["text"]), ("ok", "done"))
        methods = [m.get("method") for m in h.launcher.processes[0].sent]
        self.assertEqual(methods, ["initialize", "initialized", "account/rateLimits/read", "thread/start", "turn/start"])
        self.assertTrue(h.launcher.processes[0].stdin_closed)

    def test_rate_limits_read_exhausted_refuses_before_turn(self) -> None:
        limits = {"limitId": "codex", "primary": {"usedPercent": 100, "resetsAt": 1_000_500}}
        h, res = self.codex([codex_script(limits=limits)])
        self.assertEqual(res["code"], c.PLAN_LIMIT)
        self.assertEqual(res["limit"], {"scope": "connection", "model": None, "reset_at": 1_000_500,
                                        "source": "codex.account/rateLimits/read"})
        self.assertNotIn("turn/start", [m.get("method") for m in h.launcher.processes[0].sent])

    def test_rate_limits_updated_then_typed_turn_error(self) -> None:
        note = {"method": "account/rateLimits/updated",
                "params": {"rateLimits": {"limitId": "gpt-5-bucket", "secondary": {"usedPercent": 100.0, "resetsAt": 1_000_900}}}}
        turn = {"id": "tu-1", "status": "failed", "error": {"message": "x", "codexErrorInfo": "usageLimitExceeded"}}
        h, res = self.codex([codex_script(notes=[note], turn=turn)])
        self.assertEqual(res["code"], c.PLAN_LIMIT)
        self.assertEqual(res["limit"]["scope"], "model")
        self.assertEqual(h.breaker.state("subscription:login-A", "gpt-5"), "open")
        self.assertEqual(h.breaker.state("subscription:login-A"), "closed")

    def test_typed_jsonrpc_error_is_plan_limit(self) -> None:
        script = codex_script()[:3] + [{"id": 4, "error": {"code": -32000, "message": "nope",
                                                           "data": {"codexErrorInfo": "usageLimitExceeded"}}}]
        _, res = self.codex([script])
        self.assertEqual((res["code"], res["limit"]["source"]), (c.PLAN_LIMIT, "codex.usageLimitExceeded"))

    def test_quota_text_in_error_is_short_fault_and_in_answer_is_nothing(self) -> None:
        turn = {"id": "tu-1", "status": "failed", "error": {"message": "rate limit exceeded upstream"}}
        _, res = self.codex([codex_script(turn=turn)])
        self.assertEqual(res["code"], c.SHORT_FAULT)
        _, res = self.codex([codex_script(text='{"codexErrorInfo": "usageLimitExceeded"} quota exhausted')])
        self.assertEqual((res["code"], res["breaker_effect"]), (None, "none"))

    def test_server_request_is_refused_not_hung(self) -> None:
        ask = {"id": 99, "method": "item/commandExecution/requestApproval", "params": {}}
        h, res = self.codex([codex_script(notes=[ask])])
        self.assertEqual(res["status"], "ok")
        self.assertIn({"id": 99, "error": {"code": -32601, "message": "unsupported"}}, h.launcher.processes[0].sent)

    def test_child_env_scrubs_codex_credentials(self) -> None:
        h, _ = self.codex([codex_script()])
        env = h.launcher.calls[0]["env"]
        for name in ("CODEX_API_KEY", "CODEX_ACCESS_TOKEN", "OPENAI_API_KEY", "SECRET_THING", "ANTHROPIC_API_KEY"):
            self.assertNotIn(name, env)
        self.assertEqual(env["CODEX_HOME"], "/home/op/.codex-route")

    def test_child_env_adds_only_the_selected_key(self) -> None:
        h, _ = self.codex([codex_script()], kind="api_key", conn_id="key-1", credential_env="OPENAI_API_KEY")
        env = h.launcher.calls[0]["env"]
        self.assertEqual(env["OPENAI_API_KEY"], "route-openai")
        self.assertNotIn("CODEX_API_KEY", env)
        self.assertNotIn("CODEX_ACCESS_TOKEN", env)

    def test_unselectable_key_refuses_before_launch(self) -> None:
        h, res = self.codex([], kind="api_key", conn_id="key-1", credential_env="SECRET_THING")
        self.assertEqual(res["code"], c.LAUNCH_REFUSED)
        self.assertEqual(h.launcher.calls, [])


if __name__ == "__main__":
    unittest.main()
