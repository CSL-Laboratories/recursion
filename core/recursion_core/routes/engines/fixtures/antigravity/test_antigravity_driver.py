# Written by Claude Opus 5.5 (claude-opus-5-5), Claude Code builder lane ENGINE-E5, for Lucas.
"""Antigravity's methods, driver.v2 declarations and ACP glue; nothing registered, no spawn path.

Run: PYTHONPATH=core <python> -m unittest recursion_core.routes.engines.fixtures.antigravity.test_antigravity_driver
"""

from __future__ import annotations

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

    def test_the_personal_account_switch_is_off_and_is_not_an_option(self) -> None:
        self.assertIs(ag.PERSONAL_ACCOUNT_ENABLED, False)
        with self.assertRaises(c.RouteError) as ctx:
            ag.route_method(route("oauth-personal", "subscription", None))
        self.assertEqual((ctx.exception.code, ctx.exception.detail), (c.LAUNCH_REFUSED, ag.PERSONAL_ACCOUNT_OFF))
        import inspect
        for fn in (ag.route_method, ag.open_acp, ag.AntigravityOneShotAdapter.__init__, ag.one_shot_factory):
            self.assertNotIn("personal", " ".join(inspect.signature(fn).parameters))

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

        def on_update(_p):
            if cancel_on_update:
                holder["s"].cancel()

        session = ag.open_acp(agent, agent.clock, route("gemini-api-key"), CWD, FAR,
                              on_line=drafts.on_line, on_update=on_update)
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

    @unittest.skipUnless(schema_check.AVAILABLE, schema_check.SKIP_REASON)
    def test_every_acp_draft_fits_driver_v2(self) -> None:
        for script, cancel in (([("chunk", "hi"), ("end", "end_turn")], False),
                               ([("chunk", "x"), ("end", "end_turn")], True),
                               ([("error", -32603, "boom")], False)):
            _out, drafts, _ended = self.run_turn(script, cancel_on_update=cancel)
            for draft in drafts.drafts:
                self.assertTrue(schema_check.fits("driver.v2.json#/$defs/event_draft", draft), draft)


if __name__ == "__main__":
    unittest.main()
