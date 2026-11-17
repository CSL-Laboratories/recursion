"""R3 finding 4: the Codex rate-limit read on an api_key route ignores exactly one error (lane DRIVER-CX).

The first three tests are Astra's red tests from the R3 review (Codex GPT-6 Astra, ``fix-4.patch``,
reviewed base 1abd2f77), copied unchanged apart from the class name; the rest are this lane's, and
``ErrorBoundaryTest`` answers the two reads of #393 (an engine's error names never reach the result
unless the protocol defines them; a null in one place does not hide a typed error in the other). Under
Lucas's D0 (Nov 12, 2026: "take the stricter, fail-closed option that the reviewer's test already
encodes; reuse an existing refusal code where one fits"): only the exact ChatGPT-auth error is ignored;
``usageLimitExceeded`` is ``plan_limit``, ``unauthorized`` ``auth_failed``, EOF ``engine_exited``; raw
error text never reaches an event. Fakes only: no real ``codex``, no credential, no network.
"""

import json
import unittest
from unittest.mock import patch

from recursion_core.routes import CodexAdapter, ConnectionBreaker, RouteService
from recursion_core.routes import codex as codex_module
from recursion_core.routes import contract as c
from recursion_core.routes.tests.fakes import FakeClock, FakeLauncher, request

# These tests drive the adapter through a fake launcher only. DRIVER-CC's R3-7 (#376) refuses native Windows
# at the shared adapter boundary before the launcher; pinning the host test keeps them running there (the
# form the two reviews of #376 proposed for test_r3_finding_9, R4). create=True: the name exists only once
# #376 has merged.
on_any_host = patch("recursion_core.routes.base.platform_supported", lambda: True, create=True)

EXACT = "chatgpt authentication required to read rate limits"
READ_ONLY = ["initialize", "initialized", "account/rateLimits/read"]


@on_any_host
class AstraCodexReadFailureTest(unittest.TestCase):
    def run_response(self, error):
        clock = FakeClock()
        script = [{"id":1,"result":{}}]
        if error is not None:
            script += [{"id":2,"error":error}, {"id":3,"result":{"thread":{"id":"t"}}},
                       {"id":4,"result":{}}, {"method":"turn/completed","params":{"turn":{"status":"completed"}}}]
        launcher = FakeLauncher([script], clock)
        breaker = ConnectionBreaker(clock)
        service = RouteService({"codex":CodexAdapter(launcher,clock,lambda *_:"FAKE-key",{})},breaker,clock)
        result = service.handle(request(engine="codex",model="gpt-5",kind="api_key",conn_id="key",credential_env="CODEX_API_KEY"))
        return result, launcher.processes[0], breaker

    def test_typed_errors_are_not_ignored(self):
        for info, expected in (("usageLimitExceeded",c.PLAN_LIMIT),("unauthorized",c.AUTH_FAILED)):
            with self.subTest(info=info):
                result, proc, breaker = self.run_response({"code":-32000,"codexErrorInfo":info})
                self.assertEqual(result["code"], expected)
                self.assertNotIn("thread/start", [m.get("method") for m in proc.sent])
                if expected == c.PLAN_LIMIT:
                    self.assertEqual(breaker.state("api_key:key"), "open")

    def test_eof_does_not_send_another_request(self):
        result, proc, _ = self.run_response(None)
        self.assertEqual(result["code"], c.ENGINE_EXITED)
        self.assertNotIn("thread/start", [m.get("method") for m in proc.sent])

    def test_raw_error_text_does_not_enter_events(self):
        marker = "sk-FAKE-r3-rejected-key"
        result, _, _ = self.run_response({"code":-32000,"message":"rejected: "+marker})
        self.assertNotIn(marker, json.dumps(result["events"]))


def _turn(text="keyed"):
    return [{"id": 3, "result": {"thread": {"id": "th-1"}}},
            {"id": 4, "result": {"turn": {"id": "tu-1", "items": [], "status": "inProgress"}}},
            {"method": "item/completed", "params": {"item": {"type": "agentMessage", "id": "i1", "text": text}}},
            {"method": "turn/completed", "params": {"threadId": "th-1", "turn": {"id": "tu-1", "items": [],
                                                                              "status": "completed"}}}]


class _Reads:
    def run_read(self, error, *, kind="api_key", then=True):
        clock = FakeClock()
        script = [{"id": 1, "result": {"userAgent": "fake"}}]
        if error is not None:
            script += [{"id": 2, "error": error}, *(_turn() if then else [])]
        launcher = FakeLauncher([script], clock)
        breaker = ConnectionBreaker(clock)
        service = RouteService({"codex": CodexAdapter(launcher, clock, lambda *_: "FAKE-key", {}, wall_clock=clock)},
                               breaker, clock)
        extra = {"conn_id": "key", "credential_env": "CODEX_API_KEY"} if kind == "api_key" else {"conn_id": "login"}
        res = service.handle(request(engine="codex", model="gpt-5", kind=kind, **extra))
        return res, launcher.processes[0], breaker


