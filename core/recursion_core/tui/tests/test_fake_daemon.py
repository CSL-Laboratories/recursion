"""The TUI's fake daemon keeps to SPINE's published records and to rule (7) of methods.v1.json (reading a
log), so the TUI's later tests rest on the contract and not on a guess (TUI brief v3.3, build step 3).

The schema files and fixtures read here are SPINE's (#368, merged in at its pinned head c77aa2e0).
"""

from __future__ import annotations

import unittest

from recursion_core.tui.tests.fake_daemon import FakeDaemon, exchange, gap_line, spine


class ServedRecordsTest(unittest.TestCase):
    def test_every_served_answer_is_a_spine_fixture_verbatim(self) -> None:
        daemon = FakeDaemon()
        for method, name in (("session.list", "h27-session-list-ok"), ("approval.list", "h10-approval-list-returns-summaries"),
                             ("approval.open", "h07-approval-open"), ("queue.update", "h29-queue-update-ok"),
                             ("queue.cancel", "h30-queue-cancel-ok"), ("session.interrupt", "h23-session-interrupt-ok"),
                             ("workspace.diff", "h36-workspace-diff-ok")):
            with self.subTest(method=method):
                self.assertEqual(daemon.call(method, {}), exchange(name)["answer"])

    def test_the_approval_open_answer_carries_rendered_lines(self) -> None:
        rendered = FakeDaemon().call("approval.open", {})["approval"]["rendered"]
        self.assertTrue(rendered and all(set(line) == {"text", "trust"} for line in rendered))

    def test_methods_list_lists_every_row_and_says_what_is_built(self) -> None:
        daemon = FakeDaemon(implemented=frozenset({"run.list", "session.list", "events.read"}))
        answer = daemon.call("methods.list")
        table = spine("methods.table.v1.json")
        self.assertEqual([m["name"] for m in answer["methods"]], [r["name"] for r in table["methods"]])
        self.assertEqual(answer["table_version"], table["table_version"])
        built = {m["name"] for m in answer["methods"] if m["implemented"]}
        self.assertEqual(built, {"session.list", "events.read"}, "run.list is not served: no override makes it built")
        self.assertEqual(daemon.call("events.follow", {}), {"code": "unsupported", "detail": "not_implemented"})
        self.assertEqual(daemon.call("session.nap", {})["code"], "method_unknown")

    def test_the_log_is_the_projection_vector(self) -> None:
        self.assertEqual(FakeDaemon().lines,
                         spine("vectors/transcript.projection/g01-a-log-and-its-lines.json")["lines"])


class ReadingALogTest(unittest.TestCase):
    """Rule (7) of methods.v1.json, statement by statement."""

    def setUp(self) -> None:
        self.daemon = FakeDaemon()
        self.first = {"epoch": 1, "session_id": self.daemon.session_id, "seq": 0}

    def read(self, cursor, **params):
        return self.daemon.call("events.read", {"cursor": cursor, **params})

    def test_a_first_read_returns_every_position_in_order_and_where_to_read_on(self) -> None:
        answer = self.read(self.first)
        self.assertEqual([ln["seq"] for ln in answer["lines"]], [1, 2, 3, 4, 5, 6, 7])
        self.assertEqual(answer["cursor"], {"epoch": 1, "session_id": self.daemon.session_id, "seq": 7})
        self.assertEqual((answer["projection"], answer["more"], answer["epoch_advanced"]), ("transcript", False, False))
        again = self.read(answer["cursor"])
        self.assertEqual((again["lines"], again["cursor"]), ([], answer["cursor"]), "an empty page keeps the cursor")

    def test_limit_and_more(self) -> None:
        answer = self.read(self.first, limit=3)
        self.assertEqual(([ln["seq"] for ln in answer["lines"]], answer["more"]), ([1, 2, 3], True))
        rest = self.read(answer["cursor"], limit=10)
        self.assertEqual(([ln["seq"] for ln in rest["lines"]], rest["more"]), ([4, 5, 6, 7], False))

    def test_an_older_epoch_goes_on_by_its_seq_and_says_so(self) -> None:
        cursor = self.read(self.first, limit=2)["cursor"]
        self.daemon.restart()
        answer = self.read(cursor)
        self.assertEqual([ln["seq"] for ln in answer["lines"]], [3, 4, 5, 6, 7])
        self.assertEqual((answer["cursor"]["epoch"], answer["epoch_advanced"]), (2, True))
        self.assertFalse(self.read(answer["cursor"])["epoch_advanced"])

    def test_the_refusals_of_rule_7(self) -> None:
        sid = self.daemon.session_id
        self.assertEqual(self.read({"epoch": 2, "session_id": sid, "seq": 0}),
                         {"code": "request_invalid", "detail": "cursor_epoch_ahead"})
        self.assertEqual(self.read({"epoch": 1, "session_id": sid, "seq": 8}),
                         {"code": "request_invalid", "detail": "cursor_ahead"})
        self.assertEqual(self.read({"epoch": 1, "session_id": "ses-nobody", "seq": 0})["code"], "session_unknown")
        self.assertEqual(self.read({"epoch": 1, "session_id": sid, "seq": 7})["lines"], [], "the high-water mark is a valid empty read")

    def test_forgotten_positions_are_one_gap_line_at_the_gaps_last_position(self) -> None:
        self.daemon.forget(3, 5)
        lines = self.read(self.first)["lines"]
        self.assertEqual([(ln["seq"], ln["event_type"]) for ln in lines],
                         [(1, "turn.started"), (2, "engine.output"), (5, "replay.gap"), (6, "interrupt"), (7, "turn.ended")])
        self.assertEqual(lines[2]["text"], "events 3 to 5 are not available (retention)")
        self.assertEqual(set(gap_line(3, 5, 0)), set(spine("fixtures/good/transcript.line.v1/h07-replay-gap.json")),
                         "a gap line has the fields of SPINE's gap fixture")

    def test_follow_answers_at_once_with_new_lines_or_a_timeout(self) -> None:
        cursor = self.read(self.first)["cursor"]
        empty = self.daemon.call("events.follow", {"cursor": cursor, "wait_ms": 20000})
        self.assertEqual((empty["lines"], empty.get("timed_out")), ([], True))
        self.daemon.append("engine.output", "more\n")
        more = self.daemon.call("events.follow", {"cursor": cursor, "wait_ms": 20000})
        self.assertEqual([ln["seq"] for ln in more["lines"]], [8])
        self.assertNotIn("timed_out", more)

    def test_a_projection_left_out_is_the_transcript(self) -> None:
        self.assertEqual(self.read(self.first)["projection"], "transcript")
        self.assertEqual(self.read(self.first, projection="raw")["code"], "unsupported")


if __name__ == "__main__":
    unittest.main()
