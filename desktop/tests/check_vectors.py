"""The parity vectors the desktop's frontend is held to are true to the CLI's and the TUI's own code.

fixtures/conditions.json, walks.json and sanitize.json are read here against ``tui.protocol`` and
``tui.sanitize``; desktop/tests/js holds the desktop's JavaScript to the same files.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from recursion_core.tui import protocol, sanitize

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class ConditionsTest(unittest.TestCase):
    def test_each_answer_shows_the_conditions_the_tui_shows(self) -> None:
        doc = load("conditions.json")
        self.assertEqual(doc["labels"], list(protocol.LABELS))
        for vector in doc["vectors"]:
            with self.subTest(vector["name"]):
                got = [list(c) for c in protocol.keyed_conditions(vector["answer"])]
                self.assertEqual(got, vector["conditions"])

    def test_every_label_is_shown_by_some_vector(self) -> None:
        shown = {c[0] for v in load("conditions.json")["vectors"] for c in v["conditions"]}
        self.assertEqual(shown, set(protocol.LABELS))


class WalkTest(unittest.TestCase):
    def test_each_walk_makes_the_calls_and_shows_the_list_the_cli_does(self) -> None:
        for vector in load("walks.json")["vectors"]:
            with self.subTest(vector["name"]):
                calls: list = []

                def call(method, params, pages=vector["pages"]):
                    calls.append([method, dict(params)])
                    return pages[len(calls) - 1]

                self.assertEqual(protocol.list_all_runs(call), vector["result"])
                self.assertEqual(calls, vector["calls"])

    def test_the_walks_cover_every_refusal_the_cli_can_give(self) -> None:
        details = {v["result"].get("detail", "").split(":")[0] for v in load("walks.json")["vectors"]
                   if v["result"].get("code") == protocol.LIST_INCOMPLETE}
        self.assertEqual(details, {"runs_not_a_list", "run_without_id", "run_listed_twice", "cursor_not_advancing"})


class SanitizeTest(unittest.TestCase):
    def test_each_text_is_shown_as_the_tui_shows_it(self) -> None:
        for vector in load("sanitize.json")["vectors"]:
            with self.subTest(vector["name"]):
                self.assertEqual(sanitize.sanitize(vector["text"]), vector["sanitize"])
                self.assertEqual(sanitize.field(vector["text"]), vector["field"])

    def test_no_shown_text_carries_a_control_or_invisible_character(self) -> None:
        for vector in load("sanitize.json")["vectors"]:
            for shown in (vector["sanitize"], vector["field"]):
                with self.subTest(vector["name"]):
                    self.assertFalse(any(sanitize._marked(ch) and ch != "\n" for ch in shown))


if __name__ == "__main__":
    unittest.main()
