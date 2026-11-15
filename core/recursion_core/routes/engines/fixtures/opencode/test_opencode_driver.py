"""The OpenCode driver on driver.v2's gate (``routes.driver.Driver``, lane DRIVER-CC's #376) (lane ENGINE-E3, slice 2).

A spy stands in for the launch path: it records every spawn and gives back a plain-text process. Run by name:

    PYTHONPATH=core <python> -m unittest recursion_core.routes.engines.fixtures.opencode.test_opencode_driver
"""

from __future__ import annotations

import inspect
import sys
import unittest
from pathlib import Path

from recursion_core.routes.driver import Declaration, DriverRefused
from recursion_core.routes.engines import opencode as oc

HERE = Path(__file__).resolve().parent
ENV = {"PATH": "/usr/bin:/bin", "HOME": "/var/lib/recursion/homes/opencode", "OPENAI_API_KEY": "sk-made-up"}


class Process:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.closes = 0

    def send(self, text: str) -> None:
        assert self.closes == 0, "send after close_stdin"
        self.sent.append(text)

    def close_stdin(self) -> None:
        self.closes += 1


class Spy:
    def __init__(self) -> None:
        self.spawns: list[tuple[list, dict]] = []
        self.processes: list[Process] = []

    def __call__(self, argv: list, env: dict) -> Process:
        self.spawns.append((list(argv), dict(env)))
        self.processes.append(Process())
        return self.processes[-1]


def session(model="openai/gpt-5.5") -> dict:
    """The ``session`` argument of #376's PROVISIONAL ``start(session, workspace, env)`` (its lane questions Q7)."""
    return {"session_id": "ses-e3test", "run_id": "run-e3test",
            "route": {"engine": "opencode", "model": model, "connection": {"kind": "api_key", "id": "k1"}}}


def message(turn="turn-1", delivery="dlv-1", text="Run the tests.") -> dict:
    return {"kind": "user_message", "turn_id": turn, "text": text, "delivery_id": delivery}


