"""Operator inspect reads: envelope, typed codes, row labelling, and no path to the run control API."""

from __future__ import annotations

import ast
import json
import pathlib
import threading
import unittest

from recursion_core.control import operator_reads
from recursion_core.control.operator_reads import NO_PROFILE, OperatorReads, connect, refusal
from recursion_core.control.tests.fakes import FakeKillTree, make_api

POSITIONS, DECISIONS, BUDGETS = (
    "operator.positions.inspect", "operator.decisions.inspect", "operator.budgets.inspect")
ENVELOPE_KEYS = {"api_version", "command", "request_id", "profile", "authority_effect", "order_path", "code",
                 "status_code", "result", "errors", "stale", "unreconciled", "served_at"}
CONTROL_DIR = pathlib.Path(operator_reads.__file__).parent


def source(rows, venue="paper", **more):
    return lambda: {"venue": venue, "rows": rows, **more}


def composed(**more) -> OperatorReads:
    return OperatorReads({
        "positions": source([{"instrument_id": "AAA", "quantity": "2", "currency": "USD"}],
                            extra={"collateral": {"currency": "USD", "amount": "10.00", "state": "known"}}),
        "decisions": source([{"decision_id": "d1", "outcome": "recorded", "reason_code": "proposal_only"}]),
        "budgets": source([{"budget_id": "b1", "currency": "USD", "remaining": "0.00", "scope": "profile"}]),
    }, clock=lambda: 1000.0, **more)


class EnvelopeTest(unittest.TestCase):
    def assert_envelope(self, reply: dict, command, code: str) -> None:
        self.assertLessEqual(ENVELOPE_KEYS, set(reply))
        self.assertLessEqual(set(reply) - ENVELOPE_KEYS, {"empty_reason", "detail"})
        self.assertEqual((reply["api_version"], reply["command"]), ("o7.operator.v0", command))
        self.assertEqual((reply["code"], reply["status_code"]), (code, code))
        self.assertEqual((reply["authority_effect"], reply["order_path"]), ("none", "none"))
        self.assertIs(type(reply["stale"]), bool)
        self.assertIs(type(reply["unreconciled"]), bool)
        if code == "ok":
            self.assertEqual(reply["errors"], [])
        else:
            self.assertEqual([set(e) for e in reply["errors"]], [{"code", "status_code", "message", "recovery"}])
            self.assertEqual((reply["errors"][0]["code"], reply["errors"][0]["status_code"]), (code, code))
        json.dumps(reply, allow_nan=False)

    def test_three_reads_answer_ok_with_labelled_rows(self) -> None:
        reads = composed()
        for method, key, first in ((POSITIONS, "positions", "instrument_id"), (DECISIONS, "decisions", "decision_id"),
                                   (BUDGETS, "budgets", "budget_id")):
            for params in ({}, None):
                reply = reads.call(method, params)
                self.assert_envelope(reply, method, "ok")
                rows = reply["result"][key]
                self.assertEqual(len(rows), 1)
                self.assertIn(first, rows[0])
                self.assertEqual(rows[0]["venue"], "paper")
                self.assertNotIn("empty_reason", reply)
                self.assertEqual((reply["stale"], reply["unreconciled"], reply["served_at"]), (False, False, 1000.0))
        positions = reads.call(POSITIONS, {})["result"]
        self.assertEqual(positions["positions"],
                         [{"instrument_id": "AAA", "quantity": "2", "currency": "USD", "venue": "paper"}])
        self.assertEqual(positions["collateral"], {"currency": "USD", "amount": "10.00", "state": "known"})
        self.assertEqual(set(reads.call(BUDGETS, {})["result"]), {"budgets"})

    def test_request_ids_are_unique(self) -> None:
        reads, ids = composed(), []

        def read() -> None:
            ids.extend(reads.call(POSITIONS, {})["request_id"] for _ in range(50))

        threads = [threading.Thread(target=read) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(set(ids)), 400)
        self.assertTrue(all(type(i) is str and i.startswith("req_") for i in ids))

    def test_profile_is_the_hosts_or_a_truthful_default(self) -> None:
        self.assertEqual(OperatorReads().call(POSITIONS, {})["profile"],
                         {"profile_id": None, "signature_state": "no_profile"})
        given = {"profile_id": "p-1", "profile_class": "A", "signature_state": "unsigned"}
        reads = composed(profile=given)
        reply = reads.call(BUDGETS, {})
        self.assertEqual(reply["profile"], given)
        reply["profile"]["profile_id"] = "changed"
        given["profile_id"] = "changed too"
        self.assertEqual(reads.call(BUDGETS, {})["profile"]["profile_id"], "p-1")
        self.assertEqual(reads.call("run.stop", {})["profile"]["profile_id"], "p-1")
        self.assertEqual(NO_PROFILE, {"profile_id": None, "signature_state": "no_profile"})
        with self.assertRaises(ValueError):
            OperatorReads(profile={"x": object()})
        with self.assertRaises(TypeError):
            OperatorReads(profile=["p-1"])

    def test_unsourced_reads_answer_ok_empty_with_a_reason(self) -> None:
        reads = OperatorReads()
        for method, key in ((POSITIONS, "positions"), (DECISIONS, "decisions"), (BUDGETS, "budgets")):
            reply = reads.call(method, {})
            self.assert_envelope(reply, method, "ok")
            self.assertEqual(reply["result"], {key: []})
            self.assertEqual(reply["empty_reason"], f"no_{key}_source_attached")
        partly = OperatorReads({"budgets": source([{"budget_id": "b1"}])})
        self.assertEqual(partly.call(POSITIONS, {})["empty_reason"], "no_positions_source_attached")
        self.assertEqual(len(partly.call(BUDGETS, {})["result"]["budgets"]), 1)

    def test_a_source_with_no_rows_answers_ok_empty_with_a_reason(self) -> None:
        reply = OperatorReads({"positions": source([])}).call(POSITIONS, {})
        self.assert_envelope(reply, POSITIONS, "ok")
        self.assertEqual((reply["result"], reply["empty_reason"]), ({"positions": []}, "source_reported_no_rows"))
        named = OperatorReads({"positions": source([], empty_reason="book_is_flat", extra={"collateral": {"amount": "0"}})})
        reply = named.call(POSITIONS, {})
        self.assertEqual((reply["code"], reply["empty_reason"]), ("ok", "book_is_flat"))
        self.assertEqual(reply["result"], {"positions": [], "collateral": {"amount": "0"}})

    def test_stale_or_unreconciled_keeps_the_rows_and_says_so(self) -> None:
        for flags in ({"stale": True}, {"unreconciled": True}, {"stale": True, "unreconciled": True}):
            reply = OperatorReads({"decisions": source([{"decision_id": "d1"}], **flags)}).call(DECISIONS, {})
            self.assert_envelope(reply, DECISIONS, "stale_or_unreconciled")
            self.assertEqual(reply["result"]["decisions"], [{"decision_id": "d1", "venue": "paper"}])
            self.assertEqual((reply["stale"], reply["unreconciled"]),
                             (flags.get("stale", False), flags.get("unreconciled", False)))


