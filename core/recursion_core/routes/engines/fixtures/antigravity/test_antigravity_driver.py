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
from recursion_core.routes.engines.fixtures.antigravity.fake_acp_agent import ANTIGRAVITY_METHODS, FakeAcpAgent

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

    def test_a_stopped_turn_ends_its_open_tool_calls_cancelled_before_its_result(self) -> None:
        script = self.TOOL_TURN[:2] + [("wait_cancel",), ("end", "cancelled")]
        out, _drafts, ended = self.run_turn(script, cancel_on_update=True)
        self.assertTrue(out.cancel_confirmed)
        self.assertEqual([d["type"] for d in ended], ["tool.result", "input.accepted", "result"])
        self.assertEqual(ended[0]["payload"]["status"], "cancelled")

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


if __name__ == "__main__":
    unittest.main()
