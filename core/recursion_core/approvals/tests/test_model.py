"""The record's shape and the request's lifecycle, against the published fixtures and vectors (#368).

``approval.v1`` fixtures: every good one fits ``record.py``'s shape and every bad one does not.
``approval.lifecycle`` vectors: one request, step by step (a client's approval.open or approval.answer with
its answer, or what the core does: change the content, carry the effect out, start again from what is
durable), with the record after each step compared EXACTLY. The receipts here are the dispatcher's rule
(same command id and payload replays the first answer; another payload is ``command_id_reused``),
composed with the model the way the method handlers compose them.
"""

from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from recursion_core.approvals import model, record
from recursion_core.approvals.effects import text_of
from recursion_core.approvals.model import ApprovalRefused

V1 = Path(__file__).resolve().parents[4] / "schemas" / "control" / "v1"
needs_v1 = unittest.skipUnless((V1 / "vectors" / "approval.lifecycle").is_dir(),
                               "the published schemas and vectors (#368) are not in this tree")


def _exact(value):
    if type(value) is dict:
        return ("object", tuple(sorted((key, _exact(item)) for key, item in value.items())))
    if type(value) in (list, tuple):
        return ("array", tuple(_exact(item) for item in value))
    if type(value) is float:
        return ("float", repr(value))
    return (type(value).__name__, value)


def _problem_for(ref: str, value):
    if ref == "approval.v1.json":
        return record.shape_problem(value)
    if ref.endswith("#/$defs/summary"):
        return record.summary_problem(value)
    if ref.endswith("#/$defs/binding"):
        return record.binding_problem(value)
    if ref.endswith("#/$defs/engine_tool_effect"):
        return record.effect_problem("engine_tool", value)
    if ref.endswith("#/$defs/plugin_effect"):
        return record.effect_problem("plugin_effect", value)
    raise AssertionError("no check for " + ref)


@needs_v1
class ShapeFixtureTests(unittest.TestCase):
    def test_every_approval_fixture(self) -> None:
        index = json.loads((V1 / "fixtures" / "index.json").read_text(encoding="utf-8"))["fixtures"]
        counts = {"good": 0, "bad": 0}
        for entry in index:
            if "/approval.v1/" not in entry["file"]:
                continue
            kind = entry["file"].split("/", 1)[0]
            value = json.loads((V1 / "fixtures" / entry["file"]).read_text(encoding="utf-8"))
            with self.subTest(fixture=entry["file"]):
                problem = _problem_for(entry["schema"], value)
                if kind == "good":
                    self.assertIsNone(problem)
                else:
                    self.assertIsNotNone(problem, "a bad fixture fits")
                counts[kind] += 1
        self.assertGreater(counts["good"], 8)
        self.assertGreater(counts["bad"], 70)

    def test_timestamps_are_real_instants(self) -> None:
        self.assertEqual(record.milliseconds("1970-01-01T00:00:01.000Z"), 1000)
        for bad in ("2026-02-31T00:00:00.000Z", "2026-11-14T24:00:00.000Z", "2026-11-14T10:00:60.000Z",
                    "2026-11-14T10:00:00Z", "2026-11-14T10:00:00.000z", "2026-11-14T10:00:00.000Z\n",
                    "\u0662026-11-14T10:00:00.000Z", None, 0):
            with self.subTest(bad=bad):
                with self.assertRaises(record.TimestampRefused):
                    record.milliseconds(bad)


class _Queue:
    """The model with the dispatcher's receipt rule, as one request's vectors drive it."""

    def __init__(self, held: dict, binding, receipts=None) -> None:
        self.record = model.restored(held)
        self.binding = copy.deepcopy(binding)
        self.receipts = copy.deepcopy(receipts or {})

    def call(self, principal, method, params, at):
        refusal = model.principal_refusal(principal) or model.params_refusal(method, params)
        if refusal:
            return refusal
        key = (method, params["command_id"])
        payload = text_of({k: v for k, v in params.items() if k != "command_id"})
        if key in self.receipts:
            first_payload, first_answer = self.receipts[key]
            return copy.deepcopy(first_answer) if first_payload == payload else {"code": "conflict", "detail": "command_id_reused"}
        step = model.open_prompt if method == "approval.open" else model.answer_prompt
        self.record, answer = step(self.record, copy.deepcopy(principal), params, at)
        self.receipts[key] = (payload, copy.deepcopy(answer))
        return answer


