"""Claude Code driver per Lucas's correction (Nov 7 ~6:34pm PST), Codex own login, and limit handling.

Fakes only: no real ``claude`` or ``codex`` process, no settings or credential file, no provider call.
"""

from __future__ import annotations

import builtins
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from recursion_core.routes import contract as c
from recursion_core.routes import limits as lim
from recursion_core.routes.breaker import ConnectionBreaker
from recursion_core.routes.claude_code import ClaudeCodeAdapter, SUBSCRIPTION_DROP_ENV
from recursion_core.routes.codex import CodexAdapter
from recursion_core.routes.service import RouteService
from recursion_core.routes.tests.fakes import (FakeClock, FakeLauncher, claude_error, claude_limit, claude_ok,
                                               codex_script, request)

PKG = Path(__file__).resolve().parents[1]
PARENT = {
    "PATH": "/usr/bin", "HOME": "/home/op", "LANG": "C.UTF-8",
    "CLAUDE_CODE_OAUTH_TOKEN": "users-own-token", "CLAUDE_CONFIG_DIR": "/home/op/.claude-alt",
    "CLAUDE_CODE_SIMPLE": "1", "ANTHROPIC_API_KEY": "parent-key", "ANTHROPIC_AUTH_TOKEN": "parent-bearer",
    "ANTHROPIC_PROFILE": "work", "CLAUDE_CODE_USE_BEDROCK": "1", "CLAUDE_CODE_USE_VERTEX": "1",
    "CLAUDE_CODE_USE_FOUNDRY": "1", "ANTHROPIC_FEDERATION_EXAMPLE": "x", "AWS_REGION": "us-east-1",
    "CODEX_HOME": "/home/op/.codex", "OPENAI_API_KEY": "parent-openai", "CODEX_ACCESS_TOKEN": "parent-access",
}
KEYS = {("key-2", "ANTHROPIC_API_KEY"): "route-anthropic"}


class Harness:
    def __init__(self, scripts, limits=None, credentials=None) -> None:
        self.clock = FakeClock()
        self.launcher = FakeLauncher(scripts, self.clock)
        self.lookups: list[tuple[str, str]] = []

        def creds(conn_id, name):
            self.lookups.append((conn_id, name))
            return (credentials or KEYS)[(conn_id, name)]

        args = (self.launcher, self.clock, creds, PARENT)
        self.breaker = ConnectionBreaker(self.clock)
        self.service = RouteService({"claude_code": ClaudeCodeAdapter(*args), "codex": CodexAdapter(*args)},
                                    self.breaker, self.clock, limits=limits)

    def handle(self, req):
        res = self.service.handle(json.loads(json.dumps(req)))
        json.dumps(res)
        return res


