"""The parity vectors the desktop's frontend is held to are true to the CLI's and the TUI's own code.

fixtures/conditions.json, walks.json, sanitize.json and pystr.json are read here against ``tui.protocol``,
``tui.sanitize`` and Python's own ``str()``; desktop/tests/js holds the desktop's JavaScript to the same files.
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
    def test_the_frontend_stops_a_walk_at_the_tuis_page_ceiling(self) -> None:
        # desktop/tests/js/check_model.mjs walks to this ceiling; here it is held to the TUI's own constant.
        model = (Path(__file__).resolve().parent.parent / "ui" / "js" / "model.js").read_text(encoding="utf-8")
        self.assertIn(f"export const LIST_FOLLOW_MAX_PAGES = {protocol.LIST_FOLLOW_MAX_PAGES};", model)

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


class PyStrTest(unittest.TestCase):
    """fixtures/pystr.json: what Python's str() and the TUI's field() give for a parsed JSON value.
    desktop/tests/js/check_sanitize.mjs holds the desktop's pyStr and field to the same file."""

    def test_each_value_reads_as_pythons_str_of_it(self) -> None:
        for vector in load("pystr.json")["vectors"]:
            with self.subTest(vector["name"]):
                value = json.loads(vector["json"])
                self.assertEqual(str(value), vector["str"])
                self.assertEqual(sanitize.field(value), vector["field"])

    def test_the_vectors_cover_what_python_writes_differently(self) -> None:
        shown = "".join(v["str"] for v in load("pystr.json")["vectors"])
        for piece in ["e-07", "e+16", "1e-05", "\\n", "\\t", "\\r", "\\\\", "\\x07", "\\x85", "\\u200b", "\\U000f0000",
                      "[\"it's\"]", "\\'", "None", "True", "False"]:
            with self.subTest(piece):
                self.assertIn(piece, shown)

    def test_what_json_parse_has_dropped_is_named_not_hidden(self) -> None:
        # The desktop reads the answer with JSON.parse, which keeps no difference between 2 and 2.0 (and the rest
        # below). Each such value is listed with what Python shows and what the desktop shows instead.
        lost = load("pystr.json")["lost"]
        self.assertLessEqual({"2.0", "-0.0"}, {v["json"] for v in lost})
        for vector in lost:
            with self.subTest(vector["name"]):
                self.assertEqual(str(json.loads(vector["json"])), vector["python"])
                self.assertNotEqual(vector["python"], vector["desktop"])
                self.assertTrue(vector["why"])


if __name__ == "__main__":
    unittest.main()
