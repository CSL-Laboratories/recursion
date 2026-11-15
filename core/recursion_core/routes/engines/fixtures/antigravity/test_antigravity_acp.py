# Written by Claude Opus 5.5 (claude-opus-5-5), Claude Code builder lane ENGINE-E5, for Lucas.
"""The shared ACP client (``acp.py`` beside this file) against the fake agent. Engine-neutral cases.

Run: PYTHONPATH=core <python> -m unittest recursion_core.routes.engines.fixtures.antigravity.test_antigravity_acp
"""

from __future__ import annotations

import hashlib
import json
import threading
import unittest
from pathlib import Path

from recursion_core.routes import contract as c
from recursion_core.routes.engines.fixtures.antigravity import acp
from recursion_core.routes.engines.fixtures.antigravity.fake_acp_agent import (
    OPTIONS_ALL, OPTIONS_ALWAYS_ONLY, TOOL_CALL, FakeAcpAgent,
)

HERE = Path(__file__).resolve().parent
CWD = "/var/tmp/workspace"
FAR = 1e12   # a turn deadline far beyond anything a case reaches


def session(agent: FakeAcpAgent, **kwargs) -> acp.AcpSession:
    return acp.AcpSession(agent, agent.clock, **kwargs)


def opened(agent: FakeAcpAgent, method: str = "gemini-api-key", **kwargs) -> acp.AcpSession:
    s = session(agent, **kwargs)
    s.open(method, CWD, FAR)
    return s


def sent(agent: FakeAcpAgent, method: str) -> list:
    return [m for m in agent.received if m.get("method") == method]