class SubscriptionRouteTest(unittest.TestCase):
    def test_no_bare_and_per_run_drops(self) -> None:
        h = Harness([claude_ok()])
        self.assertEqual(h.handle(request())["status"], "ok")
        call = h.launcher.calls[0]
        self.assertNotIn("--bare", call["argv"])
        env = call["env"]
        for name in SUBSCRIPTION_DROP_ENV + ("ANTHROPIC_FEDERATION_EXAMPLE", "AWS_REGION"):
            self.assertNotIn(name, env)

    def test_users_own_sign_in_left_alone(self) -> None:
        h = Harness([claude_ok()])
        h.handle(request())
        env = h.launcher.calls[0]["env"]
        self.assertEqual(env["CLAUDE_CODE_OAUTH_TOKEN"], "users-own-token")  # the user's value, as set
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], "/home/op/.claude-alt")
        self.assertEqual(h.lookups, [])  # custody is never asked for a sign-in token

    def test_no_oauth_token_is_set_when_the_user_has_none(self) -> None:
        clock = FakeClock()
        launcher = FakeLauncher([claude_ok()], clock)
        parent = {k: v for k, v in PARENT.items() if k != "CLAUDE_CODE_OAUTH_TOKEN"}
        svc = RouteService({"claude_code": ClaudeCodeAdapter(launcher, clock, lambda *a: "x", parent)},
                           ConnectionBreaker(clock), clock)
        svc.handle(request())
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", launcher.calls[0]["env"])
        self.assertNotIn("DISABLE_LOGIN_COMMAND", launcher.calls[0]["env"])

    def test_bare_refused_on_subscription(self) -> None:
        h = Harness([])
        res = h.handle(request(bare=True))
        self.assertEqual(res["code"], c.LAUNCH_REFUSED)
        self.assertEqual(h.launcher.calls, [])

    def test_api_key_helper_present_is_shown_and_asked_not_overridden(self) -> None:
        h = Harness([claude_ok()])
        res = h.handle(request(api_key_helper="present"))
        self.assertEqual(res["code"], c.ROUTE_CHOICE_REQUIRED)
        self.assertIn("bill your API key", res["detail"])
        self.assertEqual(h.launcher.calls, [])
        ok = h.handle(request(rid="r2", api_key_helper="present", billing_ack="api_key_helper"))
        self.assertEqual(ok["status"], "ok")  # the user picked: runs as their settings say, nothing changed

    def test_unreported_api_key_helper_is_a_question(self) -> None:
        h = Harness([])
        res = h.handle(request(api_key_helper=None))
        self.assertEqual(res["code"], c.ROUTE_CHOICE_REQUIRED)
        self.assertEqual(h.handle(request(api_key_helper="maybe"))["code"], c.REQUEST_INVALID)
        self.assertEqual(h.launcher.calls, [])

    def test_bare_must_be_a_bool(self) -> None:
        self.assertEqual(Harness([]).handle(request(bare="yes"))["code"], c.REQUEST_INVALID)


