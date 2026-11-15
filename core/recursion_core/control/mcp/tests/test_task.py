"""The Task derivation against every vector of ``schemas/control/v1/vectors/task.derivation`` (SPINE's #368).

A vector holds a source (a run's view, or a session and one of its turns), its log, the approvals pending, a
Task, and ``expect``: exactly the fields of that Task that are not what the source says. For the vectors whose
``expect`` is empty the derived Task must equal the given one on every field the rule decides; for the others
the derived Task must differ from the given one in exactly those fields. ``status_message`` and
``poll_interval_ms`` are the server's and are not compared (``conformance/rules.py``, ``task_problems``).
"""

from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from recursion_core.control.mcp import task as T
from recursion_core.control.mcp.task import FIELDS, RULE_FIELDS, TaskUnavailable, differences, task_of_run, task_of_turn

ROOT = Path(__file__).resolve().parents[5]
VECTORS = ROOT / "schemas" / "control" / "v1" / "vectors" / "task.derivation"


def vectors() -> list[tuple[str, dict]]:
    return [(p.name, json.loads(p.read_text(encoding="ascii"))) for p in sorted(VECTORS.glob("*.json"))]


def derive(vector: dict) -> dict:
    if vector["source"] == "run":
        return task_of_run(vector["view"], vector["events"], vector["pending_approval_ids"])
    return task_of_turn(vector["session"], vector["turn_id"], vector["events"], vector["pending_approval_ids"])


def shape_problems(task: dict) -> list[str]:
    """mcp.task.v1's conditional rules (its allOf), as code: what a Task of each status and source must hold."""

    problems = []
    status, result = task["status"], task["result"]
    if task["source"] == "run" and any(task[k] is not None for k in ("session_id", "turn_id", "outcome", "expires_at")):
        problems.append("run task with a session, turn, outcome or expiry")
    if task["source"] == "run" and not isinstance(task["run_id"], str):
        problems.append("run task with no run")
    if task["source"] == "turn" and not (isinstance(task["session_id"], str) and isinstance(task["turn_id"], str)):
        problems.append("turn task with no session or turn")
    if status == "completed" and not (isinstance(result, dict) and result["code"] is None and not task["cancel_requested"]
                                      and task["outcome"] in ("candidate_done", "done", "waiting", "paused", None)):
        problems.append("completed")
    if status == "failed" and not (isinstance(result, dict) and isinstance(result["code"], str)
                                   and task["outcome"] in ("failed", None) and not task["cancel_requested"]):
        problems.append("failed")
    if status == "cancelled" and (task["outcome"] not in ("interrupted", "stopped", None) or task["cancel_requested"]):
        problems.append("cancelled")
    if status in ("completed", "failed", "cancelled", "working") and task["pending_approval_ids"]:
        problems.append("pending approvals outside input_required")
    if status in ("working", "input_required") and (result is not None or task["outcome"] is not None):
        problems.append("a live task with a result or an outcome")
    if status == "input_required" and not task["pending_approval_ids"]:
        problems.append("input_required with no approval")
    if task["created_at"] is not None:
        problems.append("a creation time: no source of this version establishes one")
    return problems


class VectorTest(unittest.TestCase):
    def test_the_vectors_are_there(self) -> None:
        names = [name for name, _ in vectors()]
        self.assertEqual(49, len(names), VECTORS)
        self.assertEqual(19, sum(n.startswith("r") for n in names))
        self.assertEqual(15, sum(n.startswith("t") for n in names))
        self.assertEqual(15, sum(n.startswith("x") for n in names))

    def test_each_vector(self) -> None:
        for name, vector in vectors():
            with self.subTest(vector=name):
                derived = derive(vector)
                self.assertEqual(sorted(vector["expect"]), differences(derived, vector["task"]))
                self.assertEqual([], shape_problems(derived))
                self.assertEqual(set(FIELDS), set(derived))

    def test_a_correct_vector_and_its_mistaken_twin_differ_only_where_expected(self) -> None:
        # x01 is r14 with the Task written wrong: the derivation of both sources is the same Task.
        by_name = dict(vectors())
        r14, x01 = by_name["r14-run-with-a-stop-pending.json"], by_name["x01-a-pending-stop-shown-as-cancelled.json"]
        self.assertEqual([], differences(derive(r14), derive(x01)))
        self.assertEqual(["cancel_requested", "status"], differences(derive(x01), x01["task"]))


