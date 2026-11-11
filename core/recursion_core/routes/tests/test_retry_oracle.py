"""Oracle for retry, backoff, attempt booking and final outcomes (CSL-797).

One table says, for each error class and each limit, how many attempts run, how long the next
attempt waits, and what the final code is. The breaker and the service are expected to match the
table. Fakes only: no provider, no network, no real process.
"""

from __future__ import annotations

import json
import random
import threading
import time
import unittest
from dataclasses import dataclass, field

from recursion_core.routes import contract as c
from recursion_core.routes import limits as lim
from recursion_core.routes.breaker import ConnectionBreaker
from recursion_core.routes.base import Deadline
from recursion_core.routes.claude_code import ClaudeCodeAdapter
from recursion_core.routes.codex import CodexAdapter
from recursion_core.routes.profile_route import profile_adapters
from recursion_core.routes.service import RouteService
from recursion_core.routes.tests.fakes import (
    FakeClock, FakeLauncher, FakeProcess, claude_error, claude_limit, claude_ok, codex_script, request,
)

CONN = "subscription:A"
COOLDOWN = 300
MAX_COOLDOWN = 3600
MAX_OPEN = 8 * 24 * 3600
SHORT = 60
# Consecutive limit incidents with no vendor reset. A repeat while the window is still open is not
# a new incident, so it does not appear here.
BACKOFF = (300, 600, 1200, 2400, 3600, 3600)

# code recorded on a closed key, with no typed limit signal -> effect, next admit, delay before it.
NON_LIMIT = (
    (c.DEADLINE_EXCEEDED, "none", None, 0),
    (c.ENGINE_ERROR, "none", None, 0),
    (c.AUTH_FAILED, "none", None, 0),
    (c.PROTOCOL_ERROR, "none", None, 0),
    (c.ENGINE_EXITED, "none", None, 0),
    (c.LAUNCH_REFUSED, "none", None, 0),
    (c.SUBSCRIPTION_ROUTE_REFUSED, "none", None, 0),
    (c.BREAKER_OPEN, "none", None, 0),
    (c.PROBE_IN_FLIGHT, "none", None, 0),
    (c.ROUTE_CHOICE_REQUIRED, "none", None, 0),
)


def _limit(scope: str, *, model: str | None = None, reset_at: float | None = None) -> dict:
    return c.limit_signal(scope, "oracle", model=model, reset_at=reset_at)


class BreakerOracleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.b = ConnectionBreaker(
            self.clock, cooldown_s=COOLDOWN, max_cooldown_s=MAX_COOLDOWN, max_open_s=MAX_OPEN,
            probe_lease_s=150, short_fault_s=SHORT, probe_grace_s=30)

    def _record(self, code, limit=None, probe=None, model="m", tenant="t1"):
        outcome = {"code": code}
        if limit is not None:
            outcome["limit"] = limit
        return self.b.record(CONN, model, tenant, outcome, probe), outcome

    def _admit(self, model="m", tenant="t1", deadline_s=None):
        return self.b.admit(CONN, model, tenant, deadline_s=deadline_s)

    def test_non_limit_classes_do_not_spend_a_retry(self) -> None:
        # Each class: one record, no fence, the next admit launches immediately (delay 0, not a probe).
        for code, effect, nxt, delay in NON_LIMIT:
            with self.subTest(code=code):
                got, _ = self._record(code)
                admitted = self._admit()
                self.assertEqual((got, self.b.state(CONN), admitted["code"], admitted["probe"], delay),
                                 (effect, "closed", nxt, None, 0))

    def test_plan_limit_without_a_signal_is_not_an_attempt_against_the_budget(self) -> None:
        for bad in (None, "rate limit", {"scope": "everything"}, {"scope": "connection"}):
            effect, _ = self._record(c.PLAN_LIMIT, bad)
            self.assertEqual((effect, self.b.state(CONN)), ("none", "closed"))
        self.assertIsNone(self._admit()["probe"])

    def test_backoff_schedule_and_a_repeat_while_open_is_one_incident(self) -> None:
        # Table: incident -> delay before the next probe is admitted.
        for delay in BACKOFF:
            effect, outcome = self._record(c.PLAN_LIMIT, _limit("connection"))
            self.assertEqual(effect, "opened")
            # A second booking of the same open window must not move the delay out.
            again, _ = self._record(c.PLAN_LIMIT, _limit("connection"))
            self.assertEqual(again, "opened")
            refused = self._admit()
            self.assertEqual(refused["code"], c.BREAKER_OPEN)
            self.assertEqual(refused["retry_at"] - self.clock(), delay)
            self.assertEqual(outcome["retry_at"] - self.clock(), delay)
            self.clock.advance(delay - 1)
            self.assertEqual(self._admit()["code"], c.BREAKER_OPEN)
            self.clock.advance(1)
            probe = self._admit()
            self.assertTrue(probe["probe"])
            # The probe is the next incident only when its own result is a limit. Release it so the
            # following record is that incident, not a duplicate of the one still open.
            self.assertEqual(self.b.record(CONN, "m", "t1", {"code": c.ENGINE_ERROR}, probe["probe"]),
                             "probe_released")

    def test_vendor_reset_sets_the_delay_and_a_later_reset_can_extend_it(self) -> None:
        rows = (
            # (first reset offset, second or None, expected delay, cap note)
            (500, None, 500),
            (100, 500, 500),   # a later vendor reset extends the same incident
            (500, 100, 500),   # an earlier one does not shorten it
            (10**12, None, MAX_OPEN),
        )
        for first, second, delay in rows:
            with self.subTest(first=first, second=second):
                clock = FakeClock()
                breaker = ConnectionBreaker(clock, cooldown_s=COOLDOWN, max_cooldown_s=MAX_COOLDOWN,
                                            max_open_s=MAX_OPEN)
                now = clock()
                breaker.record(CONN, "m", "t1", {"code": c.PLAN_LIMIT, "limit": _limit("connection", reset_at=now + first)})
                if second is not None:
                    breaker.record(CONN, "m", "t1",
                                   {"code": c.PLAN_LIMIT, "limit": _limit("connection", reset_at=clock() + second)})
                refused = breaker.admit(CONN, "m", "t1")
                self.assertEqual(refused["retry_at"] - clock(), delay)
                clock.advance(delay - 1)
                self.assertEqual(breaker.admit(CONN, "m", "t1")["code"], c.BREAKER_OPEN)
                clock.advance(1)
                self.assertTrue(breaker.admit(CONN, "m", "t1")["probe"])

    def test_a_vendor_window_still_advances_the_next_cooldown(self) -> None:
        # The vendor reset is incident 1. The probe that confirms the limit with no reset is incident 2,
        # so the cooldown is the second backoff step, not the first.
        now = self.clock()
        self._record(c.PLAN_LIMIT, _limit("connection", reset_at=now + 100))
        self.clock.advance(100)
        probe = self._admit()["probe"]
        _, outcome = self._record(c.PLAN_LIMIT, _limit("connection"), probe=probe)
        self.assertEqual(outcome["retry_at"] - self.clock(), BACKOFF[1])

    def test_short_fault_budget_is_one_model_one_tenant_for_short_s(self) -> None:
        effect, _ = self._record(c.SHORT_FAULT)
        self.assertEqual(effect, "short_fault")
        refused = self._admit()
        self.assertEqual((refused["code"], refused["retry_at"] - self.clock()), (c.SHORT_FAULT, SHORT))
        self.assertIsNone(self._admit(tenant="t2")["probe"])
        self.assertIsNone(self._admit(model="other")["probe"])
        self.assertEqual(self.b.state(CONN), "closed")
        self.clock.advance(SHORT - 1)
        self.assertEqual(self._admit()["code"], c.SHORT_FAULT)
        self.clock.advance(1)
        self.assertIsNone(self._admit()["probe"])

    def test_short_fault_on_a_probe_is_booked_as_a_short_fault_and_frees_the_lease(self) -> None:
        self._record(c.PLAN_LIMIT, _limit("connection"))
        self.clock.advance(COOLDOWN)
        probe = self._admit()["probe"]
        effect, _ = self._record(c.SHORT_FAULT, probe=probe)
        self.assertEqual(effect, "short_fault")
        self.assertEqual(self._admit()["code"], c.SHORT_FAULT)
        # The other tenant is not in the short fence, and the probe lease is gone, so it may probe.
        self.assertTrue(self._admit(tenant="t2")["probe"])

    def test_probe_token_is_booked_once_and_a_stale_token_does_not_clobber(self) -> None:
        self._record(c.PLAN_LIMIT, _limit("connection"))
        self.clock.advance(COOLDOWN)
        first = self._admit()["probe"]
        self.clock.advance(150)  # lease expired
        second = self._admit()["probe"]
        self.assertNotEqual(first, second)
        # The late limit from the expired probe must not clear the live lease or stack a failure.
        self.assertEqual(self.b.record(CONN, "m", "t1", {"code": c.PLAN_LIMIT, "limit": _limit("connection")}, first),
                         "none")
        self.assertEqual(self._admit()["code"], c.PROBE_IN_FLIGHT)
        # The live probe's success closes. A replay of that token does not reopen.
        self.assertEqual(self.b.record(CONN, "m", "t1", {"code": None}, second), "closed")
        self.assertEqual(self.b.record(CONN, "m", "t1", {"code": None}, second), "none")
        self.assertEqual(self.b.state(CONN), "closed")
        self.assertIsNone(self._admit()["probe"])

    def test_opus_family_is_a_token_not_a_substring(self) -> None:
        self._record(c.PLAN_LIMIT, _limit("model", model="opus"), model="claude-opus-4")
        self.assertIsNone(self._admit(model="notopus")["code"])
        self.assertTrue(self._admit(model="notopus")["admitted"])
        self.assertEqual(self._admit(model="claude-opus-4")["code"], c.BREAKER_OPEN)
        self.assertEqual(self._admit(model="claude-sonnet-4")["code"], None)
        self.assertTrue(self._admit(model="claude-sonnet-4")["admitted"])

    def test_probe_lease_is_deadline_plus_grace(self) -> None:
        self._record(c.PLAN_LIMIT, _limit("connection"))
        self.clock.advance(COOLDOWN)
        self.assertTrue(self._admit(deadline_s=600)["probe"])
        self.clock.advance(629)
        self.assertEqual(self._admit()["code"], c.PROBE_IN_FLIGHT)
        self.clock.advance(1)
        self.assertTrue(self._admit()["probe"])