class ApiKeyAndPassthroughTest(unittest.TestCase):
    def test_api_key_route_custody_key_and_bare_allowed(self) -> None:
        h = Harness([claude_ok()])
        res = h.handle(request(kind="api_key", conn_id="key-2", credential_env="ANTHROPIC_API_KEY", bare=True))
        self.assertEqual(res["status"], "ok")
        call = h.launcher.calls[0]
        self.assertEqual(call["argv"][:2], ["claude", "--bare"])
        self.assertEqual(call["env"]["ANTHROPIC_API_KEY"], "route-anthropic")  # their own key, from custody
        self.assertEqual(call["env"]["CLAUDE_CODE_OAUTH_TOKEN"], "users-own-token")  # never dropped
        for name in ("ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK"):
            self.assertNotIn(name, call["env"])

    def test_api_key_helper_route_adds_no_key(self) -> None:
        h = Harness([claude_ok()])
        conn_req = request(kind="api_key", conn_id="helper-1")
        conn_req["route"]["connection"]["key_source"] = "api_key_helper"
        self.assertEqual(h.handle(conn_req)["status"], "ok")
        env = h.launcher.calls[0]["env"]
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], "/home/op/.claude-alt")  # the user's settings, untouched
        self.assertEqual(env["CLAUDE_CODE_OAUTH_TOKEN"], "users-own-token")
        self.assertEqual(h.lookups, [])

    def test_key_source_only_for_claude_code_api_key(self) -> None:
        for engine, kind in (("codex", "api_key"), ("claude_code", "subscription")):
            req = request(engine=engine, kind=kind, conn_id="x", credential_env="OPENAI_API_KEY")
            req["route"]["connection"]["key_source"] = "api_key_helper"
            self.assertEqual(Harness([]).handle(req)["code"], c.REQUEST_INVALID, (engine, kind))

    def test_cloud_methods_pass_only_their_own_variables(self) -> None:
        # The picked method's variables pass through unchanged for this run; the daemon's other secrets
        # (another engine's key, a stray ANTHROPIC_API_KEY that would bill the API) never reach it.
        parent = dict(PARENT, AWS_SECRET_ACCESS_KEY="aws-s", AWS_PROFILE="p", CLOUD_ML_REGION="us-east5",
                      ANTHROPIC_VERTEX_PROJECT_ID="proj", VERTEX_REGION_CLAUDE_HAIKU_4_5="eu",
                      ANTHROPIC_FOUNDRY_RESOURCE="res", ANTHROPIC_BASE_URL="https://gw.example", DAEMON_SECRET="d")
        expected = {
            "bedrock": {"CLAUDE_CODE_USE_BEDROCK": "1", "AWS_REGION": "us-east-1", "AWS_SECRET_ACCESS_KEY": "aws-s",
                        "AWS_PROFILE": "p"},
            "vertex": {"CLAUDE_CODE_USE_VERTEX": "1", "CLOUD_ML_REGION": "us-east5",
                       "ANTHROPIC_VERTEX_PROJECT_ID": "proj", "VERTEX_REGION_CLAUDE_HAIKU_4_5": "eu"},
            "foundry": {"CLAUDE_CODE_USE_FOUNDRY": "1", "ANTHROPIC_FOUNDRY_RESOURCE": "res"},
            "gateway": {"ANTHROPIC_BASE_URL": "https://gw.example", "ANTHROPIC_AUTH_TOKEN": "parent-bearer"},
        }
        base = {"PATH": "/usr/bin", "HOME": "/home/op", "LANG": "C.UTF-8",
                "CLAUDE_CODE_OAUTH_TOKEN": "users-own-token", "CLAUDE_CONFIG_DIR": "/home/op/.claude-alt"}
        for method, own in expected.items():
            h = Harness([claude_ok()])
            h.service._adapters["claude_code"]._parent_env = dict(parent)
            req = request(kind="passthrough", conn_id=f"user-{method}")
            req["route"]["connection"]["method"] = method
            self.assertEqual(h.handle(req)["status"], "ok", method)
            env = h.launcher.calls[0]["env"]
            self.assertEqual(env, {**base, **own}, method)  # this run: the method's own names, values unchanged
            for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "CODEX_ACCESS_TOKEN", "DAEMON_SECRET"):
                self.assertNotIn(name, env, (method, name))

    def test_unlisted_passthrough_method_refuses_before_launch(self) -> None:
        h = Harness([])
        req = request(kind="passthrough", conn_id="user-x")
        req["route"]["connection"]["method"] = "some-other-builtin"
        self.assertEqual(h.handle(req)["code"], c.LAUNCH_REFUSED)
        self.assertEqual(h.launcher.calls, [])

    def test_passthrough_needs_method_and_is_claude_code_only(self) -> None:
        self.assertEqual(Harness([]).handle(request(kind="passthrough"))["code"], c.REQUEST_INVALID)
        req = request(engine="codex", kind="passthrough")
        req["route"]["connection"]["method"] = "x"
        self.assertEqual(Harness([]).handle(req)["code"], c.REQUEST_INVALID)

    def test_bare_is_the_users_choice_on_passthrough(self) -> None:
        h = Harness([claude_ok()])
        req = request(kind="passthrough", bare=True)
        req["route"]["connection"]["method"] = "bedrock"
        self.assertEqual(h.handle(req)["status"], "ok")
        self.assertEqual(h.launcher.calls[0]["argv"][:2], ["claude", "--bare"])
        self.assertEqual(h.launcher.calls[0]["env"]["CLAUDE_CODE_OAUTH_TOKEN"], "users-own-token")

    def test_oauth_token_kept_on_every_route_and_parent_env_never_changed(self) -> None:
        before = dict(PARENT)
        h = Harness([claude_ok(), claude_ok(), claude_ok(), claude_ok()])
        reqs = [request(rid="s"), request(rid="k", kind="api_key", conn_id="key-2", credential_env="ANTHROPIC_API_KEY"),
                request(rid="h", kind="api_key", conn_id="helper"), request(rid="p", kind="passthrough")]
        reqs[2]["route"]["connection"]["key_source"] = "api_key_helper"
        reqs[3]["route"]["connection"]["method"] = "vertex"
        for req in reqs:
            self.assertEqual(h.handle(req)["status"], "ok", req["request_id"])
        for call in h.launcher.calls:
            self.assertEqual(call["env"]["CLAUDE_CODE_OAUTH_TOKEN"], "users-own-token")
        self.assertEqual(PARENT, before)
        adapter = h.service._adapters["claude_code"]
        self.assertEqual(adapter._parent_env, before)
        from recursion_core.routes import claude_code
        for drop in (claude_code.SUBSCRIPTION_DROP_ENV, claude_code.API_KEY_DROP_ENV):
            self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", drop)

    def test_nothing_removes_a_sign_in_method_from_every_run(self) -> None:
        # Each route changes only its own run: the next subscription run still sees the user's sign-in.
        h = Harness([claude_ok(), claude_ok()])
        h.handle(request(kind="api_key", conn_id="key-2", credential_env="ANTHROPIC_API_KEY"))
        h.handle(request(rid="r2"))
        self.assertEqual(h.launcher.calls[1]["env"]["CLAUDE_CODE_OAUTH_TOKEN"], "users-own-token")
        self.assertEqual(PARENT["CLAUDE_CODE_OAUTH_TOKEN"], "users-own-token")


