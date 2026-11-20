"""Input lines (driver.v2's inputs plus interrupt) and output lines (whole, redacted, one per line)."""

from __future__ import annotations

import io
import json
import unittest

from recursion_core.loop import protocol as p
from recursion_core.loop.tests.support import FAKE_KEY, Lines


class Inputs(unittest.TestCase):
    def test_the_four_kinds(self) -> None:
        self.assertEqual(p.parse_input('{"kind":"user_message","turn_id":"turn-000c","text":"Fix it.",'
                                       '"delivery_id":"dlv-0007"}'), p.UserMessage("turn-000c", "Fix it.", "dlv-0007"))
        self.assertEqual(p.parse_input(b'{"kind":"approval_answer","approval_id":"apr-00c4","decision":"deny"}'),
                         p.ApprovalAnswer("apr-00c4", "deny"))
        self.assertEqual(p.parse_input('{"kind":"interrupt","turn_id":"turn-000c"}'), p.Interrupt("turn-000c"))
        self.assertEqual(p.parse_input('{"kind":"close"}'), p.Close())

    def test_bad_lines_are_typed_refusals(self) -> None:
        cases = {
            b"\xff": "not_utf8", "nope": "not_json", "[1]": "not_an_object", '{"kind":"start"}': "unknown_kind",
            '{"kind":"close","x":1}': "unknown_field",
            '{"kind":"user_message","turn_id":"turn-1","text":"t"}': "missing_field",
            '{"kind":"user_message","turn_id":"t1","text":"t","delivery_id":"dlv-1"}': "bad_turn_id",
            '{"kind":"user_message","turn_id":"turn-1","text":"t","delivery_id":"d-1"}': "bad_delivery_id",
            '{"kind":"user_message","turn_id":"turn-1","text":"","delivery_id":"dlv-1"}': "bad_text",
            '{"kind":"approval_answer","approval_id":"apr-1","decision":"maybe"}': "bad_decision",
            '{"kind":"approval_answer","approval_id":"x-1","decision":"approve"}': "bad_approval_id",
            '{"kind":"user_message","turn_id":"turn-1\\n","text":"t","delivery_id":"dlv-1"}': "bad_turn_id",
            '{"kind":"user_message","turn_id":"turn-1","text":"t","delivery_id":"dlv-1\\n"}': "bad_delivery_id",
            '{"kind":"approval_answer","approval_id":"apr-1\\n","decision":"approve"}': "bad_approval_id",
            '{"kind":"interrupt","turn_id":"turn-1\\n"}': "bad_turn_id",
        }
        for line, code in cases.items():
            with self.subTest(line=line), self.assertRaises(p.InputError) as cm:
                p.parse_input(line)
            self.assertEqual(cm.exception.code, code)

    def test_lines_are_read_whole_and_an_overlong_one_is_skipped(self) -> None:
        got: list[bytes] = []
        long_count = []
        old = p.MAX_INPUT_LINE
        p.MAX_INPUT_LINE = 16
        try:
            stream = io.BytesIO(b'{"kind":"close"}\n' + b"x" * 40 + b"\n\n" + b'{"a":1}\r\n')
            p.read_lines(stream, got.append, lambda: long_count.append(1))
        finally:
            p.MAX_INPUT_LINE = old
        self.assertEqual(got, [b'{"kind":"close"}', b'{"a":1}'])
        self.assertEqual(long_count, [1])


class Outputs(unittest.TestCase):
    def test_one_object_per_line_with_the_key_removed(self) -> None:
        lines = Lines([FAKE_KEY])
        lines.writer.write("text", turn_id="turn-1", text=f"the key is {FAKE_KEY}\nand more")
        lines.writer.write("tool_call", turn_id="turn-1", call_id="toolu_1", name="read",
                           input={"path": FAKE_KEY, FAKE_KEY: [FAKE_KEY]})
        raw = lines.raw()
        self.assertNotIn(FAKE_KEY, raw)
        self.assertEqual(raw.count("\n"), 2, "a newline in text is escaped; one line per object")
        docs = [json.loads(line) for line in raw.splitlines()]
        self.assertEqual(docs[0], {"type": "text", "turn_id": "turn-1", "text": "the key is [redacted]\nand more"})
        self.assertEqual(docs[1]["input"], {"path": "[redacted]", "[redacted]": ["[redacted]"]})

    def test_a_secret_json_would_escape_is_removed_too(self) -> None:
        secret = 'sk-has"quote\\and-slash-1234'
        lines = Lines([secret])
        lines.writer.write("text", turn_id="turn-1", text=f"<{secret}>")
        self.assertEqual(lines.all()[0]["text"], "<[redacted]>")
        self.assertNotIn(json.dumps(secret)[1:-1], lines.raw())

    def test_a_short_secret_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            p.Redactor(["abc"])


if __name__ == "__main__":
    unittest.main()
