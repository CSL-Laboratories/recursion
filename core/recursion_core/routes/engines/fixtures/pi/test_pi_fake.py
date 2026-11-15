# Written by Claude Opus 5.5 (claude-opus-5-5), builder lane ENGINE-E8.
"""The fake ``pi`` (SYNTHETIC) behaves as Pi's protocol pages say, and the parser reads it end to end.

These are the fake's tests and the parser's, not the driver's: the RPC worker that will hold one ``pi
--mode rpc`` per session is a later slice. What they show now is that the reader handles a second
message to the same process, an abort that leaves the process usable, a steer, and a resumed session id.

Run by dotted name: PYTHONPATH=core <python> -m unittest recursion_core.routes.engines.fixtures.pi.test_pi_fake
"""

import json
import unittest

from recursion_core.routes.engines import pi
from recursion_core.routes.engines.fixtures.pi import fake_pi as f
from recursion_core.routes.engines.fixtures.pi.test_pi_fixtures import data_file, expected
from recursion_core.routes.launch import ReadTimeout

CONN = "api_key:openai-key-1"


def drain(process, stream) -> list:
    """Read until the fake has nothing more to say (EOF or ``ReadTimeout``); return the drafts."""
    drafts = []
    while True:
        try:
            line = process.read_line(1.0)
        except ReadTimeout:
            return drafts
        if line is None:
            return drafts
        drafts += stream.feed(line)


def read(process, stream, count) -> list:
    drafts = []
    for _ in range(count):
        drafts += stream.feed(process.read_line(1.0))
    return drafts


def settles(stream) -> list:
    return [s for s in stream.signals if s["signal"] == "settled"]


class FakeJsonTest(unittest.TestCase):
    def test_a_one_shot_fake_replays_its_lines_reads_no_stdin_and_ends(self):
        lines = [line + "\n" for line in pi.records_of(data_file("synthetic-json-basic").read_bytes())]
        process = f.FakePi("json", lines=lines)
        stream = pi.PiStream("json", connection_id="subscription:pi-chatgpt")
        stream.argv_prompt("Say hello in one word.", delivery_id="dlv-1", turn_id="turn-1")
        with self.assertRaises(BrokenPipeError):
            process.send('{"type":"prompt","message":"x"}\n')
        self.assertEqual(drain(process, stream), expected("synthetic-json-basic")["drafts"])
        self.assertIsNone(process.read_line(1.0))