class UnmodifiedBinaryTest(unittest.TestCase):
    def test_binary_is_claude_itself(self) -> None:
        clock = FakeClock()
        ClaudeCodeAdapter(FakeLauncher([], clock), clock, lambda *a: "", {}, binary="/opt/claude/bin/claude")
        for binary in ("claude-wrapper", "/usr/bin/node", "", None):
            with self.assertRaises(ValueError):
                ClaudeCodeAdapter(FakeLauncher([], clock), clock, lambda *a: "", {}, binary=binary)

    def test_adapter_never_touches_settings_or_credentials(self) -> None:
        text = (PKG / "claude_code.py").read_text()
        code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
        for forbidden in ("open(", "settings.json", ".credentials", '"DISABLE_LOGIN_COMMAND"', "write_text",
                          "os.environ", "import os"):
            self.assertNotIn(forbidden, code)

    def test_run_opens_no_file(self) -> None:
        h = Harness([claude_ok()])
        real_open = builtins.open

        def guard(path, *a, **k):
            raise AssertionError(f"opened {path}")
        with patch.object(builtins, "open", guard), patch.object(os, "open", guard):
            res = h.service.handle(request())
        self.assertEqual(res["status"], "ok")
        self.assertIs(builtins.open, real_open)


class CodexOwnLoginTest(unittest.TestCase):
    def test_codex_subscription_uses_its_own_cli_login_only(self) -> None:
        h = Harness([codex_script("ok")])
        res = h.handle(request(engine="codex", model="gpt-5"))
        self.assertEqual(res["status"], "ok")
        env = h.launcher.calls[0]["env"]
        self.assertEqual(env["CODEX_HOME"], "/home/op/.codex")  # Codex's own login store location
        for name in ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN"):
            self.assertNotIn(name, env)
        self.assertEqual(h.lookups, [])  # Recursion holds no ChatGPT token and asks custody for none


def limited_then(*scripts):
    return [claude_limit(reset_at=1_000_500.0), *scripts]