@on_any_host
class ExactErrorOnlyTest(_Reads, unittest.TestCase):
    """This lane's tests: which read errors are ignored, which are classified, and what is recorded."""

    def test_the_constant_is_the_text_verified_in_the_binary(self):
        self.assertEqual(codex_module.CHATGPT_AUTH_REQUIRED, EXACT)

    def test_the_exact_error_is_ignored_on_an_api_key_route_and_recorded_as_a_fixed_event(self):
        for code in (-32600, -32000):
            with self.subTest(code=code):
                res, proc, breaker = self.run_read({"code": code, "message": EXACT})
                self.assertEqual((res["status"], res["text"]), ("ok", "keyed"))
                self.assertIn("turn/start", [m.get("method") for m in proc.sent])
                self.assertEqual(breaker.state("api_key:key"), "closed")
                read_events = [e for e in res["events"] if e["source"] == "codex.account/rateLimits/read"]
                self.assertEqual([e["data"] for e in read_events], [{"ignored": "chatgpt_auth_required"}])
                self.assertNotIn(EXACT, json.dumps(res["events"]))

    def test_the_exact_error_is_not_ignored_on_a_subscription_route(self):
        res, proc, _ = self.run_read({"code": -32600, "message": EXACT}, kind="subscription")
        self.assertNotEqual(res["status"], "ok")
        self.assertEqual([m.get("method") for m in proc.sent], READ_ONLY)

    def test_near_misses_are_not_ignored(self):
        for message in ("codex account authentication required to read rate limits",  # no Codex login at all
                        EXACT + ".", " " + EXACT, EXACT.upper(), "error: " + EXACT, None, 7):
            with self.subTest(message=message):
                error = {"code": -32600}
                if message is not None:
                    error["message"] = message
                res, proc, _ = self.run_read(error)
                self.assertNotEqual(res["status"], "ok")
                self.assertEqual([m.get("method") for m in proc.sent], READ_ONLY)

    def test_the_exact_text_with_typed_info_is_classified_by_the_info(self):
        cases = (({"codexErrorInfo": "unauthorized"}, c.AUTH_FAILED),
                 ({"codexErrorInfo": "usageLimitExceeded"}, c.PLAN_LIMIT),
                 ({"data": {"codexErrorInfo": "usageLimitExceeded"}}, c.PLAN_LIMIT),
                 ({"data": {"codexErrorInfo": {"usageLimitExceeded": {}}}}, c.PLAN_LIMIT))
        for extra, expected in cases:
            with self.subTest(extra=extra):
                res, proc, breaker = self.run_read({"code": -32600, "message": EXACT, **extra})
                self.assertEqual(res["code"], expected)
                self.assertEqual([m.get("method") for m in proc.sent], READ_ONLY)
                self.assertEqual(breaker.state("api_key:key"), "open" if expected == c.PLAN_LIMIT else "closed")

    def test_eof_during_the_read_writes_nothing_more_and_is_engine_exited(self):
        res, proc, breaker = self.run_read(None)
        self.assertEqual(res["code"], c.ENGINE_EXITED)
        self.assertEqual([m.get("method") for m in proc.sent], READ_ONLY)
        self.assertTrue(proc.stdin_closed)
        self.assertEqual(breaker.state("api_key:key"), "closed")

    def test_no_error_text_reaches_the_result(self):
        marker = "sk-FAKE-echoed-0004"
        for message in (f"Incorrect API key provided: {marker}", f"{EXACT} {marker}"):
            for extra in ({}, {"codexErrorInfo": "unauthorized"}, {"data": {"detail": marker}}):
                with self.subTest(message=message, extra=extra):
                    res, _, _ = self.run_read({"code": -32000, "message": message, **extra})
                    self.assertNotIn(marker, json.dumps(res))


def _recorded_error_names() -> list:
    from recursion_core.routes.tests.test_codex_protocol import protocol

    return next(b["enum"] for b in protocol()["definitions"]["CodexErrorInfo"]["oneOf"] if "enum" in b)


