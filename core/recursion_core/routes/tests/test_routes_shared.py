"""Route contract shape and tenant isolation through ``RouteService.handle`` (plain dicts in, plain dicts out).

Split out of the former ``test_routes.py`` with the same classes and methods; the shared
harness (``PARENT_ENV``, ``KEYS``, ``credentials``, ``Harness``) is copied verbatim into each part.
"""

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


if __name__ == "__main__":
    unittest.main()
