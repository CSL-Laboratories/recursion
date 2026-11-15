# Written by Claude Opus 5.5 (claude-opus-5-5), builder lane ENGINE-E6, for Lucas.
"""The fake ``agent`` (fixtures/cursor/fake_agent.py): what it replays, records and refuses."""

from __future__ import annotations

import json
import unittest

from recursion_core.routes.engines import cursor
from recursion_core.routes.engines.fixtures.cursor import fake_agent
from recursion_core.routes.launch import ReadTimeout


def send(process, msg: dict) -> None:
    process.send(json.dumps(msg))


class FixtureLinesTest(unittest.TestCase):
    def test_the_header_is_never_replayed(self):
        raw = (fake_agent.HERE / "oneshot" / "hello.jsonl").read_text(encoding="utf-8").splitlines()
        lines = fake_agent.fixture_lines("oneshot/hello.jsonl")
        self.assertEqual(lines, raw[1:])
        self.assertEqual(fake_agent.fixture_header("oneshot/hello.jsonl")["type"], "_fixture")

    def test_a_file_without_a_header_is_refused(self):
        with self.assertRaises(fake_agent.FakeScriptError):
            fake_agent.fixture_header("argv.json")


class OneShotTest(unittest.TestCase):
    def test_a_launch_is_recorded_and_its_stream_replayed_to_end_of_file(self):
        launcher = fake_agent.FakeLauncher("oneshot/hello.jsonl")
        argv = cursor.oneshot_argv("synthetic-model")
        process = launcher(argv, env={"PATH": "/usr/bin"}, shell=False, model="synthetic-model")
        self.assertEqual(launcher.launches, [{"argv": argv, "env": {"PATH": "/usr/bin"}, "shell": False,
                                              "options": {"model": "synthetic-model"}}])
        cursor.check_argv(launcher.launches[0]["argv"])
        lines = []
        while (text := process.read_line(1.0)) is not None:
            lines.append(text)
        self.assertEqual(lines, fake_agent.fixture_lines("oneshot/hello.jsonl"))

    def test_the_replay_parses_to_the_expected_drafts(self):
        expected = json.loads((fake_agent.HERE / "oneshot" / "tools.expected.json").read_text(encoding="utf-8"))
        process = fake_agent.FakeLauncher("oneshot/tools.jsonl")(cursor.oneshot_argv("m"), env={})
        parser = cursor.StreamParser()
        parser.wrote(**expected["wrote"])
        drafts = []
        while (text := process.read_line(1.0)) is not None:
            drafts.extend(parser.parse(text))
        self.assertEqual(drafts, expected["drafts"])

    def test_stdin_and_fence_are_recorded(self):
        process = fake_agent.FakeLauncher("oneshot/hello.jsonl")(["agent"], env={})
        process.send("one")
        process.close_stdin()
        with self.assertRaises(BrokenPipeError):
            process.send("two")
        process.fence("deadline")
        self.assertEqual((process.sent, process.fenced), (["one"], ["deadline"]))


class AcpTest(unittest.TestCase):
    def setUp(self):
        self.launcher = fake_agent.FakeLauncher("acp/prompt_turns.jsonl")
        self.process = self.launcher(cursor.acp_argv(), env={})

    def read_all(self):
        out = []
        while True:
            try:
                text = self.process.read_line(1.0)
            except ReadTimeout:
                return out, "waiting"
            if text is None:
                return out, "eof"
            out.append(json.loads(text))

    def test_the_agent_waits_for_the_client_before_anything(self):
        self.assertEqual(self.read_all(), ([], "waiting"))

    def test_a_whole_drive_with_the_clients_own_ids(self):
        send(self.process, {"jsonrpc": "2.0", "id": 901, "method": "initialize", "params": {}})
        got, state = self.read_all()
        self.assertEqual((got[0]["id"], state), (901, "waiting"))
        self.assertEqual(got[0]["result"]["authMethods"][0]["id"], "cursor_login")
        send(self.process, {"jsonrpc": "2.0", "id": 902, "method": "authenticate", "params": {"methodId": "cursor_login"}})
        send(self.process, {"jsonrpc": "2.0", "id": 903, "method": "session/new", "params": {}})
        got, _ = self.read_all()
        self.assertEqual([m["id"] for m in got], [902, 903])
        send(self.process, {"jsonrpc": "2.0", "id": 904, "method": "session/prompt", "params": {}})
        got, state = self.read_all()
        self.assertEqual(got[-1]["method"], "session/request_permission")
        self.assertEqual((got[-1]["id"], state), (100, "waiting"))   # the agent's own request keeps its id
        send(self.process, {"jsonrpc": "2.0", "id": 100, "result": {"outcome": {"outcome": "selected",
                                                                                "optionId": "reject-once"}}})
        got, _ = self.read_all()
        self.assertEqual(got[-1], {"jsonrpc": "2.0", "id": 904, "result": {"stopReason": "end_turn"}})
        send(self.process, {"jsonrpc": "2.0", "id": 905, "method": "session/prompt", "params": {}})
        got, state = self.read_all()
        self.assertEqual((got[-1]["id"], got[-1]["result"]["stopReason"], state), (905, "end_turn", "eof"))
        self.assertEqual(len(self.process.sent), 6)

    def test_a_message_out_of_order_is_refused(self):
        with self.assertRaises(fake_agent.FakeScriptError):
            send(self.process, {"jsonrpc": "2.0", "id": 1, "method": "session/new", "params": {}})

    def test_a_request_where_a_notification_is_due_and_the_reverse_are_refused(self):
        with self.assertRaises(fake_agent.FakeScriptError):
            send(self.process, {"jsonrpc": "2.0", "method": "initialize", "params": {}})
        process = fake_agent.FakeLauncher("acp/cancel.jsonl")(cursor.acp_argv(), env={})
        for i, method in enumerate(("initialize", "session/new", "session/prompt"), 1):
            send(process, {"jsonrpc": "2.0", "id": i, "method": method, "params": {}})
        with self.assertRaises(fake_agent.FakeScriptError):
            send(process, {"jsonrpc": "2.0", "id": 9, "method": "session/cancel", "params": {}})

    def test_cancel_ends_the_prompt_cancelled(self):
        process = fake_agent.FakeLauncher("acp/cancel.jsonl")(cursor.acp_argv(), env={})
        for i, method in enumerate(("initialize", "session/new", "session/prompt"), 1):
            send(process, {"jsonrpc": "2.0", "id": i, "method": method, "params": {}})
        send(process, {"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": "acp-session-0002"}})
        out = []
        while (text := process.read_line(1.0)) is not None:
            out.append(json.loads(text))
        self.assertEqual(out[-1], {"jsonrpc": "2.0", "id": 3, "result": {"stopReason": "cancelled"}})

    def test_a_message_after_the_transcript_is_refused(self):
        process = fake_agent.FakeLauncher("acp/load.jsonl")(cursor.acp_argv(), env={})
        for i, method in enumerate(("initialize", "session/load", "session/prompt"), 1):
            send(process, {"jsonrpc": "2.0", "id": i, "method": method, "params": {}})
        with self.assertRaises(fake_agent.FakeScriptError):
            send(process, {"jsonrpc": "2.0", "id": 4, "method": "session/prompt", "params": {}})


if __name__ == "__main__":
    unittest.main()
