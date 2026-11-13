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
        self.assertNotEqual(env["ANTHROPIC_API_KEY"], PARENT_ENV["ANTHROPIC_API_KEY"])
        # Lucas's correction: nothing removes a sign-in method from every run; the key outranks it here.
        self.assertEqual(env["CLAUDE_CODE_OAUTH_TOKEN"], "parent-oauth")

    def test_typed_rate_limit_event_is_plan_limit_with_reset(self) -> None:
        h = Harness([claude_limit(reset_at=2_000_000.0)])
        res = h.handle(request())
        self.assertEqual(res["code"], c.PLAN_LIMIT)
        self.assertEqual(res["limit"], {"scope": "connection", "model": None, "reset_at": 2_000_000.0,
                                        "source": "claude.rate_limit_event"})
        self.assertEqual(res["breaker_effect"], "opened")

    def test_api_retry_with_rate_limit_info_does_not_open_the_connection(self) -> None:
        # A 429 Claude is still retrying is a retry event. It is not a plan limit, even with
        # rate_limit_info, and a later unrelated error does not book the connection.
        retry = {"type": "system", "subtype": "api_retry", "attempt": 1, "error_status": 429, "error": "rate_limit",
                 "api_error_params": {"rate_limit_info": {"status": "rejected", "resetsAt": 1_500_000,
                                                         "rateLimitType": "five_hour"}}}
        h = Harness([[retry, *claude_error("error_max_turns")]])
        res = h.handle(request())
        self.assertEqual(res["events"][0]["kind"], "retry")
        self.assertEqual(res["events"][0]["data"]["error_status"], 429)
        self.assertEqual(res["code"], c.ENGINE_ERROR)
        self.assertEqual(h.breaker.state("subscription:login-A"), "closed")
        h = Harness([[retry]])  # stream ends after the retry, with no result
        res = h.handle(request())
        self.assertEqual(res["code"], c.ENGINE_EXITED)
        self.assertEqual(h.breaker.state("subscription:login-A"), "closed")

    def test_result_rate_limit_info_is_a_plan_limit_even_without_status(self) -> None:
        result = {"type": "result", "subtype": "error_during_execution", "is_error": True,
                  "api_error_params": {"rate_limit_info": {"resetsAt": 1_000_800}}}
        h = Harness([[result]])
        res = h.handle(request())
        self.assertEqual(res["code"], c.PLAN_LIMIT)
        self.assertEqual(res["limit"]["source"], "claude.result.rate_limit_info")
        self.assertEqual(res["limit"]["reset_at"], 1_000_800.0)

    def test_opus_weekly_window_does_not_fence_sonnet(self) -> None:
        info = {"status": "rejected", "rateLimitType": "seven_day_opus", "resetsAt": 1_000_400.0}
        script = [{"type": "rate_limit_event", "rate_limit_info": info},
                  {"type": "result", "subtype": "error", "is_error": True, "result": "limit"}]
        h = Harness([script, claude_ok("sonnet"), claude_ok("other-opus")])
        res = h.handle(request(model="claude-opus-4"))
        self.assertEqual(res["limit"], {"scope": "model", "model": "opus", "reset_at": 1_000_400.0,
                                        "source": "claude.rate_limit_event"})
        self.assertEqual(h.breaker.state("subscription:login-A"), "closed")
        self.assertEqual(h.handle(request(rid="s", model="claude-sonnet-4"))["status"], "ok")
        self.assertEqual(h.handle(request(rid="o", model="claude-opus-4-5"))["code"], c.BREAKER_OPEN)

    def test_vendor_epoch_reset_is_translated_onto_a_monotonic_clock(self) -> None:
        mono, wall = FakeClock(1_000.0), FakeClock(1_700_000_000.0)
        launcher = FakeLauncher([claude_limit(reset_at=wall.now + 300.0)], mono)
        adapter = ClaudeCodeAdapter(launcher, mono, credentials, PARENT_ENV, wall_clock=wall)
        service = RouteService({"claude_code": adapter}, ConnectionBreaker(mono), mono)
        res = service.handle(request())
        self.assertEqual(res["code"], c.PLAN_LIMIT)
        self.assertEqual(res["limit"]["reset_at"], 1_300.0)
        refused = service.handle(request(rid="r2"))
        self.assertEqual((refused["code"], refused["retry_at"]), (c.BREAKER_OPEN, 1_300.0))

    def test_limit_without_reset_tells_the_user_the_cooldown(self) -> None:
        h = Harness([[{"type": "result", "subtype": "error", "is_error": True, "error": "rate_limit"}]])
        res = h.handle(request(limits={"on_limit": "wait"}))
        self.assertEqual(res["code"], c.PLAN_LIMIT)
        self.assertIsNone(res["limit"]["reset_at"])
        self.assertEqual(res["limit_notice"]["choices"][0], {"action": "wait", "until": 1_000_300.0})

    def test_broken_pipe_is_a_typed_engine_error_and_fenced(self) -> None:
        class Pipe:
            def __init__(self) -> None:
                self.fenced: list[str] = []

            def send(self, line: str) -> None:
                raise BrokenPipeError("closed")

            def close_stdin(self) -> None:
                return None

            def read_line(self, timeout_s: float):
                return None

            def fence(self, reason: str) -> None:
                self.fenced.append(reason)

        clock = FakeClock()
        pipe = Pipe()
        adapter = ClaudeCodeAdapter(lambda argv, **kwargs: pipe, clock, credentials, PARENT_ENV, wall_clock=clock)
        service = RouteService({"claude_code": adapter}, ConnectionBreaker(clock), clock)
        res = service.handle(request())
        self.assertEqual(res["code"], c.ENGINE_ERROR)
        self.assertIn("BrokenPipeError", res["detail"])
        self.assertEqual(pipe.fenced, ["io_error"])
        self.assertEqual(service._breaker.state("subscription:login-A"), "closed")

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
        refused = h.handle(request())
        # The short fence's own refusal is a short fault, not a usage limit: no notice, no switch point.
        self.assertEqual((refused["code"], refused["limit_notice"]), (c.SHORT_FAULT, None))
        self.assertEqual(refused["detail"], "short_fault:claude-sonnet")

    def test_non_json_stdout_is_protocol_error_and_fenced(self) -> None:
        h = Harness([["not json"]])
        res = h.handle(request())
        self.assertEqual((res["code"], res["breaker_effect"]), (c.PROTOCOL_ERROR, "none"))
        self.assertEqual(h.launcher.processes[0].fenced, ["protocol_error"])

    def test_non_list_result_errors_are_protocol_error_and_fenced(self) -> None:
        # A result envelope whose errors field is not a list used to raise TypeError
        # after the prompt was written, and the child was left running.
        for errors in (1, True, False):
            with self.subTest(errors=errors):
                script = [{"type": "result", "subtype": "error", "is_error": True, "result": "x", "errors": errors}]
                h = Harness([script])
                res = h.handle(request())
                self.assertEqual((res["code"], res["breaker_effect"]), (c.PROTOCOL_ERROR, "none"))
                proc = h.launcher.processes[0]
                self.assertTrue(proc.sent)
                self.assertEqual(proc.fenced, ["protocol_error"])

    def test_unexpected_exception_after_launch_is_fenced(self) -> None:
        class Boom:
            def __init__(self) -> None:
                self.fenced: list[str] = []

            def send(self, line: str) -> None:
                raise RuntimeError("boom")

            def close_stdin(self) -> None:
                return None

            def read_line(self, timeout_s: float):
                return None

            def fence(self, reason: str) -> None:
                self.fenced.append(reason)

        clock = FakeClock()
        boom = Boom()
        adapter = ClaudeCodeAdapter(lambda argv, **kwargs: boom, clock, credentials, PARENT_ENV, wall_clock=clock)
        service = RouteService({"claude_code": adapter}, ConnectionBreaker(clock), clock)
        res = service.handle(request())
        self.assertEqual(res["code"], c.ENGINE_ERROR)
        self.assertEqual(res["detail"], "unexpected: RuntimeError")
        self.assertNotIn("hello", res["detail"])
        self.assertEqual(boom.fenced, ["engine_error"])
        self.assertEqual(service._breaker.state("subscription:login-A"), "closed")

    def test_fence_failure_after_launch_returns_engine_error_and_opens_breaker(self) -> None:
        class Dead:
            def __init__(self) -> None:
                self.fenced: list[str] = []
                self.killed: list[str] = []

            def send(self, line: str) -> None:
                raise RuntimeError("boom")

            def close_stdin(self) -> None:
                return None

            def read_line(self, timeout_s: float):
                return None

            def fence(self, reason: str) -> None:
                self.fenced.append(reason)
                raise OSError("cgroup")

            def kill(self) -> None:
                self.killed.append("kill")

        clock = FakeClock()
        dead = Dead()
        calls: list[list[str]] = []

        def launch(argv, **kwargs):
            calls.append(list(argv))
            return dead

        adapter = ClaudeCodeAdapter(launch, clock, credentials, PARENT_ENV, wall_clock=clock)
        service = RouteService({"claude_code": adapter}, ConnectionBreaker(clock), clock)
        res = service.handle(request())
        self.assertEqual(res["code"], c.ENGINE_ERROR)
        self.assertEqual(res["detail"], "fence failed: OSError")
        self.assertNotIn("boom", res["detail"])
        self.assertNotIn("cgroup", res["detail"])
        self.assertEqual(dead.fenced, ["engine_error", "engine_error"])
        self.assertEqual(dead.killed, ["kill"])
        self.assertEqual(res["breaker_effect"], "opened")
        self.assertEqual(service._breaker.state("subscription:login-A"), "open")
        again = service.handle(request(rid="r2"))
        self.assertEqual(again["code"], c.BREAKER_OPEN)
        self.assertEqual(len(calls), 1)

    def test_fence_retry_then_success_keeps_the_typed_result(self) -> None:
        class Flaky:
            def __init__(self) -> None:
                self.fenced: list[str] = []
                self.killed: list[str] = []

            def send(self, line: str) -> None:
                raise RuntimeError("boom")

            def close_stdin(self) -> None:
                return None

            def read_line(self, timeout_s: float):
                return None

            def fence(self, reason: str) -> None:
                self.fenced.append(reason)
                if len(self.fenced) == 1:
                    raise RuntimeError("cgroup")

            def kill(self) -> None:
                self.killed.append("kill")

        clock = FakeClock()
        flaky = Flaky()
        adapter = ClaudeCodeAdapter(lambda argv, **kwargs: flaky, clock, credentials, PARENT_ENV, wall_clock=clock)
        service = RouteService({"claude_code": adapter}, ConnectionBreaker(clock), clock)
        res = service.handle(request())
        self.assertEqual((res["code"], res["detail"]), (c.ENGINE_ERROR, "unexpected: RuntimeError"))
        self.assertNotIn("boom", res["detail"])
        self.assertNotIn("cgroup", res["detail"])
        self.assertEqual(flaky.fenced, ["engine_error", "engine_error"])
        self.assertEqual(flaky.killed, [])
        self.assertEqual(service._breaker.state("subscription:login-A"), "closed")


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
        self.assertEqual(res["limit"]["model"], "gpt-5-bucket")
        # The vendor bucket is the key. The route model is not fenced just because this turn asked for it.
        self.assertEqual(h.breaker.state("subscription:login-A", "gpt-5"), "closed")
        self.assertEqual(h.breaker.state("subscription:login-A", "gpt-5-bucket"), "open")
        self.assertEqual(h.breaker.state("subscription:login-A"), "closed")

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

    def test_api_key_rate_limit_read_typed_usage_limit_opens_the_breaker(self) -> None:
        # A typed usage limit on account/rateLimits/read is a plan limit on an api_key
        # route too. Ignoring every RPC error there sends turn/start and leaves the breaker closed.
        cases = (
            {"code": -32000, "message": "usage limit exceeded",
             "data": {"codexErrorInfo": "usageLimitExceeded"}},
            {"code": -32000, "message": "chatgpt authentication required to read rate limits",
             "data": {"codexErrorInfo": "usageLimitExceeded"}},
        )
        for error in cases:
            with self.subTest(error=error["message"]):
                script = [{"id": 1, "result": {"userAgent": "fake"}}, {"id": 2, "error": error}]
                h, res = self.codex([script], kind="api_key", conn_id="key-1", credential_env="OPENAI_API_KEY")
                self.assertEqual(res["code"], c.PLAN_LIMIT)
                self.assertEqual(res["limit"]["source"], "codex.usageLimitExceeded")
                self.assertEqual(res["breaker_effect"], "opened")
                self.assertEqual(h.breaker.state("api_key:key-1"), "open")
                methods = [m.get("method") for m in h.launcher.processes[0].sent]
                self.assertNotIn("thread/start", methods)
                self.assertNotIn("turn/start", methods)

    def test_api_key_rate_limit_read_ignores_only_the_chatgpt_auth_rejection(self) -> None:
        script = [{"id": 1, "result": {"userAgent": "fake"}},
                  {"id": 2, "error": {"code": -32000, "message": "internal failure"}}]
        h, res = self.codex([script], kind="api_key", conn_id="key-1", credential_env="OPENAI_API_KEY")
        self.assertEqual(res["code"], c.ENGINE_ERROR)
        self.assertEqual(h.breaker.state("api_key:key-1"), "closed")
        methods = [m.get("method") for m in h.launcher.processes[0].sent]
        self.assertNotIn("thread/start", methods)
        self.assertNotIn("turn/start", methods)

    def test_api_key_ignores_only_the_exact_chatgpt_auth_shape(self) -> None:
        # Substring "chatgpt" + "auth" must not start a turn. Typed codexErrorInfo in any
        # shape that is not a recognized benign auth code fails closed before thread/start.
        auth = "chatgpt authentication required to read rate limits"
        text_cases = {
            "You've hit your ChatGPT Plus limit. Re-authenticate to continue.": c.ENGINE_ERROR,
            "rate limit exceeded for chatgpt oauth users": c.SHORT_FAULT,
            "author of the chatgpt plugin failed": c.ENGINE_ERROR,
        }
        for message, code in text_cases.items():
            with self.subTest(message=message):
                script = [{"id": 1, "result": {"userAgent": "fake"}},
                          {"id": 2, "error": {"code": -32000, "message": message}},
                          {"id": 3, "result": {"thread": {"id": "th-1"}}},
                          {"id": 4, "result": {"turn": {"id": "tu-1"}}},
                          {"method": "turn/completed",
                           "params": {"turn": {"id": "tu-1", "status": "completed"}}}]
                h, res = self.codex([script], kind="api_key", conn_id="key-1", credential_env="OPENAI_API_KEY")
                self.assertEqual(res["code"], code)
                methods = [m.get("method") for m in h.launcher.processes[0].sent]
                self.assertNotIn("thread/start", methods)
                self.assertNotIn("turn/start", methods)
        typed = (
            {"message": auth, "data": {"codexErrorInfo": "quotaExceeded"}},
            {"message": auth, "data": {"codexErrorInfo": "UsageLimitExceeded"}},
            {"message": auth, "data": {"codexErrorInfo": ["usageLimitExceeded"]}},
            {"message": auth, "data": {"codexErrorInfo": {"type": "usageLimitExceeded"}}},
            {"message": auth, "data": {"codexErrorInfo": {"detail": "x", "usageLimitExceeded": {}}}},
            {"message": auth, "codexErrorInfo": None, "data": {"codexErrorInfo": "usageLimitExceeded"}},
        )
        for error in typed:
            with self.subTest(error=error):
                script = [{"id": 1, "result": {"userAgent": "fake"}},
                          {"id": 2, "error": {"code": -32000, **error}},
                          {"id": 3, "result": {"thread": {"id": "th-1"}}},
                          {"id": 4, "result": {"turn": {"id": "tu-1"}}},
                          {"method": "item/completed",
                           "params": {"item": {"type": "agentMessage", "text": "should-not-run"}}},
                          {"method": "turn/completed",
                           "params": {"turn": {"id": "tu-1", "status": "completed"}}}]
                h, res = self.codex([script], kind="api_key", conn_id="key-1", credential_env="OPENAI_API_KEY")
                self.assertEqual(res["code"], c.PLAN_LIMIT)
                self.assertEqual(res["breaker_effect"], "opened")
                self.assertEqual(h.breaker.state("api_key:key-1"), "open")
                info = error.get("data", {}).get("codexErrorInfo")
                if info == "quotaExceeded":
                    self.assertEqual(res["limit"]["source"], "codex.quotaExceeded")
                else:
                    self.assertEqual(res["limit"]["source"], "codex.usageLimitExceeded")
                methods = [m.get("method") for m in h.launcher.processes[0].sent]
                self.assertNotIn("thread/start", methods)
                self.assertNotIn("turn/start", methods)
                self.assertNotEqual(res.get("text"), "should-not-run")

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