class LabellingTest(unittest.TestCase):
    def read(self, rows, venue="paper", **more) -> dict:
        return OperatorReads({"positions": source(rows, venue, **more)}).call(POSITIONS, {})

    def assert_withheld(self, reply: dict, code: str) -> None:
        self.assertEqual((reply["code"], reply["status_code"], reply["result"]), (code, code, {}))
        self.assertNotIn("empty_reason", reply)
        self.assertEqual(reply["errors"][0]["code"], code)

    def test_rows_get_the_source_venue(self) -> None:
        for venue in ("paper", "stock"):
            reply = self.read([{"instrument_id": "AAA"}, {"instrument_id": "BBB", "venue": venue}], venue)
            self.assertEqual(reply["code"], "ok")
            self.assertEqual([r["venue"] for r in reply["result"]["positions"]], [venue, venue])

    def test_the_sources_rows_are_not_changed(self) -> None:
        rows = [{"instrument_id": "AAA"}]
        reply = self.read(rows)
        self.assertEqual(rows, [{"instrument_id": "AAA"}])
        reply["result"]["positions"][0]["instrument_id"] = "changed"
        self.assertEqual(rows, [{"instrument_id": "AAA"}])

    def test_a_row_of_another_venue_withholds_the_whole_read(self) -> None:
        good = {"instrument_id": "AAA"}
        for bad in ({"instrument_id": "X", "venue": "stock"}, {"instrument_id": "X", "venue": "elsewhere"},
                    {"instrument_id": "X", "venue": None}, {"instrument_id": "X", "venue": 1}):
            reply = self.read([good, bad])
            self.assert_withheld(reply, "venue_refused")
            self.assertEqual(reply["detail"], "row 1: venue")

    def test_a_source_of_another_venue_is_refused(self) -> None:
        for venue in ("elsewhere", "", "PAPER", None, 3, ["paper"]):
            self.assert_withheld(self.read([{"instrument_id": "AAA"}], venue), "venue_refused")
        self.assert_withheld(OperatorReads({"positions": lambda: {"rows": []}}).call(POSITIONS, {}), "venue_refused")

    def test_a_row_that_is_not_flat_json_scalars_withholds_the_whole_read(self) -> None:
        class Sub(dict):
            pass

        good = {"instrument_id": "AAA", "quantity": 1, "price": 1.5, "open": True, "note": None}
        self.assertEqual(self.read([good])["code"], "ok")
        cases = {
            "nested dict": {"instrument_id": "X", "leg": {"quantity": 1}},
            "list value": {"instrument_id": "X", "legs": [1]},
            "nan": {"instrument_id": "X", "price": float("nan")},
            "inf": {"instrument_id": "X", "price": float("inf")},
            "-inf": {"instrument_id": "X", "price": float("-inf")},
            "bytes": {"instrument_id": "X", "raw": b"x"},
            "camelCase key": {"instrumentId": "X"},
            "non-str key": {1: "X"},
            "empty key": {"": "X"},
            "dunder key": {"_private": "X"},
            "not a dict": ["instrument_id", "X"],
            "dict subclass": Sub(instrument_id="X"),
            "none": None,
        }
        for name, bad in cases.items():
            with self.subTest(name):
                reply = self.read([good, bad])
                self.assert_withheld(reply, "row_invalid")
                self.assertTrue(reply["detail"].startswith("row 1: "))
                json.dumps(reply, allow_nan=False)

    def test_a_source_answer_of_the_wrong_shape_is_source_invalid(self) -> None:
        answers = (
            None, [], "rows",
            {"venue": "paper"},
            {"venue": "paper", "rows": "abc"},
            {"venue": "paper", "rows": {"a": 1}},
            {"venue": "paper", "rows": [], "stale": "yes"},
            {"venue": "paper", "rows": [], "unreconciled": 1},
            {"venue": "paper", "rows": [], "empty_reason": ""},
            {"venue": "paper", "rows": [], "empty_reason": 3},
            {"venue": "paper", "rows": [{"a": 1}], "empty_reason": "but there are rows"},
            {"venue": "paper", "rows": [], "extra": [1]},
            {"venue": "paper", "rows": [], "extra": {"positions": []}},
            {"venue": "paper", "rows": [], "extra": {"Bad Key": 1}},
            {"venue": "paper", "rows": [], "extra": {"collateral": {"amount": float("nan")}}},
            {"venue": "paper", "rows": [], "extra": {"collateral": object()}},
            {"venue": "paper", "rows": [], "surprise": 1},
        )
        for answer in answers:
            with self.subTest(answer=repr(answer)):
                reply = OperatorReads({"positions": lambda answer=answer: answer}).call(POSITIONS, {})
                self.assert_withheld(reply, "source_invalid")

    def test_a_raising_source_is_source_unavailable_and_its_message_stays_out(self) -> None:
        def broken():
            raise KeyError("secret-account-17")

        reply = OperatorReads({"budgets": broken}).call(BUDGETS, {})
        self.assert_withheld(reply, "source_unavailable")
        self.assertEqual(reply["detail"], "KeyError")
        self.assertNotIn("secret-account-17", json.dumps(reply))

    def test_a_raising_clock_does_not_break_a_read(self) -> None:
        def clock():
            raise OSError("no clock")

        for bad in (clock, lambda: float("nan"), lambda: "now"):
            reply = OperatorReads({"budgets": source([{"budget_id": "b1"}])}, clock=bad).call(BUDGETS, {})
            self.assertEqual((reply["code"], reply["served_at"]), ("ok", None))