class ServiceOracleTest(unittest.TestCase):
    """Attempt counts and final codes through RouteService.handle. One deadline, one in-flight id."""

    def _service(self, scripts, limits=None):
        clock = FakeClock()
        launcher = FakeLauncher(scripts, clock)
        args = (launcher, clock, lambda *_: "route-key", {"PATH": "/usr/bin", "HOME": "/h", "LANG": "C"})
        breaker = ConnectionBreaker(clock)
        service = RouteService(
            {"claude_code": ClaudeCodeAdapter(*args, wall_clock=clock),
             "codex": CodexAdapter(*args, wall_clock=clock)},
            breaker, clock, limits=limits)
        return clock, launcher, breaker, service

    def test_attempt_table(self) -> None:
        # on_limit, scripts, expected launches, final code, attempt count, switched connection or None.
        order_b = {"engine": "claude_code", "model": "m-b", "connection": {"kind": "subscription", "id": "login-B"},
                   "api_key_helper": "absent"}
        order_c = {"engine": "claude_code", "model": "m-c", "connection": {"kind": "subscription", "id": "login-C"},
                   "api_key_helper": "absent"}
        rows = (
            ("ask", [claude_limit(), claude_ok("no")], [order_b], 1, c.PLAN_LIMIT, 1, None),
            ("wait", [claude_limit()], [], 1, c.PLAN_LIMIT, 1, None),
            ("fallback", [claude_limit(), claude_limit(), claude_ok("from C")], [order_b, order_c],
             3, None, 3, "subscription:login-C"),
            ("fallback", [claude_limit(), claude_limit()], [order_b], 2, c.PLAN_LIMIT, 2, None),
            ("fallback", [claude_error("boom")], [order_b], 1, c.ENGINE_ERROR, None, None),
        )
        for on_limit, scripts, fallback, launches, code, attempts, switched in rows:
            with self.subTest(on_limit=on_limit, code=code):
                limits = {"on_limit": on_limit, "fallback": fallback} if on_limit != "ask" or fallback else None
                if on_limit == "ask" and fallback:
                    limits = {"fallback": fallback}
                _, launcher, _, service = self._service(list(scripts), limits=limits)
                res = service.handle(request(limits=limits) if limits else request())
                self.assertEqual(res["code"], code)
                self.assertEqual(len(launcher.calls), launches)
                if attempts is None:
                    self.assertIsNone(res["limit_notice"])
                else:
                    self.assertEqual(len(res["limit_notice"]["attempts"]), attempts)
                    got = None if res["limit_notice"]["switched_to"] is None else res["limit_notice"]["switched_to"]["connection"]
                    self.assertEqual(got, switched)

    def test_short_fault_is_not_a_fallback_attempt(self) -> None:
        order = [{"engine": "codex", "model": "gpt-5", "connection": {"kind": "subscription", "id": "c"}}]
        _, launcher, _, service = self._service([claude_error("Quota exceeded, try later")])
        res = service.handle(request(limits={"on_limit": "fallback", "fallback": order}))
        self.assertEqual((res["code"], res["limit_notice"], len(launcher.calls)), (c.SHORT_FAULT, None, 1))

    def test_quota_phrase_table(self) -> None:
        # text -> final code. Word boundaries and a leading "not"/"no".
        rows = (
            ("Quota exceeded, try later", c.SHORT_FAULT),
            ("rate limit exceeded upstream", c.SHORT_FAULT),
            ("too many requests", c.SHORT_FAULT),
            ("unexhausted", c.ENGINE_ERROR),
            ("not exhausted", c.ENGINE_ERROR),
            ("isn't exhausted", c.ENGINE_ERROR),
            ("quota is not exhausted", c.ENGINE_ERROR),
            ("not_exhausted", c.ENGINE_ERROR),
            ("not-rate-limited", c.ENGINE_ERROR),
            ("quota_is_not_exhausted", c.ENGINE_ERROR),
            ('"not exhausted"', c.ENGINE_ERROR),
            ("(not exhausted)", c.ENGINE_ERROR),
            ('{"status":"not exhausted"}', c.ENGINE_ERROR),
            ("quota_exhausted", c.SHORT_FAULT),
            ("quota is not available", c.SHORT_FAULT),
            ("éexhaustedé", c.ENGINE_ERROR),
            ("rate-limited", c.SHORT_FAULT),
            ("rate limiting", c.SHORT_FAULT),
            ("no quota remains", c.SHORT_FAULT),  # "no" is the claim; only not/never cancels
            ("something broke", c.ENGINE_ERROR),
        )
        for text, code in rows:
            with self.subTest(text=text):
                _, _, breaker, service = self._service([claude_error(text)])
                res = service.handle(request())
                self.assertEqual(res["code"], code)
                if code == c.SHORT_FAULT:
                    self.assertEqual(breaker.state("claude_code:subscription:login-A"), "closed")

    def test_same_connection_id_on_two_engines_is_two_meters(self) -> None:
        _, _, breaker, service = self._service([claude_limit(), codex_script("codex-ok")])
        self.assertEqual(service.handle(request(conn_id="shared"))["code"], c.PLAN_LIMIT)
        res = service.handle(request(rid="c", engine="codex", model="gpt-5", conn_id="shared"))
        self.assertEqual(res["text"], "codex-ok")
        self.assertEqual(breaker.state("claude_code:subscription:shared"), "open")
        self.assertEqual(breaker.state("codex:subscription:shared"), "closed")

    def test_api_key_codex_keeps_a_typed_limit_and_drops_only_the_auth_read(self) -> None:
        limited = [{"id": 1, "result": {}},
                   {"id": 2, "error": {"code": -32000, "message": "usage limit",
                                       "codexErrorInfo": "usageLimitExceeded"}}]
        _, launcher, breaker, service = self._service([limited])
        res = service.handle(request(engine="codex", model="gpt-5", kind="api_key", conn_id="key-1",
                                     credential_env="OPENAI_API_KEY"))
        self.assertEqual(res["code"], c.PLAN_LIMIT)
        self.assertNotIn("turn/start", [m.get("method") for m in launcher.processes[0].sent])
        self.assertEqual(breaker.state("api_key:openai:OPENAI_API_KEY:key-1"), "open")

    def test_malformed_engine_objects_are_a_typed_fenced_result(self) -> None:
        # Claude: errors is not a list. Codex: params is a list. Neither escapes, neither leaves the child up.
        _, launcher, breaker, service = self._service(
            [[{"type": "result", "subtype": "success", "is_error": True, "errors": 1}]])
        res = service.handle(request())
        self.assertEqual(res["code"], c.ENGINE_ERROR)
        self.assertEqual(breaker.state("claude_code:subscription:login-A"), "closed")
        self.assertEqual(launcher.processes[0].fenced, [])  # the envelope was classified; the child was not leaked by an exception

        note = {"method": "account/rateLimits/updated", "params": ["not", "an", "object"]}
        _, launcher, _, service = self._service([codex_script(notes=(note,))])
        res = service.handle(request(engine="codex", model="gpt-5"))
        self.assertEqual(res["code"], c.PROTOCOL_ERROR)
        self.assertEqual(launcher.processes[-1].fenced, ["protocol_error"])

    def test_a_secret_in_a_vendor_error_is_not_in_the_result(self) -> None:
        secret = "sk-ant-api03-supersecretvalue"
        retry = {"type": "system", "subtype": "api_retry", "attempt": 1, "max_retries": 2,
                 "error_status": 401, "error": f"bad key {secret}"}
        _, _, _, service = self._service([[retry, *claude_ok()]])
        res = service.handle(request())
        self.assertEqual(res["status"], "ok")
        blob = json.dumps(res)
        self.assertNotIn(secret, blob)
        self.assertIn("[redacted]", blob)
        self.assertEqual(res["events"][0]["data"]["attempt"], 1)

    def test_request_in_flight_does_not_launch_a_second_attempt(self) -> None:
        started, release = threading.Event(), threading.Event()

        class Blocking(FakeProcess):
            def read_line(self, timeout_s):
                started.set()
                self.assert_wait(release)
                return super().read_line(timeout_s)

            @staticmethod
            def assert_wait(flag):
                assert flag.wait(5)

        clock = FakeClock()
        launcher = FakeLauncher([claude_ok(), claude_ok()], clock)

        def launch(argv, **kwargs):
            proc = Blocking([json.dumps(item) if isinstance(item, dict) else item for item in claude_ok()], clock)
            launcher.calls.append({"argv": list(argv), "env": dict(kwargs["env"]), "kwargs": kwargs})
            launcher.processes.append(proc)
            return proc

        # One script is consumed by FakeLauncher only if we use it. Drive the service with the gate launcher.
        launcher.scripts = [claude_ok(), claude_ok()]
        args = (launch, clock, lambda *_: "", {"PATH": "/usr/bin"})
        service = RouteService({"claude_code": ClaudeCodeAdapter(*args, wall_clock=clock)},
                               ConnectionBreaker(clock), clock)
        holder: dict = {}

        def first():
            holder["res"] = service.handle(request())

        thread = threading.Thread(target=first, daemon=True)
        thread.start()
        try:
            self.assertTrue(started.wait(5))
            dup = service.handle(request())
            self.assertEqual((dup["code"], dup["detail"]), (c.LAUNCH_REFUSED, "request in flight"))
            self.assertEqual(len(launcher.calls), 1)
        finally:
            release.set()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(holder["res"]["status"], "ok")
        # After the first run finishes, the same id is a new attempt, not a cached replay.
        again = service.handle(request(rid="r1"))
        self.assertEqual(again["status"], "ok")
        self.assertEqual(len(launcher.calls), 2)

    def test_shutdown_fences_the_child_and_refuses_the_next_request(self) -> None:
        started, release = threading.Event(), threading.Event()

        class Blocking(FakeProcess):
            def read_line(self, timeout_s):
                started.set()
                assert release.wait(5)
                return super().read_line(timeout_s)

        clock = FakeClock()
        seen: list = []

        def launch(argv, **kwargs):
            proc = Blocking([json.dumps(item) for item in claude_ok()], clock)
            seen.append(proc)
            return proc

        service = RouteService(
            {"claude_code": ClaudeCodeAdapter(launch, clock, lambda *_: "", {"PATH": "/usr/bin"}, wall_clock=clock)},
            ConnectionBreaker(clock), clock)
        holder: dict = {}
        thread = threading.Thread(target=lambda: holder.update(res=service.handle(request())), daemon=True)
        thread.start()
        try:
            self.assertTrue(started.wait(5))
            service.shutdown()
            self.assertEqual(seen[0].fenced, ["shutdown"])
            refused = service.handle(request(rid="r2"))
            self.assertEqual((refused["code"], refused["detail"]), (c.LAUNCH_REFUSED, "shutdown"))
            self.assertEqual(len(seen), 1)
        finally:
            release.set()
        thread.join(5)
        self.assertFalse(thread.is_alive())

    def test_cancel_fences_and_does_not_open_the_breaker(self) -> None:
        class Cancel(BaseException):
            pass

        class Proc:
            def __init__(self):
                self.fenced: list[str] = []

            def send(self, line: str) -> None:
                raise Cancel()

            def close_stdin(self) -> None:
                return None

            def read_line(self, timeout_s: float):
                return None

            def fence(self, reason: str) -> None:
                self.fenced.append(reason)

        clock = FakeClock()
        proc = Proc()
        service = RouteService(
            {"claude_code": ClaudeCodeAdapter(lambda argv, **kwargs: proc, clock, lambda *_: "",
                                              {"PATH": "/usr/bin"}, wall_clock=clock)},
            ConnectionBreaker(clock), clock)
        with self.assertRaises(Cancel):
            service.handle(request())
        self.assertEqual(proc.fenced, ["cancelled"])
        self.assertEqual(service._breaker.state("claude_code:subscription:login-A"), "closed")


