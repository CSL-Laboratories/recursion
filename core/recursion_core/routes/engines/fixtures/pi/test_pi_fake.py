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
            process.send('{"type":"prompt","message":"x"}')
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

    def test_records_that_are_not_commands_get_pis_answers(self):
        self.send("not json")
        self.send('{"id":"x1","type":"bash","command":"ls"}')
        drain(self.process, self.stream)
        self.assertEqual(self.stream.signals[0], {"signal": "parse_error",
                                                  "error": "Failed to parse command: not valid JSON"})
        self.assertEqual(self.stream.ignored, {"response_unmatched": 1})

    def test_send_adds_the_line_end_so_a_record_that_has_one_reaches_pi_as_two(self):
        # As the production send and Pi's reader do: the second, empty record gets a parse error.
        _token, record = self.stream.command("get_state")
        self.send(record + "\n")
        drain(self.process, self.stream)
        self.assertEqual([s["signal"] for s in self.stream.signals], ["session", "response", "parse_error"])
        self.assertEqual(self.stream.signals[2]["error"], "Failed to parse command: Unexpected end of JSON input")
        self.send("")
        self.send('{"id":"x2","type":"get_state"}\r')            # Pi strips a CR before the LF
        records = []
        while True:
            try:
                records.append(json.loads(self.process.read_line(1.0)))
            except ReadTimeout:
                break
        self.assertEqual([(r["command"], r["success"]) for r in records], [("parse", False), ("get_state", True)])

    def test_a_queued_steer_is_counted_until_its_message_starts(self):
        self.prompt("Long task.", "dlv-1", "turn-1")
        read(self.process, self.stream, 7)            # the response, the run's start, the reply streaming
        self.prompt("Also this.", "dlv-2", "turn-1", how="mid_turn")
        self.assertEqual(self.pending_count(), 1)
        records = self.read_all()
        steer_at = next(i for i, r in enumerate(records) if r["type"] == "message_start"
                        and r["message"].get("content") == "Also this.")
        self.assertLess(steer_at, [r["type"] for r in records].index("agent_end"))
        self.assertEqual(self.pending_count(), 0)

    def test_an_abort_keeps_a_queued_steer_and_the_next_run_delivers_it(self):
        self.prompt("Long task.", "dlv-1", "turn-1")
        read(self.process, self.stream, 7)
        self.prompt("Also this.", "dlv-2", "turn-1", how="mid_turn")
        _token, record = self.stream.command("abort")
        self.send(record)
        aborted = self.read_all()
        self.assertNotIn("Also this.", [r["message"].get("content") for r in aborted if "message" in r])
        self.assertEqual(settles(self.stream)[-1]["aborted"], True)
        self.assertEqual(self.pending_count(), 1)                     # not dropped by the abort
        self.prompt("Next.", "dlv-3", "turn-2")
        records = self.read_all()
        users = [r["message"]["content"] for r in records
                 if r["type"] == "message_start" and r["message"].get("role") == "user"]
        self.assertEqual(users, ["Next.", "Also this."])               # after the prompt, in its first turn
        self.assertEqual([r["type"] for r in records].count("turn_start"), 1)
        self.assertEqual(settles(self.stream)[-1], {"signal": "settled", "aborted": False, "stop_reason": "stop",
                                                     "text": "two"})
        self.assertEqual(self.pending_count(), 0)

    def users(self, records):
        return [r["message"]["content"] for r in records
                if r["type"] == "message_start" and r["message"].get("role") == "user"]

    def test_steers_with_the_same_text_as_the_next_prompt_are_each_delivered(self):
        # Delivery takes Pi's steering queue by position; only the count matches text (agent.ts steeringQueue
        # against agent-session.ts _steeringMessages). A prompt with a queued steer's text takes nothing.
        self.prompt("First.", "dlv-1", "turn-1")
        drafts = read(self.process, self.stream, 7)
        self.prompt("repeat", "dlv-2", "turn-1", how="mid_turn")
        self.prompt("repeat", "dlv-3", "turn-1", how="mid_turn")
        _token, record = self.stream.command("abort")
        self.send(record)
        self.read_all(drafts)
        self.assertEqual(self.pending_count(), 2)
        self.prompt("repeat", "dlv-4", "turn-2")
        records = self.read_all(drafts)
        self.assertEqual(self.users(records), ["repeat", "repeat", "repeat"])
        self.assertEqual(self.accepted(drafts), [("dlv-1", "new_turn"), ("dlv-2", "mid_turn"), ("dlv-3", "mid_turn"),
                                                 ("dlv-4", "new_turn")])
        self.assertEqual(self.pending_count(), 0)

    def test_the_count_drops_when_the_steers_message_starts_not_when_it_ends(self):
        self.prompt("Long task.", "dlv-1", "turn-1")
        read(self.process, self.stream, 7)
        self.prompt("Also this.", "dlv-2", "turn-1", how="mid_turn")
        while True:
            record = json.loads(self.process.read_line(1.0))
            if record["type"] == "message_start" and record["message"].get("content") == "Also this.":
                break
        self.assertEqual(self.pending_count(), 0)          # its message_end is still to come
        self.assertEqual(json.loads(self.process.read_line(1.0))["type"], "message_end")

    def test_an_abort_after_the_loop_took_a_steer_still_writes_the_steers_message(self):
        self.prompt("Long task.", "dlv-1", "turn-1")
        read(self.process, self.stream, 7)
        self.prompt("Also this.", "dlv-2", "turn-1", how="mid_turn")
        read(self.process, self.stream, 4)                 # queued, the reply's end, turn_end, the steer's turn_start
        _token, record = self.stream.command("abort")
        self.send(record)
        aborted = self.read_all()
        self.assertEqual(self.users(aborted), ["Also this."])
        self.assertEqual([r["type"] for r in aborted][-4:], ["turn_end", "agent_end", "agent_settled", "response"])
        self.assertEqual(settles(self.stream)[-1]["aborted"], True)
        self.assertEqual(self.pending_count(), 0)
        self.prompt("Next.", "dlv-3", "turn-2")
        self.assertEqual(self.users(self.read_all()), ["Next."])     # delivered once, not again

    def test_a_run_after_an_aborted_one_polls_its_steers_again(self):
        self.prompt("Long task.", "dlv-1", "turn-1")
        read(self.process, self.stream, 7)
        _token, record = self.stream.command("abort")
        self.send(record)
        self.read_all()
        self.prompt("Next.", "dlv-2", "turn-2")
        read(self.process, self.stream, 7)
        self.prompt("More.", "dlv-3", "turn-2", how="mid_turn")
        records = self.read_all()
        self.assertEqual(self.users(records), ["More."])                # its own turn, before the run ends
        steer_at = next(i for i, r in enumerate(records) if r["type"] == "message_start"
                        and r["message"].get("content") == "More.")
        self.assertLess(steer_at, [r["type"] for r in records].index("agent_end"))
        self.assertEqual(self.pending_count(), 0)

    def pending_count(self):
        """Pi's ``pendingMessageCount``, read with a ``get_state`` (what comes before its answer is fed too)."""
        _token, record = self.stream.command("get_state")
        self.send(record)
        while True:
            line = self.process.read_line(1.0)
            self.stream.feed(line)
            got = json.loads(line)
            if got["type"] == "response" and got["command"] == "get_state":
                return got["data"]["pendingMessageCount"]

    def read_all(self, drafts=None):
        """Every record the fake has to give, each fed to the stream; its drafts go to ``drafts`` if given."""
        records = []
        while True:
            try:
                line = self.process.read_line(1.0)
            except ReadTimeout:
                return records
            records.append(json.loads(line))
            got = self.stream.feed(line)
            if drafts is not None:
                drafts += got

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
            self.process.send("{}")
        fenced = f.FakePi("rpc")
        fenced.send(pi.PiStream("rpc", connection_id=CONN).command("get_state")[1])
        fenced.fence("deadline")
        self.assertIsNone(fenced.read_line(1.0))
        with self.assertRaises(BrokenPipeError):
            fenced.send("{}")


if __name__ == "__main__":
    unittest.main()
