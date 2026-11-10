"""Oracle for retry, backoff, attempt booking and final outcomes (CSL-797).

One table says, for each error class and each limit, how many attempts run, how long the next
attempt waits, and what the final code is. The breaker and the service are expected to match the
table. Fakes only: no provider, no network, no real process.
"""

from __future__ import annotations

import json
import threading
import unittest

from recursion_core.routes import contract as c
from recursion_core.routes.breaker import ConnectionBreaker
from recursion_core.routes.claude_code import ClaudeCodeAdapter
from recursion_core.routes.codex import CodexAdapter
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
        self.assertEqual(breaker.state("codex:api_key:key-1"), "open")

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


if __name__ == "__main__":
    unittest.main()
