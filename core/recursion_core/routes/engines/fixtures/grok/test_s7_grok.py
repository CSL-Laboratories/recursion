"""Synthetic checks for repeated JSON keys, the stop-reason detail cap, and a tool
completion that omits output an earlier update already carried.

Run from the repository root (this folder has no ``__init__.py``):

    PYTHONPATH=core python -m unittest recursion_core.routes.engines.fixtures.grok.test_s7_grok
"""
import json
import unittest

from recursion_core.routes import contract as c
from recursion_core.routes.engines import grok

CONN = "subscription:review-example"
END = '{"type":"end","stopReason":"end_turn"}\n'


def _results(drafts):
    return [d["payload"] for d in drafts if d["type"] == "tool.result"]


class ReadyRegressions(unittest.TestCase):
    def test_f1_duplicate_tool_input_is_not_given_a_canonical_digest(self):
        raw = (
            '{"type":"tool_call","toolCallId":"c1","toolName":"write_file",'
            '"rawInput":{"path":"a","path":"b"}}\n'
        )
        drafts, _ = grok.parse_run(CONN, [raw, END], 0)
        self.assertEqual(drafts[0]["type"], "engine.output")
        self.assertFalse(any(d["type"] == "tool.call" for d in drafts))
        self.assertEqual(drafts[-1]["payload"]["code"], c.PROTOCOL_ERROR)

    def test_f1_duplicate_envelope_type_cannot_erase_an_error(self):
        raw = (
            '{"type":"error","message":"quota exhausted",'
            '"type":"end","stopReason":"end_turn"}\n'
        )
        drafts, _ = grok.parse_run(CONN, [raw], 0)
        self.assertEqual(drafts[-1]["payload"]["code"], c.PROTOCOL_ERROR)


class StopDetailCap(unittest.TestCase):
    def test_stop_reason_detail_is_bounded(self):
        for prefix in ([], ['{"type":"max_turns_reached"}\n']):
            with self.subTest(after_max_turns=bool(prefix)):
                raw = json.dumps({"type": "end", "stopReason": "x" * 4000}) + "\n"
                drafts, _ = grok.parse_run(CONN, prefix + [raw], 1)
                detail = drafts[-1]["payload"]["detail"]
                self.assertLessEqual(len(detail), grok.MAX_DETAIL_CHARS)


class StreamedToolOutput(unittest.TestCase):
    """A completion that leaves content and raw output empty keeps an earlier line's output.

    The lines are synthetic. Whether a live Grok build always repeats that output on the
    completed line was not run.
    """

    def _run(self, lines):
        call = (
            '{"type":"tool_call","toolCallId":"c1","toolName":"read_file",'
            '"status":"in_progress","rawInput":{},"content":[],"locations":[]}\n'
        )
        return grok.parse_run(CONN, [call, *lines, END], 0)

    def test_a_completion_that_omits_output_keeps_what_was_streamed_earlier(self):
        streamed = (
            '{"type":"tool_call_update","toolCallId":"c1","status":"in_progress",'
            '"content":[{"type":"content","content":{"type":"text","text":"streamed"}}],'
            '"rawOutput":null,"locations":[]}\n'
        )
        later = (
            '{"type":"tool_call_update","toolCallId":"c1","status":"in_progress",'
            '"content":[{"type":"content","content":{"type":"text","text":"streamed more"}}],'
            '"rawOutput":null,"locations":[]}\n'
        )
        done = (
            '{"type":"tool_call_update","toolCallId":"c1","status":"completed",'
            '"content":[],"rawOutput":null,"locations":[]}\n'
        )
        drafts, _parser = self._run([streamed, later, done])
        (result,) = _results(drafts)
        self.assertEqual(result["text"], "streamed more")
        self.assertIs(result["truncated"], False)

    def test_a_completion_that_omits_output_keeps_an_earlier_raw_output(self):
        streamed = (
            '{"type":"tool_call_update","toolCallId":"c1","status":"in_progress",'
            '"content":[],"rawOutput":"from-stream","locations":[]}\n'
        )
        done = (
            '{"type":"tool_call_update","toolCallId":"c1","status":"completed",'
            '"content":[],"rawOutput":null,"locations":[]}\n'
        )
        drafts, _parser = self._run([streamed, done])
        (result,) = _results(drafts)
        self.assertEqual((result["text"], result["truncated"]), ("from-stream", False))

    def test_a_completion_that_carries_output_uses_its_own(self):
        streamed = (
            '{"type":"tool_call_update","toolCallId":"c1","status":"in_progress",'
            '"content":[{"type":"content","content":{"type":"text","text":"old"}}],'
            '"rawOutput":null,"locations":[]}\n'
        )
        done = (
            '{"type":"tool_call_update","toolCallId":"c1","status":"completed",'
            '"content":[{"type":"content","content":{"type":"text","text":"new"}}],'
            '"rawOutput":null,"locations":[]}\n'
        )
        drafts, _parser = self._run([streamed, done])
        (result,) = _results(drafts)
        self.assertEqual((result["text"], result["truncated"]), ("new", False))


if __name__ == "__main__":
    unittest.main()
