# Written by Claude Opus 5.5 (claude-opus-5-5), Claude Code builder lane ENGINE-E5, for Lucas.
"""Antigravity's methods, driver.v2 declarations and ACP glue; nothing registered, no spawn path.

Run: PYTHONPATH=core <python> -m unittest recursion_core.routes.engines.fixtures.antigravity.test_antigravity_driver
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from recursion_core.routes import contract as c
from recursion_core.routes.engines import CATALOGUE, EngineTable
from recursion_core.routes.engines import antigravity as ag
from recursion_core.routes.engines.fixtures.antigravity import acp, schema_check
from recursion_core.routes.engines.fixtures.antigravity.fake_acp_agent import (
    ANTIGRAVITY_METHODS,
    OPTIONS_ALL,
    TOOL_CALL,
    FakeAcpAgent,
)

HERE = Path(__file__).resolve().parent
CWD = "/var/tmp/workspace"
FAR = 1e12


def route(method: str, kind: str = "api_key", cred: str | None = "GEMINI_API_KEY") -> dict:
    connection = {"kind": kind, "id": "conn-A"}
    if cred is not None:
        connection["credential_env"] = cred
    return {"engine": "antigravity", "model": "gemini-3.6-flash", "connection": connection, "auth_method": method}


class MethodTest(unittest.TestCase):
    def test_the_four_methods_are_the_agents_documented_ids(self) -> None:
        self.assertEqual(set(ag.METHODS), set(ANTIGRAVITY_METHODS))

    def test_the_personal_account_refuses_unconditionally(self) -> None:
        with self.assertRaises(c.RouteError) as ctx:
            ag.route_method(route("oauth-personal", "subscription", None))
        self.assertEqual((ctx.exception.code, ctx.exception.detail), (c.LAUNCH_REFUSED, ag.PERSONAL_ACCOUNT_OFF))
        import inspect
        for fn in (ag.route_method, ag.open_acp, ag.AntigravityOneShotAdapter.__init__, ag.one_shot_factory):
            self.assertNotIn("personal", " ".join(inspect.signature(fn).parameters))

    def test_f8_no_module_attribute_an_importer_sets_turns_it_on(self) -> None:
        for name in ("PERSONAL_ACCOUNT_ENABLED", "PERSONAL_ACCOUNT_ALLOWED", "ENABLED"):
            setattr(ag, name, True)
            self.addCleanup(delattr, ag, name)
        with self.assertRaises(c.RouteError) as ctx:
            ag.route_method(route("oauth-personal", "subscription", None))
        self.assertEqual(ctx.exception.detail, ag.PERSONAL_ACCOUNT_OFF)

    def test_an_api_key_route_must_name_the_methods_own_key(self) -> None:
        for method, cred in (("gemini-api-key", "GOOGLE_API_KEY"), ("agent-platform", "GEMINI_API_KEY"),
                             ("gemini-api-key", "XAI_API_KEY")):
            with self.subTest(method=method, cred=cred), self.assertRaises(c.RouteError) as ctx:
                ag.route_method(route(method, "api_key", cred))
            self.assertEqual(ctx.exception.code, c.LAUNCH_REFUSED)
        agent = FakeAcpAgent()
        with self.assertRaises(c.RouteError):
            ag.open_acp(agent, agent.clock, route("gemini-api-key", "api_key", "GOOGLE_API_KEY"), CWD, FAR)
        self.assertEqual(agent.received, [])

    def test_each_built_method_is_accepted_with_its_own_connection(self) -> None:
        self.assertIs(ag.route_method(route("gemini-api-key")), ag.GEMINI_API_KEY_METHOD)
        self.assertIs(ag.route_method(route("oauth-business", "subscription", None)), ag.ENTERPRISE_METHOD)
        self.assertIs(ag.route_method(route("agent-platform", "api_key", "GOOGLE_API_KEY")), ag.AGENT_PLATFORM_METHOD)
        self.assertIs(ag.route_method(route("agent-platform", "passthrough", None)), ag.AGENT_PLATFORM_METHOD)


class DeclarationTest(unittest.TestCase):
    def test_one_shot_declares_usage_only(self) -> None:
        flags = ag.ONE_SHOT_DECLARATION["flags"]
        self.assertEqual([name for name, on in flags.items() if on], ["usage"])
        self.assertIs(ag.ONE_SHOT_DECLARATION["input_ack"], False)

    def test_acp_declares_what_the_protocol_gives_and_no_limits_usage_or_resume(self) -> None:
        flags = ag.ACP_DECLARATION["flags"]
        self.assertEqual([name for name, on in flags.items() if on], ["multi_turn", "interrupt", "permission_bridge"])
        self.assertIs(ag.ACP_DECLARATION["input_ack"], False)
        for drive, names in ag.UNVERIFIED_FLAGS.items():
            declared = ag.ONE_SHOT_DECLARATION if drive == "one_shot" else ag.ACP_DECLARATION
            self.assertEqual(sorted(names), sorted(n for n, on in declared["flags"].items() if on))

    @unittest.skipUnless(schema_check.AVAILABLE, schema_check.SKIP_REASON)
    def test_both_declarations_fit_driver_v2(self) -> None:
        for declaration in (ag.ONE_SHOT_DECLARATION, ag.ACP_DECLARATION):
            self.assertTrue(schema_check.fits("driver.v2.json", declaration), declaration)


class RegistrationTest(unittest.TestCase):
    def test_antigravity_is_a_catalogue_name_with_no_factory(self) -> None:
        self.assertIn(ag.ENGINE, CATALOGUE)
        self.assertNotIn(ag.ENGINE, EngineTable())

    def test_r7_through_the_shared_hooks_a_composition_has_no_antigravity_factory(self) -> None:
        """SPINE's own composition (shared ``Hooks()``, every installed registrant, frozen tables): this slice
        registers nothing, and no registrant SPINE's fixed list names is this lane's. START forbids calling the
        engine table's register even on a test table, so the factory's S3 shape is shown by calling it."""
        from recursion_core.control import hooks as spine_hooks
        composed = spine_hooks.compose()
        self.assertNotIn(ag.ENGINE, composed.engines)
        self.assertNotIn(ag.ENGINE, composed.engines.names())
        self.assertFalse(any("antigravity" in name or "engine_e5" in name for name in spine_hooks.REGISTRANTS))
        made = ag.one_shot_factory(lambda *a, **k: None, lambda: 0.0, lambda *_: "", {})
        self.assertEqual(made.executable, ag.DEFAULT_EXECUTABLE)

    def test_argv0_obeys_the_default_executable_promise_on_both_drives(self) -> None:
        self.assertEqual(ag.DEFAULT_EXECUTABLE, "agy")
        self.assertEqual(ag.acp_argv("agy"), ["agy", "acp"])
        for options in ({}, {"binary": "/opt/antigravity/bin/agy"}):
            made = ag.one_shot_factory(lambda *a, **k: None, lambda: 0.0, lambda *_: "", {}, **options)
            want = options.get("binary", ag.DEFAULT_EXECUTABLE)
            self.assertEqual(made.executable, want)
            self.assertEqual(ag.one_shot_argv(made.executable, "m")[0], want)
            self.assertEqual(ag.acp_argv(made.executable)[0], want)

    def test_no_spawn_path_in_the_new_modules(self) -> None:
        sources = [Path(ag.__file__), HERE / "acp.py"]
        for path in sources:
            text = path.read_text(encoding="utf-8")
            for banned in ("import os", "from os", "subprocess", "Popen", "prctl", "preexec_fn=", "os.environ"):
                self.assertNotIn(banned, text, f"{path.name}: {banned}")

    def test_the_acp_client_imports_nothing_of_antigravity_or_of_its_folder(self) -> None:
        text = (HERE / "acp.py").read_text(encoding="utf-8")
        imports = [line for line in text.splitlines() if line.startswith(("import ", "from "))]
        for line in imports:
            self.assertNotIn("antigravity", line)
            self.assertNotIn("fixtures", line)
        self.assertEqual(sorted(i for i in imports if "recursion_core" in i),
                         ["from recursion_core.routes import contract as c",
                          "from recursion_core.routes.launch import EngineProcess, ReadTimeout"])


class AcpGlueTest(unittest.TestCase):
    def test_the_personal_account_refuses_before_a_byte_is_written(self) -> None:
        agent = FakeAcpAgent()
        with self.assertRaises(c.RouteError) as ctx:
            ag.open_acp(agent, agent.clock, route("oauth-personal", "subscription", None), CWD, FAR)
        self.assertEqual(ctx.exception.detail, ag.PERSONAL_ACCOUNT_OFF)
        self.assertEqual(agent.received, [])

    def test_a_route_opens_with_exactly_its_method(self) -> None:
        agent = FakeAcpAgent()
        ag.open_acp(agent, agent.clock, route("gemini-api-key"), CWD, FAR)
        self.assertEqual(agent.authenticated, ["gemini-api-key"])

    def test_a_method_the_agent_does_not_offer_never_falls_back_to_one_it_does(self) -> None:
        agent = FakeAcpAgent(methods=("oauth-personal", "gemini-api-key"))
        with self.assertRaises(acp.AcpRefused):
            ag.open_acp(agent, agent.clock, route("oauth-business", "subscription", None), CWD, FAR)
        self.assertEqual(agent.authenticated, [])

    def test_f12_the_glue_refuses_what_it_cannot_take_with_a_typed_error(self) -> None:
        agent = FakeAcpAgent()
        with self.assertRaises(TypeError) as ctx:
            ag.open_acp(agent, agent.clock, route("gemini-api-key"), CWD, FAR, hooks=acp.EngineHooks())
        self.assertIn("Antigravity's own", str(ctx.exception))
        self.assertEqual(agent.received, [])
        drafts = ag.AcpDrafts()
        with self.assertRaises(TypeError) as ctx:
            drafts.ended(None)
        self.assertIn("TurnOutcome", str(ctx.exception))
        for bad in (5, b"bytes", None):
            with self.subTest(bad=bad), self.assertRaises(TypeError) as ctx:
                drafts.on_line(bad)
            self.assertEqual(str(ctx.exception), "on_line takes the line as a str")
        with self.assertRaises(acp.AcpProtocolError):
            drafts.on_line('{"x":"\udcff"}')
        self.assertEqual(drafts.drafts, [])

    def test_the_acp_argv_takes_agy_or_an_absolute_path_only(self) -> None:
        self.assertEqual(ag.acp_argv("agy"), ["agy", "acp"])
        self.assertEqual(ag.acp_argv("/opt/antigravity/bin/agy"), ["/opt/antigravity/bin/agy", "acp"])
        for bad in (None, "", "-x", "relative/agy", "/opt/a" + chr(0) + "gy", b"/opt/agy"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                ag.acp_argv(bad)

    def test_antigravity_maps_no_acp_error_to_a_plan_limit(self) -> None:
        agent = FakeAcpAgent([[("error", 429, "Usage limit reached")]])
        session = ag.open_acp(agent, agent.clock, route("gemini-api-key"), CWD, FAR)
        out = session.prompt("x", FAR)
        self.assertNotEqual(out.code, c.PLAN_LIMIT)
        self.assertEqual(out.code, c.SHORT_FAULT)


class AcpDraftsTest(unittest.TestCase):
    def run_turn(self, script, *, cancel_on_update=False):
        agent = FakeAcpAgent([script])
        drafts = ag.AcpDrafts()
        holder = {}

        def on_update(params):
            drafts.on_update(params)
            if cancel_on_update:
                holder["s"].cancel()

        session = ag.open_acp(agent, agent.clock, route("gemini-api-key"), CWD, FAR,
                              on_line=drafts.on_line, on_update=on_update, on_ask=drafts.on_ask)
        holder["s"] = session
        out = session.prompt("x", FAR)
        return out, drafts, drafts.ended(out, "turn-1", "dlv-1")

    def test_a_turn_gives_spans_of_every_line_then_its_acceptance_and_result(self) -> None:
        out, drafts, ended = self.run_turn([("chunk", "hi"), ("end", "end_turn")])
        self.assertEqual([d["type"] for d in ended], ["input.accepted", "result"])
        self.assertEqual(ended[0]["payload"], {"delivery_id": "dlv-1", "turn_id": "turn-1", "how": "new_turn"})
        self.assertEqual(ended[1]["payload"], {"code": None, "detail": ""})
        spans = [d["payload"] for d in drafts.drafts if d["type"] == "engine.output"]
        self.assertGreaterEqual(len(spans), 5)
        for one, two in zip(spans, spans[1:]):
            self.assertEqual(one["offset_end"], two["offset_start"])

    def test_a_turn_the_agent_never_answered_gives_no_acceptance(self) -> None:
        out, _drafts, ended = self.run_turn([("chunk", "half"), ("die",)])
        self.assertEqual(out.code, c.ENGINE_EXITED)
        self.assertEqual([d["type"] for d in ended], ["result"])

    def test_the_cancel_race_result_is_cancelled_but_the_prompt_was_taken(self) -> None:
        out, _drafts, ended = self.run_turn([("chunk", "answer"), ("end", "end_turn")], cancel_on_update=True)
        self.assertEqual(out.code, acp.CANCELLED)
        self.assertEqual([d["type"] for d in ended], ["input.accepted", "result"])
        self.assertEqual(ended[1]["payload"]["code"], "cancelled")

    def test_med1_a_line_that_is_not_utf8_leaves_the_session_able_to_take_the_next_prompt(self) -> None:
        # DeepSeek's round-2 MED1: on_line refuses a line a surrogateescape reader gives for invalid UTF-8.
        update = {"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": "fake-session-1", "update": {
            "sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "x" + chr(0xDCFF)}}}}
        agent = FakeAcpAgent([[("raw", json.dumps(update, ensure_ascii=False)), ("wait_cancel",), ("end", "cancelled")],
                              [("chunk", "second"), ("end", "end_turn")]])
        drafts = ag.AcpDrafts()
        session = ag.open_acp(agent, agent.clock, route("gemini-api-key"), CWD, FAR, on_line=drafts.on_line)
        with self.assertRaises(acp.AcpProtocolError):
            session.prompt("first", FAR)
        self.assertEqual(agent.cancels, 1)
        out = session.prompt("second", FAR)
        self.assertEqual((out.code, out.text), (None, "second"))
        spans = [d["payload"] for d in drafts.drafts if d["type"] == "engine.output"]
        for one, two in zip(spans, spans[1:]):
            self.assertEqual(one["offset_end"], two["offset_start"])   # the refused line left no partial span

    def test_f2_ended_takes_both_ids_of_common_v1_form_or_neither(self) -> None:
        out = acp.TurnOutcome(1, None, "end_turn", False, False, None, None, None, "", "hi")
        drafts = ag.AcpDrafts()
        for turn_id, delivery_id in ((5, "dlv-1"), ("turn-1", {"d": 1}), ("x", "dlv-1"), ("turn-1", "dlv-"),
                                     ("turn-a b", "dlv-1"), ("turn-1" + chr(10), "dlv-1"), ("turn-1", None),
                                     (None, "dlv-1")):
            with self.subTest(turn_id=turn_id, delivery_id=delivery_id), self.assertRaises(TypeError):
                drafts.ended(out, turn_id, delivery_id)
        self.assertEqual(drafts.drafts, [])
        good = drafts.ended(out, "turn-1", "dlv-1")
        self.assertEqual([d["type"] for d in good], ["input.accepted", "result"])
        if schema_check.AVAILABLE:
            for draft in good:
                self.assertTrue(schema_check.fits("driver.v2.json#/$defs/event_draft", draft), draft)

    def test_r3_acp_drafts_given_the_key_mask_it_and_keep_the_offsets(self) -> None:
        key = "r4-made-up-custody-value-0e5"
        line = json.dumps({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": "s", "update": {
            "sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "the key is " + key}}}})
        plain, masked = ag.AcpDrafts(offset=3), ag.AcpDrafts(offset=3, redact=(key,))
        plain.on_line(line)
        masked.on_line(line)
        got, want = masked.drafts[0]["payload"], plain.drafts[0]["payload"]
        self.assertNotIn(key, got["text"])
        self.assertEqual((got["offset_start"], got["offset_end"]), (want["offset_start"], want["offset_end"]))
        out = acp.TurnOutcome(1, c.ENGINE_ERROR, None, False, False, None, None, None, "acp error 1: " + key, "")
        self.assertNotIn(key, json.dumps(masked.ended(out)))


    TOOL_TURN = [
        ("update", {"sessionUpdate": "tool_call", "toolCallId": "c1", "title": "Run ls", "kind": "execute",
                    "status": "pending", "rawInput": {}}),
        ("update", {"sessionUpdate": "tool_call_update", "toolCallId": "c1", "status": "in_progress",
                    "rawInput": {"CommandLine": "ls", "Cwd": CWD}}),
        ("update", {"sessionUpdate": "tool_call_update", "toolCallId": "c1", "status": "completed",
                    "content": [{"type": "content", "content": {"type": "text", "text": "a\n"}}]}),
        ("chunk", "done"), ("end", "end_turn")]

    def test_tool_calls_are_drafted_with_antigravitys_rfc8785_digest(self) -> None:
        _out, drafts, _ended = self.run_turn(self.TOOL_TURN)
        calls = [d["payload"] for d in drafts.drafts if d["type"] == "tool.call"]
        results = [d["payload"] for d in drafts.drafts if d["type"] == "tool.result"]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["input_digest"], ag.input_digest({"CommandLine": "ls", "Cwd": CWD}))
        self.assertEqual([(r["call_id"], r["status"], r["text"]) for r in results], [("c1", "ok", "a\n")])

    def test_each_turn_drafts_its_own_tool_calls_even_under_a_reused_id(self) -> None:
        agent = FakeAcpAgent([self.TOOL_TURN, self.TOOL_TURN])
        drafts = ag.AcpDrafts()
        session = ag.open_acp(agent, agent.clock, route("gemini-api-key"), CWD, FAR, on_line=drafts.on_line,
                              on_update=drafts.on_update, on_ask=drafts.on_ask)
        for turn in ("turn-1", "turn-2"):
            drafts.ended(session.prompt("x", FAR), turn, "dlv-" + turn)
        self.assertEqual(len([d for d in drafts.drafts if d["type"] == "tool.call"]), 2)
        self.assertEqual(len([d for d in drafts.drafts if d["type"] == "tool.result"]), 2)

    def test_a_stopped_turn_ends_its_open_tool_calls_cancelled_before_its_result(self) -> None:
        script = self.TOOL_TURN[:2] + [("wait_cancel",), ("end", "cancelled")]
        out, drafts, ended = self.run_turn(script, cancel_on_update=True)
        self.assertTrue(out.cancel_confirmed)
        tail = [d for d in drafts.drafts if d["type"] != "engine.output"][-3:]   # #408 round 3: one owner
        self.assertEqual([d["type"] for d in tail], ["tool.result", "input.accepted", "result"])
        self.assertEqual(tail[0]["payload"]["status"], "cancelled")
        self.assertEqual([d["type"] for d in ended], ["input.accepted", "result"])

    @unittest.skipUnless(schema_check.AVAILABLE, schema_check.SKIP_REASON)
    def test_every_acp_draft_fits_driver_v2(self) -> None:
        for script, cancel in (([("chunk", "hi"), ("end", "end_turn")], False),
                               ([("chunk", "x"), ("end", "end_turn")], True),
                               ([("error", -32603, "boom")], False),
                               (self.TOOL_TURN, False),
                               (self.TOOL_TURN[:2] + [("wait_cancel",), ("end", "cancelled")], True)):
            _out, drafts, _ended = self.run_turn(script, cancel_on_update=cancel)
            for draft in drafts.drafts:
                self.assertTrue(schema_check.fits("driver.v2.json#/$defs/event_draft", draft), draft)



TOOL_KEY = "r5-made-up-custody-value-7d1"   # invented here; no real key, custody or auth file is read


def tool_update(raw_input, text, call_id="c1"):
    return {"sessionId": "s", "update": {"sessionUpdate": "tool_call", "toolCallId": call_id, "kind": "execute",
                                         "status": "completed", "rawInput": raw_input,
                                         "content": [{"type": "content", "content": {"type": "text", "text": text}}]}}


class ToolDraftRedactionTest(unittest.TestCase):
    """ACP tool-call drafts carry no spelling of the route's selected credential (the round-5 rule of #396,
    applied to the drafts this slice adds). Each fails without the masking of the update's params."""

    def spelled(self):
        nested = json.dumps({"k": TOOL_KEY})                               # JSON carried inside a string
        escaped = json.dumps(json.dumps({"k": TOOL_KEY}).replace("r", chr(92) + "u0072", 1))
        return {"as a value": ({"cmd": "echo " + TOOL_KEY}, "out " + TOOL_KEY),
                "as a key": ({TOOL_KEY: 1}, "ok"),
                "in a list": ({"args": ["echo", TOOL_KEY]}, "ok"),
                "inside JSON in a string": ({"body": nested}, nested),
                "escaped inside JSON in a string": ({"body": json.loads(escaped)}, json.loads(escaped))}

    def test_a_selected_value_never_reaches_a_tool_draft(self) -> None:
        for name, (raw_input, text) in self.spelled().items():
            with self.subTest(spelling=name):
                drafts = ag.AcpDrafts(redact=(TOOL_KEY,))
                drafts.on_update(tool_update(raw_input, text))
                kinds = [d["type"] for d in drafts.drafts]
                self.assertEqual(kinds, ["tool.call", "tool.result"])
                for draft in drafts.drafts:
                    value = draft
                    for _ in range(4):
                        self.assertNotIn(TOOL_KEY, json.dumps(value, ensure_ascii=False))
                        value = _unwrap(value)
                call = drafts.drafts[0]["payload"]
                self.assertEqual(call["input_digest"], ag.input_digest(call["input"]))   # the masked input's

    def test_a_float_that_spells_the_value_is_a_protocol_error(self) -> None:
        drafts = ag.AcpDrafts(redact=("12345",))
        with self.assertRaises(acp.AcpProtocolError):
            drafts.on_update(tool_update({"n": 12345.0}, "ok"))
        self.assertEqual(drafts.drafts, [])

    def test_without_a_value_to_keep_out_the_input_is_carried_as_it_came(self) -> None:   # guard
        drafts = ag.AcpDrafts()
        drafts.on_update(tool_update({"cmd": "echo " + TOOL_KEY}, "out"))
        self.assertEqual(drafts.drafts[0]["payload"]["input"], {"cmd": "echo " + TOOL_KEY})