class PinTest(unittest.TestCase):
    """Every name the client uses is the pinned schema's."""

    def test_the_kept_method_list_is_the_pinned_file(self) -> None:
        data = (HERE / "acp_meta_v1.json").read_bytes()
        self.assertEqual(hashlib.sha256(data).hexdigest(), acp.SCHEMA_PIN["meta_sha256"])

    def test_every_method_sent_or_answered_is_in_the_pinned_list(self) -> None:
        meta = json.loads((HERE / "acp_meta_v1.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["version"], acp.PROTOCOL_VERSION)
        for method in acp.SENT_METHODS:
            self.assertIn(method, meta["agentMethods"].values(), method)
        for method in acp.ANSWERED_METHODS:
            self.assertIn(method, meta["clientMethods"].values(), method)

    def test_enums_and_codes_are_the_pinned_schemas(self) -> None:
        excerpt = json.loads((HERE / "acp_schema_excerpt.json").read_text(encoding="utf-8"))
        self.assertEqual(excerpt["schema_sha256"], acp.SCHEMA_PIN["schema_sha256"])
        self.assertEqual(list(acp.STOP_REASONS), excerpt["StopReason"])
        self.assertEqual(list(acp.OPTION_KINDS), excerpt["PermissionOptionKind"])
        self.assertEqual(excerpt["ErrorCode"]["Method not found"], acp.METHOD_NOT_FOUND)
        self.assertEqual(excerpt["ErrorCode"]["Authentication required"], acp.AUTH_REQUIRED)
        self.assertEqual(sorted(excerpt["RequestPermissionOutcome"]), ["cancelled", "selected"])


class OpenTest(unittest.TestCase):
    def test_initialize_offers_no_file_or_terminal_capability(self) -> None:
        agent = FakeAcpAgent()
        opened(agent)
        init = sent(agent, "initialize")
        self.assertEqual(len(init), 1)
        self.assertEqual(init[0]["params"], {"protocolVersion": 1, "clientCapabilities": {
            "fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False}})

    def test_authenticates_with_exactly_the_named_method_then_opens_a_session_in_the_workspace(self) -> None:
        agent = FakeAcpAgent()
        s = opened(agent, "oauth-business")
        self.assertEqual(agent.authenticated, ["oauth-business"])
        self.assertEqual([m["method"] for m in agent.received], ["initialize", "authenticate", "session/new"])
        self.assertEqual(sent(agent, "session/new")[0]["params"], {"cwd": CWD, "mcpServers": []})
        self.assertEqual(s.connection.session_id, "fake-session-1")

    def test_a_method_the_agent_did_not_advertise_is_refused_before_authenticate_is_written(self) -> None:
        agent = FakeAcpAgent(methods=("gemini-api-key",))
        s = session(agent)
        with self.assertRaises(acp.AcpRefused) as ctx:
            s.open("agent-platform", CWD, FAR)
        self.assertEqual(ctx.exception.code, c.LAUNCH_REFUSED)
        self.assertEqual(ctx.exception.detail, "acp_method_not_advertised:agent-platform")
        self.assertEqual(sent(agent, "authenticate"), [])
        self.assertEqual(sent(agent, "session/new"), [])

    def test_a_failed_authenticate_never_tries_another_method(self) -> None:
        agent = FakeAcpAgent(accept_methods=("oauth-personal",))   # the agent would take another method
        s = session(agent)
        with self.assertRaises(acp.AcpError) as ctx:
            s.open("gemini-api-key", CWD, FAR)
        self.assertEqual(ctx.exception.code, c.AUTH_FAILED)
        self.assertEqual(agent.authenticated, ["gemini-api-key"])
        self.assertEqual(sent(agent, "session/new"), [])
        with self.assertRaises(acp.AcpStateError):        # the connection keeps its one method
            s.connection.authenticate("oauth-personal")

    def test_another_protocol_version_is_a_protocol_error(self) -> None:
        agent = FakeAcpAgent(protocol_version=2)
        with self.assertRaises(acp.AcpProtocolError) as ctx:
            session(agent).open("gemini-api-key", CWD, FAR)
        self.assertEqual(ctx.exception.code, c.PROTOCOL_ERROR)

    def test_load_is_refused_before_a_byte_when_the_agent_does_not_advertise_it(self) -> None:
        agent = FakeAcpAgent(load_session=False)
        with self.assertRaises(acp.AcpUnsupported):
            session(agent).open("gemini-api-key", CWD, FAR, load_session_id="old-1")
        self.assertEqual(sent(agent, "session/load"), [])
        self.assertEqual(sent(agent, "session/new"), [])

    def test_load_when_advertised_files_the_replay_under_no_turn(self) -> None:
        replay = [{"sessionUpdate": "user_message_chunk", "content": {"type": "text", "text": "earlier"}}]
        agent = FakeAcpAgent(load_session=True, replay=replay)
        s = session(agent)
        self.assertEqual(s.open("gemini-api-key", CWD, FAR, load_session_id="old-1"), "old-1")
        self.assertEqual(sent(agent, "session/load")[0]["params"], {"sessionId": "old-1", "cwd": CWD, "mcpServers": []})
        self.assertEqual(s.connection.stray_updates, 1)

    def test_a_relative_cwd_is_refused(self) -> None:
        agent = FakeAcpAgent()
        s = session(agent)
        s.connection.initialize()
        with self.assertRaises(ValueError):
            s.connection.new_session("relative/path")

    def test_the_agent_dying_while_opening_is_engine_exited(self) -> None:
        agent = FakeAcpAgent()
        agent.close_stdin()   # nothing is answered after this
        with self.assertRaises((acp.AcpError, BrokenPipeError)):
            session(agent).open("gemini-api-key", CWD, FAR)


class TurnTest(unittest.TestCase):
    def test_a_turn_streams_its_updates_and_ends_with_its_text(self) -> None:
        agent = FakeAcpAgent([[("chunk", "hel"), ("chunk", "lo"), ("end", "end_turn")]])
        updates, lines = [], []
        s = opened(agent, on_update=updates.append, on_line=lines.append)
        out = s.prompt("say hello", FAR)
        self.assertIsNone(out.code)
        self.assertEqual(out.stop_reason, "end_turn")
        self.assertEqual(out.text, "hello")
        self.assertEqual(len(updates), 2)
        self.assertEqual(sent(agent, "session/prompt")[0]["params"],
                         {"sessionId": "fake-session-1", "prompt": [{"type": "text", "text": "say hello"}]})
        self.assertTrue(all(isinstance(line, str) for line in lines))
        self.assertGreaterEqual(len(lines), 6)   # three answers while opening, two updates, the prompt's answer

    def test_two_turns_on_one_session(self) -> None:
        agent = FakeAcpAgent([[("chunk", "one"), ("end", "end_turn")], [("chunk", "two"), ("end", "end_turn")]])
        s = opened(agent)
        self.assertEqual(s.prompt("first", FAR).text, "one")
        self.assertEqual(s.prompt("second", FAR).text, "two")

    def test_a_second_prompt_while_one_is_unanswered_is_refused_before_writing(self) -> None:
        agent = FakeAcpAgent([[("wait_cancel",), ("end", "cancelled")]])
        s = opened(agent)
        s.connection.prompt("first", FAR)
        with self.assertRaises(acp.AcpStateError):
            s.connection.prompt("second", FAR)

    def test_typed_stop_reasons_other_than_end_turn_are_not_a_success(self) -> None:
        for reason in ("max_tokens", "max_turn_requests", "refusal"):
            with self.subTest(reason=reason):
                agent = FakeAcpAgent([[("chunk", "partial"), ("end", reason)]])
                out = opened(agent).prompt("x", FAR)
                self.assertEqual(out.code, c.ENGINE_ERROR)
                self.assertEqual(out.stop_reason, reason)
                self.assertEqual(out.text, "")

    def test_an_unknown_stop_reason_is_a_protocol_error(self) -> None:
        agent = FakeAcpAgent([[("end", "finished")]])
        with self.assertRaises(acp.AcpProtocolError):
            opened(agent).prompt("x", FAR)

    def test_an_update_of_another_session_is_stray_and_reaches_no_one(self) -> None:
        other = json.dumps({"jsonrpc": "2.0", "method": "session/update", "params": {
            "sessionId": "another-session", "update": {
                "sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "not ours"}}}})
        agent = FakeAcpAgent([[("raw", other), ("chunk", "ours"), ("end", "end_turn")]])
        updates = []
        s = opened(agent, on_update=updates.append)
        out = s.prompt("x", FAR)
        self.assertEqual(out.text, "ours")
        self.assertEqual(len(updates), 1)
        self.assertEqual(s.connection.stray_updates, 1)

    def test_updates_outside_a_turn_reach_no_one(self) -> None:
        replay = [{"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "earlier answer"}}]
        agent = FakeAcpAgent([[("chunk", "now"), ("end", "end_turn")]], load_session=True, replay=replay)
        updates = []
        s = session(agent, on_update=updates.append)
        s.open("gemini-api-key", CWD, FAR, load_session_id="old-1")
        self.assertEqual(updates, [])
        self.assertEqual(s.prompt("x", FAR).text, "now")

    def test_a_response_to_an_id_never_sent_is_counted_and_dropped(self) -> None:
        agent = FakeAcpAgent([[("stale_response",), ("chunk", "ok"), ("end", "end_turn")]])
        s = opened(agent)
        out = s.prompt("x", FAR)
        self.assertIsNone(out.code)
        self.assertEqual(out.text, "ok")
        self.assertEqual(s.connection.stray_responses, 1)

    def test_a_line_that_is_not_json_rpc_is_a_protocol_error(self) -> None:
        for raw in ("not json", "[1, 2]", '{"id": 1, "result": {}}'):
            with self.subTest(raw=raw):
                agent = FakeAcpAgent([[("raw", raw), ("end", "end_turn")]])
                with self.assertRaises(acp.AcpProtocolError):
                    opened(agent).prompt("x", FAR)

    def test_file_and_terminal_requests_are_refused_and_nothing_is_read(self) -> None:
        agent = FakeAcpAgent([[("fs_read", "/etc/hostname"), ("chunk", "ok"), ("end", "end_turn")]])
        out = opened(agent).prompt("x", FAR)
        self.assertIsNone(out.code)
        self.assertEqual(len(agent.refused), 1)
        self.assertEqual(agent.refused[0]["error"]["code"], acp.METHOD_NOT_FOUND)

    def test_the_agent_dying_mid_turn_is_engine_exited(self) -> None:
        agent = FakeAcpAgent([[("chunk", "half"), ("die",)]])
        out = opened(agent).prompt("x", FAR)
        self.assertEqual(out.code, c.ENGINE_EXITED)
        self.assertEqual(out.text, "")


class CancelTest(unittest.TestCase):
    def test_the_cancel_race_ends_cancelled_and_never_shows_the_late_result_as_success(self) -> None:
        # The agent has queued end_turn before it reads the cancel: its answer races the cancel and wins.
        agent = FakeAcpAgent([[("chunk", "the answer"), ("end", "end_turn")]])
        s = None

        def on_update(_params):
            s.cancel()

        s = opened(agent, on_update=on_update)
        out = s.prompt("x", FAR)
        self.assertEqual(agent.cancels, 1)
        self.assertEqual(out.code, acp.CANCELLED)
        self.assertTrue(out.cancel_requested)
        self.assertFalse(out.cancel_confirmed)
        self.assertEqual(out.late_stop_reason, "end_turn")
        self.assertEqual(out.text, "")

    def test_a_cancel_the_agent_honours_is_confirmed(self) -> None:
        agent = FakeAcpAgent([[("chunk", "working"), ("wait_cancel",), ("end", "cancelled")]])
        s = None

        def on_update(_params):
            s.cancel()

        s = opened(agent, on_update=on_update)
        out = s.prompt("x", FAR)
        self.assertEqual(out.code, acp.CANCELLED)
        self.assertTrue(out.cancel_confirmed)
        self.assertIsNone(out.late_stop_reason)
        self.assertEqual(sent(agent, "session/cancel")[0]["params"], {"sessionId": "fake-session-1"})
        self.assertNotIn("id", sent(agent, "session/cancel")[0])   # a notification

    def test_cancelling_twice_sends_one_notification(self) -> None:
        agent = FakeAcpAgent([[("chunk", "working"), ("wait_cancel",), ("end", "cancelled")]])
        s = None

        def on_update(_params):
            s.cancel()
            s.cancel()

        s = opened(agent, on_update=on_update)
        s.prompt("x", FAR)
        self.assertEqual(len(sent(agent, "session/cancel")), 1)

    def test_an_error_after_a_cancel_is_still_cancelled_not_a_failure_of_another_kind(self) -> None:
        agent = FakeAcpAgent([[("chunk", "x"), ("wait_cancel",), ("error", -32603, "internal")]])
        s = None

        def on_update(_params):
            s.cancel()

        s = opened(agent, on_update=on_update)
        out = s.prompt("x", FAR)
        self.assertEqual(out.code, acp.CANCELLED)
        self.assertFalse(out.cancel_confirmed)

    def test_no_turn_in_progress_means_no_cancel_is_written(self) -> None:
        agent = FakeAcpAgent()
        s = opened(agent)
        s.cancel()
        self.assertEqual(sent(agent, "session/cancel"), [])

    def test_cancel_from_another_thread_while_the_turn_is_being_pumped(self) -> None:
        agent = FakeAcpAgent([[("chunk", "working"), ("wait_cancel",), ("end", "cancelled")]], blocking=True)
        read_chunk = threading.Event()
        s = opened(agent, on_update=lambda _p: read_chunk.set())
        result: list = []
        worker = threading.Thread(target=lambda: result.append(s.prompt("x", FAR)), daemon=True)
        worker.start()
        self.assertTrue(read_chunk.wait(10), "the turn never streamed its update")
        s.cancel()
        worker.join(10)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result[0].code, acp.CANCELLED)
        self.assertTrue(result[0].cancel_confirmed)


class PermissionTest(unittest.TestCase):
    def test_an_ask_with_no_answer_is_denied_at_its_deadline(self) -> None:
        agent = FakeAcpAgent([[("ask", TOOL_CALL, OPTIONS_ALL), ("chunk", "after"), ("end", "end_turn")]])
        asks = []
        s = opened(agent, permission_timeout_s=30.0, on_ask=asks.append)
        start = agent.clock()
        out = s.prompt("x", FAR)
        self.assertIsNone(out.code)
        self.assertEqual(len(asks), 1)
        ask = s.connection.ask(asks[0].token)
        self.assertEqual((ask.answer, ask.answered_by, ask.option_id), ("deny", "deadline", "reject"))
        self.assertEqual(agent.permission_answers["perm-1"], {"outcome": {"outcome": "selected", "optionId": "reject"}})
        self.assertGreaterEqual(agent.clock() - start, 30.0)
        self.assertFalse(s.answer_permission(ask.token, acp.APPROVE))   # too late: the deadline answered it

    def test_nothing_is_denied_before_its_deadline(self) -> None:
        agent = FakeAcpAgent([[("ask", TOOL_CALL, OPTIONS_ALL), ("end", "end_turn")]])
        s = opened(agent, permission_timeout_s=30.0)
        conn = s.connection
        rpc_id, line = conn.prompt("x", FAR)
        agent.send(line)
        fed = conn.feed(agent.read_line(1.0))
        ask = fed.asks[0]
        self.assertEqual(conn.expire(ask.deadline_at - 0.001), [])
        self.assertIsNone(conn.ask(ask.token).answer)
        self.assertEqual(len(conn.expire(ask.deadline_at)), 1)

    def test_two_answers_the_first_wins(self) -> None:
        agent = FakeAcpAgent([[("ask", TOOL_CALL, OPTIONS_ALL), ("end", "end_turn")]])
        results = []
        s = None

        def on_ask(ask):
            results.append(s.answer_permission(ask.token, acp.APPROVE))
            results.append(s.answer_permission(ask.token, acp.DENY))

        s = opened(agent, permission_timeout_s=30.0, on_ask=on_ask)
        s.prompt("x", FAR)
        self.assertEqual(results, [True, False])
        self.assertEqual(agent.permission_answers["perm-1"],
                         {"outcome": {"outcome": "selected", "optionId": "proceed_once"}})
        self.assertEqual(s.connection.late_answers, 1)
        self.assertEqual(len([m for m in agent.received if m.get("id") == "perm-1"]), 1)

    def test_approve_never_selects_allow_always_and_deny_never_reject_always(self) -> None:
        for decision in (acp.APPROVE, acp.DENY):
            with self.subTest(decision=decision):
                agent = FakeAcpAgent([[("ask", TOOL_CALL, OPTIONS_ALWAYS_ONLY), ("end", "end_turn")]])
                s = None

                def on_ask(ask, decision=decision):
                    s.answer_permission(ask.token, decision)

                s = opened(agent, on_ask=on_ask)
                s.prompt("x", FAR)
                self.assertEqual(agent.permission_answers["perm-1"], {"outcome": {"outcome": "cancelled"}})

    def test_an_ask_that_arrives_after_a_cancel_is_answered_cancelled_and_never_reaches_the_caller(self) -> None:
        agent = FakeAcpAgent([[("chunk", "x"), ("wait_cancel",), ("ask", TOOL_CALL, OPTIONS_ALL), ("end", "cancelled")]])
        asks = []
        s = None

        def on_update(_params):
            s.cancel()

        s = opened(agent, permission_timeout_s=30.0, on_update=on_update, on_ask=asks.append)
        out = s.prompt("x", FAR)
        self.assertEqual(asks, [])
        self.assertEqual(agent.permission_answers["perm-1"], {"outcome": {"outcome": "cancelled"}})
        self.assertTrue(out.cancel_confirmed)

    def test_a_cancel_answers_every_open_ask_cancelled(self) -> None:
        agent = FakeAcpAgent([[("ask", TOOL_CALL, OPTIONS_ALL), ("end", "cancelled")]])
        s = None

        def on_ask(_ask):
            s.cancel()

        s = opened(agent, permission_timeout_s=30.0, on_ask=on_ask)
        out = s.prompt("x", FAR)
        self.assertEqual(agent.permission_answers["perm-1"], {"outcome": {"outcome": "cancelled"}})
        self.assertEqual(out.code, acp.CANCELLED)
        self.assertTrue(out.cancel_confirmed)

    def test_an_answer_for_an_unknown_ask_raises(self) -> None:
        s = opened(FakeAcpAgent())
        with self.assertRaises(KeyError):
            s.answer_permission("ask-404", acp.APPROVE)
        with self.assertRaises(ValueError):
            s.connection.answer_permission("ask-1", "maybe")

    def test_an_ask_outside_any_turn_runs_nothing(self) -> None:
        conn = acp.AcpConnection()
        conn.agent_capabilities, conn.session_id = {}, "s1"
        fed = conn.feed(json.dumps({"jsonrpc": "2.0", "id": "perm-9", "method": "session/request_permission",
                                    "params": {"sessionId": "s1", "toolCall": TOOL_CALL, "options": OPTIONS_ALL}}))
        self.assertEqual(fed.asks, [])
        self.assertEqual(json.loads(fed.replies[0])["result"], {"outcome": {"outcome": "cancelled"}})


class ErrorTest(unittest.TestCase):
    def outcome(self, code: int, message: str, hooks: acp.EngineHooks = acp.NO_HOOKS) -> acp.TurnOutcome:
        agent = FakeAcpAgent([[("error", code, message)]])
        return opened(agent, hooks=hooks).prompt("x", FAR)

    def test_only_the_engines_hook_makes_a_typed_limit(self) -> None:
        class Hooks(acp.EngineHooks):
            def limit(self, method, error):
                return c.limit_signal("connection", "acp:-32003") if error.code == -32003 else None

        out = self.outcome(-32003, "Usage limit reached", Hooks())
        self.assertEqual(out.code, c.PLAN_LIMIT)
        self.assertEqual(out.limit["source"], "acp:-32003")
        # The same code without the hook is not a plan limit: the client maps no numeric code itself.
        self.assertNotEqual(self.outcome(-32003, "Usage limit reached").code, c.PLAN_LIMIT)

    def test_quota_sounding_text_is_a_short_fault_never_a_plan_limit(self) -> None:
        out = self.outcome(429, "Rate limit exceeded. Try again later.")
        self.assertEqual(out.code, c.SHORT_FAULT)
        self.assertIsNone(out.limit)

    def test_authentication_required_is_auth_failed(self) -> None:
        self.assertEqual(self.outcome(-32000, "Authentication required").code, c.AUTH_FAILED)

    def test_anything_else_is_engine_error_with_the_code_in_the_detail(self) -> None:
        out = self.outcome(-32603, "internal error")
        self.assertEqual(out.code, c.ENGINE_ERROR)
        self.assertIn("-32603", out.detail)
        self.assertEqual(out.error, acp.RpcError(-32603, "internal error"))


class DeadlineTest(unittest.TestCase):
    def test_the_deadline_ends_the_turn_and_a_late_answer_never_reaches_a_later_turn(self) -> None:
        agent = FakeAcpAgent([[("chunk", "slow"), ("wait_cancel",), ("end", "cancelled")],
                              [("chunk", "second"), ("end", "end_turn")]])
        s = opened(agent)
        deadline = agent.clock() + 60.0
        out = s.prompt("x", deadline)
        self.assertEqual(out.code, c.DEADLINE_EXCEEDED)
        self.assertEqual(agent.cancels, 1)                     # the abandoned turn is asked to stop
        with self.assertRaises(acp.AcpStateError):              # its prompt is still unanswered
            s.connection.prompt("too soon", FAR)
        # The abandoned turn's answer arrives; it ends nothing and is shown to nobody.
        fed = s.connection.feed(agent.read_line(1.0))
        self.assertIsNone(fed.ended)
        self.assertEqual(s.prompt("again", FAR).text, "second")


if __name__ == "__main__":
    unittest.main()
