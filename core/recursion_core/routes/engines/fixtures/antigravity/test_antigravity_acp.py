# Written by Claude Opus 5.5 (claude-opus-5-5), Claude Code builder lane ENGINE-E5, for Lucas.
"""The shared ACP client (``acp.py`` beside this file) against the fake agent. Engine-neutral cases.

Run: PYTHONPATH=core <python> -m unittest recursion_core.routes.engines.fixtures.antigravity.test_antigravity_acp
"""

from __future__ import annotations

import hashlib
import json
import os
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
        with self.assertRaises(acp.AcpError) as ctx:
            session(agent).open("gemini-api-key", CWD, FAR)
        self.assertEqual(ctx.exception.code, c.ENGINE_EXITED)


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
        agent = FakeAcpAgent([[("chunk", "x"), ("wait_cancel",), ("ask", TOOL_CALL, OPTIONS_ALL),
                               ("end", "cancelled")]])
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


def permission_line(conn: acp.AcpConnection, rpc_id: str = "perm-1") -> str:
    return json.dumps({"jsonrpc": "2.0", "id": rpc_id, "method": "session/request_permission",
                       "params": {"sessionId": conn.session_id, "toolCall": TOOL_CALL, "options": OPTIONS_ALL}})


class ReviewRoundOneTest(unittest.TestCase):
    """Regressions for the two reads of #396 at e8c3a695 (Codex R1-R4, DeepSeek F1-F11 and the guards both
    reads found untested). Each fails on that head."""

    # -- R1: an answer at the deadline is the deadline's deny, whether or not expire has run ---------------
    def test_r1_an_approval_at_the_deadline_is_denied_without_waiting_for_expire(self) -> None:
        agent = FakeAcpAgent()
        conn = opened(agent, permission_timeout_s=30).connection
        conn.prompt("x", FAR)
        ask = conn.feed(permission_line(conn)).asks[0]
        agent.clock.advance(30)
        answer = json.loads(conn.answer_permission(ask.token, acp.APPROVE))
        self.assertEqual(answer["result"]["outcome"], {"outcome": "selected", "optionId": "reject"})
        self.assertEqual((ask.answer, ask.answered_by), (acp.DENY, "deadline"))
        self.assertIsNone(conn.answer_permission(ask.token, acp.APPROVE))   # and only once

    def test_r1_the_session_reports_a_late_approval_as_not_taken(self) -> None:
        agent = FakeAcpAgent([[("ask", TOOL_CALL, OPTIONS_ALL), ("end", "end_turn")]])
        results = []
        s = None

        def on_ask(ask):
            agent.clock.advance(31)
            results.append(s.answer_permission(ask.token, acp.APPROVE))

        s = opened(agent, permission_timeout_s=30, on_ask=on_ask)
        s.prompt("x", FAR)
        self.assertEqual(results, [False])
        self.assertEqual(agent.permission_answers["perm-1"], {"outcome": {"outcome": "selected", "optionId": "reject"}})

    # -- R2: a state change and its write keep their order on the wire ----------------------------------
    def test_r2_a_cancel_cannot_overtake_its_prompt_on_the_wire(self) -> None:
        reached, release = threading.Event(), threading.Event()

        class PausedWriter(acp.AcpSession):
            def _write(self, lines):
                if lines and json.loads(lines[0]).get("method") == "session/prompt":
                    reached.set()
                    if not release.wait(10):
                        raise AssertionError("schedule watchdog")
                return super()._write(lines)

        agent = FakeAcpAgent([[("wait_cancel",), ("end", "cancelled")]], blocking=True, watchdog_s=10)
        s = PausedWriter(agent, agent.clock)
        s.open("gemini-api-key", CWD, FAR)
        outcomes, errors = [], []
        worker = threading.Thread(target=lambda: outcomes.append(s.prompt("x", FAR)), daemon=True)
        worker.start()
        self.assertTrue(reached.wait(10), "the prompt never reached its write")
        canceller = threading.Thread(target=lambda: s.cancel(), daemon=True)
        canceller.start()
        canceller.join(0.2)   # a correct client keeps the cancel waiting behind the unsent prompt
        release.set()
        worker.join(10)
        canceller.join(10)
        self.assertFalse(worker.is_alive() or canceller.is_alive())
        self.assertEqual(errors, [])
        order = [m["method"] for m in agent.received if m.get("method") in ("session/prompt", "session/cancel")]
        self.assertEqual(order, ["session/prompt", "session/cancel"])
        self.assertTrue(outcomes[0].cancel_confirmed)

    def test_r2_an_old_turns_cancel_is_on_the_wire_before_the_next_prompt(self) -> None:
        reached, release = threading.Event(), threading.Event()

        class PausedCancel(acp.AcpSession):
            def _write(self, lines):
                if lines and json.loads(lines[0]).get("method") == "session/cancel":
                    reached.set()
                    if not release.wait(10):
                        raise AssertionError("schedule watchdog")
                return super()._write(lines)

        agent = FakeAcpAgent([[("chunk", "first"), ("end", "end_turn")], [("chunk", "second"), ("end", "end_turn")]],
                             blocking=True, watchdog_s=10)
        threads = []
        s = None

        def on_update(params):
            if params["update"]["content"]["text"] == "first":
                t = threading.Thread(target=s.cancel, daemon=True)
                threads.append(t)
                t.start()
                if not reached.wait(10):
                    raise AssertionError("the cancel never reached its write")

        s = PausedCancel(agent, agent.clock, on_update=on_update)
        s.open("gemini-api-key", CWD, FAR)
        outcomes = []
        worker = threading.Thread(target=lambda: (outcomes.append(s.prompt("first", FAR)),
                                                  outcomes.append(s.prompt("second", FAR))), daemon=True)
        worker.start()
        self.assertTrue(reached.wait(10))
        worker.join(0.2)      # turn 2's prompt waits behind turn 1's unsent cancel
        release.set()
        worker.join(10)
        for t in threads:
            t.join(10)
        self.assertFalse(worker.is_alive())
        order = [m["method"] for m in agent.received if m.get("method") in ("session/prompt", "session/cancel")]
        self.assertEqual(order, ["session/prompt", "session/cancel", "session/prompt"])
        self.assertEqual([o.code for o in outcomes], [acp.CANCELLED, None])
        self.assertFalse(outcomes[1].cancel_requested)

    # -- R3: the deadline before a write that starts work, and after each read -------------------------
    def test_r3_an_expired_prompt_is_never_sent(self) -> None:
        agent = FakeAcpAgent()
        s = opened(agent)
        with self.assertRaises(acp.AcpError) as ctx:
            s.prompt("x", agent.clock() - 1)
        self.assertEqual(ctx.exception.code, c.DEADLINE_EXCEEDED)
        self.assertEqual(sent(agent, "session/prompt"), [])
        self.assertIsNone(s.connection.turn_in_progress)

    def test_r3_an_answer_read_after_the_deadline_cannot_succeed(self) -> None:
        class LateRead(FakeAcpAgent):
            def read_line(self, timeout_s):
                line = super().read_line(timeout_s)
                if line and "stopReason" in line:
                    self.clock.advance(timeout_s + 1)
                return line

        agent = LateRead([[("chunk", "late success"), ("end", "end_turn")]])
        out = opened(agent).prompt("x", agent.clock() + 10)
        self.assertEqual(out.code, c.DEADLINE_EXCEEDED)
        self.assertEqual(out.text, "")

    def test_r3_an_open_whose_deadline_passed_writes_nothing(self) -> None:
        agent = FakeAcpAgent()
        with self.assertRaises(acp.AcpError) as ctx:
            session(agent).open("gemini-api-key", CWD, agent.clock())
        self.assertEqual(ctx.exception.code, c.DEADLINE_EXCEEDED)
        self.assertEqual(agent.received, [])

    # -- R4 / F1: the engine's hook is called with no lock held -------------------------------------------
    def test_r4_the_hook_runs_with_the_connections_lock_free(self) -> None:
        free = []
        s = None

        class Hooks(acp.EngineHooks):
            def limit(self, method, error):
                got = s.connection._lock.acquire(blocking=False)
                free.append(got)
                if got:
                    s.connection._lock.release()
                return None

        s = opened(FakeAcpAgent([[("error", -32603, "synthetic failure")]]), hooks=Hooks())
        s.prompt("x", FAR)
        self.assertEqual(free, [True])

    def test_f1_a_hook_that_calls_back_into_the_connection_finishes(self) -> None:
        seen = {}
        s = None

        class Hooks(acp.EngineHooks):
            def limit(self, method, error):
                seen["deadline"] = s.connection.next_deadline()
                seen["turn"] = s.connection.turn_in_progress
                return c.limit_signal("connection", "acp:hook")

        s = opened(FakeAcpAgent([[("error", -32003, "limit")]]), hooks=Hooks())
        result = []
        worker = threading.Thread(target=lambda: result.append(s.prompt("x", FAR)), daemon=True)
        worker.start()
        worker.join(10)
        self.assertFalse(worker.is_alive(), "the hook deadlocked")
        self.assertEqual(result[0].code, c.PLAN_LIMIT)
        self.assertEqual(seen, {"deadline": None, "turn": None})

    def test_a_hook_that_raises_counts_as_no_limit(self) -> None:
        class Hooks(acp.EngineHooks):
            def limit(self, method, error):
                raise RuntimeError("engine hook bug")

        s = opened(FakeAcpAgent([[("error", -32603, "boom")]]), hooks=Hooks())
        out = s.prompt("x", FAR)
        self.assertEqual(out.code, c.ENGINE_ERROR)
        self.assertEqual(s.connection.hook_failures, 1)

    # -- F2 and F3: a line too deep to read, or not UTF-8 ---------------------------------------------------
    def test_f2_a_line_nested_too_deeply_is_a_protocol_error(self) -> None:
        conn = acp.AcpConnection()
        with self.assertRaises(acp.AcpProtocolError):
            conn.feed("[" * 100_000 + "]" * 100_000)

    def test_f3_a_reply_carrying_a_lone_surrogate_is_written_as_ascii(self) -> None:
        options = [{"optionId": "once\ud800", "name": "x", "kind": "allow_once"}]
        agent = FakeAcpAgent([[("ask", TOOL_CALL, options), ("end", "end_turn")]])
        s = None
        s = opened(agent, on_ask=lambda ask: s.answer_permission(ask.token, acp.APPROVE))
        self.assertIsNone(s.prompt("x", FAR).code)
        raw = [m for m in agent.received if m.get("id") == "perm-1"]
        self.assertEqual(raw[0]["result"]["outcome"]["optionId"], "once\ud800")
        self.assertTrue(all(ord(ch) < 128 for ch in acp._line(raw[0])))

    # -- F4: the agent going away noticed on a write ----------------------------------------------------------
    def test_f4_a_prompt_to_a_gone_agent_ends_engine_exited(self) -> None:
        agent = FakeAcpAgent()
        s = opened(agent)
        agent.stdin_closed = True     # its input is gone; a write now fails as a closed pipe does
        out = s.prompt("x", FAR)
        self.assertEqual(out.code, c.ENGINE_EXITED)
        self.assertTrue(s.input_lost)
        s.cancel()                    # never raises for a gone agent
        self.assertIsNone(s.connection.turn_in_progress)

    def test_f4_an_open_to_a_gone_agent_raises_engine_exited(self) -> None:
        for stage in ("initialize", "authenticate", "session/new"):
            with self.subTest(stage=stage):
                agent = FakeAcpAgent()
                original = agent.send

                def send(line, stage=stage, agent=agent, original=original):
                    if json.loads(line).get("method") == stage:
                        agent.stdin_closed = True
                    original(line)

                agent.send = send
                with self.assertRaises(acp.AcpError) as ctx:
                    session(agent).open("gemini-api-key", CWD, FAR)
                self.assertEqual(ctx.exception.code, c.ENGINE_EXITED)

    def test_f4_an_answer_to_a_gone_agent_is_not_taken(self) -> None:
        agent = FakeAcpAgent([[("ask", TOOL_CALL, OPTIONS_ALL), ("end", "end_turn")]])
        results = []
        s = None

        def on_ask(ask):
            agent.stdin_closed = True
            results.append(s.answer_permission(ask.token, acp.APPROVE))

        s = opened(agent, on_ask=on_ask)
        s.prompt("x", 10 ** 7 + agent.clock())
        self.assertEqual(results, [False])

    # -- F7: a refused load can be tried again ------------------------------------------------------------
    def test_f7_a_refused_load_leaves_the_connection_able_to_load(self) -> None:
        conn = acp.AcpConnection()
        conn.agent_capabilities = {"loadSession": True}
        rpc_id, _ = conn.load_session("one", CWD)
        conn.feed(json.dumps({"jsonrpc": "2.0", "id": rpc_id, "error": {"code": -32603, "message": "no such session"}}))
        rpc_id, _ = conn.load_session("two", CWD)    # refused by the agent, not by stale client state
        conn.feed(json.dumps({"jsonrpc": "2.0", "id": rpc_id, "result": {}}))
        self.assertEqual(conn.session_id, "two")

    # -- F9: every refusal carries a route contract code ------------------------------------------------------
    def test_f9_an_unadvertised_load_carries_a_contract_code(self) -> None:
        with self.assertRaises(acp.AcpUnsupported) as ctx:
            session(FakeAcpAgent(load_session=False)).open("gemini-api-key", CWD, FAR, load_session_id="old")
        self.assertIn(ctx.exception.code, c.CODES)
        self.assertEqual(c.result("r1", ctx.exception.code)["code"], c.LAUNCH_REFUSED)
        self.assertIsInstance(ctx.exception, acp.AcpRefused)

    # -- F10: a long-lived worker keeps nothing of finished turns -----------------------------------------
    def test_f10_a_thousand_turns_leave_no_requests_and_only_the_last_turns_asks(self) -> None:
        agent = FakeAcpAgent([[("ask", TOOL_CALL, OPTIONS_ALL), ("end", "end_turn")]] * 1000)
        s = None
        s = opened(agent, on_ask=lambda ask: s.answer_permission(ask.token, acp.APPROVE))
        for _ in range(1000):
            s.prompt("x", FAR)
        self.assertEqual(len(s.connection._pending), 0)
        self.assertLessEqual(len(s.connection._asks), 1)
        s.prompt("one more", FAR)                       # the last turn's ask is dropped by the next prompt
        self.assertEqual(s.connection.late_answers, 0)
        self.assertIsNone(s.connection.answer_permission("ask-1", acp.APPROVE))   # a dropped ask is late
        self.assertEqual(s.connection.late_answers, 1)

    # -- F11: the docstring names files that exist -------------------------------------------------------------
    def test_f11_every_json_file_the_docstring_names_is_here(self) -> None:
        import re
        names = set(re.findall(r"``([a-z0-9_]+\.json)``", acp.__doc__))
        self.assertEqual(names, {"acp_meta_v1.json", "acp_schema_excerpt.json"})
        for name in names:
            self.assertTrue((HERE / name).is_file(), name)

    # -- guards both reads found untested -----------------------------------------------------------------------
    def test_after_the_output_ends_nothing_can_be_started(self) -> None:          # M09
        conn = acp.AcpConnection()
        conn.end_of_output()
        with self.assertRaises(acp.AcpStateError):
            conn.initialize()

    def test_the_permission_timeout_must_be_a_positive_finite_number(self) -> None:   # M10, DeepSeek acp:249
        for value in (0, -1, float("nan"), float("inf"), True, "30"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                acp.AcpConnection(permission_timeout_s=value)
        acp.AcpConnection(permission_timeout_s=0.5)

    def test_a_cwd_with_a_nul_is_refused(self) -> None:                             # DeepSeek acp:465
        conn = acp.AcpConnection()
        conn.agent_capabilities = {}
        with self.assertRaises(ValueError):
            conn.new_session("/var/tmp/x\x00y")

    def test_a_second_session_on_one_connection_is_refused(self) -> None:          # M15
        s = opened(FakeAcpAgent())
        with self.assertRaises(acp.AcpStateError):
            s.connection.new_session(CWD)

    def test_an_error_object_with_a_bad_code_or_message_is_a_protocol_error(self) -> None:   # M17
        for error in ({"code": "not-an-integer", "message": "x"}, {"code": 1, "message": 5}, "boom", {"code": True,
                                                                                                    "message": "x"}):
            with self.subTest(error=error):
                s = opened(FakeAcpAgent())
                rpc_id, _ = s.connection.prompt("x", FAR)
                with self.assertRaises(acp.AcpProtocolError):
                    s.connection.feed(json.dumps({"jsonrpc": "2.0", "id": rpc_id, "error": error}))

    def test_a_second_load_while_one_is_in_flight_is_refused(self) -> None:        # M18
        conn = acp.AcpConnection()
        conn.agent_capabilities = {"loadSession": True}
        conn.load_session("one", CWD)
        with self.assertRaises(acp.AcpStateError):
            conn.load_session("two", CWD)


class ObservedLock:
    """A real lock that sets ``entered`` as a thread tries to take it, so a test can hold it and know who waits.
    Its locking is an ordinary lock's (Codex's round-2 schedule)."""

    def __init__(self, reentrant: bool = False) -> None:
        self.lock = threading.RLock() if reentrant else threading.Lock()
        self.entered = threading.Event()

    def __enter__(self):
        self.entered.set()
        self.lock.acquire()
        return self

    def __exit__(self, *exc) -> bool:
        self.lock.release()
        return False

    def acquire(self, blocking: bool = True) -> bool:
        return self.lock.acquire(blocking)

    def release(self) -> None:
        self.lock.release()


class ReviewRoundTwoTest(unittest.TestCase):
    """Regressions for the two reads of #396 at ca153c32 (Codex C1-C4, DeepSeek MED1 and LOW1-LOW5). The
    C1-C3 and MED1 cases fail on that head; the C4 cases each fail with one rule of this client removed."""

    # -- C1: the answer is decided at its claim, against a clock read after it ---------------------------
    def test_c1_an_answer_whose_claim_waits_past_the_deadline_is_denied(self) -> None:
        agent = FakeAcpAgent()
        conn = opened(agent, permission_timeout_s=30).connection
        conn.prompt("x", FAR)
        ask = conn.feed(permission_line(conn)).asks[0]
        lock = ObservedLock()
        conn._lock = lock
        answers, errors = [], []

        def answer():
            try:
                answers.append(conn.answer_permission(ask.token, acp.APPROVE))
            except BaseException as error:   # reported below
                errors.append(error)

        lock.lock.acquire()
        worker = threading.Thread(target=answer, daemon=True)
        try:
            worker.start()
            self.assertTrue(lock.entered.wait(10))
            agent.clock.advance(30)          # the deadline passes while the answer waits for the lock
        finally:
            lock.lock.release()
            worker.join(10)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(json.loads(answers[0])["result"]["outcome"], {"outcome": "selected", "optionId": "reject"})
        self.assertEqual((ask.answer, ask.answered_by), (acp.DENY, "deadline"))

    def test_c1_a_claimed_ask_is_left_to_its_claimer_by_expire_and_cancel(self) -> None:
        agent = FakeAcpAgent()
        conn = opened(agent, permission_timeout_s=30).connection
        conn.prompt("x", FAR)
        ask = conn.feed(permission_line(conn)).asks[0]
        during = {}

        def clock():
            # Called between the claim and the decision, with no lock held: the deadline passes, and expire
            # and a cancel run now. Neither may answer the claimed ask; it is answered once, by its claimer.
            if "expired" not in during:
                agent.clock.advance(30)
                during["expired"] = conn.expire(agent.clock())
                during["cancel"] = conn.cancel()
            return agent.clock()

        conn._clock = clock
        line = conn.answer_permission(ask.token, acp.APPROVE)
        self.assertEqual(during["expired"], [])
        self.assertEqual([json.loads(x).get("method") for x in during["cancel"]], ["session/cancel"])
        self.assertEqual(json.loads(line)["result"]["outcome"], {"outcome": "selected", "optionId": "reject"})
        self.assertEqual((ask.answer, ask.answered_by), (acp.DENY, "deadline"))
        self.assertIsNone(conn.answer_permission(ask.token, acp.APPROVE))

    def test_c1_a_clock_that_raises_during_a_claim_gives_the_claim_back(self) -> None:
        agent = FakeAcpAgent()
        conn = opened(agent, permission_timeout_s=30).connection
        conn.prompt("x", FAR)
        ask = conn.feed(permission_line(conn)).asks[0]
        calls = []

        def clock():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("synthetic clock failure")
            return agent.clock()

        conn._clock = clock
        with self.assertRaises(RuntimeError):
            conn.answer_permission(ask.token, acp.APPROVE)
        self.assertIsNone(ask.answer)
        line = conn.answer_permission(ask.token, acp.APPROVE)
        self.assertEqual(json.loads(line)["result"]["outcome"], {"outcome": "selected", "optionId": "proceed_once"})

    def test_c1_the_first_claim_wins_and_an_answer_during_it_is_late(self) -> None:
        agent = FakeAcpAgent()
        conn = opened(agent, permission_timeout_s=30).connection
        conn.prompt("x", FAR)
        ask = conn.feed(permission_line(conn)).asks[0]
        seen = {}

        def clock():
            if not seen:                                       # during the first answer's claim
                seen["second"] = "pending"
                seen["second"] = conn.answer_permission(ask.token, acp.DENY)
            return agent.clock()

        conn._clock = clock
        first = conn.answer_permission(ask.token, acp.APPROVE)
        self.assertIsNone(seen["second"])
        self.assertEqual(json.loads(first)["result"]["outcome"], {"outcome": "selected", "optionId": "proceed_once"})
        self.assertEqual((ask.answer, ask.answered_by), (acp.APPROVE, "caller"))
        self.assertEqual(conn.late_answers, 1)

    def test_c1_an_ask_the_turn_ending_answers_during_the_claim_is_not_answered_again(self) -> None:
        for ending in ("the prompt answered", "the output ended"):
            with self.subTest(ending=ending):
                agent = FakeAcpAgent()
                conn = opened(agent, permission_timeout_s=30).connection
                rpc_id, _ = conn.prompt("x", FAR)
                ask = conn.feed(permission_line(conn)).asks[0]
                done = []

                def clock(conn=conn, rpc_id=rpc_id, ending=ending, done=done, agent=agent):
                    if not done:
                        done.append(True)
                        if ending == "the prompt answered":
                            answer = {"jsonrpc": "2.0", "id": rpc_id, "result": {"stopReason": "end_turn"}}
                            conn.feed(json.dumps(answer))
                        else:
                            conn.end_of_output()
                    return agent.clock()

                conn._clock = clock
                self.assertIsNone(conn.answer_permission(ask.token, acp.APPROVE))
                self.assertEqual((ask.answer, ask.answered_by), ("cancelled", "cancel"))

    def test_a_second_answer_to_an_ask_with_no_deadline_is_late(self) -> None:
        conn = opened(FakeAcpAgent()).connection                # no permission timeout: no claim is needed
        conn.prompt("x", FAR)
        ask = conn.feed(permission_line(conn)).asks[0]
        self.assertIsNone(ask.deadline_at)
        line = conn.answer_permission(ask.token, acp.APPROVE)
        self.assertEqual(json.loads(line)["result"]["outcome"], {"outcome": "selected", "optionId": "proceed_once"})
        self.assertIsNone(conn.answer_permission(ask.token, acp.DENY))
        self.assertEqual((ask.answer, conn.late_answers), (acp.APPROVE, 1))

    def test_c1_n42_the_clock_is_called_with_the_connections_lock_free(self) -> None:
        agent = FakeAcpAgent()
        conn = opened(agent, permission_timeout_s=30).connection
        free = []

        def clock():
            got = conn._lock.acquire(blocking=False)
            free.append(got)
            if got:
                conn._lock.release()
            return agent.clock()

        conn._clock = clock
        conn.prompt("x", FAR)
        ask = conn.feed(permission_line(conn)).asks[0]      # the ask's deadline is read from the clock
        conn.feed(json.dumps({"jsonrpc": "2.0", "method": "unrecognized", "params": {}}))
        conn.answer_permission(ask.token, acp.APPROVE)
        self.assertGreaterEqual(len(free), 3)
        self.assertTrue(all(free), free)

    # -- C2: the deadline is checked when the step holds the ordering lock ----------------------------------
    def test_c2_a_prompt_whose_deadline_passes_behind_an_old_cancel_is_never_sent(self) -> None:
        reached, release, second_waits = threading.Event(), threading.Event(), threading.Event()

        class PausedCancel(acp.AcpSession):
            second = False

            def _write(self, lines):
                if lines and json.loads(lines[0]).get("method") == "session/cancel" and not reached.is_set():
                    reached.set()
                    if not release.wait(10):
                        raise AssertionError("schedule watchdog")
                return super()._write(lines)

            def _step(self, make):
                if self.second:
                    second_waits.set()
                return super()._step(make)

        agent = FakeAcpAgent([[("chunk", "first"), ("end", "end_turn")], [("end", "end_turn")]],
                             blocking=True, watchdog_s=10)
        threads, outcomes, errors = [], [], []
        s = None

        def on_update(_params):
            t = threading.Thread(target=s.cancel, daemon=True)
            threads.append(t)
            t.start()
            if not reached.wait(10):
                raise AssertionError("the cancel never reached its write")

        s = PausedCancel(agent, agent.clock, on_update=on_update)
        s.open("gemini-api-key", CWD, FAR)

        def work():
            try:
                outcomes.append(s.prompt("first", FAR))
                s.second = True
                outcomes.append(s.prompt("second", agent.clock() + 10))
            except BaseException as error:   # reported below
                errors.append(error)

        worker = threading.Thread(target=work, daemon=True)
        worker.start()
        try:
            self.assertTrue(second_waits.wait(10))
            agent.clock.advance(11)          # turn 2's deadline passes while its prompt waits behind the cancel
        finally:
            release.set()
            worker.join(10)
            for t in threads:
                t.join(10)
        self.assertFalse(worker.is_alive())
        self.assertEqual([type(e) for e in errors], [acp.AcpError])
        self.assertEqual(errors[0].code, c.DEADLINE_EXCEEDED)
        self.assertEqual(len(sent(agent, "session/prompt")), 1)
        self.assertIsNone(s.connection.turn_in_progress)

    def test_c2_an_open_whose_deadline_passes_while_it_waits_for_the_ordering_lock_sends_nothing(self) -> None:
        agent = FakeAcpAgent()
        s = session(agent)
        lock = ObservedLock(reentrant=True)
        s._order = lock
        errors = []
        deadline = agent.clock() + 10

        def go():
            try:
                s.open("gemini-api-key", CWD, deadline)
            except BaseException as error:   # reported below
                errors.append(error)

        lock.lock.acquire()
        worker = threading.Thread(target=go, daemon=True)
        try:
            worker.start()
            self.assertTrue(lock.entered.wait(10))
            agent.clock.advance(11)
        finally:
            lock.lock.release()
            worker.join(10)
        self.assertFalse(worker.is_alive())
        self.assertEqual(agent.received, [])
        self.assertEqual([e.code for e in errors], [c.DEADLINE_EXCEEDED])

    def test_c2_n07_n08_each_open_step_checks_the_deadline_again(self) -> None:
        for answered, forbidden in ((1, "authenticate"), (2, "session/new"), (2, "session/load")):
            with self.subTest(after=answered, forbidden=forbidden):
                agent = FakeAcpAgent(load_session=True)

                def on_line(line, agent=agent, answered=answered):
                    if json.loads(line).get("id") == answered:
                        agent.clock.advance(10)       # the deadline passes as this step's answer is read

                s = session(agent, on_line=on_line)
                with self.assertRaises(acp.AcpError) as ctx:
                    s.open("gemini-api-key", CWD, agent.clock() + 10,
                           load_session_id="old" if forbidden == "session/load" else None)
                self.assertEqual(ctx.exception.code, c.DEADLINE_EXCEEDED)
                self.assertEqual(sent(agent, forbidden), [])

    # -- C3: a failed reply write ends the pumping turn engine_exited ---------------------------------------
    def test_c3_a_permission_answer_into_a_broken_pipe_ends_the_turn_at_once(self) -> None:
        reader, writer = os.pipe()
        os.close(reader)
        self.addCleanup(os.close, writer)
        agent = FakeAcpAgent([[("ask", TOOL_CALL, OPTIONS_ALL), ("wait_cancel",), ("end", "cancelled")]])
        original = agent.send

        def send(line):
            if json.loads(line).get("id") == "perm-1":
                os.write(writer, line.encode("utf-8"))   # a real EPIPE: the pipe has no reader
            else:
                original(line)

        agent.send = send
        answered = []
        s = None
        s = opened(agent, on_ask=lambda ask: answered.append(s.answer_permission(ask.token, acp.APPROVE)))
        start = agent.clock()
        out = s.prompt("x", start + 10)
        self.assertEqual(answered, [False])
        self.assertEqual(out.code, c.ENGINE_EXITED)
        self.assertEqual(agent.clock(), start)          # at once: no wait for the deadline
        self.assertTrue(s.input_lost and s.connection.closed)

    def test_c3_a_write_that_fails_on_another_thread_ends_the_turn_at_the_next_line(self) -> None:
        asked = threading.Event()
        agent = FakeAcpAgent([[("ask", TOOL_CALL, OPTIONS_ALL), ("end", "end_turn")]], blocking=True, watchdog_s=5)
        tokens, outcomes = [], []
        s = opened(agent, on_ask=lambda ask: (tokens.append(ask.token), asked.set()))
        worker = threading.Thread(target=lambda: outcomes.append(s.prompt("x", FAR)), daemon=True)
        worker.start()
        self.assertTrue(asked.wait(10))
        agent.stdin_closed = True                                          # the agent's input goes away
        self.assertFalse(s.answer_permission(tokens[0], acp.APPROVE))      # this thread's write fails
        with agent._cond:                                                  # the agent still writes a line
            agent._update({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "more"}})
            agent._cond.notify_all()
        worker.join(10)
        self.assertFalse(worker.is_alive())
        self.assertEqual(outcomes[0].code, c.ENGINE_EXITED)

    def test_c3_an_outcome_the_turn_already_had_stays_when_the_input_goes(self) -> None:
        agent = FakeAcpAgent([[("chunk", "slow"), ("wait_cancel",), ("end", "cancelled")]])
        original = agent.send

        def send(line):
            if json.loads(line).get("method") == "session/cancel":
                raise BrokenPipeError("synthetic: the agent's input is gone")
            original(line)

        agent.send = send
        s = opened(agent)
        out = s.prompt("x", agent.clock() + 60)
        self.assertEqual(out.code, c.DEADLINE_EXCEEDED)       # the cancel's failed write changes nothing
        self.assertTrue(s.input_lost)
        with self.assertRaises(acp.AcpError) as ctx:          # the next prompt: the agent is gone
            s.prompt("next", FAR)
        self.assertEqual(ctx.exception.code, c.ENGINE_EXITED)
        self.assertEqual(len(sent(agent, "session/prompt")), 1)

    def test_c3_an_open_whose_write_fails_leaves_the_agent_gone(self) -> None:
        agent = FakeAcpAgent()
        agent.stdin_closed = True                             # the first write of the open fails
        s = session(agent)
        with self.assertRaises(acp.AcpError) as ctx:
            s.open("gemini-api-key", CWD, FAR)
        self.assertEqual(ctx.exception.code, c.ENGINE_EXITED)
        self.assertTrue(s.connection.closed)
        with self.assertRaises(acp.AcpStateError) as ctx:
            s.connection.initialize()
        self.assertEqual(ctx.exception.code, c.ENGINE_EXITED)

    # -- C4: one test for each rule Codex's removals showed untested ----------------------------------------
    def test_n29_a_retired_ask_token_cannot_answer_the_current_ask(self) -> None:
        conn = opened(FakeAcpAgent()).connection
        rpc_id, _ = conn.prompt("first", FAR)
        one = conn.feed(permission_line(conn, "p1")).asks[0]
        conn.feed(json.dumps({"jsonrpc": "2.0", "id": rpc_id, "result": {"stopReason": "end_turn"}}))
        conn.prompt("second", FAR)
        two = conn.feed(permission_line(conn, "p2")).asks[0]
        self.assertNotEqual(one.token, two.token)
        self.assertIsNone(conn.answer_permission(one.token, acp.APPROVE))
        self.assertIsNone(two.answer)

    def test_n40_a_permission_answer_keeps_its_place_before_a_cancel(self) -> None:
        reached, release, errors = threading.Event(), threading.Event(), []

        class PausedAnswer(acp.AcpSession):
            def _write(self, lines):
                if lines and json.loads(lines[0]).get("id") == "perm-1":
                    reached.set()
                    if not release.wait(10):
                        raise AssertionError("schedule watchdog")
                return super()._write(lines)

        agent = FakeAcpAgent([[("ask", TOOL_CALL, OPTIONS_ALL), ("end", "end_turn")]])
        s = PausedAnswer(agent, agent.clock)
        s.open("gemini-api-key", CWD, FAR)
        s._step(lambda: s.connection.prompt("x", FAR))
        s._pump_one(FAR)                                     # reads the ask
        token = next(iter(s.connection._asks))

        def answer():
            try:
                s.answer_permission(token, acp.APPROVE)
            except BaseException as error:   # reported below
                errors.append(error)

        worker = threading.Thread(target=answer, daemon=True)
        worker.start()
        self.assertTrue(reached.wait(10))
        canceller = threading.Thread(target=s.cancel, daemon=True)
        canceller.start()
        canceller.join(0.1)                                  # the cancel waits behind the answer's write
        release.set()
        worker.join(10)
        canceller.join(10)
        self.assertFalse(worker.is_alive() or canceller.is_alive())
        self.assertEqual(errors, [])
        order = ["answer" if m.get("id") == "perm-1" else m.get("method") for m in agent.received]
        self.assertLess(order.index("answer"), order.index("session/cancel"))

    def test_n41_the_agent_going_gives_no_second_outcome_for_an_abandoned_turn(self) -> None:
        for gone in ("end_of_output", "input_closed"):
            with self.subTest(gone=gone):
                conn = opened(FakeAcpAgent()).connection
                conn.prompt("x", FAR)
                self.assertEqual(conn.deadline().code, c.DEADLINE_EXCEEDED)
                self.assertIsNone(getattr(conn, gone)())

    def test_n43_the_output_ending_after_each_open_send_is_engine_exited(self) -> None:
        for stage in ("initialize", "authenticate", "session/new"):
            with self.subTest(stage=stage):
                agent = FakeAcpAgent()
                original = agent.send

                def send(line, stage=stage, agent=agent, original=original):
                    if json.loads(line).get("method") == stage:
                        agent.close_stdin()       # the agent exits instead of answering: its output ends
                    else:
                        original(line)

                agent.send = send
                with self.assertRaises(acp.AcpError) as ctx:
                    session(agent).open("gemini-api-key", CWD, FAR)
                self.assertEqual(ctx.exception.code, c.ENGINE_EXITED)

    # -- DeepSeek MED1: a failure while a turn's lines are handled abandons it; the session lives on ----------
    def test_med1_when_on_line_raises_the_turn_is_abandoned_and_the_next_prompt_runs(self) -> None:
        agent = FakeAcpAgent([[("chunk", "refused"), ("wait_cancel",), ("end", "cancelled")],
                              [("chunk", "second"), ("end", "end_turn")]])

        def on_line(line):
            if "refused" in line:
                raise ValueError("synthetic: the caller cannot take this line")

        s = opened(agent, on_line=on_line)
        with self.assertRaises(ValueError):
            s.prompt("first", FAR)
        self.assertEqual(agent.cancels, 1)                    # the abandoned turn is asked to stop
        self.assertEqual(s.connection.abandoned_turn, 1)
        out = s.prompt("second", FAR)                         # reads turn 1's answer first, then sends
        self.assertEqual((out.turn, out.code, out.text), (2, None, "second"))
        self.assertIsNone(s.connection.abandoned_turn)

    def test_med1_a_protocol_error_mid_turn_leaves_the_session_able_to_take_the_next_turn(self) -> None:
        agent = FakeAcpAgent([[("raw", "not json"), ("wait_cancel",), ("end", "cancelled")],
                              [("chunk", "second"), ("end", "end_turn")]])
        s = opened(agent)
        with self.assertRaises(acp.AcpProtocolError):
            s.prompt("first", FAR)
        self.assertEqual(agent.cancels, 1)
        out = s.prompt("second", FAR)
        self.assertEqual((out.code, out.text), (None, "second"))

    def test_med1_a_refused_line_that_answers_the_prompt_still_closes_the_turn(self) -> None:
        agent = FakeAcpAgent([[("end", "end_turn")], [("chunk", "second"), ("end", "end_turn")]])

        def on_line(line):
            message = json.loads(line)
            if message.get("id") == 4:         # turn 1's answer (ids 1-3 opened the session)
                raise ValueError("synthetic: the caller cannot take this line")

        s = opened(agent, on_line=on_line)
        with self.assertRaises(ValueError):
            s.prompt("first", FAR)
        self.assertIsNone(s.connection.turn_in_progress)       # the connection still read the answer
        self.assertEqual(agent.cancels, 0)                    # so no cancel was needed
        self.assertEqual(s.prompt("second", FAR).text, "second")

    def test_med1_a_read_that_fails_is_the_agent_going_away(self) -> None:
        class BrokenRead(FakeAcpAgent):
            def read_line(self, timeout_s):
                if self.received and self.received[-1].get("method") == "session/prompt":
                    raise OSError("synthetic read failure")
                return super().read_line(timeout_s)

        agent = BrokenRead([[("chunk", "never read"), ("end", "end_turn")]])
        out = opened(agent).prompt("x", FAR)
        self.assertEqual(out.code, c.ENGINE_EXITED)

        class BrokenOpen(FakeAcpAgent):
            def read_line(self, timeout_s):
                raise OSError("synthetic read failure")

        with self.assertRaises(acp.AcpError) as ctx:
            session(BrokenOpen()).open("gemini-api-key", CWD, FAR)
        self.assertEqual(ctx.exception.code, c.ENGINE_EXITED)

    def test_med1_a_malformed_error_answer_to_the_prompt_closes_the_turn(self) -> None:
        agent = FakeAcpAgent([[("raw", '{"jsonrpc":"2.0","id":4,"error":{"code":"x","message":"bad"}}')],
                              [("chunk", "second"), ("end", "end_turn")]])
        s = opened(agent)
        with self.assertRaises(acp.AcpProtocolError):
            s.prompt("first", FAR)
        self.assertIsNone(s.connection.turn_in_progress)
        self.assertEqual(agent.cancels, 0)
        self.assertEqual(s.prompt("second", FAR).text, "second")

    def test_med1_reading_to_an_abandoned_answer_stops_at_the_deadline_and_writes_nothing(self) -> None:
        agent = FakeAcpAgent([[("chunk", "slow"), ("wait_cancel",)]])   # never answers the abandoned prompt
        s = opened(agent)
        self.assertEqual(s.prompt("first", agent.clock() + 60).code, c.DEADLINE_EXCEEDED)
        with self.assertRaises(acp.AcpError) as ctx:
            s.prompt("second", agent.clock() + 60)
        self.assertEqual(ctx.exception.code, c.DEADLINE_EXCEEDED)
        self.assertEqual(len(sent(agent, "session/prompt")), 1)
        self.assertEqual(s.connection.abandoned_turn, 1)

    # -- DeepSeek's N3 (e), two callers opening one session: one reader at a time ---------------------------
    def test_one_reader_a_second_open_or_prompt_while_one_runs_is_refused_at_once(self) -> None:
        for paused in ("initialize", "session/prompt"):
            with self.subTest(paused=paused):
                reached, release = threading.Event(), threading.Event()

                class Paused(acp.AcpSession):
                    def _write(self, lines, paused=paused):
                        if lines and json.loads(lines[0]).get("method") == paused:
                            reached.set()
                            if not release.wait(10):
                                raise AssertionError("schedule watchdog")
                        return super()._write(lines)

                agent = FakeAcpAgent()
                s = Paused(agent, agent.clock)
                if paused == "session/prompt":
                    s.open("gemini-api-key", CWD, FAR)
                results = []

                def first(s=s, paused=paused):
                    results.append(s.open("gemini-api-key", CWD, FAR) if paused == "initialize" else s.prompt("x", FAR))

                worker = threading.Thread(target=first, daemon=True)
                worker.start()
                self.assertTrue(reached.wait(10))
                before = len(agent.received)
                with self.assertRaises(acp.AcpStateError):
                    s.open("gemini-api-key", CWD, FAR)
                with self.assertRaises(acp.AcpStateError):
                    s.prompt("y", FAR)
                self.assertEqual(len(agent.received), before)    # the refused calls wrote nothing
                release.set()
                worker.join(10)
                self.assertFalse(worker.is_alive())
                self.assertEqual(len(results), 1)

    # -- DeepSeek LOW1-LOW5 ---------------------------------------------------------------------------------
    def test_low1_an_interruption_out_of_the_hook_reaches_the_caller_and_the_session_lives_on(self) -> None:
        class Hooks(acp.EngineHooks):
            def limit(self, method, error):
                raise SystemExit(3)

        agent = FakeAcpAgent([[("error", -32603, "boom")], [("chunk", "second"), ("end", "end_turn")]])
        s = opened(agent, hooks=Hooks())
        with self.assertRaises(SystemExit):
            s.prompt("first", FAR)
        self.assertEqual(s.connection.hook_failures, 0)
        self.assertIsNone(s.connection.turn_in_progress)
        self.assertEqual(s.prompt("second", FAR).text, "second")

    def test_low2_a_deadline_that_is_not_a_finite_number_is_refused_before_anything_is_written(self) -> None:
        for bad in (float("nan"), float("inf"), -float("inf"), None, True, "60"):
            with self.subTest(deadline=bad):
                agent = FakeAcpAgent()
                with self.assertRaises(acp.AcpInvalid):
                    session(agent).open("gemini-api-key", CWD, bad)
                self.assertEqual(agent.received, [])
                s = opened(agent)
                before = len(agent.received)
                with self.assertRaises(acp.AcpInvalid):
                    s.prompt("x", bad)
                with self.assertRaises(acp.AcpInvalid):
                    s.connection.prompt("x", bad)
                self.assertEqual(len(agent.received), before)
                self.assertIsNone(s.connection.turn_in_progress)

    def test_low3_a_line_fed_after_the_output_ended_changes_nothing(self) -> None:
        conn = opened(FakeAcpAgent()).connection
        conn.prompt("x", FAR)
        conn.end_of_output()
        fed = conn.feed(permission_line(conn))
        self.assertEqual((fed.updates, fed.asks, fed.replies, fed.ended), ([], [], [], None))
        self.assertEqual(conn._asks, {})

    def test_low3_session_new_answered_without_a_session_id_is_a_protocol_error(self) -> None:
        for result in ({}, {"sessionId": ""}, {"sessionId": 5}, []):
            with self.subTest(result=result):
                conn = acp.AcpConnection()
                conn.agent_capabilities = {}
                rpc_id, _ = conn.new_session(CWD)
                with self.assertRaises(acp.AcpProtocolError):
                    conn.feed(json.dumps({"jsonrpc": "2.0", "id": rpc_id, "result": result}))
                self.assertIsNone(conn.session_id)

    def test_low3_a_response_whose_id_is_not_an_integer_is_stray(self) -> None:
        for wrong in ("true", "1.0"):
            with self.subTest(id=wrong):
                conn = acp.AcpConnection()
                rpc_id, _ = conn.initialize()
                self.assertEqual(rpc_id, 1)
                fed = conn.feed('{"jsonrpc":"2.0","id":' + wrong + ',"result":{"protocolVersion":1}}')
                self.assertIsNone(fed.ended)
                self.assertEqual(conn.stray_responses, 1)
                self.assertFalse(conn.pending_result(1).done)
                self.assertIsNone(conn.agent_capabilities)

    def test_low4_a_hook_answer_that_is_not_a_limit_signal_counts_as_no_limit(self) -> None:
        bad_answers = ("plan_limit", {"scope": "connection"}, dict(c.limit_signal("connection", "x"), extra=1),
                       {"scope": "model", "model": None, "reset_at": None, "source": "x"},
                       {"scope": "connection", "model": "m", "reset_at": None, "source": "x"},
                       {"scope": "connection", "model": None, "reset_at": float("nan"), "source": "x"},
                       {"scope": "connection", "model": None, "reset_at": True, "source": "x"},
                       {"scope": "connection", "model": None, "reset_at": None, "source": ""}, 0, [])
        for given in bad_answers:
            with self.subTest(given=given):
                class Hooks(acp.EngineHooks):
                    def limit(self, method, error, given=given):
                        return given

                s = opened(FakeAcpAgent([[("error", -32003, "synthetic")]]), hooks=Hooks())
                out = s.prompt("x", FAR)
                self.assertEqual((out.code, out.limit), (c.ENGINE_ERROR, None))
                self.assertEqual(s.connection.hook_failures, 1)
        good = c.limit_signal("model", "synthetic:hook", model="m1", reset_at=12.5)

        class Good(acp.EngineHooks):
            def limit(self, method, error):
                return good

        out = opened(FakeAcpAgent([[("error", -32003, "synthetic")]]), hooks=Good()).prompt("x", FAR)
        self.assertEqual((out.code, out.limit), (c.PLAN_LIMIT, good))

    def test_low5_every_refusal_of_a_call_is_an_acp_error_with_a_contract_code(self) -> None:
        agent = FakeAcpAgent()
        for call in (lambda: session(agent).open(None, CWD, FAR),
                     lambda: session(agent).open("gemini-api-key", "relative/path", FAR),
                     lambda: session(agent).open("gemini-api-key", CWD, FAR, load_session_id=""),
                     lambda: acp.AcpConnection(permission_timeout_s=0)):
            with self.assertRaises(acp.AcpInvalid) as ctx:
                call()
            self.assertIsInstance(ctx.exception, acp.AcpError)
            self.assertIsInstance(ctx.exception, ValueError)
            self.assertEqual(ctx.exception.code, c.REQUEST_INVALID)
        self.assertEqual(agent.received, [])                 # an argument refused writes nothing
        s = opened(agent)
        for call in (lambda: s.open("gemini-api-key", CWD, FAR), lambda: s.connection.new_session(CWD),
                     lambda: s.prompt("", FAR)):
            with self.assertRaises(acp.AcpError) as ctx:
                call()
            self.assertEqual(ctx.exception.code, c.REQUEST_INVALID)
        s.connection.end_of_output()
        with self.assertRaises(acp.AcpStateError) as ctx:
            s.connection.prompt("x", FAR)
        self.assertIsInstance(ctx.exception, acp.AcpError)
        self.assertIsInstance(ctx.exception, RuntimeError)
        self.assertEqual(ctx.exception.code, c.ENGINE_EXITED)


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