def _unwrap(value):
    if isinstance(value, dict):
        return {_unwrap(k): _unwrap(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_unwrap(v) for v in value]
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


R1_TOOL = [
    ("update", {"sessionUpdate": "tool_call", "toolCallId": "c1", "title": "Run ls", "kind": "execute",
                "status": "pending", "rawInput": {}}),
    ("update", {"sessionUpdate": "tool_call_update", "toolCallId": "c1", "status": "in_progress",
                "rawInput": {"CommandLine": "ls"}})]


def r1_session(agent, drafts, **kwargs):
    return ag.open_acp(agent, agent.clock, route("gemini-api-key"), CWD, FAR, on_line=drafts.on_line,
                       on_update=kwargs.pop("on_update", drafts.on_update), on_ask=drafts.on_ask, **kwargs)


def pairs(drafts):
    return [(d["type"], d["payload"].get("call_id"), d["payload"].get("status")) for d in drafts
            if d["type"] in ("tool.call", "tool.result", "result")]


class ReviewRoundOne408Test(unittest.TestCase):
    """Regressions for DeepSeek's round-1 read of #408 at 0e2c512d (F1-F3 HIGH, F5-F6 MED, F9 LOW). Each fails
    on that head, except the guards marked as such."""

    def test_f1_a_number_bool_or_null_that_spells_a_value_is_a_protocol_error(self) -> None:
        for value, key in ((12345, "12345"), (True, "true"), (False, "False"), (None, "null"), (1e10, "1e10"),
                           (12345.0, "12345")):
            with self.subTest(value=value, key=key):
                drafts = ag.AcpDrafts(redact=(key,))
                with self.assertRaises(acp.AcpProtocolError):
                    drafts.on_update(tool_update({"n": value}, "ok"))
                self.assertEqual(drafts.drafts, [])
        drafts = ag.AcpDrafts(redact=("12345",))                                       # guard
        drafts.on_update(tool_update({"n": 7, "flag": True}, "ok"))
        self.assertEqual(drafts.drafts[0]["payload"]["input"], {"n": 7, "flag": True})

    def test_f2_a_value_split_across_content_items_is_not_joined_back(self) -> None:
        half = len(TOOL_KEY) // 2
        update = tool_update({}, "unused")
        update["update"]["content"] = [{"type": "content", "content": {"type": "text", "text": TOOL_KEY[:half]}},
                                       {"type": "content", "content": {"type": "text", "text": TOOL_KEY[half:]}}]
        drafts = ag.AcpDrafts(redact=(TOOL_KEY,))
        drafts.on_update(update)
        result = [d["payload"] for d in drafts.drafts if d["type"] == "tool.result"][0]
        self.assertNotIn(TOOL_KEY, result["text"])
        self.assertEqual(len(result["text"]), len(TOOL_KEY))

    def test_f3_a_turn_that_raised_leaves_no_tool_state_to_the_next(self) -> None:
        for close in ("abandoned", "start_turn"):
            with self.subTest(close=close):
                agent = FakeAcpAgent([
                    [("update", {"sessionUpdate": "tool_call", "toolCallId": "c0", "status": "in_progress",
                                 "rawInput": {"ok": 1}}),
                     ("update", {"sessionUpdate": "tool_call", "toolCallId": "c1", "status": "in_progress",
                                 "rawInput": {"n": 12345.0}}), ("wait_cancel",), ("end", "cancelled")],
                    [("update", {"sessionUpdate": "tool_call_update", "toolCallId": "c1", "status": "completed",
                                 "rawInput": {"n": 1}}),
                     ("update", {"sessionUpdate": "tool_call", "toolCallId": "c0", "status": "completed",
                                 "rawInput": {"ok": 2}}), ("end", "end_turn")]])
                drafts = ag.AcpDrafts(redact=("12345",))
                s = r1_session(agent, drafts)
                with self.assertRaises(acp.AcpProtocolError):
                    s.prompt("first", FAR)
                # round 2, MED 1: the raise inside on_update closed c0 already, with no call of the driver's
                self.assertIn(("tool.result", "c0", "cancelled"), pairs(drafts.drafts))
                if close == "abandoned":   # #408 round 3: the session's turn_end drafted the result; harmless
                    self.assertIn(("result", None, None), pairs(drafts.drafts))
                    self.assertEqual(drafts.abandoned(c.PROTOCOL_ERROR, "an update could not be taken"), [])
                else:
                    self.assertEqual(drafts.start_turn(), [])
                mark = len(drafts.drafts)
                out = s.prompt("second", FAR)
                drafts.ended(out, "turn-2", "dlv-2")
                self.assertEqual(pairs(drafts.drafts[mark:]), [("tool.call", "c1", None), ("tool.result", "c1", "ok"),
                                                               ("tool.call", "c0", None), ("tool.result", "c0", "ok"),
                                                               ("result", None, None)])   # c0 reused: afresh

    def test_f3_a_call_refused_at_its_draft_is_not_counted_as_drafted(self) -> None:
        calls = acp.ToolCalls(ag.tool_input_digest)
        with self.assertRaises(acp.AcpProtocolError):
            calls.update({"sessionId": "s", "update": {"sessionUpdate": "tool_call", "toolCallId": "c1",
                                                       "status": "in_progress", "rawInput": {"n": float("nan")}}})
        later = calls.update({"sessionId": "s", "update": {"sessionUpdate": "tool_call_update", "toolCallId": "c1",
                                                           "status": "completed", "rawInput": {"n": 1}}})
        self.assertEqual([d["type"] for d in later], ["tool.call", "tool.result"])

    def test_f5_every_turn_end_closes_its_open_calls(self) -> None:
        cases = {"the agent went away": ([R1_TOOL + [("die",)]], c.ENGINE_EXITED, "error"),
                 "end_turn with a call open": ([R1_TOOL + [("end", "end_turn")]], None, "error"),
                 "the deadline": ([R1_TOOL + [("wait_cancel",), ("end", "cancelled")]], c.DEADLINE_EXCEEDED,
                                  "cancelled")}
        for name, (script, code, status) in cases.items():
            with self.subTest(case=name):
                agent = FakeAcpAgent(script)
                drafts = ag.AcpDrafts()
                s = r1_session(agent, drafts)
                deadline = agent.clock.now + 10.0 if code == c.DEADLINE_EXCEEDED else FAR
                out = s.prompt("x", deadline)
                self.assertEqual(out.code, code)
                ended = drafts.ended(out, "turn-1", "dlv-1") if out.stop_reason is not None else drafts.ended(out)
                results = [(d["payload"]["call_id"], d["payload"]["status"]) for d in drafts.drafts
                           if d["type"] == "tool.result"]
                self.assertEqual(results, [("c1", status)])
                self.assertEqual(ended[-1]["type"], "result")
        out, drafts, ended = self.cancelled_turn()                                          # guard: as before
        self.assertEqual([(d["payload"]["call_id"], d["payload"]["status"]) for d in drafts.drafts
                          if d["type"] == "tool.result"], [("c1", "cancelled")])

    def cancelled_turn(self):
        agent = FakeAcpAgent([R1_TOOL + [("wait_cancel",), ("end", "cancelled")]])
        drafts = ag.AcpDrafts()
        holder = {}

        def on_update(params):
            drafts.on_update(params)
            holder["s"].cancel()

        holder["s"] = r1_session(agent, drafts, on_update=on_update)
        out = holder["s"].prompt("x", FAR)
        return out, drafts, drafts.ended(out, "turn-1", "dlv-1")

    def test_f6_a_value_given_bare_instead_of_in_a_collection_is_refused(self) -> None:
        for bad in (TOOL_KEY, TOOL_KEY.encode()):
            with self.subTest(bad=type(bad).__name__):
                with self.assertRaises(ValueError):
                    ag.AcpDrafts(redact=bad)
                with self.assertRaises(ValueError):
                    ag.mask_forms(bad)
                with self.assertRaises(ValueError):
                    ag.OneShotParser("c", "m", redact=bad)

    def test_f9_an_update_nested_too_deeply_is_a_protocol_error(self) -> None:
        value = {}
        for _ in range(5000):
            value = {"k": value}
        drafts = ag.AcpDrafts(redact=(TOOL_KEY,))
        with self.assertRaises(acp.AcpProtocolError):
            drafts.on_update(tool_update(value, "ok"))

    def test_abandoned_takes_a_route_contract_code(self) -> None:                         # guard
        with self.assertRaises(TypeError):
            ag.AcpDrafts().abandoned("not-a-code", "x")


C0_OPEN = ("update", {"sessionUpdate": "tool_call", "toolCallId": "c0", "status": "in_progress", "rawInput": {"ok": 1}})
C0_AGAIN = ("update", {"sessionUpdate": "tool_call", "toolCallId": "c0", "status": "completed", "rawInput": {"ok": 2}})


def orphans(drafts) -> list:
    """tool.result drafts whose call_id has no tool.call in the same stretch of drafts."""
    called = {d["payload"]["call_id"] for d in drafts if d["type"] == "tool.call"}
    return [d["payload"]["call_id"] for d in drafts
            if d["type"] == "tool.result" and d["payload"]["call_id"] not in called]


class ReviewRoundTwo408Test(unittest.TestCase):
    """Regressions for DeepSeek's round-2 read of #408 at 890f4272 (MED 1, MED 2 and the lows). Each fails on that
    head, except the guards and the F4 witness, marked as such."""

    def session_raise_script(self, late=()):
        # turn 1 drafts c0, then a line that is not JSON-RPC makes the session raise and abandon the turn
        return [[C0_OPEN, ("raw", "{not json"), ("wait_cancel",), *late, ("end", "cancelled")],
                [C0_AGAIN, ("end", "end_turn")]]

    def test_r2_m1_a_turn_the_session_abandons_is_closed_through_open_acp_drafts(self) -> None:
        agent = FakeAcpAgent(self.session_raise_script())
        drafts = ag.AcpDrafts()
        s = ag.open_acp(agent, agent.clock, route("gemini-api-key"), CWD, FAR, drafts=drafts)
        with self.assertRaises(acp.AcpProtocolError):
            s.prompt("first", FAR)
        self.assertEqual(pairs(drafts.drafts), [("tool.call", "c0", None), ("tool.result", "c0", "cancelled"),
                                                ("result", None, None)])
        self.assertEqual(drafts.drafts[-1]["payload"]["code"], c.PROTOCOL_ERROR)
        mark = len(drafts.drafts)
        out = s.prompt("second", FAR)
        drafts.ended(out, "turn-2", "dlv-2")
        self.assertEqual(pairs(drafts.drafts[mark:]), [("tool.call", "c0", None), ("tool.result", "c0", "ok"),
                                                       ("result", None, None)])

    def test_r2_m1_a_raise_inside_on_update_closes_the_turn_without_the_drivers_help(self) -> None:
        agent = FakeAcpAgent([[C0_OPEN, ("update", {"sessionUpdate": "tool_call", "toolCallId": "c1",
                                                    "status": "in_progress", "rawInput": {"n": 12345.0}}),
                               ("wait_cancel",), ("end", "cancelled")],
                              [("update", {"sessionUpdate": "tool_call_update", "toolCallId": "c1",
                                           "status": "completed", "rawInput": {"n": 1}}), ("end", "end_turn")]])
        drafts = ag.AcpDrafts(redact=("12345",))
        s = r1_session(agent, drafts)                                   # the four callbacks only, nothing else
        with self.assertRaises(acp.AcpProtocolError):
            s.prompt("first", FAR)
        self.assertEqual(pairs(drafts.drafts), [("tool.call", "c0", None), ("tool.result", "c0", "cancelled"),
                                                ("result", None, None)])
        mark = len(drafts.drafts)
        out = s.prompt("second", FAR)
        drafts.ended(out, "turn-2", "dlv-2")
        self.assertEqual(orphans(drafts.drafts[mark:]), [])
        self.assertEqual(pairs(drafts.drafts[mark:]), [("tool.call", "c1", None), ("tool.result", "c1", "ok"),
                                                       ("result", None, None)])

    def test_r2_m1_a_raise_inside_on_line_ends_the_turns_open_calls(self) -> None:
        drafts = ag.AcpDrafts()
        drafts.on_update({"sessionId": "s", "update": C0_OPEN[1]})
        with self.assertRaises(acp.AcpProtocolError):
            drafts.on_line('{"jsonrpc":"2.0","method":"x","params":{"t":"' + chr(0xDCFF) + '"}}')   # not UTF-8
        self.assertEqual(pairs(drafts.drafts), [("tool.call", "c0", None), ("tool.result", "c0", "cancelled")])
        self.assertEqual(drafts.start_turn(), [])

    def test_r3_a_raise_inside_on_update_ends_the_turns_open_calls_itself(self) -> None:
        drafts = ag.AcpDrafts(redact=("12345",))
        drafts.on_update({"sessionId": "s", "update": C0_OPEN[1]})
        with self.assertRaises(acp.AcpProtocolError):
            drafts.on_update({"sessionId": "s", "update": SPELLS_KEY[1]})
        self.assertEqual(pairs(drafts.drafts), [("tool.call", "c0", None), ("tool.result", "c0", "cancelled")])

    def test_r3_ended_and_abandoned_end_open_calls_with_no_session(self) -> None:
        # the drafts used with no session at all: ended() and abandoned() reach the one owner too
        drafts = ag.AcpDrafts()
        drafts.on_update({"sessionId": "s", "update": C0_OPEN[1]})
        drafts.ended(acp.TurnOutcome(1, None, "end_turn", False, False, None, None, None, "", ""))
        self.assertIn(("tool.result", "c0", "error"), pairs(drafts.drafts))
        drafts = ag.AcpDrafts()
        drafts.on_update({"sessionId": "s", "update": C0_OPEN[1]})
        drafts.abandoned(c.ENGINE_ERROR, "the driver's except")
        self.assertIn(("tool.result", "c0", "cancelled"), pairs(drafts.drafts))
        drafts.on_update({"sessionId": "s", "update": C0_AGAIN[1]})                 # the id again: afresh
        self.assertEqual(pairs(drafts.drafts)[-2:], [("tool.call", "c0", None), ("tool.result", "c0", "ok")])

    def test_r2_m1_start_turn_closes_what_a_session_raise_left_when_nothing_is_wired(self) -> None:   # guard
        agent = FakeAcpAgent(self.session_raise_script())
        drafts = ag.AcpDrafts()
        s = ag.open_acp(agent, agent.clock, route("gemini-api-key"), CWD, FAR,      # wrapped: no turn_end comes along
                        on_line=lambda line: drafts.on_line(line), on_update=lambda p: drafts.on_update(p),
                        on_ask=lambda ask: drafts.on_ask(ask))
        with self.assertRaises(acp.AcpProtocolError):
            s.prompt("first", FAR)
        self.assertEqual(pairs(drafts.start_turn()), [("tool.result", "c0", "cancelled")])
        mark = len(drafts.drafts)
        out = s.prompt("second", FAR)
        drafts.ended(out, "turn-2", "dlv-2")
        self.assertEqual(orphans(drafts.drafts[mark:]), [])

    def test_r2_m1_an_on_turn_end_that_raises_does_not_hide_the_turns_error(self) -> None:
        agent = FakeAcpAgent(self.session_raise_script())

        def failing(number, outcome, code, detail):
            raise ValueError("synthetic")

        s = ag.open_acp(agent, agent.clock, route("gemini-api-key"), CWD, FAR, on_turn_end=failing)
        with self.assertRaises(acp.AcpProtocolError):
            s.prompt("first", FAR)
        self.assertIsNone(s.prompt("second", FAR).code)               # and the outcome is not replaced either
        self.assertEqual(s.turn_end_failures, 2)

    def test_r2_m1_open_acp_drafts_takes_every_callback_itself(self) -> None:
        agent = FakeAcpAgent()
        drafts = ag.AcpDrafts()
        for given in ({"on_line": print}, {"on_turn_end": print}):
            with self.subTest(given=sorted(given)), self.assertRaises(TypeError):
                ag.open_acp(agent, agent.clock, route("gemini-api-key"), CWD, FAR, drafts=drafts, **given)
        with self.assertRaises(TypeError):
            ag.open_acp(agent, agent.clock, route("gemini-api-key"), CWD, FAR, drafts=object())
        self.assertEqual(agent.received, [])

    def test_r2_f4_a_late_call_reads_as_the_next_turns_own(self) -> None:          # the witness (passes)
        late = ("update", {"sessionUpdate": "tool_call", "toolCallId": "late-1", "status": "in_progress",
                           "rawInput": {"a": 2}})
        streams = {"late from turn 1": [[R1_TOOL[1], ("wait_cancel",), ("end", "cancelled"), late],
                                        [("chunk", "second"), ("end", "end_turn")]],
                   "turn 2's own call": [[R1_TOOL[1], ("wait_cancel",), ("end", "cancelled")],
                                         [late, ("chunk", "second"), ("end", "end_turn")]]}
        seen = {}
        for name, script in streams.items():
            agent = FakeAcpAgent(script)
            drafts = ag.AcpDrafts()
            lines = []
            holder = {}

            def on_update(params, d=drafts, h=holder):
                d.on_update(params)
                if not h.get("cancelled"):
                    h["cancelled"] = True
                    h["s"].cancel()

            holder["s"] = ag.open_acp(agent, agent.clock, route("gemini-api-key"), CWD, FAR,
                                      on_line=lambda line, d=drafts, ls=lines: (ls.append(line), d.on_line(line)),
                                      on_update=on_update, on_ask=drafts.on_ask)
            out1 = holder["s"].prompt("first", FAR)
            drafts.ended(out1, "turn-1", "dlv-1")
            self.assertIsNone(holder["s"].connection.abandoned_turn)   # turn 1 was answered: nothing to drain
            mark, line_mark = len(drafts.drafts), len(lines)
            out2 = holder["s"].prompt("second", FAR)
            drafts.ended(out2, "turn-2", "dlv-2")
            seen[name] = (lines[line_mark:], pairs(drafts.drafts[mark:]))
        # the client reads the very same lines in turn 2 either way: no rule over what it reads can tell them apart
        self.assertEqual(seen["late from turn 1"], seen["turn 2's own call"])
        self.assertIn(("tool.call", "late-1", None), seen["turn 2's own call"][1])

    def test_r2_f4_an_abandoned_turns_updates_read_while_draining_are_not_drafted(self) -> None:   # guard
        drained = ("update", {"sessionUpdate": "tool_call", "toolCallId": "drained-1", "status": "in_progress",
                              "rawInput": {}})
        agent = FakeAcpAgent(self.session_raise_script(late=(drained,)))
        drafts = ag.AcpDrafts()
        s = ag.open_acp(agent, agent.clock, route("gemini-api-key"), CWD, FAR, drafts=drafts)
        with self.assertRaises(acp.AcpProtocolError):
            s.prompt("first", FAR)
        out = s.prompt("second", FAR)
        drafts.ended(out, "turn-2", "dlv-2")
        self.assertNotIn("drained-1", [d["payload"].get("call_id") for d in drafts.drafts])

    def test_r2_f7_a_turns_end_is_drafted_once(self) -> None:
        def outcome(number):
            return acp.TurnOutcome(number, None, "end_turn", False, False, None, None, None, "", "hi")

        drafts = ag.AcpDrafts()
        drafts.ended(outcome(1), "turn-1", "dlv-1")
        # #408 round 3: a second end of one turn drafts nothing and RAISES nothing (no close replaces an error)
        self.assertEqual(drafts.ended(outcome(1), "turn-1", "dlv-1"), [])
        self.assertEqual(drafts.abandoned(c.ENGINE_ERROR, "x"), [])
        self.assertEqual([d["type"] for d in drafts.drafts], ["input.accepted", "result"])
        drafts.on_line('{"jsonrpc":"2.0","id":3,"result":{"stopReason":"end_turn"}}')     # the next turn read
        self.assertEqual(len(drafts.ended(outcome(2), "turn-2", "dlv-2")), 2)
        drafts.start_turn()                                                  # a turn begun by the driver
        self.assertEqual(len(drafts.ended(outcome(3), "turn-3", "dlv-3")), 2)
        # abandoned() is gated by the turn: a line read, or start_turn, begins the next one
        self.assertEqual(drafts.abandoned(c.ENGINE_ERROR, "x"), [])
        drafts.on_line('{"jsonrpc":"2.0","method":"x"}')
        self.assertEqual(len(drafts.abandoned(c.ENGINE_ERROR, "x")), 1)
        self.assertEqual(drafts.abandoned(c.ENGINE_ERROR, "x"), [])
        drafts.start_turn()
        self.assertEqual(len(drafts.abandoned(c.ENGINE_ERROR, "x")), 1)

    def test_r2_f8_wrong_shaped_callbacks_are_refused(self) -> None:
        drafts = ag.AcpDrafts()
        for bad in ({}, {"sessionUpdate": "tool_call", "toolCallId": "c"}, [1], "x", None, {"update": [1]}):
            with self.subTest(params=repr(bad)), self.assertRaises(TypeError):
                drafts.on_update(bad)
        for bad in ({"tool_call": {}}, None, "ask-1"):
            with self.subTest(ask=repr(bad)), self.assertRaises(TypeError):
                drafts.on_ask(bad)
        self.assertEqual(drafts.drafts, [])

    def test_r2_f10_a_lone_surrogate_in_a_tool_result_or_name_leaves_the_draft_writable(self) -> None:
        line = ('{"sessionId":"s","update":{"sessionUpdate":"tool_call","toolCallId":"c1","status":"completed",'
                '"name":"bad' + chr(92) + 'ud800name","kind":"execute","rawInput":{},'
                '"content":[{"type":"content","content":{"type":"text","text":"a' + chr(92) + 'ud800b"}}]}}')
        drafts = ag.AcpDrafts()
        drafts.on_update(json.loads(line))
        for draft in drafts.drafts:
            json.dumps(draft, ensure_ascii=False).encode("utf-8")          # raises if a surrogate is left
        call, result = drafts.drafts
        self.assertEqual(call["payload"]["name"], "execute")
        self.assertEqual(result["payload"]["text"], "a" + chr(0xFFFD) + "b")
        bad_id = json.loads('{"sessionId":"s","update":{"sessionUpdate":"tool_call","toolCallId":"c' + chr(92)
                            + 'ud800","status":"completed","rawInput":{}}}')
        drafts = ag.AcpDrafts()
        drafts.on_update(bad_id)
        self.assertEqual(drafts.drafts, [])                                  # an id no draft could carry

    def test_r2_f12_a_digest_not_of_sha256_form_is_refused(self) -> None:
        for digest in (lambda v: "not-a-digest", lambda v: "A" * 64, lambda v: 5):
            with self.subTest(digest=digest(None)), self.assertRaises(acp.AcpInvalid):
                acp.ToolCalls(digest).update(tool_update({}, "ok"))
        good = acp.ToolCalls(ag.input_digest).update(tool_update({}, "ok"))                   # guard
        self.assertEqual(good[0]["payload"]["input_digest"], ag.input_digest({}))

    def test_r2_the_class_docstring_names_every_scalar_the_mask_refuses(self) -> None:
        doc = ag.AcpDrafts.__doc__
        self.assertIn("a number, a bool or null", doc)
        self.assertNotIn("a float whose text spells a value is a protocol error", doc)


def unanswered(drafts) -> list:
    """tool.call ids with no tool.result anywhere in ``drafts``."""
    resulted = {d["payload"]["call_id"] for d in drafts if d["type"] == "tool.result"}
    return [d["payload"]["call_id"] for d in drafts
            if d["type"] == "tool.call" and d["payload"]["call_id"] not in resulted]


NEXT_TURN = [C0_AGAIN, ("end", "end_turn")]
UNPARSEABLE = ("raw", "{not json")
NOT_JSON_RPC = ("raw", '{"jsonrpc":"1.0","id":99}')
NOT_UTF8 = ("raw", '{"jsonrpc":"2.0","method":"x","params":{"t":"' + chr(0xDCFF) + '"}}')
SPELLS_KEY = ("update", {"sessionUpdate": "tool_call", "toolCallId": "c1", "status": "in_progress",
                         "rawInput": {"n": 12345.0}})


class ReviewRoundThree408Test(unittest.TestCase):
    """DeepSeek's round-3 read of #408 at 1858b3d4: ONE place owns a turn's open tool calls and every way a turn
    ends reaches it. One test per exit of the report's table; the two wirings are open_acp(drafts=) and the
    drafts' own three callbacks (bound, so the session brings turn_end along)."""

    WIRINGS = ("drafts=", "three callbacks")

    def session(self, scripts, wiring, drafts, on_ask=None):
        agent = FakeAcpAgent(scripts)
        if wiring == "drafts=":
            return agent, ag.open_acp(agent, agent.clock, route("gemini-api-key"), CWD, FAR, drafts=drafts)
        return agent, ag.open_acp(agent, agent.clock, route("gemini-api-key"), CWD, FAR, on_line=drafts.on_line,
                                  on_update=drafts.on_update, on_ask=on_ask or drafts.on_ask)

    def raised(self, trigger, error, *, redact=(), on_ask=None, wirings=WIRINGS, tail=(("wait_cancel",),)):
        for wiring in wirings:
            with self.subTest(wiring=wiring):
                drafts = ag.AcpDrafts(redact=redact)
                _agent, s = self.session([[C0_OPEN, trigger, *tail, ("end", "cancelled")], NEXT_TURN],
                                         wiring, drafts, on_ask)
                with self.assertRaises(error):                       # the turn's own error, not a close's
                    s.prompt("first", FAR)
                self.assertEqual(unanswered(drafts.drafts), [])     # closed at the raise, not in the next turn
                self.assertIn(("tool.result", "c0", "cancelled"), pairs(drafts.drafts))
                self.assertEqual(pairs(drafts.drafts)[-1], ("result", None, None))
                self.assertEqual(drafts.abandoned(c.ENGINE_ERROR, "the driver's own close"), [])   # harmless
                mark = len(drafts.drafts)
                drafts.ended(s.prompt("second", FAR), "turn-2", "dlv-2")
                self.assertEqual(orphans(drafts.drafts[mark:]), [])
                self.assertEqual(pairs(drafts.drafts[mark:]), [("tool.call", "c0", None), ("tool.result", "c0", "ok"),
                                                               ("result", None, None)])

    def answered(self, script, deadline_in, status):
        for wiring in self.WIRINGS:
            with self.subTest(wiring=wiring):
                drafts = ag.AcpDrafts()
                agent, s = self.session([script], wiring, drafts)
                out = s.prompt("x", FAR if deadline_in is None else agent.clock.now + deadline_in)
                self.assertEqual(unanswered(drafts.drafts), [])     # closed when the turn ended
                self.assertIn(("tool.result", "c0", status), pairs(drafts.drafts))
                ended = drafts.ended(out, "turn-1", "dlv-1") if out.stop_reason is not None else drafts.ended(out)
                self.assertEqual([d["type"] for d in ended if d["type"] == "tool.result"], [])
                self.assertEqual(drafts.ended(out), [])             # a second end: harmless

    # -- the exits (the report's table) ----------------------------------------------------------------------
    def test_exit_the_agent_answers_end_turn_with_a_call_open(self) -> None:
        self.answered([C0_OPEN, ("end", "end_turn")], None, "error")

    def test_exit_the_agent_answers_cancelled_after_the_callers_cancel(self) -> None:
        for wiring in ("three callbacks", "four given"):
            with self.subTest(wiring=wiring):
                drafts = ag.AcpDrafts()
                holder = {}

                def on_update(params, d=drafts, h=holder):
                    d.on_update(params)
                    h["s"].cancel()

                agent = FakeAcpAgent([[C0_OPEN, ("wait_cancel",), ("end", "cancelled")]])
                extra = {"on_turn_end": drafts.turn_end} if wiring == "four given" else {}
                holder["s"] = s = ag.open_acp(agent, agent.clock, route("gemini-api-key"), CWD, FAR,
                                              on_line=drafts.on_line, on_update=on_update, on_ask=drafts.on_ask,
                                              **extra)
                out = s.prompt("x", FAR)
                self.assertTrue(out.cancel_requested)
                self.assertEqual(unanswered(drafts.drafts), [])
                self.assertIn(("tool.result", "c0", "cancelled"), pairs(drafts.drafts))

    def test_exit_the_agent_stops_the_turn_itself(self) -> None:
        self.answered([C0_OPEN, ("end", "cancelled")], None, "cancelled")

    def test_exit_wrapped_callbacks_end_the_turn_through_the_drivers_abandoned(self) -> None:
        # callbacks wrapped (not bound): no turn_end comes along, so the driver's documented abandoned() in its
        # except ends c0 at the raise, before the next prompt
        drafts = ag.AcpDrafts()
        agent = FakeAcpAgent([[C0_OPEN, UNPARSEABLE, ("wait_cancel",), ("end", "cancelled")], NEXT_TURN])
        s = ag.open_acp(agent, agent.clock, route("gemini-api-key"), CWD, FAR,
                        on_line=lambda line: drafts.on_line(line), on_update=lambda p: drafts.on_update(p),
                        on_ask=lambda ask: drafts.on_ask(ask))
        try:
            s.prompt("first", FAR)
        except acp.AcpProtocolError as err:
            drafts.abandoned(err.code, "the driver's except")
        self.assertEqual(unanswered(drafts.drafts), [])
        mark = len(drafts.drafts)
        drafts.ended(s.prompt("second", FAR), "turn-2", "dlv-2")
        self.assertEqual(orphans(drafts.drafts[mark:]), [])
        self.assertEqual(unanswered(drafts.drafts), [])
        self.assertEqual(pairs(drafts.drafts), [("tool.call", "c0", None), ("tool.result", "c0", "cancelled"),
                                                ("result", None, None), ("tool.call", "c0", None),
                                                ("tool.result", "c0", "ok"), ("result", None, None)])

    def test_exit_the_deadline(self) -> None:
        self.answered([C0_OPEN, ("wait_cancel",), ("end", "cancelled")], 10.0, "cancelled")

    def test_exit_the_agent_goes_away(self) -> None:
        self.answered([C0_OPEN, ("die",)], None, "error")

    def test_exit_a_line_the_connection_cannot_parse(self) -> None:
        self.raised(UNPARSEABLE, acp.AcpProtocolError)

    def test_exit_a_protocol_error(self) -> None:
        self.raised(NOT_JSON_RPC, acp.AcpProtocolError)

    def test_exit_on_line_raises(self) -> None:
        self.raised(NOT_UTF8, acp.AcpProtocolError)

    def test_exit_on_update_raises(self) -> None:
        self.raised(SPELLS_KEY, acp.AcpProtocolError, redact=("12345",))

    def test_exit_on_ask_raises(self) -> None:
        def on_ask(ask):
            raise RuntimeError("synthetic: the approval queue failed")

        # the ask is answered "cancelled" with the turn's cancel, so the fake agent then answers the prompt
        self.raised(("ask", TOOL_CALL, OPTIONS_ALL), RuntimeError, on_ask=on_ask, wirings=("three callbacks",),
                    tail=())

    def test_exit_on_turn_end_itself_raises(self) -> None:
        drafts = ag.AcpDrafts()

        def turn_end(number, outcome, code, detail):
            drafts.turn_end(number, outcome, code, detail)
            raise ValueError("synthetic: the driver's turn end failed")

        agent = FakeAcpAgent([[C0_OPEN, UNPARSEABLE, ("wait_cancel",), ("end", "cancelled")], NEXT_TURN])
        s = ag.open_acp(agent, agent.clock, route("gemini-api-key"), CWD, FAR, on_line=drafts.on_line,
                        on_update=drafts.on_update, on_ask=drafts.on_ask, on_turn_end=turn_end)
        with self.assertRaises(acp.AcpProtocolError):                # not the ValueError
            s.prompt("first", FAR)
        self.assertEqual(unanswered(drafts.drafts), [])
        out = s.prompt("second", FAR)                                # its outcome is not replaced either
        self.assertIsNone(out.code)
        self.assertEqual(s.turn_end_failures, 2)

    def test_exit_an_abandoned_prompt_then_the_drivers_documented_close(self) -> None:   # round 3, MED 2
        drafts = ag.AcpDrafts()
        _agent, s = self.session([[C0_OPEN, UNPARSEABLE, ("wait_cancel",), ("end", "cancelled")], NEXT_TURN],
                                 "drafts=", drafts)
        try:
            s.prompt("first", FAR)
        except acp.AcpProtocolError as err:
            self.assertEqual(drafts.abandoned(err.code, "the driver's except"), [])   # no TypeError replaces it
        else:
            self.fail("the turn did not raise")
        self.assertEqual([d["type"] for d in drafts.drafts if d["type"] == "result"], ["result"])

    def test_low3_a_turn_results_detail_with_a_lone_surrogate_stays_writable(self) -> None:
        drafts = ag.AcpDrafts()
        out = acp.TurnOutcome(1, c.ENGINE_ERROR, None, False, False, None, None, None, "acp error: a" + chr(0xD800), "")
        ended = drafts.ended(out)
        json.dumps(ended, ensure_ascii=False).encode("utf-8")
        self.assertEqual(ended[-1]["payload"]["detail"], "acp error: a" + chr(0xFFFD))
        drafts = ag.AcpDrafts()
        drafts.abandoned(c.ENGINE_ERROR, "x" + chr(0xDC00))
        json.dumps(drafts.drafts, ensure_ascii=False).encode("utf-8")

    def test_low5_a_digest_that_is_not_callable_or_fails_is_typed(self) -> None:
        with self.assertRaises(acp.AcpInvalid):
            acp.ToolCalls("not callable")

        def failing(value):
            raise TypeError("synthetic")

        with self.assertRaises(acp.AcpInvalid):
            acp.ToolCalls(failing).update(tool_update({}, "ok"))


STRAY = ("raw", '{"jsonrpc":"2.0","id":9999,"result":{"stopReason":"end_turn"}}')
C0_DONE = ("update", {"sessionUpdate": "tool_call_update", "toolCallId": "c0", "status": "completed",
                      "content": [{"type": "content", "content": {"type": "text", "text": "out"}}]})


class SubStr(str):
    """A str subclass that lies about what it holds."""

    def __contains__(self, item):
        return False

    def find(self, *args, **kwargs):
        return -1


class ReviewRoundFour408Test(unittest.TestCase):
    """DeepSeek's round-4 read of #408 at ee885aed (MED 1, MED 2, LOW 3 to 7, the two green guards), and what
    the lane's own attack on the answer found (the report lists each). Each fails on that head."""

    def test_m1_one_drafts_across_two_sessions_drafts_each_sessions_result(self) -> None:
        drafts = ag.AcpDrafts()
        for start_turn in (False, True):
            with self.subTest(start_turn=start_turn):
                before = len([d for d in drafts.drafts if d["type"] == "result"])
                for _session in range(2):
                    agent = FakeAcpAgent([[("chunk", "hi"), ("end", "end_turn")]])
                    s = ag.open_acp(agent, agent.clock, route("gemini-api-key"), CWD, FAR, drafts=drafts)
                    if start_turn:
                        drafts.start_turn()
                    out = s.prompt("x", FAR)
                    self.assertEqual(out.turn, 1)                       # numbers start again in each session
                    self.assertEqual(len(drafts.ended(out, "turn-1", "dlv-1")), 2)
                self.assertEqual(len([d for d in drafts.drafts if d["type"] == "result"]) - before, 2)

    def test_m2_a_stray_response_line_ends_nothing(self) -> None:
        for wiring in ("drafts=", "wrapped"):
            with self.subTest(wiring=wiring):
                drafts = ag.AcpDrafts()
                agent = FakeAcpAgent([[C0_OPEN, STRAY, ("stale_response",), C0_DONE, ("end", "end_turn")]])
                if wiring == "drafts=":
                    s = ag.open_acp(agent, agent.clock, route("gemini-api-key"), CWD, FAR, drafts=drafts)
                else:
                    s = ag.open_acp(agent, agent.clock, route("gemini-api-key"), CWD, FAR,
                                    on_line=lambda line: drafts.on_line(line), on_update=lambda p: drafts.on_update(p))
                out = s.prompt("x", FAR)
                drafts.ended(out, "turn-1", "dlv-1")
                self.assertEqual(pairs(drafts.drafts), [("tool.call", "c0", None), ("tool.result", "c0", "ok"),
                                                        ("result", None, None)])
                self.assertEqual(s.connection.stray_responses, 2)

    def test_low3_ended_after_abandoned_drafts_no_second_result(self) -> None:
        drafts = ag.AcpDrafts()
        drafts.on_update({"sessionId": "s", "update": C0_OPEN[1]})
        drafts.abandoned(c.ENGINE_ERROR, "x")
        out = acp.TurnOutcome(1, None, "end_turn", False, False, None, None, None, "", "")
        self.assertEqual(drafts.ended(out, "turn-1", "dlv-1"), [])
        self.assertEqual([d["type"] for d in drafts.drafts if d["type"] == "result"], ["result"])

    def test_low4_a_callback_that_is_not_callable_is_refused(self) -> None:
        agent = FakeAcpAgent()
        for name in ("on_turn_end", "on_line", "on_update", "on_ask"):
            with self.subTest(callback=name), self.assertRaises(acp.AcpInvalid):
                acp.AcpSession(agent, agent.clock, **{name: 1})

    def test_low5_two_draft_makers_are_refused_not_left_with_no_turn_end(self) -> None:
        one, two = ag.AcpDrafts(), ag.AcpDrafts()
        agent = FakeAcpAgent()
        with self.assertRaises(acp.AcpInvalid):
            acp.AcpSession(agent, agent.clock, on_line=one.on_line, on_update=two.on_update)
        s = acp.AcpSession(agent, agent.clock, on_line=one.on_line, on_update=two.on_update,
                           on_turn_end=one.turn_end)                       # guard: given, it is taken
        self.assertIsNotNone(s)

    def test_low6_a_tool_input_with_a_lone_surrogate_is_the_agents_protocol_error(self) -> None:
        drafts = ag.AcpDrafts()
        params = json.loads('{"sessionId":"s","update":{"sessionUpdate":"tool_call","toolCallId":"c1",'
                            '"status":"in_progress","rawInput":{"t":"a' + chr(92) + 'ud800"}}}')
        with self.assertRaises(acp.AcpProtocolError) as ctx:
            drafts.on_update(params)
        self.assertEqual(ctx.exception.code, c.PROTOCOL_ERROR)
        self.assertIn("unpaired surrogate", acp.ToolCalls._call.__doc__)

    def test_low7_a_digest_that_raises_value_error_is_the_callers_failure(self) -> None:
        def raising(value):
            raise ValueError("synthetic")

        with self.assertRaises(acp.AcpInvalid):
            acp.ToolCalls(raising).update(tool_update({}, "ok"))
        with self.assertRaises(acp.AcpProtocolError):                     # None: no canonical text
            acp.ToolCalls(lambda v: None).update(tool_update({}, "ok"))

    def test_guard6_turn_end_twice_for_one_turn_drafts_one_result(self) -> None:
        drafts = ag.AcpDrafts()
        drafts.turn_end((7, 1), None, c.PROTOCOL_ERROR, "x")
        drafts.on_line('{"jsonrpc":"2.0","method":"x"}')
        self.assertEqual(drafts.turn_end((7, 1), None, c.PROTOCOL_ERROR, "x"), [])
        self.assertEqual(len(drafts.turn_end((8, 1), None, c.PROTOCOL_ERROR, "x")), 1)   # another session

    def test_guard7_a_turn_end_outside_the_contract_is_refused(self) -> None:
        drafts = ag.AcpDrafts()
        for code, detail in (("not_a_code", "x"), (None, "x"), (c.ENGINE_ERROR, 5), (SubStr(c.ENGINE_ERROR), "x")):
            with self.subTest(code=code, detail=detail):
                for call in (lambda: drafts.turn_end((1, 1), None, code, detail),
                             lambda: drafts.abandoned(code, detail)):
                    with self.assertRaises(TypeError):
                        call()
        for key in ((1,), (1, "1"), [1, 1], (True, 1), "1"):
            with self.subTest(key=key), self.assertRaises(TypeError):
                drafts.turn_end(key, None, c.ENGINE_ERROR, "x")
        self.assertEqual(drafts.drafts, [])

    def test_m1_one_drafts_across_two_sessions_drafts_each_raised_turns_result(self) -> None:
        drafts = ag.AcpDrafts()
        for _session in range(2):
            agent = FakeAcpAgent([[C0_OPEN, UNPARSEABLE, ("wait_cancel",), ("end", "cancelled")]])
            s = ag.open_acp(agent, agent.clock, route("gemini-api-key"), CWD, FAR, drafts=drafts)
            with self.assertRaises(acp.AcpProtocolError):                # turn 1 of each session raises
                s.prompt("x", FAR)
        self.assertEqual(len([d for d in drafts.drafts if d["type"] == "result"]), 2)
        self.assertEqual(unanswered(drafts.drafts), [])

    # -- what the lane's own attack found (the report lists each attempt) --------------------------------------
    def test_attack_a_line_or_mask_text_that_is_a_str_subclass_is_refused(self) -> None:
        key = "r4-made-up-key"
        drafts = ag.AcpDrafts(redact=(key,))
        with self.assertRaisesRegex(TypeError, "on_line takes"):          # on_line's own refusal, not mask's
            drafts.on_line(SubStr('{"t":"' + key + '"}'))
        with self.assertRaises(TypeError):
            ag.mask(SubStr(key), ag.mask_forms((key,)))
        self.assertEqual(drafts.drafts, [])

    def test_attack_ids_that_are_str_subclasses_are_refused(self) -> None:
        out = acp.TurnOutcome(1, None, "end_turn", False, False, None, None, None, "", "")
        drafts = ag.AcpDrafts()
        for turn_id, delivery_id in ((SubStr("turn-1"), "dlv-1"), ("turn-1", SubStr("dlv-1"))):
            with self.subTest(turn_id=type(turn_id).__name__), self.assertRaises(TypeError):
                drafts.ended(out, turn_id, delivery_id)

    def test_attack_an_offset_that_is_not_a_count_is_refused(self) -> None:
        for offset in (-1, True, 1.0, "0"):
            with self.subTest(offset=offset), self.assertRaises(TypeError):
                ag.AcpDrafts(offset=offset)

    def test_attack_every_end_in_every_order_drafts_one_result_per_turn(self) -> None:
        out = acp.TurnOutcome(1, None, "end_turn", False, False, None, None, None, "", "")
        orders = {
            "ended, ended": [lambda d: d.ended(out), lambda d: d.ended(out)],
            "ended, abandoned": [lambda d: d.ended(out), lambda d: d.abandoned(c.ENGINE_ERROR, "x")],
            "abandoned, ended": [lambda d: d.abandoned(c.ENGINE_ERROR, "x"), lambda d: d.ended(out)],
            "abandoned, abandoned": [lambda d: d.abandoned(c.ENGINE_ERROR, "x"),
                                     lambda d: d.abandoned(c.ENGINE_ERROR, "x")],
            "turn_end raised, abandoned, ended": [lambda d: d.turn_end((0, 1), None, c.ENGINE_ERROR, "x"),
                                                  lambda d: d.abandoned(c.ENGINE_ERROR, "x"), lambda d: d.ended(out)],
            "turn_end answered, ended, ended": [lambda d: d.turn_end((0, 1), out, None, ""), lambda d: d.ended(out),
                                                lambda d: d.ended(out)],
            "start_turn, start_turn, ended": [lambda d: d.start_turn(), lambda d: d.start_turn(),
                                              lambda d: d.ended(out)],
        }
        for name, steps in orders.items():
            with self.subTest(order=name):
                drafts = ag.AcpDrafts()
                drafts.on_update({"sessionId": "s", "update": C0_OPEN[1]})
                for step in steps:
                    step(drafts)
                self.assertEqual(len([d for d in drafts.drafts if d["type"] == "result"]), 1)
                self.assertEqual(unanswered(drafts.drafts), [])
                self.assertEqual(len([d for d in drafts.drafts if d["type"] == "tool.result"]), 1)

    def test_attack_a_repeated_ended_after_new_lines_drafts_nothing(self) -> None:
        out = acp.TurnOutcome(1, None, "end_turn", False, False, None, None, None, "", "")
        drafts = ag.AcpDrafts()
        drafts.ended(out)
        drafts.on_line('{"jsonrpc":"2.0","method":"x"}')                   # read since: the turn is still named
        self.assertEqual(drafts.ended(out), [])
        self.assertEqual(len([d for d in drafts.drafts if d["type"] == "result"]), 1)

    def test_attack_a_late_and_a_repeated_answer_line_end_nothing(self) -> None:
        answer = '{"jsonrpc":"2.0","id":3,"result":{"stopReason":"end_turn"}}'
        drafts = ag.AcpDrafts()
        drafts.on_update({"sessionId": "s", "update": C0_OPEN[1]})
        for _ in range(3):
            drafts.on_line(answer)                                       # repeated: ends nothing by itself
        drafts.on_update({"sessionId": "s", "update": C0_DONE[1]})
        self.assertEqual(pairs(drafts.drafts), [("tool.call", "c0", None), ("tool.result", "c0", "ok")])

if __name__ == "__main__":
    unittest.main()