@needs_v1
class LifecycleVectorTests(unittest.TestCase):
    def test_every_lifecycle_vector(self) -> None:
        seen = 0
        for path in sorted((V1 / "vectors" / "approval.lifecycle").glob("*.json")):
            vector = json.loads(path.read_text(encoding="utf-8"))
            with self.subTest(vector=path.name):
                seen += 1
                self._run(vector)
        self.assertEqual(seen, 17)

    def _run(self, vector: dict) -> None:
        binding = vector.get("binding")
        if binding is not None:
            self.assertIsNone(record.binding_problem(binding))
        queue = _Queue(vector["record"], binding)
        for number, step in enumerate(vector["steps"], 1):
            before = (_exact(queue.record), _exact(queue.binding))
            if "core" in step:
                core = dict(step["core"])
                happening = core.pop("happening")
                try:
                    if happening == "change":
                        queue.record = model.change(queue.record, **core)
                    elif happening == "consume":
                        queue.record = model.consume(queue.record, queue.binding, core["revision"], core["digest"],
                                                     core.get("binding"))
                    elif happening == "restart":
                        queue = _Queue(queue.record, queue.binding, queue.receipts)
                    else:
                        self.fail("unknown happening " + happening)
                    result = "ok"
                except ApprovalRefused as refused:
                    result = refused.code
                self.assertEqual(result, step["result"], "step %d" % number)
                if result != "ok":
                    self.assertEqual((_exact(queue.record), _exact(queue.binding)), before,
                                     "step %d: a refused step changed something" % number)
            else:
                request = step["request"]
                answer = queue.call(step["principal"], request["method"], request["params"], step["at"])
                self.assertEqual(answer, step["answer"], "step %d" % number)
            self.assertEqual(_exact(queue.record), _exact(step["record"]), "step %d: the record" % number)
            self.assertEqual(_exact(queue.binding), _exact(binding), "step %d: the binding changed" % number)


@needs_v1
class NewRequestTests(unittest.TestCase):
    def _effect(self) -> dict:
        vector = json.loads((V1 / "vectors" / "approval.binding" / "g01-a-tool-call.json").read_text(encoding="utf-8"))
        return vector["record"]["effect"], vector["record"]

    def test_a_new_request_is_a_pending_record_that_agrees_with_itself(self) -> None:
        effect, published = self._effect()
        made = model.new_request(approval_id="apr-new", source="engine_tool", effect=effect,
                                 session_id=effect["session_id"], run_id=effect["run_id"], turn_id="turn-0001",
                                 requested_at="2026-11-14T10:00:00.000Z", expires_at="2026-11-14T10:15:00.000Z")
        self.assertIsNone(record.shape_problem(made))
        self.assertEqual((made["state"], made["revision"], made["opened_by"]), ("pending", 1, []))
        self.assertEqual((made["digest"], made["rendered"]), (published["digest"], published["rendered"]))

    def test_a_request_that_cannot_be_made_whole_is_refused(self) -> None:
        effect, _ = self._effect()
        base = dict(approval_id="apr-new", source="engine_tool", effect=effect, session_id=effect["session_id"],
                    run_id=effect["run_id"], turn_id=None, requested_at="2026-11-14T10:00:00.000Z",
                    expires_at="2026-11-14T10:15:00.000Z")
        cases = (("envelope", dict(run_id="run-other")), ("envelope", dict(session_id=None)),
                 ("timestamp", dict(expires_at="2026-11-14T10:00:00.000Z")),
                 ("timestamp", dict(expires_at="2026-02-31T00:00:00.000Z")),
                 ("record_invalid", dict(approval_id="not-an-id")),
                 ("too_many_lines", dict(effect=dict(effect, input={"k%03d" % i: i for i in range(200)}))))
        for code, change in cases:
            with self.subTest(code=code, change=sorted(change)):
                with self.assertRaises(ApprovalRefused) as refused:
                    model.new_request(**dict(base, **change))
                self.assertEqual(refused.exception.code, code)


if __name__ == "__main__":
    unittest.main()