class LimitsTest(unittest.TestCase):
    def test_default_is_show_and_ask_never_switch(self) -> None:
        h = Harness(limited_then(claude_ok("never")))
        fallback = [{"engine": "claude_code", "model": "m2", "connection": {"kind": "subscription", "id": "login-B"},
                     "api_key_helper": "absent"}]
        res = h.handle(request(limits={"fallback": fallback}))  # on_limit omitted: ask
        self.assertEqual(res["code"], c.PLAN_LIMIT)
        notice = res["limit_notice"]
        self.assertEqual(notice["on_limit"], "ask")
        self.assertIsNone(notice["switched_to"])
        self.assertEqual(notice["reset_at"], 1_000_500.0)
        self.assertEqual([ch["action"] for ch in notice["choices"]], ["wait", "switch", "cancel"])
        self.assertIn("subscription:login-A", notice["message"])
        self.assertEqual(len(h.launcher.calls), 1)  # nothing else ran

    def test_service_default_is_ask(self) -> None:
        h = Harness(limited_then())
        res = h.handle(request())
        self.assertEqual(res["limit_notice"]["on_limit"], "ask")
        self.assertEqual(res["limit_notice"]["choices"], [{"action": "wait", "until": 1_000_500.0},
                                                          {"action": "cancel"}])

    def test_breaker_open_refusal_also_shows_the_limit(self) -> None:
        h = Harness(limited_then())
        h.handle(request())
        res = h.handle(request(rid="r2"))
        self.assertEqual(res["code"], c.BREAKER_OPEN)
        self.assertEqual(res["limit_notice"]["reset_at"], 1_000_500.0)

    def test_short_fault_refusal_is_no_limit_and_never_switches(self) -> None:
        # Grill Q23: vague quota text fences one model for one tenant; it is not a usage limit, so the
        # follow-up refusal gets no notice and the user's fallback order is not used.
        h = Harness([claude_error("Quota exceeded, try later")])
        order = [{"engine": "codex", "model": "gpt-5", "connection": {"kind": "subscription", "id": "c"}}]
        first = h.handle(request(limits={"on_limit": "fallback", "fallback": order}))
        self.assertEqual((first["code"], first["limit_notice"]), (c.SHORT_FAULT, None))
        res = h.handle(request(rid="r2", limits={"on_limit": "fallback", "fallback": order}))
        self.assertEqual((res["code"], res["limit_notice"]), (c.SHORT_FAULT, None))
        self.assertEqual(len(h.launcher.calls), 1)  # no fallback route ever launched

    def test_probe_in_flight_shows_the_limit_and_follows_the_users_choice(self) -> None:
        h = Harness(limited_then(codex_script("codex answer")))
        h.handle(request())
        h.clock.advance(500)
        self.assertIsNotNone(h.breaker.admit("subscription:login-A", "claude-sonnet", "t9")["probe"])
        asked = h.handle(request(rid="r2"))
        self.assertEqual(asked["code"], c.PROBE_IN_FLIGHT)
        self.assertEqual(asked["limit_notice"]["on_limit"], "ask")
        self.assertEqual([ch["action"] for ch in asked["limit_notice"]["choices"]], ["wait", "cancel"])
        order = [{"engine": "codex", "model": "gpt-5", "connection": {"kind": "subscription", "id": "codex-login"}}]
        res = h.handle(request(rid="r3", limits={"on_limit": "fallback", "fallback": order}))
        self.assertEqual(res["text"], "codex answer")
        self.assertEqual(res["limit_notice"]["code"], c.PROBE_IN_FLIGHT)
        self.assertEqual(res["limit_notice"]["switched_to"]["engine"], "codex")

    def test_probe_lease_follows_the_run_deadline(self) -> None:
        # A probe run with a 600 s deadline still holds its lease 599 s in: no second probe is admitted.
        seen = []

        def scripts(argv, env):
            if len(seen) == 0:
                seen.append("limit")
                return claude_limit(reset_at=1_000_500.0)
            h.clock.advance(599)
            seen.append(h.breaker.admit("subscription:login-A", "claude-sonnet", "t2", deadline_s=600)["code"])
            return claude_ok("probe")

        h = Harness(scripts)
        h.handle(request())
        h.clock.advance(500)
        res = h.handle(request(rid="r2", deadline_s=600))
        self.assertEqual((res["text"], res["breaker_effect"]), ("probe", "closed"))
        self.assertEqual(seen, ["limit", c.PROBE_IN_FLIGHT])

    def test_wait_runs_nothing_else(self) -> None:
        h = Harness(limited_then())
        res = h.handle(request(limits={"on_limit": "wait"}))
        self.assertEqual((res["limit_notice"]["on_limit"], len(h.launcher.calls)), ("wait", 1))

    def test_fallback_follows_the_users_order_visibly(self) -> None:
        h = Harness(limited_then(claude_limit(), claude_ok("from C")))
        order = [{"engine": "claude_code", "model": "m-b", "connection": {"kind": "subscription", "id": "login-B"},
                  "api_key_helper": "absent"},
                 {"engine": "claude_code", "model": "m-c", "connection": {"kind": "subscription", "id": "login-C"},
                  "api_key_helper": "absent"}]
        res = h.handle(request(limits={"on_limit": "fallback", "fallback": order}))
        self.assertEqual((res["status"], res["text"]), ("ok", "from C"))
        notice = res["limit_notice"]
        self.assertEqual(notice["switched_to"]["connection"], "subscription:login-C")
        self.assertEqual([a["connection"] for a in notice["attempts"]],
                         ["subscription:login-A", "subscription:login-B", "subscription:login-C"])
        self.assertEqual(res["events"][0]["kind"], "fallback")
        self.assertEqual([call["argv"][-1] for call in h.launcher.calls], ["claude-sonnet", "m-b", "m-c"])

    def test_fallback_all_limited_returns_the_first_limit_with_notice(self) -> None:
        h = Harness(limited_then(claude_limit()))
        order = [{"engine": "claude_code", "model": "m-b", "connection": {"kind": "subscription", "id": "login-B"},
                  "api_key_helper": "absent"}]
        res = h.handle(request(limits={"on_limit": "fallback", "fallback": order}))
        self.assertEqual(res["code"], c.PLAN_LIMIT)
        self.assertIsNone(res["limit_notice"]["switched_to"])
        self.assertEqual([e["kind"] for e in res["events"]][-1], "fallback")

    def test_fallback_to_another_provider_per_request(self) -> None:
        h = Harness(limited_then(codex_script("codex answer")))
        order = [{"engine": "codex", "model": "gpt-5", "connection": {"kind": "subscription", "id": "codex-login"}}]
        res = h.handle(request(limits={"on_limit": "fallback", "fallback": order}))
        self.assertEqual(res["text"], "codex answer")
        self.assertEqual(res["limit_notice"]["switched_to"]["engine"], "codex")

    def test_non_limit_failures_never_switch(self) -> None:
        h = Harness([[{"type": "result", "subtype": "error", "is_error": True, "result": "boom"}]])
        order = [{"engine": "codex", "model": "gpt-5", "connection": {"kind": "subscription", "id": "c"}}]
        res = h.handle(request(limits={"on_limit": "fallback", "fallback": order}))
        self.assertEqual((res["code"], res["limit_notice"]), (c.ENGINE_ERROR, None))
        self.assertEqual(len(h.launcher.calls), 1)

    def test_invalid_limit_configs_refused(self) -> None:
        bad = [{"on_limit": "fallback"}, {"on_limit": "switch_silently"}, {"fallback": "x"}, {"other": 1},
               {"on_limit": "fallback", "fallback": [{"engine": "nope", "model": "m",
                                                      "connection": {"kind": "subscription", "id": "x"}}]},
               {"on_limit": "fallback", "fallback": [{"engine": "codex", "model": "m", "limits": {},
                                                      "connection": {"kind": "subscription", "id": "x"}}]},
               {"fallback": [{"engine": "codex", "model": "m", "connection": {"kind": "subscription", "id": "x"}}] * 9}]
        for limits in bad:
            h = Harness([])
            self.assertIn(h.handle(request(limits=limits))["code"], (c.REQUEST_INVALID, c.ENGINE_UNKNOWN), limits)
            self.assertEqual(h.launcher.calls, [])
        with self.assertRaises(c.RouteError):
            lim.validate_limits({"on_limit": "fallback", "fallback": []})

    def test_notice_never_carries_a_credential(self) -> None:
        h = Harness(limited_then())
        res = h.handle(request(kind="api_key", conn_id="key-2", credential_env="ANTHROPIC_API_KEY"))
        self.assertNotIn("route-anthropic", json.dumps(res["limit_notice"]))


if __name__ == "__main__":
    unittest.main()