class FakeRpcTest(unittest.TestCase):
    def setUp(self):
        self.process = f.FakePi("rpc", replies=["one", "two", "three", "four"])
        self.stream = pi.PiStream("rpc", connection_id=CONN)

    def send(self, line):
        self.process.send(line)

    def prompt(self, text, delivery, turn, how="new_turn"):
        _token, line = self.stream.prompt(text, delivery_id=delivery, turn_id=turn, how=how)
        self.send(line)

    def accepted(self, drafts):
        return [(d["payload"]["delivery_id"], d["payload"]["how"]) for d in drafts if d["type"] == "input.accepted"]

    def test_a_second_message_reaches_the_same_process(self):
        self.prompt("First.", "dlv-1", "turn-1")
        first = drain(self.process, self.stream)
        self.prompt("Second.", "dlv-2", "turn-2")
        second = drain(self.process, self.stream)
        self.assertEqual(self.accepted(first), [("dlv-1", "new_turn")])
        self.assertEqual(self.accepted(second), [("dlv-2", "new_turn")])
        self.assertEqual([d["type"] for d in second], ["input.accepted", "usage"])
        self.assertEqual([s["text"] for s in settles(self.stream)], ["one", "two"])
        self.assertEqual([c["message"] for c in self.process.commands], ["First.", "Second."])
        _token, line = self.stream.command("get_state")
        self.send(line)
        drain(self.process, self.stream)
        self.assertEqual(self.stream.engine_session_id, f.SESSION_ID)
        self.assertIsNone(self.process.fenced)
        self.assertFalse(self.process.stdin_closed)

    def test_abort_ends_the_turn_and_the_process_stays_usable(self):
        self.prompt("Long task.", "dlv-1", "turn-1")
        read(self.process, self.stream, 6)            # the response, the run's start and the reply streaming
        token, line = self.stream.command("abort")
        self.send(line)
        drain(self.process, self.stream)
        self.assertEqual(settles(self.stream)[-1], {"signal": "settled", "aborted": True, "stop_reason": "aborted",
                                                     "text": ""})
        self.assertIn({"signal": "response", "command": "abort", "token": token, "success": True}, self.stream.signals)
        # The abort's response came after the settle, as Pi's abort waits for the session to be idle.
        order = [s["signal"] for s in self.stream.signals if s["signal"] in ("settled", "response")]
        self.assertEqual(order, ["settled", "response"])
        self.prompt("Again.", "dlv-2", "turn-2")
        drafts = drain(self.process, self.stream)
        self.assertEqual(self.accepted(drafts), [("dlv-2", "new_turn")])
        self.assertEqual(settles(self.stream)[-1], {"signal": "settled", "aborted": False, "stop_reason": "stop",
                                                     "text": "two"})

    def test_an_abort_when_idle_is_answered_at_once(self):
        token, line = self.stream.command("abort")
        self.send(line)
        drain(self.process, self.stream)
        self.assertEqual(self.stream.signals, [{"signal": "response", "command": "abort", "token": token,
                                                "success": True}])

    def test_a_message_during_a_run_must_steer_and_is_delivered_before_the_run_ends(self):
        self.prompt("Check the build.", "dlv-1", "turn-1")
        read(self.process, self.stream, 3)
        self.prompt("Interrupting.", "dlv-2", "turn-1", how="new_turn")      # not marked as a steer: refused
        self.prompt("Also the tests.", "dlv-3", "turn-1", how="mid_turn")
        records = []
        while True:
            try:
                line = self.process.read_line(1.0)
            except ReadTimeout:
                break
            records.append(json.loads(line))
            self.stream.feed(line)
        self.assertIn({"signal": "refused", "delivery_id": "dlv-2", "error": "Agent is already processing. Specify "
                       "streamingBehavior ('steer' or 'followUp') to queue the message."}, self.stream.signals)
        kinds = [(r["type"], r.get("message", {}).get("content")) for r in records]
        steer_at = kinds.index(("message_end", "Also the tests."))
        self.assertLess(steer_at, kinds.index(("agent_end", None)))
        self.assertEqual(len(settles(self.stream)), 1)
        self.assertEqual(settles(self.stream)[0]["text"], "two")       # the steered turn's reply ends the run
        self.assertEqual(self.stream.pending_deliveries(), [])

    def test_a_resumed_worker_reports_the_session_it_was_given(self):
        resumed = f.FakePi("rpc", session_id="019a5b2e-2222-7000-8000-00000000e802")
        stream = pi.PiStream("rpc", connection_id=CONN)
        _token, line = stream.command("get_state")
        resumed.send(line)
        drain(resumed, stream)
        self.assertEqual(stream.engine_session_id, "019a5b2e-2222-7000-8000-00000000e802")

    def test_lines_that_are_not_commands_get_pis_answers(self):
        self.send("not json\n")
        self.send('{"id":"x1","type":"bash","command":"ls"}\n')
        drain(self.process, self.stream)
        self.assertEqual(self.stream.signals[0], {"signal": "parse_error", "error": "Failed to parse command"})
        self.assertEqual(self.stream.ignored, {"response_unmatched": 1})

    def test_a_dialog_answer_gets_no_response(self):
        self.send(self.stream.cancel_dialog("u1"))
        self.assertEqual(drain(self.process, self.stream), [])
        self.assertEqual(self.stream.signals, [])

    def test_closing_stdin_ends_the_stream_and_fencing_ends_it_at_once(self):
        self.prompt("x", "dlv-1", "turn-1")
        self.process.close_stdin()
        drain(self.process, self.stream)
        self.assertIsNone(self.process.read_line(1.0))
        self.assertEqual(settles(self.stream), [])                   # the run was not finished
        self.assertEqual(self.stream.pending_deliveries(), [])        # its prompt had been accepted
        with self.assertRaises(BrokenPipeError):
            self.process.send("{}\n")
        fenced = f.FakePi("rpc")
        fenced.send(pi.PiStream("rpc", connection_id=CONN).command("get_state")[1])
        fenced.fence("deadline")
        self.assertIsNone(fenced.read_line(1.0))
        with self.assertRaises(BrokenPipeError):
            fenced.send("{}\n")


if __name__ == "__main__":
    unittest.main()