class RunTest(unittest.TestCase):
    VIEW = {"run_id": "run-1", "state": "running", "stop": None}

    def test_a_pending_stop_never_reads_cancelled(self) -> None:
        for view in (dict(self.VIEW, state="stop_pending"), dict(self.VIEW, stop={"reason": "cgroup_populated"}),
                     dict(self.VIEW, state="starting", stop={"reason": None}), dict(self.VIEW, state="unknown", stop={})):
            with self.subTest(view=view):
                task = task_of_run(view)
                self.assertEqual(("working", True, None), (task["status"], task["cancel_requested"], task["result"]))
        self.assertEqual("stop pending: cgroup_populated",
                         task_of_run(dict(self.VIEW, stop={"reason": "cgroup_populated"}))["status_message"])
        self.assertEqual("stop pending", task_of_run(dict(self.VIEW, state="stop_pending"))["status_message"])

    def test_only_a_confirmed_stop_is_cancelled(self) -> None:
        task = task_of_run(dict(self.VIEW, state="stopped", stop={"reason": None}))
        self.assertEqual(("cancelled", False, None), (task["status"], task["cancel_requested"], task["status_message"]))

    def test_results(self) -> None:
        cases = [
            ({"status": "ok", "code": None, "detail": "d"}, "completed", {"code": None, "detail": "d", "result_ref": "run-1"}),
            ({"status": "error", "code": "plan_limit", "detail": "x"}, "failed", {"code": "plan_limit", "detail": "x", "result_ref": "run-1"}),
            ({"status": "ok", "code": "engine_error", "detail": ""}, "failed", {"code": "engine_error", "detail": "", "result_ref": "run-1"}),
            ({"status": "error", "code": None, "detail": ""}, "failed", {"code": "result_invalid", "detail": "", "result_ref": "run-1"}),
            ({"code": None, "detail": None}, "failed", {"code": "result_invalid", "detail": None, "result_ref": "run-1"}),
            (None, "failed", {"code": "result_unavailable", "detail": None, "result_ref": None}),
        ]
        for result, status, want in cases:
            with self.subTest(result=result):
                task = task_of_run(dict(self.VIEW, state="ended", result=result))
                self.assertEqual((status, want), (task["status"], task["result"]))

    def test_a_start_that_never_launched_keeps_only_a_kept_reason(self) -> None:
        self.assertIsNone(task_of_run(dict(self.VIEW, state="start_failed"))["result"]["detail"])
        kept = task_of_run(dict(self.VIEW, state="start_failed", start_error={"code": "route_not_configured", "detail": "x"}))
        self.assertEqual({"code": "start_failed", "detail": "route_not_configured", "result_ref": None}, kept["result"])

    def test_approvals_pending(self) -> None:
        task = task_of_run(self.VIEW, pending_approval_ids=["apr-1", "apr-2"])
        self.assertEqual(("input_required", ["apr-1", "apr-2"]), (task["status"], task["pending_approval_ids"]))
        self.assertEqual([], task_of_run(dict(self.VIEW, state="stopped"), pending_approval_ids=["apr-1"])["pending_approval_ids"])
        for bad in (["apr-1", "apr-1"], [""], [1]):
            with self.subTest(bad=bad), self.assertRaises(TaskUnavailable):
                task_of_run(self.VIEW, pending_approval_ids=bad)

    def test_updated_at_is_the_latest_state_change_and_created_at_is_never_given(self) -> None:
        events = [{"type": "state.changed", "time": "t1", "payload": {"reason": "snapshot"}},
                  {"type": "engine.output", "time": "t2"},
                  {"type": "state.changed", "time": "t3", "payload": {"reason": "commit"}},
                  {"type": "stop.result", "time": "t4"}]
        task = task_of_run(self.VIEW, events)
        self.assertEqual((None, "t3"), (task["created_at"], task["updated_at"]))
        self.assertIsNone(task_of_run(self.VIEW, [{"type": "engine.output", "time": "t2"}])["updated_at"])

    def test_sources_it_cannot_read_are_refused(self) -> None:
        for view, reason in (({"run_id": "r", "state": "paused"}, "state"), ({"state": "running"}, "source"),
                             ({"run_id": "", "state": "running"}, "source"), (None, "source"),
                             ({"run_id": "r", "state": "ended", "result": "ok"}, "source")):
            with self.subTest(view=view), self.assertRaises(TaskUnavailable) as caught:
                task_of_run(view)
            self.assertEqual(reason, caught.exception.reason)

    def test_poll_interval_is_the_callers_and_checked(self) -> None:
        self.assertEqual(1000, task_of_run(self.VIEW, poll_interval_ms=1000)["poll_interval_ms"])
        self.assertIsNone(task_of_run(self.VIEW)["poll_interval_ms"])
        for bad in (99, 1000.0, True, "1000"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                task_of_run(self.VIEW, poll_interval_ms=bad)


class TurnTest(unittest.TestCase):
    def session(self, **turn) -> dict:
        base = {"turn_id": "turn-b", "run_id": "run-1", "state": "running", "outcome": None, "result": None,
                "expires_at": None}
        shared = {"turn_id": "turn-a", "run_id": "run-1", "state": "ended", "outcome": "done",
                  "result": {"code": None, "detail": "", "result_ref": "result:turn-a"}, "expires_at": None}
        return {"session_id": "ses-1", "state": "live", "turns": [shared, dict(base, **turn)]}

    def test_each_turn_state(self) -> None:
        for state, outcome, status in (("queued", None, "working"), ("running", None, "working"),
                                       ("ended", "waiting", "completed"), ("failed", "failed", "failed"),
                                       ("interrupted", "interrupted", "cancelled"), ("stopped", "stopped", "cancelled")):
            result = {"code": "engine_error" if state == "failed" else None, "detail": "", "result_ref": None}
            with self.subTest(state=state):
                task = task_of_turn(self.session(state=state, outcome=outcome,
                                                 result=result if state in ("ended", "failed") else None), "turn-b")
                self.assertEqual((status, outcome), (task["status"], task["outcome"]))

    def test_a_turn_never_takes_another_turns_result_on_a_shared_run(self) -> None:
        session = self.session(state="ended", outcome="waiting", result={"code": None, "detail": "", "result_ref": "result:turn-b"},
                               expires_at="2026-12-01T00:00:00.000Z")
        self.assertEqual("result:turn-b", task_of_turn(session, "turn-b")["result"]["result_ref"])
        self.assertEqual("result:turn-a", task_of_turn(session, "turn-a")["result"]["result_ref"])
        self.assertEqual(("2026-12-01T00:00:00.000Z", None),
                         (task_of_turn(session, "turn-b")["expires_at"], task_of_turn(session, "turn-a")["expires_at"]))

    def test_a_session_stop_pending_marks_only_a_live_turn(self) -> None:
        session = dict(self.session(), state="stop_pending")
        self.assertTrue(task_of_turn(session, "turn-b")["cancel_requested"])
        self.assertFalse(task_of_turn(session, "turn-a")["cancel_requested"])
        self.assertEqual("working", task_of_turn(session, "turn-b")["status"])

    def test_approvals_count_only_for_a_live_turn_and_its_log_is_its_own(self) -> None:
        session = self.session()
        self.assertEqual("input_required", task_of_turn(session, "turn-b", (), ["apr-1"])["status"])
        self.assertEqual(("completed", []), tuple(task_of_turn(session, "turn-a", (), ["apr-1"])[k]
                                                   for k in ("status", "pending_approval_ids")))
        events = [{"type": "state.changed", "turn_id": "turn-a", "time": "ta"},
                  {"type": "state.changed", "turn_id": "turn-b", "time": "tb"},
                  {"type": "state.changed", "turn_id": None, "time": "tsession"}]
        self.assertEqual(("ta", "tb"), (task_of_turn(session, "turn-a", events)["updated_at"],
                                        task_of_turn(session, "turn-b", events)["updated_at"]))

    def test_sources_it_cannot_read_are_refused(self) -> None:
        for session, turn_id, reason in ((self.session(), "turn-z", "turn_unknown"), ({"session_id": "s"}, "turn-b", "source"),
                                         (self.session(state="paused"), "turn-b", "state")):
            with self.subTest(reason=reason), self.assertRaises(TaskUnavailable) as caught:
                task_of_turn(session, turn_id)
            self.assertEqual(reason, caught.exception.reason)


class DifferencesTest(unittest.TestCase):
    def test_json_values_not_python_values(self) -> None:
        task = task_of_run({"run_id": "r", "state": "running"})
        self.assertEqual(["cancel_requested"], differences(task, dict(task, cancel_requested=0)))
        self.assertEqual([], differences(task, dict(task, status_message="other", poll_interval_ms=5000)))

    def test_the_rule_fields_are_the_record_less_the_servers_two(self) -> None:
        self.assertEqual(set(FIELDS) - {"status_message", "poll_interval_ms"}, set(RULE_FIELDS))
        self.assertEqual(15, len(FIELDS))
        record = json.loads((ROOT / "schemas" / "control" / "v1" / "mcp.task.v1.json").read_text(encoding="utf-8"))
        self.assertEqual(sorted(record["required"]), sorted(FIELDS))

    def test_the_derivation_does_not_mutate_its_source(self) -> None:
        for name, vector in vectors():
            before = copy.deepcopy(vector)
            derive(vector)
            self.assertEqual(before, vector, name)
        self.assertIs(T.task_of_run, task_of_run)


if __name__ == "__main__":
    unittest.main()
