"""sync_spine.py's rules, held to cases with no checkout and no ``jsonschema``.

A declared schema gap waives only the one error it names; any other error fails, beside it or alone, and so
does a gap the schema no longer shows. A SPINE-sourced action fails when its fixture is not the recorded
file, names another method, or differs in a key no delta names. The schema errors here are written in the
shape ``sync_spine._params_errors`` gives them, as jsonschema reported them against #368 for these params.
"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sync_spine

ACTIONS = json.loads((Path(__file__).resolve().parent / "fixtures" / "actions.json").read_text(encoding="utf-8"))
BY_NAME = {a["action"]: a for a in ACTIONS["actions"]}
APPROVAL_SURFACES = ["cli", "tui", "page", "phone"]
SURFACE = {"at": "surface", "rule": "enum", "value": "desktop", "allowed": APPROVAL_SURFACES,
           "message": "'desktop' is not one of ['cli', 'tui', 'page', 'phone']"}
DIGEST = {"at": "digest", "rule": "pattern", "value": "not-a-digest", "allowed": "^[0-9a-f]{64}$",
          "message": "'not-a-digest' does not match '^[0-9a-f]{64}$'"}
COMMAND_ID = {"at": "command_id", "rule": "pattern", "value": "bad", "allowed": "^[A-Za-z0-9._:-]{8,128}$",
              "message": "'bad' does not match '^[A-Za-z0-9._:-]{8,128}$'"}


class VerdictTest(unittest.TestCase):
    def setUp(self) -> None:
        self.gapped = BY_NAME["approval.answer"]
        self.plain = BY_NAME["run.open"]

    def test_the_declared_gap_alone_is_a_gap(self) -> None:
        found, said = sync_spine.verdict(self.gapped, [], [SURFACE])
        self.assertEqual(found, "GAP")
        self.assertEqual(said, [f"surface: {SURFACE['message']} (the declared gap)"])

    def test_another_error_beside_the_gap_fails(self) -> None:
        for other in (DIGEST, COMMAND_ID):
            with self.subTest(other["at"]):
                found, said = sync_spine.verdict(self.gapped, [], [other, SURFACE])
                self.assertEqual(found, "FAIL")
                self.assertIn(f"{other['at']}: {other['message']}", said)

    def test_another_error_alone_fails_with_or_without_a_gap(self) -> None:
        self.assertEqual(sync_spine.verdict(self.gapped, [], [DIGEST])[0], "FAIL")
        self.assertEqual(sync_spine.verdict(self.plain, [], [DIGEST])[0], "FAIL")

    def test_the_surface_error_without_a_declared_gap_fails(self) -> None:
        self.assertEqual(sync_spine.verdict(self.plain, [], [SURFACE])[0], "FAIL")

    def test_a_gap_the_schema_no_longer_shows_fails(self) -> None:
        found, said = sync_spine.verdict(self.gapped, [], [])
        self.assertEqual(found, "FAIL")
        self.assertTrue(any("no longer refuses" in s for s in said), said)

    def test_the_gap_is_not_waived_once_the_schema_lists_the_value(self) -> None:
        listed = {**SURFACE, "allowed": [*APPROVAL_SURFACES, "desktop"]}
        self.assertEqual(sync_spine.verdict(self.gapped, [], [listed])[0], "FAIL")

    def test_a_gap_must_match_place_rule_and_value(self) -> None:
        for change in ({"at": "digest"}, {"rule": "pattern"}, {"value": "page"}):
            with self.subTest(change):
                self.assertEqual(sync_spine.verdict(self.gapped, [], [{**SURFACE, **change}])[0], "FAIL")

    def test_only_a_known_gap_can_be_declared(self) -> None:
        for gap in ("no desktop surface", {"at": "digest", "rule": "pattern", "value": "not-a-digest", "why": "x"}):
            with self.subTest(gap):
                action = {**self.gapped, "schema_gap": gap}
                found, said = sync_spine.verdict(action, [], [DIGEST if isinstance(gap, dict) else SURFACE])
                self.assertEqual(found, "FAIL")
                self.assertTrue(any("is not one of" in s and "declared schema gap" in s for s in said), said)
        # And gap_waives itself waives nothing for a gap that is not known, even one shaped like the known one.
        unknown = {"at": "digest", "rule": "enum", "value": "x", "why": "not a known gap"}
        enum_error = {"at": "digest", "rule": "enum", "value": "x", "allowed": ["a"], "message": "'x' is not one of ['a']"}
        self.assertFalse(sync_spine.gap_waives(unknown, enum_error))

    def test_a_fixture_or_row_problem_fails_even_with_only_the_gap(self) -> None:
        self.assertEqual(sync_spine.verdict(self.gapped, ["x is not the recorded file"], [SURFACE])[0], "FAIL")

    def test_no_problem_and_no_error_is_ok(self) -> None:
        self.assertEqual(sync_spine.verdict(self.plain, [], []), ("OK", []))

    def test_the_actions_declare_only_known_gaps_and_only_on_the_approval_requests(self) -> None:
        gapped = {a["action"] for a in ACTIONS["actions"] if "schema_gap" in a}
        self.assertEqual(gapped, {"approval.open", "approval.answer"})
        for name in gapped:
            self.assertIn(sync_spine._gap_key(BY_NAME[name]["schema_gap"]), sync_spine.KNOWN_GAPS)


class CheckTest(unittest.TestCase):
    """``sync_spine.check`` itself, end to end: only what needs #368 is stood in for (the checkout's head, the
    schema registry and validator, the rows and answers it would build, the fixture files), so each case runs
    without a checkout or jsonschema. The recorded rows and answers it compares with are this folder's own."""

    def setUp(self) -> None:
        self.checkout = Path(tempfile.mkdtemp(prefix="desktop-sync-check-"))
        schemas = self.checkout / sync_spine.SCHEMA_DIR
        schemas.mkdir(parents=True)
        (schemas / "methods.v1.json").write_text(json.dumps({"$id": "methods.v1.json"}), encoding="utf-8")
        fixtures = Path(__file__).resolve().parent / "fixtures"
        self.rows = json.loads((fixtures / "spine-rows.json").read_text(encoding="utf-8"))
        self.answers = json.loads((fixtures / "answers.json").read_text(encoding="utf-8"))

    def run_check(self, *, head=None, sources=None, errors=None, answers=None) -> tuple[int, str]:
        def schema_errors(registry, methods_id, method, params):
            return (errors or {}).get(method, [SURFACE] if method in ("approval.open", "approval.answer") else [])

        out = io.StringIO()
        with mock.patch.object(sync_spine, "_checkout_head", return_value=head or ACTIONS["spine_head"]), \
                mock.patch.object(sync_spine, "_registry", return_value=None), \
                mock.patch.object(sync_spine, "_params_errors", side_effect=schema_errors), \
                mock.patch.object(sync_spine, "build_rows", return_value=copy.deepcopy(self.rows)), \
                mock.patch.object(sync_spine, "build_answers", return_value=answers or copy.deepcopy(self.answers)), \
                mock.patch.object(sync_spine, "source_problems", side_effect=sources or (lambda action, where: [])), \
                contextlib.redirect_stdout(out):
            code = sync_spine.check(self.checkout, False)
        return code, out.getvalue()

    def test_a_true_checkout_gives_nineteen_ok_and_the_two_gaps(self) -> None:
        code, out = self.run_check()
        self.assertEqual(code, 0, out)
        said = {}
        for line in out.splitlines():
            if line.split()[0] in ("OK", "GAP", "FAIL") and line.split()[1] in BY_NAME:
                said[line.split()[1]] = line.split()[0]
        self.assertEqual(set(said), set(BY_NAME))
        self.assertEqual(sorted(name for name, verdict in said.items() if verdict == "GAP"), ["approval.answer", "approval.open"])
        self.assertEqual(sum(verdict == "OK" for verdict in said.values()), 19)
        self.assertIn("OK   answers.json matches this checkout", out)

    def test_a_checkout_at_another_head_fails_before_anything_else(self) -> None:
        code, out = self.run_check(head="0" * 40)
        self.assertEqual(code, 1)
        self.assertTrue(out.startswith("FAIL the checkout is at " + "0" * 40), out)

    def test_a_fixture_that_is_not_the_recorded_file_fails_the_check(self) -> None:
        def sources(action, where):
            return ["h07-approval-open.json is not the recorded file"] if action["action"] == "approval.open" else []
        code, out = self.run_check(sources=sources)
        self.assertEqual(code, 1)
        self.assertRegex(out, r"(?m)^FAIL approval\.open .*h07-approval-open\.json is not the recorded file")

    def test_another_schema_error_beside_the_gap_fails_the_check(self) -> None:
        code, out = self.run_check(errors={"approval.answer": [DIGEST, SURFACE]})
        self.assertEqual(code, 1)
        self.assertRegex(out, r"(?m)^FAIL approval\.answer .*digest: 'not-a-digest' does not match")

    def test_answers_that_differ_from_the_recorded_ones_fail_the_check(self) -> None:
        drifted = copy.deepcopy(self.answers)
        drifted["answers"][0]["answer"]["code"] = "pending"
        code, out = self.run_check(answers=drifted)
        self.assertEqual(code, 1)
        self.assertIn("FAIL answers.json differs from this checkout", out)
        self.assertIn("OK   spine-rows.json matches this checkout", out)

    def test_an_answer_recorded_for_another_method_fails_the_check(self) -> None:
        wrong = copy.deepcopy(self.answers)
        wrong["answers"][0]["method"] = "session.list"
        code, out = self.run_check(answers=wrong)
        self.assertEqual(code, 1)
        self.assertIn(f"FAIL {wrong['answers'][0]['file']} answers session.list", out)


