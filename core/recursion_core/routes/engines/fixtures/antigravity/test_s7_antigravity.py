"""Deadline checks of the Antigravity driver: a permission answer, a late prompt
response, a cancel, and a one-shot launch all stop when the turn's deadline has passed.

Synthetic. No real engine, key, or account is used.
"""

import json
import os
import tempfile
import unittest

from recursion_core.routes import contract as c
from recursion_core.routes.engines import antigravity as ag
from recursion_core.routes.engines.fixtures.antigravity import acp
from recursion_core.routes.engines.fixtures.antigravity.fake_acp_agent import (
    FakeAcpAgent, OPTIONS_ALL, TOOL_CALL,
)
from recursion_core.routes.engines.fixtures.antigravity.test_antigravity_driver import route
from recursion_core.routes.engines.fixtures.antigravity.test_antigravity_one_shot import (
    Clock, Launcher, adapter, request,
)

FAR = 1e12


class ReadyDeadlineRegressions(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.cwd = self._dir.name
        self.home = os.path.join(self._dir.name, "route-home")

    def test_f1_a_permission_reply_after_the_turn_deadline_cannot_approve(self):
        for permission_timeout in (None, 60.0):
            with self.subTest(permission_timeout=permission_timeout):
                agent = FakeAcpAgent([
                    [("ask", TOOL_CALL, OPTIONS_ALL), ("end", "end_turn")],
                ])
                replies = []
                session = None

                def on_ask(ask):
                    agent.clock.advance(2.0)  # model descheduling before the answer claims the ask
                    replies.append(session.answer_permission(ask.token, acp.APPROVE))

                session = ag.open_acp(
                    agent, agent.clock, route("gemini-api-key"), self.cwd, FAR,
                    permission_timeout_s=permission_timeout, on_ask=on_ask,
                )
                outcome = session.prompt("hello", agent.clock() + 1.0)
                self.assertEqual(outcome.code, c.DEADLINE_EXCEEDED)
                self.assertEqual(replies, [False])
                selected = agent.permission_answers.get("perm-1", {}).get("outcome")
                self.assertNotEqual(selected, {"outcome": "selected", "optionId": "proceed_once"})

    def test_f2_the_first_late_prompt_response_still_delivers_its_deadline_outcome(self):
        agent = FakeAcpAgent()
        session = ag.open_acp(agent, agent.clock, route("gemini-api-key"), self.cwd, FAR)
        conn = session.connection
        rpc_id, _line_to_agent = conn.prompt("hello", agent.clock() + 1.0)
        agent.clock.advance(2.0)
        fed = conn.feed(json.dumps({
            "jsonrpc": "2.0", "id": rpc_id, "result": {"stopReason": "end_turn"},
        }))
        self.assertIsNotNone(fed.ended)
        self.assertEqual(fed.ended.code, c.DEADLINE_EXCEEDED)
        self.assertIsNone(conn.turn_in_progress)

    def test_f3_a_deadline_crossed_in_on_line_sends_cancel_and_keeps_the_session_usable(self):
        agent = FakeAcpAgent([
            [("chunk", "first"), ("wait_cancel",), ("end", "cancelled")],
            [("chunk", "second"), ("end", "end_turn")],
        ])
        delayed = []

        def on_line(line):
            if not delayed and '"agent_message_chunk"' in line:
                delayed.append(True)
                agent.clock.advance(2.0)

        session = ag.open_acp(
            agent, agent.clock, route("gemini-api-key"), self.cwd, FAR, on_line=on_line,
        )
        outcome = session.prompt("first", agent.clock() + 1.0)
        self.assertEqual(outcome.code, c.DEADLINE_EXCEEDED)
        self.assertEqual(agent.cancels, 1)
        self.assertEqual(session.prompt("second", agent.clock() + 10.0).text, "second")

    def test_f4_no_one_shot_launch_after_its_deadline_including_time_spent_preparing(self):
        for preparation_crosses_deadline in (False, True):
            with self.subTest(preparation_crosses_deadline=preparation_crosses_deadline):
                clock = Clock()
                launcher = Launcher([
                    json.dumps({"event": "result", "result": {"status": "SUCCESS", "response": "hi"}}),
                ], clock)

                def home(_route, _method):
                    if preparation_crosses_deadline:
                        clock.now += 2.0
                    return self.home

                made = adapter(launcher, clock, home=home)
                deadline = clock() + (1.0 if preparation_crosses_deadline else -1.0)
                result = made.run(request(), deadline)
                self.assertEqual(result["code"], c.DEADLINE_EXCEEDED)
                self.assertEqual(launcher.calls, [])


if __name__ == "__main__":
    unittest.main()