class DriverTests(unittest.TestCase):
    def setUp(self):
        self.spy = Spy()
        self.driver = oc.OpenCodeDriver(self.spy, wall=lambda: 1794614400.0, clock=lambda: 100.0)

    def replay(self, handle, name: str) -> list[dict]:
        drafts = []
        for line in (HERE / name).read_text("utf-8").splitlines(keepends=True):
            drafts.extend(self.driver.parse(handle, line))
        return drafts

    def test_the_declaration_is_driver_v2s_record(self):
        self.assertEqual(self.driver.declaration.as_record(), oc.declaration())
        self.assertEqual(Declaration.from_record(oc.declaration()), self.driver.declaration)

    def test_a_one_shot_turn_from_start_to_result(self):
        handle = self.driver.start(session(), None, ENV)
        self.assertEqual(self.spy.spawns, [(["opencode", "run", "--format", "json", "--model", "openai/gpt-5.5"], ENV)])
        self.driver.send(handle, message())
        process = self.spy.processes[0]
        self.assertEqual((process.sent, process.closes), (["Run the tests."], 1))
        self.assertEqual(handle.live_turn, "turn-1")
        drafts = self.replay(handle, "synthetic-v1-tool-use.jsonl")
        kinds = [d["type"] for d in drafts]
        self.assertEqual(kinds.count("input.accepted"), 1)          # on the first step_start, once
        self.assertEqual(drafts[1], {"type": "input.accepted",
                                     "payload": {"delivery_id": "dlv-1", "turn_id": "turn-1", "how": "new_turn"}})
        self.assertEqual(handle.ignored_acceptances, 0)
        self.assertEqual(handle.engine_session_id, "ses_synthv1tools000000000001")
        self.assertEqual((kinds.count("tool.call"), kinds.count("usage")), (2, 2))
        (result,) = self.driver.finish(handle, 0)
        self.assertEqual(result, {"type": "result", "payload": {"code": None, "detail": ""}})
        self.assertIsNone(handle.live_turn)

    def test_a_typed_429_comes_through_the_gate_as_a_limit_and_the_result(self):
        handle = self.driver.start(session(), None, ENV)
        self.driver.send(handle, message())
        (limit,) = [d["payload"] for d in self.replay(handle, "synthetic-v1-limit-429.jsonl") if d["type"] == "limit"]
        self.assertEqual((limit["connection_id"], limit["reset_at"]), ("api_key:k1", "2026-11-14T00:02:00.000Z"))
        self.assertEqual(self.driver.finish(handle, 1)[0]["payload"]["code"], "plan_limit")

    def test_every_verb_the_drive_lacks_refuses_with_no_process_started_and_nothing_written(self):
        with self.assertRaises(DriverRefused) as caught:
            self.driver.resume("ses_abc", session(), None, ENV)
        self.assertEqual(caught.exception.as_record(), {"code": "unsupported", "call": "resume", "flag": "resume"})
        with self.assertRaises(DriverRefused):
            self.driver.fork("ses_abc", "turn-1")
        self.assertEqual(self.spy.spawns, [])                                              # nothing started
        handle = self.driver.start(session(), None, ENV)
        refused = []
        for call in (lambda: self.driver.send(handle, {"kind": "approval_answer", "approval_id": "apr-1",
                                                       "decision": "approve"}),
                     lambda: self.driver.interrupt(handle, "turn-1")):
            with self.assertRaises(DriverRefused) as caught:
                call()
            refused.append(caught.exception.flag)
        self.driver.send(handle, message())
        for item in (message(delivery="dlv-2"), message(turn="turn-2", delivery="dlv-3")):
            with self.assertRaises(DriverRefused) as caught:                              # mid-turn, then a second turn
                self.driver.send(handle, item)
            refused.append(caught.exception.flag)
        self.driver.finish(handle, 0)
        with self.assertRaises(DriverRefused) as caught:
            self.driver.send(handle, message(turn="turn-2", delivery="dlv-4"))
        refused.append(caught.exception.flag)
        # A message while turn-1 is live is mid-turn input, whatever turn it names; after the end, a second turn.
        self.assertEqual(refused, ["permission_bridge", "interrupt", "mid_turn_input", "mid_turn_input", "multi_turn"])
        self.assertEqual(len(self.spy.spawns), 1)
        self.assertEqual((self.spy.processes[0].sent, self.spy.processes[0].closes), (["Run the tests."], 1))

    def test_a_close_before_any_message_ends_the_input_once(self):
        handle = self.driver.start(session(), None, ENV)
        self.driver.send(handle, {"kind": "close"})
        self.assertEqual((self.spy.processes[0].sent, self.spy.processes[0].closes), ([], 1))
        self.assertEqual(self.driver.finish(handle, None)[0]["payload"]["code"], "engine_exited")

    def test_no_acceptance_without_a_step_start_and_none_before_the_message(self):
        handle = self.driver.start(session(), None, ENV)
        early = self.replay(handle, "synthetic-v1-success.jsonl")       # stdout before the prompt was written
        self.assertNotIn("input.accepted", [d["type"] for d in early])
        handle = self.driver.start(session(), None, ENV)
        self.driver.send(handle, message())
        drafts = self.replay(handle, "synthetic-v1-auth-error.jsonl")  # an error line, no step
        self.assertNotIn("input.accepted", [d["type"] for d in drafts])

    def test_the_engine_default_model_names_no_model(self):
        self.driver.start(session(model=None), None, ENV)
        self.assertEqual(self.spy.spawns[0][0], ["opencode", "run", "--format", "json"])
        driver = oc.OpenCodeDriver(self.spy, binary="/opt/oc/bin/opencode")
        driver.start(session(), None, ENV)
        self.assertEqual(self.spy.spawns[1][0][0], "/opt/oc/bin/opencode")


class ParserStateTests(unittest.TestCase):
    """The two fields this slice adds to ``Parser`` are put back with the rest when a line is interrupted."""

    def test_an_interrupted_line_leaves_the_step_reason_and_the_last_type_as_they_were(self):
        p = oc.Parser(connection_id="api_key:k1", model=None, wall=lambda: 1794614400.0, clock=lambda: 100.0)
        p.feed(b'{"type":"step_start","sessionID":"ses_x","part":{"type":"step-start"}}\n')
        finish = (b'{"type":"step_finish","sessionID":"ses_x","part":{"type":"step-finish","reason":"stop",'
                  b'"tokens":{"input":1}}}\n')
        lines, first = inspect.getsourcelines(oc.Parser.feed)
        target = first + next(i for i, text in enumerate(lines) if text.strip() == "return drafts")

        def tracer(frame, event, arg):
            if frame.f_code is oc.Parser.feed.__code__:
                if event == "line" and frame.f_lineno == target:
                    raise KeyboardInterrupt
                return tracer
            return None

        sys.settrace(tracer)
        try:
            with self.assertRaises(KeyboardInterrupt):
                p.feed(finish)                                   # interrupted on its return
        finally:
            sys.settrace(None)
        self.assertEqual((p._finish, p.last_type), (None, "step_start"))
        p.feed(finish)
        self.assertEqual((p._finish, p.last_type), ("stop", "step_finish"))
        self.assertEqual(p.end(None)[0]["payload"]["code"], None)  # the stream's own end of the turn


if __name__ == "__main__":
    unittest.main()