class GetOnlyTest(unittest.TestCase):
    def test_any_other_method_is_method_not_allowed_and_no_source_is_called(self) -> None:
        calls = []

        def counted():
            calls.append(1)
            return {"venue": "paper", "rows": []}

        reads = OperatorReads({"positions": counted, "decisions": counted, "budgets": counted})
        methods = ("run.start", "run.stop", "run.list", "run.status", "operator.orders.inspect",
                   "operator.positions.close", "operator.positions.inspect ", "OPERATOR.POSITIONS.INSPECT",
                   "positions", "", None, 7, ["operator.positions.inspect"], {"a": 1}, "x" * 100_000)
        for method in methods:
            with self.subTest(method=repr(method)[:40]):
                reply = reads.call(method, {})
                self.assertEqual((reply["code"], reply["status_code"], reply["result"]),
                                 ("method_not_allowed", "method_not_allowed", {}))
                self.assertEqual(reply["command"], method if type(method) is str and len(method) <= 128 else None)
                json.dumps(reply, allow_nan=False)
        self.assertEqual(calls, [])

    def test_params_other_than_empty_are_request_invalid(self) -> None:
        calls = []
        reads = OperatorReads({"positions": lambda: calls.append(1) or {"venue": "paper", "rows": []}})
        for params in ({"venue": "paper"}, {"run_id": "r1"}, [], [1], "x", 0, False, ()):
            with self.subTest(params=params):
                reply = reads.call(POSITIONS, params)
                self.assertEqual((reply["code"], reply["status_code"], reply["result"]),
                                 ("request_invalid", "request_invalid", {}))
        self.assertEqual(calls, [])

    def test_the_run_control_api_is_never_touched(self) -> None:
        api, registry, kill, starter = make_api(FakeKillTree())
        reads = composed()
        for method, params in (("run.start", {"tenant": "t1", "engine": "codex", "prompt": "p"}),
                               ("run.stop", {"run_id": "run-1"}), ("run.stop", {}), ("run.list", {})):
            self.assertIn(reads.call(method, params)["code"], ("method_not_allowed",))
        self.assertEqual((starter.calls, kill.requests, kill.confirms, registry.list()), ([], [], [], []))
        self.assertEqual(api.call("run.list", {})["code"], "ok")

    def test_refusal_builds_only_non_ok_envelopes(self) -> None:
        reply = refusal("busy")
        self.assertEqual((reply["code"], reply["status_code"], reply["result"], reply["command"]),
                         ("busy", "busy", {}, None))
        for code in ("ok", "stopped", "control_api_unavailable"):
            with self.assertRaises(ValueError):
                refusal(code)

    def test_constructor_rejects_unknown_reads_and_non_callables(self) -> None:
        with self.assertRaises(ValueError):
            OperatorReads({"orders": lambda: {}})
        with self.assertRaises(ValueError):
            OperatorReads({"operator.positions.inspect": lambda: {}})
        with self.assertRaises(TypeError):
            OperatorReads({"positions": {"venue": "paper", "rows": []}})