class ReviewFollowupTest(unittest.TestCase):
    """The second pass: redaction, one meter per credential, shutdown that actually finishes."""

    def test_redaction_covers_tokens_and_leaves_ordinary_words(self) -> None:
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
        rows = (
            ("task-12345678 failed", "task-12345678 failed"),
            ("route-anthropic", "route-anthropic"),
            ("BEARER abcdefghijklmnop", "[redacted]"),
            ("Bearer ab+cd/ef== trailing", "[redacted] trailing"),
            (jwt, "[redacted]"),
            ("https://user:s3cret@example.com/v1", "https://[redacted]@example.com/v1"),
            ("sk-\\u002dabcdefghij", "[redacted]"),
            ("\\u0073\\u006b-abcdefghij", "[redacted]"),
            ("key sk-ant-api03-supersecretvalue end", "key [redacted] end"),
        )
        for raw, expected in rows:
            with self.subTest(raw=raw):
                self.assertEqual(c.redact(raw), expected)

    def test_same_anthropic_key_is_one_meter_and_codex_is_not(self) -> None:
        clock = FakeClock()
        launcher = FakeLauncher([claude_limit(), codex_script("codex-ok")], clock)
        parent = {"PATH": "/usr/bin", "HOME": "/home/operator"}
        profiles = profile_adapters(
            launcher, clock, lambda *_: "sk-test", parent,
            argv=lambda req: ["opencode"], driver=lambda *a: c.result(a[1]["request_id"], None, text="no"),
            operator_home="/home/operator")
        breaker = ConnectionBreaker(clock)
        service = RouteService(
            {"claude_code": ClaudeCodeAdapter(launcher, clock, lambda *_: "sk-test", parent, wall_clock=clock),
             "codex": CodexAdapter(launcher, clock, lambda *_: "sk-test", parent, wall_clock=clock),
             **profiles},
            breaker, clock)
        claude = request(kind="api_key", conn_id="k1", credential_env="ANTHROPIC_API_KEY")
        opencode = request(
            rid="o", engine="opencode", model="m", kind="api_key", conn_id="k1",
            credential_env="ANTHROPIC_API_KEY",
            profile={"provider": "anthropic", "auth": "api_key", "credential_env": "ANTHROPIC_API_KEY"})
        codex = request(rid="c", engine="codex", model="gpt-5", kind="api_key", conn_id="k1",
                        credential_env="OPENAI_API_KEY")
        self.assertEqual(c.breaker_key(claude["route"]), "api_key:anthropic:ANTHROPIC_API_KEY:k1")
        self.assertEqual(c.breaker_key(opencode["route"]), c.breaker_key(claude["route"]))
        self.assertNotEqual(c.breaker_key(codex["route"]), c.breaker_key(claude["route"]))
        self.assertEqual(service.handle(claude)["code"], c.PLAN_LIMIT)
        blocked = service.handle(opencode)
        self.assertEqual(blocked["code"], c.BREAKER_OPEN)
        self.assertEqual(len(launcher.calls), 1)
        self.assertEqual(service.handle(codex)["text"], "codex-ok")
        self.assertEqual(breaker.state("api_key:anthropic:ANTHROPIC_API_KEY:k1"), "open")
        self.assertEqual(breaker.state("api_key:openai:OPENAI_API_KEY:k1"), "closed")

    def test_unhashable_process_is_tracked_and_fenced(self) -> None:
        started, release = threading.Event(), threading.Event()

        @dataclass
        class Proc:
            fenced: list = field(default_factory=list)

            def send(self, line: str) -> None:
                return None

            def close_stdin(self) -> None:
                return None

            def read_line(self, timeout_s: float):
                started.set()
                assert release.wait(5)
                return json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "ok"})

            def fence(self, reason: str) -> None:
                self.fenced.append(reason)

        clock = FakeClock()
        proc = Proc()
        service = RouteService(
            {"claude_code": ClaudeCodeAdapter(lambda argv, **kwargs: proc, clock, lambda *_: "",
                                              {"PATH": "/usr/bin"}, wall_clock=clock)},
            ConnectionBreaker(clock), clock)
        holder: dict = {}
        thread = threading.Thread(target=lambda: holder.update(res=service.handle(request())), daemon=True)
        thread.start()
        try:
            self.assertTrue(started.wait(5))
            service.shutdown()
            self.assertEqual(proc.fenced, ["shutdown"])
        finally:
            release.set()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(holder["res"]["status"], "ok")

    def test_shutdown_waits_for_a_launch_still_inside_the_spawner(self) -> None:
        started, release = threading.Event(), threading.Event()
        order: list[str] = []

        def launch(argv, **kwargs):
            started.set()
            assert release.wait(5)
            proc = FakeProcess([json.dumps(item) for item in claude_ok()], FakeClock())
            order.append("created")

            def fence(reason, _proc=proc):
                order.append("fenced")
                _proc.fenced.append(reason)

            proc.fence = fence
            return proc

        clock = FakeClock()
        service = RouteService(
            {"claude_code": ClaudeCodeAdapter(launch, clock, lambda *_: "", {"PATH": "/usr/bin"}, wall_clock=clock)},
            ConnectionBreaker(clock), clock)
        worker = threading.Thread(target=lambda: service.handle(request()), daemon=True)
        worker.start()
        self.assertTrue(started.wait(5))
        stopper = threading.Thread(target=lambda: (service.shutdown(), order.append("shutdown_returned")), daemon=True)
        stopper.start()
        stopper.join(0.2)
        self.assertNotIn("shutdown_returned", order)
        release.set()
        stopper.join(5)
        worker.join(5)
        self.assertFalse(stopper.is_alive())
        self.assertEqual(order, ["created", "fenced", "shutdown_returned"])

    def test_a_failed_fence_stays_tracked_and_the_next_shutdown_retries(self) -> None:
        started, release = threading.Event(), threading.Event()

        class Proc:
            def __init__(self) -> None:
                self.calls = 0
                self.fenced: list[str] = []

            def send(self, line: str) -> None:
                return None

            def close_stdin(self) -> None:
                return None

            def read_line(self, timeout_s: float):
                started.set()
                assert release.wait(5)
                return json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "ok"})

            def fence(self, reason: str) -> None:
                self.calls += 1
                if self.calls == 1:
                    raise OSError("busy")
                self.fenced.append(reason)

        clock = FakeClock()
        proc = Proc()
        service = RouteService(
            {"claude_code": ClaudeCodeAdapter(lambda argv, **kwargs: proc, clock, lambda *_: "k",
                                              {"PATH": "/usr/bin"}, wall_clock=clock)},
            ConnectionBreaker(clock), clock)
        worker = threading.Thread(target=lambda: service.handle(request()), daemon=True)
        worker.start()
        try:
            self.assertTrue(started.wait(5))
            with self.assertRaises(OSError):
                service.shutdown()
            self.assertEqual((proc.calls, len(service._live)), (1, 1))
            service.shutdown()
            self.assertEqual(proc.fenced, ["shutdown"])
            self.assertEqual(service._live, {})
        finally:
            release.set()
        worker.join(5)
        self.assertFalse(worker.is_alive())

    def test_wait_notice_uses_the_breaker_hold_not_an_earlier_vendor_reset(self) -> None:
        clock = FakeClock()
        breaker = ConnectionBreaker(clock)
        now = clock()
        first = {"code": c.PLAN_LIMIT, "limit": _limit("connection", reset_at=now + 500)}
        breaker.record(CONN, "m", "t1", first)
        second = {"code": c.PLAN_LIMIT, "limit": _limit("connection", reset_at=now + 100)}
        breaker.record(CONN, "m", "t1", second)
        self.assertEqual(second["retry_at"], now + 500)
        self.assertEqual(second["limit"]["reset_at"], now + 100)
        notice = lim.notice(
            c.result("r", c.PLAN_LIMIT, limit=second["limit"], retry_at=second["retry_at"]),
            request()["route"], {"on_limit": "wait", "fallback": []})
        self.assertEqual(notice["reset_at"], now + 500)
        self.assertEqual(notice["choices"][0]["until"], now + 500)

    def test_backoff_exponent_saturates_before_float_overflow(self) -> None:
        clock = FakeClock()
        breaker = ConnectionBreaker(clock)  # the float defaults, not integer stand-ins
        for _ in range(1025):
            breaker.record(CONN, "m", "t1", {"code": c.PLAN_LIMIT, "limit": _limit("connection")})
            clock.advance(3600)
        refused = breaker.admit(CONN, "m", "t1")
        # The last record opened a window of the capped cooldown; advance already passed it, so the
        # next admit is the probe. Recording once more must still be the cap, not an overflow.
        breaker.record(CONN, "m", "t1", {"code": c.PLAN_LIMIT, "limit": _limit("connection")}, refused["probe"])
        again = breaker.admit(CONN, "m", "t1")
        self.assertEqual(again["code"], c.BREAKER_OPEN)
        self.assertEqual(again["retry_at"] - clock(), 3600)

    def test_api_key_helper_ignores_credential_env_and_stays_off_the_custody_meter(self) -> None:
        clock = FakeClock()
        launcher = FakeLauncher([claude_limit(), claude_ok("custody")], clock)
        parent = {"PATH": "/usr/bin", "HOME": "/h"}
        service = RouteService(
            {"claude_code": ClaudeCodeAdapter(launcher, clock, lambda *_: "sk-test", parent, wall_clock=clock)},
            ConnectionBreaker(clock), clock)
        bare = request(rid="h1", kind="api_key", conn_id="helper-1")
        bare["route"]["connection"]["key_source"] = "api_key_helper"
        with_env = request(rid="h2", kind="api_key", conn_id="helper-1", credential_env="ANTHROPIC_API_KEY")
        with_env["route"]["connection"]["key_source"] = "api_key_helper"
        self.assertEqual(c.breaker_key(bare["route"]), c.breaker_key(with_env["route"]))
        self.assertEqual(c.breaker_key(bare["route"]), "api_key:anthropic:api_key_helper:helper-1")
        custody = request(rid="k", kind="api_key", conn_id="helper-1", credential_env="ANTHROPIC_API_KEY")
        self.assertNotEqual(c.breaker_key(custody["route"]), c.breaker_key(bare["route"]))
        self.assertEqual(service.handle(bare)["code"], c.PLAN_LIMIT)
        blocked = service.handle(with_env)
        self.assertEqual(blocked["code"], c.BREAKER_OPEN)
        self.assertEqual(len(launcher.calls), 1)
        self.assertEqual(service.handle(custody)["text"], "custody")
        self.assertEqual(len(launcher.calls), 2)

    def test_slash_escaped_url_password_is_redacted_in_the_vendor_event(self) -> None:
        secret_url = "https:\\/\\/user:password@example.invalid/hook"
        retry = {"type": "system", "subtype": "api_retry", "attempt": 1, "max_retries": 2,
                 "error_status": 401, "error": f"upstream {secret_url}"}
        _, _, _, service = ServiceOracleTest._service(self, [[retry, *claude_ok()]])
        res = service.handle(request())
        self.assertEqual(res["status"], "ok")
        blob = json.dumps(res)
        self.assertNotIn("password", blob)
        self.assertNotIn("user:password", blob)
        self.assertIn("[redacted]", res["events"][0]["data"]["error"])
        self.assertIn("example.invalid", res["events"][0]["data"]["error"])

    def test_a_failed_driver_fence_keeps_the_child_and_chains_under_the_driver_error(self) -> None:
        class Boom(RuntimeError):
            pass

        class Proc:
            def __init__(self) -> None:
                self.fence_calls = 0
                self.fenced: list[str] = []

            def send(self, line: str) -> None:
                raise Boom("driver broke")

            def close_stdin(self) -> None:
                return None

            def read_line(self, timeout_s: float):
                return None

            def fence(self, reason: str) -> None:
                self.fence_calls += 1
                if self.fence_calls == 1:
                    raise OSError("fence broke")
                self.fenced.append(reason)

        clock = FakeClock()
        proc = Proc()
        service = RouteService(
            {"claude_code": ClaudeCodeAdapter(lambda argv, **kwargs: proc, clock, lambda *_: "",
                                              {"PATH": "/usr/bin"}, wall_clock=clock)},
            ConnectionBreaker(clock), clock)
        with self.assertRaises(Boom) as caught:
            service.handle(request())
        self.assertIsInstance(caught.exception.__cause__, OSError)
        self.assertEqual(len(service._live), 1)
        service.shutdown()
        self.assertEqual(proc.fenced, ["shutdown"])
        self.assertEqual(service._live, {})

    def test_a_failed_deadline_fence_keeps_the_child_and_reraises_deadline(self) -> None:
        class Proc:
            def __init__(self) -> None:
                self.fenced: list[str] = []
                self.fence_calls = 0

            def send(self, line: str) -> None:
                return None

            def close_stdin(self) -> None:
                return None

            def read_line(self, timeout_s: float):
                raise Deadline()

            def fence(self, reason: str) -> None:
                self.fence_calls += 1
                if self.fence_calls == 1:
                    raise OSError("fence broke")
                self.fenced.append(reason)

        clock = FakeClock()
        proc = Proc()
        service = RouteService(
            {"claude_code": ClaudeCodeAdapter(lambda argv, **kwargs: proc, clock, lambda *_: "",
                                              {"PATH": "/usr/bin"}, wall_clock=clock)},
            ConnectionBreaker(clock), clock)
        with self.assertRaises(Deadline) as caught:
            service.handle(request())
        self.assertIsInstance(caught.exception.__cause__, OSError)
        self.assertEqual(len(service._live), 1)
        service.shutdown()
        self.assertEqual(proc.fenced, ["shutdown"])
        self.assertEqual(service._live, {})

    def test_nested_url_escapes_are_redacted_in_the_vendor_event(self) -> None:
        rows = (
            r"prefix https:\\/\\/user:password@example.invalid/hook suffix",
            r"prefix https:\u005c/\u005c/user:password@example.invalid/hook suffix",
            r"prefix https:\\/\u005c/user:password@example.invalid/hook suffix",
        )
        for error in rows:
            with self.subTest(error=error):
                event = c.event("retry", "vendor", {"error": error})
                blob = json.dumps(event)
                self.assertNotIn("password", blob)
                self.assertNotIn("user:password", blob)
                self.assertIn("[redacted]", event["data"]["error"])
                self.assertIn("example.invalid", event["data"]["error"])

    def test_negated_quota_predicates_use_the_same_separators(self) -> None:
        for text in ("not_exhausted", "not-rate-limited", "quota_is_not_exhausted"):
            with self.subTest(text=text):
                self.assertFalse(c.sounds_like_quota(text))
        self.assertTrue(c.sounds_like_quota("quota_exhausted"))
        self.assertTrue(c.sounds_like_quota("quota is not available"))

    def test_quoted_negation_is_not_a_quota_fault(self) -> None:
        for text in ('"not exhausted"', "(not exhausted)", '{"status":"not exhausted"}'):
            with self.subTest(text=text):
                self.assertFalse(c.sounds_like_quota(text))

    def test_escape_cap_redacts_the_whole_field_past_the_boundary(self) -> None:
        def escaped(backslashes: int) -> str:
            slash = "\\" * backslashes + "/"
            return "prefix https:" + slash + slash + "user:dummy_password@example.invalid/hook suffix"

        def nest(text: str, rounds: int) -> str:
            for _ in range(rounds):
                text = text.replace("\\", "\\\\").replace("/", "\\/")
            return text

        at_cap = c.event("retry", "vendor", {"error": escaped(8)})["data"]["error"]
        self.assertNotIn("dummy_password", at_cap)
        self.assertIn("prefix", at_cap)
        self.assertIn("example.invalid", at_cap)
        self.assertIn("[redacted]", at_cap)
        self.assertNotEqual(at_cap, "[redacted]")

        past_cap = c.event("retry", "vendor", {"error": escaped(9)})["data"]["error"]
        self.assertEqual(past_cap, "[redacted]")
        self.assertNotIn("dummy_password", past_cap)

        four_rounds = nest("prefix https://user:dummy_password@example.invalid/hook suffix", 4)
        self.assertEqual(c.event("retry", "vendor", {"error": four_rounds})["data"]["error"], "[redacted]")

    def test_percent_encoded_url_userinfo_is_redacted(self) -> None:
        samples = (
            "prefix https:%2F%2Fuser:dummy_password@example.invalid/hook suffix",
            "prefix https%3A%2F%2Fuser%3Adummy_password%40example.invalid%2Fhook suffix",
            "prefix https:\\u00252f\\u00252fuser:dummy_password@example.invalid/hook suffix",
        )
        for raw in samples:
            with self.subTest(raw=raw):
                error = c.event("retry", "vendor", {"error": raw})["data"]["error"]
                self.assertNotIn("dummy_password", error)
                self.assertIn("[redacted]", error)
                self.assertIn("example.invalid", error)
                self.assertIn("prefix", error)
                self.assertNotEqual(error, "[redacted]")
        control = c.event("retry", "vendor", {"error": "https://user:dummy%20password@host"})["data"]["error"]
        self.assertNotIn("dummy%20password", control)
        self.assertIn("https://", control)
        self.assertIn("[redacted]", control)

        def nested_slash(depth: int) -> str:
            encoded = "%2F"
            for _ in range(depth - 1):
                encoded = "%25" + encoded[1:]
            return encoded

        at_cap = "prefix https:" + nested_slash(8) + nested_slash(8) + "user:dummy_password@example.invalid/hook suffix"
        precise = c.event("retry", "vendor", {"error": at_cap})["data"]["error"]
        self.assertNotIn("dummy_password", precise)
        self.assertIn("prefix", precise)
        self.assertIn("example.invalid", precise)
        self.assertNotEqual(precise, "[redacted]")
        past = "prefix https:" + nested_slash(9) + nested_slash(9) + "user:dummy_password@example.invalid/hook suffix"
        self.assertEqual(c.event("retry", "vendor", {"error": past})["data"]["error"], "[redacted]")

    def test_mixed_url_encodings_do_not_split_or_reorder_the_password(self) -> None:
        cases = {
            "PCT_SPLIT": ("prefix https:%2F%2Fuser:dummy%20password@example.invalid/hook suffix", "dummy"),
            "PCT_SPLIT40": ("prefix https:%2F%2Fuser:dummy%40password@example.invalid/hook suffix", "password@"),
            "PCT_SPLIT_TAB": ("prefix https:%2F%2Fuser:dummy%09password@example.invalid/hook suffix", "password"),
            "PCT_SPLIT_SLASH": ("prefix https:%2F%2Fuser:dummy%2Fpassword@example.invalid/hook suffix", "password"),
            "PCT_ORDER": ("prefix https:%5C%2F%5C%2Fuser:dummy_password@example.invalid/hook suffix", "dummy_password"),
            "PCT_ORDER_U": ("prefix https:%5Cu002f%5Cu002fuser:dummy_password@example.invalid/hook suffix", "dummy_password"),
            "JSON_PCT_JSON": ("prefix https:\\u00255c/\\u00255c/user:dummy_password@example.invalid/hook suffix",
                              "dummy_password"),
        }
        for name, (raw, marker) in cases.items():
            with self.subTest(name=name):
                error = c.event("retry", "vendor", {"error": raw})["data"]["error"]
                self.assertNotIn(marker, error)
                self.assertIn("example.invalid", error)
                self.assertNotEqual(error, "[redacted]")
        self.assertEqual(c.redact("rate 100% of %20 budget"), "rate 100% of %20 budget")
        self.assertEqual(c.redact("a literal % sign"), "a literal % sign")

    def test_random_escape_layers_cannot_preserve_a_url_password(self) -> None:
        password = "dummy_password"
        host = "example.invalid"
        forms = (
            f"user:{password}",
            f"user:dummy%20{password}",
            f"user:dummy%40{password}",
            f"user:dummy%2F{password}",
            f"user:dummy%09{password}",
        )
        alphabet = {
            "/": ("%2F", "%2f", "\\/", "\\u002f", "\\u002F"),
            ":": ("%3A", "%3a", "\\:", "\\u003a", "\\u003A"),
            "\\": ("%5C", "%5c", "\\u005c"),
            "@": ("%40",),
        }
        rng = random.Random(326)

        def apply_one(text: str) -> str:
            if rng.random() < 0.5:
                start, end = 0, len(text)
            else:
                cut = text.find(password)
                start, end = 0, cut if cut > 0 else len(text)
            chunk = text[start:end]
            char = rng.choice(tuple(alphabet))
            chunk = chunk.replace(char, rng.choice(alphabet[char]))
            return text[:start] + chunk + text[end:]

        for _ in range(40):
            text = f"prefix https://{rng.choice(forms)}@{host}/hook suffix"
            for _layer in range(rng.randrange(0, 5)):
                text = apply_one(text)
            redacted = c.redact(text)
            self.assertNotIn(password, redacted, text)
        self.assertEqual(c.redact("rate 100% of %20 budget"), "rate 100% of %20 budget")

    def test_authority_walk_stops_at_the_path(self) -> None:
        plain = "see https://example.com/docs and mail ops@example.com"
        self.assertEqual(c.redact(plain), plain)
        cred = "prefix https://user:dummy_password@example.invalid/hook\nops@example.com"
        redacted = c.redact(cred)
        self.assertNotIn("dummy_password", redacted)
        self.assertIn("example.invalid", redacted)
        self.assertIn("ops@example.com", redacted)
        path = "https://user:secret@example.com/files/a@b.txt"
        redacted = c.redact(path)
        self.assertNotIn("secret", redacted)
        self.assertIn("example.com/files", redacted)
        self.assertIn("a@b.txt", redacted)

    def test_encoded_scheme_separator_keeps_percent_in_the_password(self) -> None:
        raw = "prefix https:%5C%2F%5C%2Fuser:dummy%20password@example.invalid/hook suffix"
        colon = "prefix https:%5C%3A%2F%2Fuser:dummy%20password@example.invalid/hook suffix"
        for text in (raw, colon):
            redacted = c.redact(text)
            self.assertNotIn("dummy%20password", redacted)
            self.assertIn("example.invalid", redacted)

    def test_authority_stops_before_the_host_delimiter(self) -> None:
        js = '{"url":"https://user:dummy_password@example.invalid","email":"ops@example.com"}'
        path = "https://user:dummy_password@example.invalid%2Ffiles%2Fa@b.txt"
        public = "https://example.com%3Fq=user@host"
        markdown = "[site](https://example.com)[mail](mailto:ops@example.com)"
        redacted = c.redact(js)
        self.assertIn("example.invalid", redacted)
        self.assertIn("ops@example.com", redacted)
        self.assertNotIn("dummy_password", redacted)
        redacted = c.redact(path)
        self.assertIn("example.invalid%2Ffiles", redacted)
        self.assertNotIn("dummy_password", redacted)
        self.assertEqual(c.redact(public), public)
        self.assertEqual(c.redact(markdown), markdown)
        for password in ("user:dummy%2Fpassword@example.invalid/hook", "user:dummy%3Fpassword@example.invalid/hook"):
            redacted = c.redact("https://" + password)
            self.assertNotIn("password", redacted)
            self.assertIn("example.invalid", redacted)

    def test_userinfo_walk_covers_delimiter_and_scheme_forms(self) -> None:
        hidden = (
            "prefix https:%2F%2Fuser:dummy%20password@example.invalid/hook suffix",
            "prefix https:%2F%2Fuser:dummy%40password@example.invalid/hook suffix",
            "prefix https:%2F%2Fuser:dummy%09password@example.invalid/hook suffix",
            "prefix https:%2F%2Fuser:dummy%2Fpassword@example.invalid/hook suffix",
            "prefix https:%5C%2F%5C%2Fuser:dummy_password@example.invalid/hook suffix",
            "prefix https:%5Cu002f%5Cu002fuser:dummy_password@example.invalid/hook suffix",
            "prefix https:\\u00255c/\\u00255c/user:dummy_password@example.invalid/hook suffix",
            "prefix https:%5C%2F%5C%2Fuser:dummy%20password@example.invalid/hook suffix",
            "prefix https:%5C%3A%2F%2Fuser:dummy%20password@example.invalid/hook suffix",
            'https://user:dummy"password@example.invalid/hook',
            "https://user:dummy;password@example.invalid/hook",
            "https://user:dummy?password@example.invalid/hook",
            "https://user:dummy_password@example.invalid%5C%2Ffiles%5C%2Fa@b.txt",
            "https://user:dummy_password@example.invalid\\/files\\/a@b.txt",
            "https:%255C%252F%255C%252Fuser:dummy%20password@example.invalid/hook",
            "https:%5C%5C%2F%5C%5C%2Fuser:dummy%20password@example.invalid/hook",
            "https://user:dummy%40password@example.invalid/hook",
            "https://user:dummy@password%40example.invalid/hook",
        )
        for raw in hidden:
            with self.subTest(raw=raw):
                redacted = c.redact(raw)
                self.assertNotIn("password", redacted)
                self.assertIn("example.invalid", redacted)
                self.assertNotEqual(redacted, "[redacted]")
        unchanged = (
            "see https://example.com/docs and mail ops@example.com",
            "https://example.com%3Fq=user@host",
            "[site](https://example.com)[mail](mailto:ops@example.com)",
            "ops@example.com",
            "ping @ops",
            "rate 100% of %20 budget",
            "https://example.com\\/files\\/a@b.txt",
        )
        slash_forms = (
            "/", "%2F", "%2f", "%252F", "\\/", "%5C%2F", "%5C%5C%2F", "%255C%252F", "\\u002f", "%5Cu002f",
        )
        for form in slash_forms:
            unchanged += (f"https://example.com{form}files{form}a@b.txt",)
        for raw in unchanged:
            with self.subTest(raw=raw):
                self.assertEqual(c.redact(raw), raw)
        for char in "\"',;<>()[]{}?#/":
            hexed = format(ord(char), "02X")
            for encoded in (char, "%" + hexed, "%25" + hexed, "\\" + char):
                raw = f"https://user:dummy{encoded}password@example.invalid/hook"
                with self.subTest(raw=raw):
                    redacted = c.redact(raw)
                    self.assertNotIn("password", redacted)
                    self.assertIn("example.invalid", redacted)

    def test_a_numeric_port_is_not_a_password(self) -> None:
        forms = (
            "/", "%2F", "%2f", "%252F", "\\/", "%5C%2F", "%5C%5C%2F",
            "?", "%3F", "%3f", "%253F", "\\?",
            "#", "%23", "%2523", "\\#",
        )
        for port in ("443", "8080"):
            for form in forms:
                raw = f"https://example.com:{port}{form}user@host"
                with self.subTest(raw=raw):
                    self.assertEqual(c.redact(raw), raw)
        for raw in (
            "https://example.com:443/search?q=user@host",
            "https://localhost:8080/files/a@b.txt",
            "https://example.com:443/a@b/user:dummy_password@example.invalid/hook",
        ):
            with self.subTest(raw=raw):
                self.assertEqual(c.redact(raw), raw)
        hosted = c.redact("https://user:dummy_password@example.invalid:443/hook")
        self.assertNotIn("dummy_password", hosted)
        self.assertIn("example.invalid:443", hosted)
        numeric = c.redact("https://user:8080@example.invalid/hook")
        self.assertNotIn("8080@", numeric)
        self.assertIn("example.invalid", numeric)
        slashed = c.redact("https://user:dummy/password@example.invalid/hook")
        self.assertNotIn("password", slashed)
        self.assertIn("example.invalid", slashed)

    def test_repeated_urls_redact_within_a_second(self) -> None:
        for unit in ("https://a@b ", "https://example.com/docs "):
            blob = (unit * (400_000 // len(unit) + 1))[:400_000]
            started = time.perf_counter()
            c.redact(blob)
            self.assertLess(time.perf_counter() - started, 1.0, unit)

    def test_unsettled_nested_field_redacts_within_a_second(self) -> None:
        def nest(text: str, rounds: int) -> str:
            for _ in range(rounds):
                text = text.replace("\\", "\\\\").replace("/", "\\/")
            return text

        blob = (nest("https://user:dummy_password@example.invalid/hook", 4) + "a" * 400_000)[:400_000]
        started = time.perf_counter()
        redacted = c.redact(blob)
        self.assertLess(time.perf_counter() - started, 1.0)
        self.assertNotIn("dummy_password", redacted)

    def test_opencode_subscription_providers_are_different_breakers(self) -> None:
        chatgpt = request(
            rid="cg", engine="opencode", model="chatgpt/gpt", kind="subscription", conn_id="shared",
            profile={"provider": "chatgpt", "auth": "subscription", "method": "sign_in_with_chatgpt",
                     "model": "chatgpt/gpt"})
        grok = request(
            rid="gk", engine="opencode", model="grok/g", kind="subscription", conn_id="shared",
            profile={"provider": "grok", "auth": "subscription", "method": "grok_sign_in",
                     "model": "grok/g"})
        spelled = request(
            rid="sp", engine="opencode", model="chatgpt/gpt", kind="subscription", conn_id="shared",
            profile={"provider": "ChatGPT", "auth": "subscription", "method": "Sign in with ChatGPT",
                     "model": "chatgpt/gpt"})
        chatgpt_key = c.breaker_key(chatgpt["route"])
        grok_key = c.breaker_key(grok["route"])
        self.assertEqual(chatgpt_key, "opencode:subscription:chatgpt:sign_in_with_chatgpt:shared")
        self.assertEqual(grok_key, "opencode:subscription:grok:grok_sign_in:shared")
        self.assertNotEqual(chatgpt_key, grok_key)
        self.assertEqual(c.breaker_key(spelled["route"]), chatgpt_key)
        clock = FakeClock()
        breaker = ConnectionBreaker(clock)
        breaker.record(chatgpt_key, "chatgpt/gpt", "t1",
                       c.result("cg", c.PLAN_LIMIT, limit=c.limit_signal("connection", "vendor")))
        self.assertEqual(breaker.state(chatgpt_key), "open")
        self.assertEqual(breaker.state(grok_key), "closed")
        grok_admit = breaker.admit(grok_key, "grok/g", "t1")
        self.assertTrue(grok_admit["admitted"])
        self.assertFalse(breaker.admit(chatgpt_key, "chatgpt/gpt", "t1")["admitted"])


if __name__ == "__main__":
    unittest.main()