class RowProblemsTest(unittest.TestCase):
    def test_rows(self) -> None:
        good = {"page_allowed": True, "scope": "read"}
        self.assertEqual(sync_spine.row_problems("run.list", good), [])
        self.assertEqual(sync_spine.row_problems("run.list", None), ["run.list has no row in the table"])
        self.assertEqual(sync_spine.row_problems("x", {**good, "page_allowed": False}), ["x is not allowed to the page"])
        self.assertEqual(sync_spine.row_problems("x", {**good, "scope": "manage"}), ["x is of the manage scope"])


class SourceProblemsTest(unittest.TestCase):
    """A stand-in fixture folder: the action's fixture written so that its hash is the one recorded."""

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="desktop-sync-"))
        self.action = copy.deepcopy(BY_NAME["approval.open"])
        self.fixture = {"method": "approval.open", "params": {**self.action["params"], "command_id": "tui-7c1e.0042",
                                                               "surface": "tui"}, "answer": {"code": "ok"}}
        self.write(self.fixture)

    def write(self, fixture: dict, *, record: bool = True) -> None:
        data = json.dumps(fixture).encode("utf-8")
        (self.dir / self.action["source"]["file"]).write_bytes(data)
        if record:
            self.action["source"]["sha256"] = hashlib.sha256(data).hexdigest()

    def test_the_recorded_file_with_named_deltas_has_no_problem(self) -> None:
        self.assertEqual(sync_spine.source_problems(self.action, self.dir), [])

    def test_a_file_that_is_not_the_recorded_one_is_a_problem(self) -> None:
        self.write({**self.fixture, "answer": {"code": "pending"}}, record=False)
        self.assertEqual(sync_spine.source_problems(self.action, self.dir),
                         [f"{self.action['source']['file']} is not the recorded file"])

    def test_a_missing_file_is_a_problem(self) -> None:
        (self.dir / self.action["source"]["file"]).unlink()
        self.assertEqual(sync_spine.source_problems(self.action, self.dir),
                         [f"{self.action['source']['file']} is not in the checkout"])

    def test_another_method_or_an_unnamed_difference_or_an_empty_delta_is_a_problem(self) -> None:
        self.write({**self.fixture, "method": "approval.list"})
        self.assertIn("the fixture's method is approval.list", sync_spine.source_problems(self.action, self.dir))
        self.write({**self.fixture, "params": {**self.fixture["params"], "approval_id": "apr-0001"}})
        self.assertIn("params.approval_id differs from the fixture and no delta names it",
                      sync_spine.source_problems(self.action, self.dir))
        self.write({**self.fixture, "params": {**self.fixture["params"], "surface": "desktop"}})
        self.assertIn("delta surface is named but the value is the fixture's", sync_spine.source_problems(self.action, self.dir))

    def test_an_action_from_the_cli_has_no_fixture(self) -> None:
        self.assertEqual(sync_spine.source_problems(BY_NAME["run.open"], self.dir), [])


if __name__ == "__main__":
    unittest.main()