class StructureTest(unittest.TestCase):
    """The reads modules cannot reach run.start, run.stop or the ControlAPI: they never import them."""

    MODULES = ("operator_reads.py", "operator_reads_socket.py")
    STDLIB = {"__future__", "collections.abc", "json", "math", "os", "re", "socket", "stat", "struct",
              "threading", "time", "uuid"}
    OWN = {"recursion_core.control.operator_reads", "recursion_core.control.operator_reads_socket"}

    def trees(self):
        for name in self.MODULES:
            yield name, ast.parse((CONTROL_DIR / name).read_text(encoding="utf-8"))

    def test_imports_are_stdlib_or_the_reads_modules_only(self) -> None:
        for name, tree in self.trees():
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    modules = {alias.name for alias in node.names}
                elif isinstance(node, ast.ImportFrom):
                    self.assertEqual(node.level, 0, f"{name}: relative import")
                    modules = {node.module}
                else:
                    continue
                self.assertLessEqual(modules, self.STDLIB | self.OWN, f"{name} imports {modules}")

    def test_no_run_method_name_or_control_api_identifier(self) -> None:
        for name, tree in self.trees():
            for node in ast.walk(tree):
                if isinstance(node, ast.Name):
                    ident = node.id
                elif isinstance(node, ast.Attribute):
                    ident = node.attr
                elif isinstance(node, ast.alias):
                    ident = node.name
                else:
                    ident = ""
                for banned in ("ControlAPI", "KillTree", "RunRegistry", "call_tool", "importlib", "__import__",
                               "getattr_static", "eval", "exec"):
                    self.assertNotEqual(ident.rsplit(".", 1)[-1], banned, f"{name} uses {banned}")
                if isinstance(node, ast.Constant) and isinstance(node.value, str) and node is not _docstring(tree):
                    for banned in ("run.start", "run.stop", "run.list", "run.status"):
                        self.assertNotIn(banned, node.value, f"{name} names {banned}")

    def test_the_method_table_is_exactly_the_three_reads(self) -> None:
        self.assertEqual(operator_reads.METHODS, {POSITIONS: "positions", DECISIONS: "decisions", BUDGETS: "budgets"})
        public = {n for n in vars(OperatorReads) if not n.startswith("_")}
        self.assertEqual(public, {"call", "profile"})

    def test_connect_builds_a_client_without_opening_anything(self) -> None:
        client = connect("/nonexistent/recursion/operator-reads.sock", timeout_s=0.2)
        self.assertEqual({n for n in dir(client) if not n.startswith("_")}, {"call"})
        reply = client.call("run.stop", {"run_id": "run-1"})
        self.assertEqual((reply["code"], reply["delivered"]), ("method_not_allowed", False))


def _docstring(tree: ast.Module):
    first = tree.body[0] if tree.body else None
    return first.value if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) else None


if __name__ == "__main__":
    unittest.main()