@on_any_host
class ErrorBoundaryTest(_Reads, unittest.TestCase):
    """The two #393 reads (GPT-6.1 Sol R1 and R2; GPT-6 Astra's case of every recorded name): what an
    error's ``codexErrorInfo`` may put into the result, and where it is read."""

    MARKER = "FAKE-review-error-marker-393"

    def run_turn_error(self, error):
        clock = FakeClock()
        script = [{"id": 1, "result": {}}, {"id": 2, "result": {"rateLimits": {"limitId": "codex"}}},
                  *_turn()[:2], {"method": "turn/completed", "params": {"threadId": "th-1", "turn": {
                      "id": "tu-1", "items": [], "status": "failed", "error": error}}}]
        launcher = FakeLauncher([script], clock)
        service = RouteService({"codex": CodexAdapter(launcher, clock, lambda *_: "FAKE-key", {}, wall_clock=clock)},
                               ConnectionBreaker(clock), clock)
        return service.handle(request(engine="codex", model="gpt-5", conn_id="login"))

    def test_unknown_error_info_is_not_saved_as_raw_detail(self):
        for info in (self.MARKER, {self.MARKER: {"detail": "x"}}):
            for kind in ("api_key", "subscription"):
                with self.subTest(info=info, kind=kind, at="rate-limit read"):
                    res, proc, _ = self.run_read({"code": -32000, "message": "rejected",
                                                  "data": {"codexErrorInfo": info}}, kind=kind)
                    self.assertEqual((res["code"], res["detail"]), (c.ENGINE_ERROR, "-32000"))
                    self.assertNotIn(self.MARKER, json.dumps(res))
                    self.assertEqual([m.get("method") for m in proc.sent], READ_ONLY)
            with self.subTest(info=info, at="failed turn"):
                res = self.run_turn_error({"message": "rejected", "codexErrorInfo": info})
                self.assertEqual((res["code"], res["detail"]), (c.ENGINE_ERROR, "error"))
                self.assertNotIn(self.MARKER, json.dumps(res))

    def test_a_text_code_is_not_saved_either(self):
        res, _, _ = self.run_read({"code": self.MARKER, "message": "rejected"})
        self.assertEqual((res["code"], res["detail"]), (c.ENGINE_ERROR, "error"))

    def test_a_recorded_error_name_is_the_detail(self):
        for name in _recorded_error_names():
            with self.subTest(name=name):
                res, proc, _ = self.run_read({"code": -32000, "message": "ordinary provider failure",
                                              "data": {"codexErrorInfo": name}})
                expected = {"usageLimitExceeded": c.PLAN_LIMIT, "unauthorized": c.AUTH_FAILED}.get(name, c.ENGINE_ERROR)
                self.assertEqual(res["code"], expected)
                if expected == c.ENGINE_ERROR:
                    self.assertEqual(res["detail"], name)
                self.assertEqual([m.get("method") for m in proc.sent], READ_ONLY)
        res = self.run_turn_error({"message": "x", "codexErrorInfo": {"responseStreamDisconnected": {
            "httpStatusCode": 502}}})
        self.assertEqual((res["code"], res["detail"]), (c.ENGINE_ERROR, "responseStreamDisconnected"))

    def test_nested_typed_limit_is_not_hidden_by_top_level_null(self):
        for top in (None, {}):
            for nested in ("usageLimitExceeded", {"usageLimitExceeded": {}}):
                with self.subTest(top=top, nested=nested):
                    res, proc, breaker = self.run_read({"code": -32000, "message": EXACT, "codexErrorInfo": top,
                                                        "data": {"codexErrorInfo": nested}})
                    self.assertEqual(res["code"], c.PLAN_LIMIT)
                    self.assertEqual([m.get("method") for m in proc.sent], READ_ONLY)
                    self.assertEqual(breaker.state("api_key:key"), "open")

    def test_nested_auth_is_not_hidden_by_top_level_null(self):
        for top in (None, {}):
            with self.subTest(top=top):
                res, proc, breaker = self.run_read({"code": -32000, "message": EXACT, "codexErrorInfo": top,
                                                    "data": {"codexErrorInfo": "unauthorized"}})
                self.assertEqual(res["code"], c.AUTH_FAILED)
                self.assertEqual([m.get("method") for m in proc.sent], READ_ONLY)
                self.assertEqual(breaker.state("api_key:key"), "closed")

    def test_the_exact_error_with_null_or_empty_info_in_both_places_is_still_ignored(self):
        for extra in ({"codexErrorInfo": None}, {"data": {"codexErrorInfo": None}}, {"data": {"codexErrorInfo": {}}},
                      {"codexErrorInfo": None, "data": {"codexErrorInfo": None}}):
            with self.subTest(extra=extra):
                res, proc, _ = self.run_read({"code": -32600, "message": EXACT, **extra})
                self.assertEqual((res["status"], res["text"]), ("ok", "keyed"))


if __name__ == "__main__":
    unittest.main()
