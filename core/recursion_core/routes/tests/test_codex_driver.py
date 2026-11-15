"""The Codex session driver (routes/engines/codex.py) against a stand-in app-server (lane DRIVER-CX).

The stand-in answers each request the driver writes the way codex-cli 0.160.0's protocol says it answers
(the schema extract in routes/engines/fixtures/codex/), and checks every message in BOTH directions against
that extract: the method must be in the protocol and the params must carry every required key and no key
the protocol lacks. Its lines are synthetic, built from the schema, not captured from a real run: the
captured fixtures come from Lucas's attended run. No real codex process, credential or network is used.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from recursion_core.routes import contract as c
from recursion_core.routes.codex import CodexAdapter
from recursion_core.routes.driver import DriverRefused, DriverStateError
from recursion_core.routes.engines import codex as cx
from recursion_core.routes.launch import ReadTimeout
from recursion_core.routes.tests.fakes import FakeClock

FIXTURE = Path(__file__).resolve().parents[1] / "engines" / "fixtures" / "codex" / "app-server-0.160.0.json"
PROTOCOL = json.loads(FIXTURE.read_text(encoding="utf-8"))
TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


def shape(node):
    for _ in range(8):
        if isinstance(node, str) and node.startswith("ref:"):
            node = PROTOCOL["definitions"][node[4:]]
            continue
        if isinstance(node, dict) and ("anyOf" in node or "oneOf" in node) and "properties" not in node:
            branches = [b for b in node.get("anyOf") or node.get("oneOf") if b != {"type": "null"}]
            if len(branches) == 1:
                node = branches[0]
                continue
        return node
    raise AssertionError(node)


def check_params(table: str, method: str, params) -> None:
    assert method in PROTOCOL[table], f"{method} is not in {table}"
    declared = PROTOCOL[table][method]
    if declared is None:
        assert params in (None, {}), (method, params)
        return
    obj = shape(declared)
    if params is None:
        return
    unknown = set(params) - set(obj.get("properties", {}))
    missing = set(obj.get("required", [])) - set(params)
    assert not unknown and not missing, (method, "unknown", unknown, "missing", missing)


def turn(tid, status="inProgress", error=None):
    return {"id": tid, "items": [], "status": status, **({"error": error} if error else {})}


class AppServer:
    """An EngineProcess (routes.launch) that answers like the 0.160.0 app-server. ``script`` maps a turn
    number to the notifications that follow its ``turn/start`` answer."""

    def __init__(self, threads=("thr-1",), script=None, refuse=(), model="gpt-5.1-codex", silent=False,
                 silent_turns=False, steer_turn=None):
        self.threads, self.script, self.refuse, self.model = set(threads), script or {}, dict(refuse), model
        self.out: list[str] = []
        self.sent: list[dict] = []
        self.stdin_closed, self.fenced, self.turns, self.silent = False, [], 0, silent
        self.silent_turns, self.steer_turn = silent_turns, steer_turn

    # -- EngineProcess ----------------------------------------------------------------------------
    def send(self, line: str) -> None:
        assert not self.stdin_closed, "send after close_stdin"
        msg = json.loads(line)
        self.sent.append(msg)
        method = msg.get("method")
        if method is None:  # our answer to a server request
            assert "id" in msg and len({"result", "error"} & set(msg)) == 1, msg
            return
        if "id" not in msg:
            check_params("client_notifications", method, msg.get("params"))
            return
        check_params("client_requests", method, msg.get("params"))
        if self.silent or (self.silent_turns and method == "turn/start"):
            return
        if method in self.refuse:
            self.emit({"id": msg["id"], "error": self.refuse[method]})
            return
        p = msg["params"]
        if method == "initialize":
            self.emit({"id": msg["id"], "result": {"userAgent": "fake/0.160.0", "codexHome": "/h/.codex",
                                                   "platformFamily": "unix", "platformOs": "linux"}})
        elif method in ("thread/start", "thread/resume"):
            tid = p.get("threadId", "thr-1")
            if method == "thread/resume" and tid not in self.threads:
                self.emit({"id": msg["id"], "error": {"code": -32600, "message": "no rollout found"}})
                return
            self.emit({"id": msg["id"], "result": {
                "thread": {"id": tid}, "model": self.model, "modelProvider": "openai", "cwd": "/w",
                "approvalPolicy": "on-request", "approvalsReviewer": "user",
                "sandbox": {"type": "workspaceWrite"}}})
        elif method == "turn/start":
            self.turns += 1
            tid = f"trn-{self.turns}"
            self.emit({"id": msg["id"], "result": {"turn": turn(tid)}})
            self.emit({"method": "turn/started", "params": {"threadId": "thr-1", "turn": turn(tid)}})
            for note in self.script.get(self.turns, ()):
                self.emit(note(tid) if callable(note) else note)
        elif method == "turn/steer":
            self.emit({"id": msg["id"], "result": {"turnId": self.steer_turn or p["expectedTurnId"]}})
        elif method == "turn/interrupt":
            self.emit({"id": msg["id"], "result": {}})
            self.emit({"method": "turn/completed", "params": {"threadId": "thr-1",
                                                              "turn": turn(p["turnId"], "interrupted")}})

    def close_stdin(self) -> None:
        self.stdin_closed = True

    def read_line(self, timeout_s: float):
        if not self.out:
            raise ReadTimeout()
        line = self.out.pop(0)
        return None if line is EOF else line

    def fence(self, reason: str) -> None:
        self.fenced.append(reason)

    # -- the stand-in's own output ----------------------------------------------------------------
    def emit(self, msg) -> None:
        if msg is EOF:
            self.out.append(EOF)
            return
        method = msg.get("method")
        if method is not None:
            table = "server_requests" if "id" in msg else "server_notifications"
            check_params(table, method, msg.get("params"))
        self.out.append(json.dumps(msg))


EOF = object()


def agent(text):
    return lambda tid: {"method": "item/completed", "params": {"threadId": "thr-1", "turnId": tid,
                                                               "completedAtMs": 1, "item": {
                                                                   "type": "agentMessage", "id": "i1", "text": text}}}


def usage(total, last=None):
    def note(tid):
        return {"method": "thread/tokenUsage/updated", "params": {"threadId": "thr-1", "turnId": tid, "tokenUsage": {
            "total": total, "last": last or total}}}
    return note


def done(status="completed", error=None):
    return lambda tid: {"method": "turn/completed", "params": {"threadId": "thr-1", "turn": turn(tid, status, error)}}


BREAKDOWN = {"inputTokens": 1200, "cachedInputTokens": 1000, "outputTokens": 80, "reasoningOutputTokens": 30,
             "totalTokens": 1280}


class Launcher:
    def __init__(self, *servers):
        self.servers, self.calls = list(servers), []

    def __call__(self, argv, **kwargs):
        self.calls.append({"argv": list(argv), **kwargs})
        return self.servers.pop(0)


def record(esid=None, model="gpt-5.1-codex", run_id="run-1"):
    return {"session_id": "ses-1", "run_id": run_id, "workspace": None, "engine_session_id": esid,
            "route": {"engine": "codex", "model": model, "connection": {"kind": "subscription", "id": "login-A"}}}


def message(turn_id="turn-1", delivery="dlv-1", text="list the files"):
    return {"kind": "user_message", "turn_id": turn_id, "text": text, "delivery_id": delivery}


class Base(unittest.TestCase):
    def open(self, server=None, esid=None, **kw):
        self.server = server or AppServer(**kw)
        self.launcher = Launcher(self.server)
        self.driver = cx.CodexDriver(self.launcher, FakeClock())
        self.handle = self.driver.start(record(esid), {"PATH": "/usr/bin"})
        return self.handle

    def drain(self):
        drafts = []
        while self.server.out:
            drafts += self.driver.pump(self.handle, 1.0) or []
        return drafts

    def sent_methods(self):
        return [m.get("method") for m in self.server.sent]


class StartTest(Base):
    def test_start_runs_initialize_then_thread_start_once_and_sends_no_policy(self):
        h = self.open()
        self.assertEqual(self.sent_methods(), ["initialize", "initialized", "thread/start"])
        self.assertEqual(self.server.sent[2]["params"], {"model": "gpt-5.1-codex"})
        self.assertEqual(h.engine_session_id, "thr-1")
        self.assertEqual(self.launcher.calls[0]["argv"], ["codex", "-c", "check_for_update_on_startup=false",
                                                          "app-server"])
        self.assertEqual(self.launcher.calls[0]["env"], {"PATH": "/usr/bin"})
        self.assertEqual(h._codex.effective, {"approvalPolicy": "on-request", "sandbox": {"type": "workspaceWrite"}})

    def test_a_model_left_to_the_engine_is_not_sent(self):
        server = AppServer()
        driver = cx.CodexDriver(Launcher(server), FakeClock())
        driver.start(record(model=None), {})
        self.assertEqual(server.sent[2]["params"], {})

    def test_eof_during_start_is_engine_exited_and_fences(self):
        server = AppServer(silent=True)
        server.out.append(EOF)
        with self.assertRaises(c.RouteError) as cm:
            cx.CodexDriver(Launcher(server), FakeClock()).start(record(), {})
        self.assertEqual(cm.exception.code, c.ENGINE_EXITED)
        self.assertEqual((server.stdin_closed, server.fenced), (True, ["start_refused"]))

    def test_a_typed_refusal_of_the_start_keeps_its_type(self):
        for info, code in (("unauthorized", c.AUTH_FAILED), ("usageLimitExceeded", c.PLAN_LIMIT)):
            with self.subTest(info=info):
                server = AppServer(refuse={"thread/start": {"code": -32000, "message": "x", "codexErrorInfo": info}})
                with self.assertRaises(c.RouteError) as cm:
                    cx.CodexDriver(Launcher(server), FakeClock()).start(record(), {})
                self.assertEqual(cm.exception.code, code)
                self.assertTrue(server.stdin_closed)


class ResumeTest(Base):
    def test_a_stored_thread_id_sends_thread_resume_not_thread_start(self):
        h = self.open(esid="thr-1")
        self.assertEqual(self.sent_methods(), ["initialize", "initialized", "thread/resume"])
        self.assertEqual(self.server.sent[2]["params"], {"threadId": "thr-1", "model": "gpt-5.1-codex"})
        self.assertEqual(h.engine_session_id, "thr-1")

    def test_an_unknown_id_refuses_cleanly(self):
        server = AppServer(threads=())
        with self.assertRaises(c.RouteError) as cm:
            cx.CodexDriver(Launcher(server), FakeClock()).resume(record("thr-gone"), {})
        self.assertEqual(cm.exception.code, c.REQUEST_INVALID)
        self.assertEqual([m.get("method") for m in server.sent], ["initialize", "initialized", "thread/resume"])
        self.assertEqual((server.stdin_closed, server.fenced), (True, ["start_refused"]))

    def test_a_thread_another_worker_writes_refuses_cleanly(self):
        busy = {"code": -32600, "message": "thread thr-1 already has an active writer"}
        server = AppServer(refuse={"thread/resume": busy})
        with self.assertRaises(c.RouteError) as cm:
            cx.CodexDriver(Launcher(server), FakeClock()).resume(record("thr-1"), {})
        self.assertEqual(cm.exception.code, c.REQUEST_INVALID)

    def test_an_answer_naming_another_thread_is_a_protocol_error(self):
        server = AppServer(threads=("thr-1", "thr-2"))
        driver = cx.CodexDriver(Launcher(server), FakeClock())
        orig = server.send

        def send(line):
            msg = json.loads(line)
            if msg.get("method") == "thread/resume":
                msg["params"]["threadId"] = "thr-2"  # the stand-in answers for another thread
                line = json.dumps(msg)
            orig(line)
        server.send = send
        with self.assertRaises(c.RouteError) as cm:
            driver.resume(record("thr-1"), {})
        self.assertEqual(cm.exception.code, c.PROTOCOL_ERROR)


class TwoTurnsTest(Base):
    def test_two_turns_on_one_thread_in_one_worker_with_input_open_between(self):
        self.open(script={1: [agent("three files"), usage(BREAKDOWN), done()],
                          2: [agent("3"), done()]})
        self.driver.send(self.handle, message())
        first = self.drain()
        self.assertEqual([d["type"] for d in first], ["input.accepted", "engine.output", "usage", "result"])
        self.assertEqual(first[0]["payload"], {"delivery_id": "dlv-1", "turn_id": "turn-1", "how": "new_turn"})
        self.assertEqual(first[1]["payload"]["text"], "three files")
        self.assertEqual(first[3]["payload"], {"code": None, "detail": ""})
        self.assertFalse(self.server.stdin_closed)
        self.assertIsNone(self.handle.live_turn)
        self.driver.send(self.handle, message("turn-2", "dlv-2", "count them"))
        second = self.drain()
        self.assertEqual(second[0]["payload"], {"delivery_id": "dlv-2", "turn_id": "turn-2", "how": "new_turn"})
        starts = [m for m in self.server.sent if m.get("method") == "turn/start"]
        self.assertEqual([s["params"]["threadId"] for s in starts], ["thr-1", "thr-1"])
        self.assertEqual([s["params"]["clientUserMessageId"] for s in starts], ["dlv-1", "dlv-2"])
        self.assertEqual(self.sent_methods().count("thread/start"), 1)
        self.assertEqual(len(self.launcher.calls), 1)  # no process but the one app-server

    def test_close_closes_input_and_nothing_is_written_after(self):
        self.open()
        self.driver.send(self.handle, {"kind": "close"})
        self.assertTrue(self.server.stdin_closed)
        with self.assertRaises(DriverStateError):
            self.driver.send(self.handle, message())

    def test_engine_output_spans_are_byte_offsets_of_the_output(self):
        self.open(script={1: [agent("a\u00e9"), done()]})
        before = self.handle._codex.offset
        self.driver.send(self.handle, message())
        lines = list(self.server.out)
        drafts = self.drain()
        out = next(d for d in drafts if d["type"] == "engine.output")["payload"]
        sizes = [len(x.encode("utf-8")) + 1 for x in lines]
        self.assertEqual(out["offset_start"], before + sum(sizes[:2]))
        self.assertEqual(out["offset_end"], before + sum(sizes[:3]))


class AcceptanceTest(Base):
    def test_no_acceptance_for_a_message_the_server_refused(self):
        refused = {"code": -32000, "message": "x", "codexErrorInfo": "badRequest"}
        self.open(refuse={"turn/start": refused})
        self.driver.send(self.handle, message())
        drafts = self.drain()
        self.assertEqual([d["type"] for d in drafts], ["result"])
        self.assertEqual(drafts[0]["payload"]["code"], c.ENGINE_ERROR)
        self.assertIsNone(self.handle.live_turn)

    def test_notifications_and_stray_answers_never_accept(self):
        self.open(silent_turns=True)
        self.driver.send(self.handle, message())
        self.server.emit({"method": "turn/started", "params": {"threadId": "thr-1", "turn": turn("trn-9")}})
        self.server.emit({"id": 99, "result": {"turn": turn("trn-9")}})        # no request of ours
        self.server.emit({"method": "item/completed", "params": {"threadId": "thr-1", "turnId": "trn-9",
                                                                 "completedAtMs": 1, "item": {
                                                                     "type": "userMessage", "id": "u",
                                                                     "content": [], "clientId": "dlv-1"}}})
        self.assertEqual([d["type"] for d in self.drain()], [])

    def test_a_turn_refused_on_a_typed_limit_drafts_the_limit(self):
        self.open(refuse={"turn/start": {"code": -32000, "message": "x", "codexErrorInfo": "usageLimitExceeded"}})
        self.driver.send(self.handle, message())
        self.assertEqual([d["type"] for d in self.drain()], ["limit", "result"])


class MidTurnTest(Base):
    def test_a_steer_is_turn_steer_and_its_typed_answer_accepts_mid_turn(self):
        self.open()
        self.driver.send(self.handle, message())
        self.drain()
        self.driver.send(self.handle, message("turn-1", "dlv-2", "also count them"))
        steer = self.server.sent[-1]
        self.assertEqual(steer["method"], "turn/steer")
        self.assertEqual(steer["params"], {"threadId": "thr-1", "expectedTurnId": "trn-1",
                                           "input": [{"type": "text", "text": "also count them"}],
                                           "clientUserMessageId": "dlv-2"})
        self.assertEqual(self.drain(), [{"type": "input.accepted", "payload": {
            "delivery_id": "dlv-2", "turn_id": "turn-1", "how": "mid_turn"}}])

    def test_a_steer_before_the_engine_named_the_turn_is_held_then_written(self):
        self.open()
        self.driver.send(self.handle, message())
        self.driver.send(self.handle, message("turn-1", "dlv-2", "more"))  # turn/start not answered yet
        self.assertEqual(self.sent_methods()[-1], "turn/start")
        drafts = self.drain()
        self.assertEqual(self.sent_methods()[-1], "turn/steer")
        self.assertEqual(self.server.sent[-1]["params"]["expectedTurnId"], "trn-1")
        drafts += self.drain()
        self.assertEqual([(d["payload"]["delivery_id"], d["payload"]["how"]) for d in drafts
                          if d["type"] == "input.accepted"], [("dlv-1", "new_turn"), ("dlv-2", "mid_turn")])

    def test_a_refused_steer_is_not_accepted(self):
        self.open(refuse={"turn/steer": {"code": -32000, "message": "x",
                                         "codexErrorInfo": {"activeTurnNotSteerable": {}}}})
        self.driver.send(self.handle, message())
        self.drain()
        self.driver.send(self.handle, message("turn-1", "dlv-2", "more"))
        self.assertEqual(self.drain(), [])
        self.assertEqual(self.handle.live_turn, "turn-1")

    def test_a_steer_answer_naming_another_turn_is_not_an_acceptance(self):
        self.open(steer_turn="trn-other")
        self.driver.send(self.handle, message())
        self.drain()
        self.driver.send(self.handle, message("turn-1", "dlv-2", "more"))
        self.assertEqual(self.drain(), [])

    def test_the_declaration_says_the_engine_acknowledges_mid_turn_input(self):
        self.assertEqual(cx.DECLARATION.as_record(), {"engine": "codex", "driver": 2, "input_ack": True, "flags": {
            "multi_turn": True, "mid_turn_input": True, "interrupt": True, "resume": True, "fork": False,
            "permission_bridge": False, "typed_limits": True, "usage": True}})


class OtherThreadTest(Base):
    def test_notifications_about_another_thread_are_not_this_workers(self):
        def other(note):
            def build(tid):
                msg = note(tid)
                msg["params"]["threadId"] = "thr-other"
                return msg
            return build
        self.open(script={1: [other(agent("not mine")), other(done()), agent("mine"), done()]})
        self.driver.send(self.handle, message())
        drafts = self.drain()
        self.assertEqual([d["payload"]["text"] for d in drafts if d["type"] == "engine.output"], ["mine"])
        self.assertEqual([d["type"] for d in drafts].count("result"), 1)


class InterruptTest(Base):
    def test_interrupt_is_the_protocols_own_and_ends_the_turn(self):
        self.open()
        self.driver.send(self.handle, message())
        self.drain()
        self.driver.interrupt(self.handle, "turn-1")
        self.assertEqual(self.server.sent[-1]["method"], "turn/interrupt")
        self.assertEqual(self.server.sent[-1]["params"], {"threadId": "thr-1", "turnId": "trn-1"})
        drafts = self.drain()
        self.assertEqual(drafts[-1]["payload"]["code"], "interrupted")
        self.assertIsNone(self.handle.live_turn)
        self.assertFalse(self.server.stdin_closed)

    def test_an_interrupt_before_the_engine_named_the_turn_is_held(self):
        self.open()
        self.driver.send(self.handle, message())
        self.driver.interrupt(self.handle, "turn-1")
        self.assertNotIn("turn/interrupt", self.sent_methods())
        self.drain()
        self.assertIn("turn/interrupt", self.sent_methods())


class UsageTest(Base):
    def usage_of(self, notes):
        self.open(script={1: [*notes, done()]})
        self.driver.send(self.handle, message())
        return next(d for d in self.drain() if d["type"] == "usage")["payload"]

    def test_observed_counts_and_unknown_cost_and_cache_writes(self):
        p = self.usage_of([usage(BREAKDOWN)])
        self.assertEqual(p["span"], "session_total")
        self.assertEqual(p["connection_id"], "subscription:login-A")
        self.assertEqual(p["model"], "gpt-5.1-codex")
        self.assertEqual(p["input_tokens"], {"value": 1200, "quality": "known", "provenance": "observed"})
        self.assertEqual(p["cache_read_tokens"]["value"], 1000)
        self.assertEqual(p["reasoning_tokens"]["value"], 30)
        unknown = {"value": None, "quality": "unknown", "provenance": "unavailable"}
        self.assertEqual(p["cache_write_tokens"], unknown)
        self.assertEqual(p["cost_usd"], unknown)

    def test_no_usage_notice_gives_every_count_unknown_never_zero(self):
        p = self.usage_of([])
        self.assertEqual(p["span"], "turn")
        for name in ("input_tokens", "output_tokens", "reasoning_tokens", "cache_read_tokens", "cache_write_tokens"):
            self.assertEqual(p[name], {"value": None, "quality": "unknown", "provenance": "unavailable"})

    def test_the_last_notice_of_the_turn_counts_and_a_rerouted_model_is_named(self):
        later = dict(BREAKDOWN, inputTokens=2000)
        reroute = lambda tid: {"method": "model/rerouted", "params": {  # noqa: E731
            "threadId": "thr-1", "turnId": tid, "fromModel": "gpt-5.1-codex", "toModel": "gpt-5.1-codex-mini",
            "reason": "highRiskCyberActivity"}}
        p = self.usage_of([usage(BREAKDOWN), reroute, usage(later)])
        self.assertEqual(p["input_tokens"]["value"], 2000)
        self.assertEqual(p["model"], "gpt-5.1-codex-mini")

    def test_one_usage_event_per_turn(self):
        self.open(script={1: [usage(BREAKDOWN), usage(BREAKDOWN), done()], 2: [done()]})
        for n in (1, 2):
            self.driver.send(self.handle, message(f"turn-{n}", f"dlv-{n}"))
            self.assertEqual([d["type"] for d in self.drain()].count("usage"), 1)


class LimitTest(Base):
    def window(self, limit_id="codex", used=100, resets=1_763_200_000):
        snap = {"limitId": limit_id, "primary": {"usedPercent": used, "resetsAt": resets}}
        return lambda tid: {"method": "account/rateLimits/updated", "params": {"rateLimits": snap}}

    def test_an_exhausted_window_fences_the_connection_and_names_its_bucket(self):
        for bucket in ("codex", "codex_gpt5_weekly"):
            with self.subTest(bucket=bucket):
                self.open(script={1: [self.window(bucket), done()]})
                self.driver.send(self.handle, message())
                limit = next(d for d in self.drain() if d["type"] == "limit")["payload"]
                self.assertEqual((limit["scope"], limit["model"], limit["bucket"], limit["kind"]),
                                 ("connection", None, bucket, "plan_limit"))
                self.assertEqual(limit["reset_at"], "2025-11-15T09:46:40.000Z")
                self.assertTrue(TIMESTAMP.match(limit["reset_at"]))

    def test_a_window_below_100_drafts_nothing(self):
        self.open(script={1: [self.window(used=99), done()]})
        self.driver.send(self.handle, message())
        self.assertNotIn("limit", [d["type"] for d in self.drain()])

    def test_a_typed_usage_limit_fails_the_turn_as_plan_limit(self):
        err = {"message": "You've hit your usage limit.", "codexErrorInfo": "usageLimitExceeded"}
        retry = lambda tid: {"method": "error", "params": {"threadId": "thr-1", "turnId": tid,  # noqa: E731
                                                           "willRetry": True, "error": {"message": "reconnecting"}}}
        self.open(script={1: [retry, self.window(), done("failed", err)]})
        self.driver.send(self.handle, message())
        drafts = self.drain()
        self.assertEqual([d["type"] for d in drafts], ["input.accepted", "limit", "usage", "limit", "result"])
        self.assertEqual(drafts[-1]["payload"], {"code": c.PLAN_LIMIT, "detail": "the turn failed"})
        self.assertNotIn("usage limit", json.dumps(drafts))

    def test_an_untyped_failure_after_an_exhausted_window_in_that_turn_is_the_limit(self):
        self.open(script={1: [self.window(), done("failed", {"message": "upstream error"})], 2: [
            done("failed", {"message": "upstream error"})]})
        self.driver.send(self.handle, message())
        self.assertEqual(self.drain()[-1]["payload"]["code"], c.PLAN_LIMIT)
        self.driver.send(self.handle, message("turn-2", "dlv-2"))
        self.assertEqual(self.drain()[-1]["payload"]["code"], c.ENGINE_ERROR)  # the window was the last turn's

    def test_a_retried_error_notice_is_not_an_outcome(self):
        retry = lambda tid: {"method": "error", "params": {"threadId": "thr-1", "turnId": tid,  # noqa: E731
                                                           "willRetry": True, "error": {
                                                               "message": "x", "codexErrorInfo": "usageLimitExceeded"}}}
        self.open(script={1: [retry, done("failed", {"message": "upstream error"})], 2: [retry, done()]})
        self.driver.send(self.handle, message())
        drafts = self.drain()
        self.assertEqual(drafts[-1]["payload"]["code"], c.ENGINE_ERROR)
        self.assertNotIn("limit", [d["type"] for d in drafts])
        self.driver.send(self.handle, message("turn-2", "dlv-2"))
        self.assertEqual(self.drain()[-1]["payload"], {"code": None, "detail": ""})

    def test_typed_auth_and_untyped_quota_text(self):
        cases = (({"message": "x", "codexErrorInfo": "unauthorized"}, c.AUTH_FAILED),
                 ({"message": "rate limit exceeded upstream"}, c.SHORT_FAULT),
                 ({"message": "boom", "codexErrorInfo": {"responseStreamDisconnected": {}}}, c.ENGINE_ERROR))
        for err, code in cases:
            with self.subTest(err=err):
                self.open(script={1: [done("failed", err)]})
                self.driver.send(self.handle, message())
                self.assertEqual(self.drain()[-1]["payload"]["code"], code)


class ServerRequestTest(Base):
    def test_an_approval_is_declined_on_its_own_id_and_never_refused_unsupported(self):
        for method in cx.APPROVAL_REQUESTS:
            with self.subTest(method=method):
                ask = lambda tid, m=method: {"id": 77, "method": m, "params": {  # noqa: E731
                    "threadId": "thr-1", "turnId": tid, "itemId": "it-1", "startedAtMs": 1}}
                self.open(script={1: [ask, done()]})
                self.driver.send(self.handle, message())
                self.drain()
                answers = [m for m in self.server.sent if m.get("id") == 77 and "method" not in m]
                self.assertEqual(answers, [{"id": 77, "result": {"decision": "decline"}}])

    def test_other_server_requests_are_refused_unsupported(self):
        ask = {"id": "q", "method": "item/tool/requestUserInput", "params": {
            "threadId": "thr-1", "turnId": "trn-1", "itemId": "it", "questions": [], "isBlocking": True}}
        self.open(script={1: [ask, done()]})
        self.driver.send(self.handle, message())
        self.drain()
        self.assertIn({"id": "q", "error": {"code": -32601, "message": "unsupported"}}, self.server.sent)

    def test_an_approval_answer_is_refused_until_the_bridge_exists(self):
        self.open()
        with self.assertRaises(DriverRefused):
            self.driver.send(self.handle, {"kind": "approval_answer", "approval_id": "apr-1", "decision": "approve"})


class RegistrationTest(unittest.TestCase):
    def test_register_replaces_the_seeded_factory(self):
        calls = []

        class Engines:
            def register(self, *args, **kwargs):
                calls.append((args, kwargs))

        class Hooks:
            engines = Engines()
        cx.register(Hooks())
        self.assertEqual(calls, [(("codex", cx.factory), {"replace": True})])

    def test_the_factory_runs_the_binary_it_was_given_or_the_default(self):
        for options, argv0 in (({}, "codex"), ({"binary": "/opt/codex/bin/codex"}, "/opt/codex/bin/codex")):
            with self.subTest(options=options):
                adapter = cx.factory(Launcher(), FakeClock(), lambda *_: "", {}, **options)
                self.assertIsInstance(adapter, CodexAdapter)
                self.assertEqual(adapter.argv({})[0], argv0)
                self.assertEqual(adapter.session.argv()[0], argv0)

    def test_against_spines_engine_table(self):
        try:
            from recursion_core.routes.engines import EngineTable
        except ImportError:
            self.skipTest("SPINE's engine table (#369) is not in this tree")
        table = EngineTable()
        cx.register(type("Hooks", (), {"engines": table})())
        self.assertIs(dict(table.items())["codex"], cx.factory)


if __name__ == "__main__":
    unittest.main()
