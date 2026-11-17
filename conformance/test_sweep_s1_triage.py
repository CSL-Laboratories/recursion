"""Red tests for the S1 findings that no open draft answers (triage control-schemas, Nov 16, 2026).

Each test states the behaviour the published contract asks for and is RED on master ff3f8071.
Run from the repository root:  python -m unittest discover -s conformance -p 'test_sweep_s1_triage.py'
Tests that need the wire client's validator (jsonschema) are skipped where it is not installed.
"""
import importlib.util
import json
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import rules  # noqa: E402

HAVE_CLIENT = importlib.util.find_spec("jsonschema") is not None
if HAVE_CLIENT:
    import wire_client as client  # noqa: E402
    CONTROL = client.SchemaSet(ROOT / "schemas/control/v1")
    PLUGIN = client.SchemaSet(ROOT / "schemas/plugin/v1")

FIXTURES = ROOT / "schemas/control/v1/fixtures/good"
OWN = HERE / "sweep_s1_triage"


def valid(ref, doc):
    if HAVE_CLIENT:
        return CONTROL.validator(ref).is_valid(doc)
    return rules.fits(ref, doc)


def fixture(pattern):
    return json.loads(next(FIXTURES.glob(pattern)).read_text(encoding="utf-8"))


def own(name):
    return json.loads((OWN / name).read_text(encoding="utf-8"))


class LegacyStartKey(unittest.TestCase):
    # s1-common astra F5, s1-methods-tables astra F5 / sol F6, s1-rules-3 astra F4, s1-runs-sessions sol F4
    def test_keyed_start_without_engine_yields_a_storable_key(self):
        request = {"tenant": "t1", "prompt": "p", "client_key": "k-1"}
        self.assertTrue(valid("methods.v1.json#/$defs/params.run.start", request))
        try:
            key = rules.start_key_of(request, None)
        except rules.Refused:
            return  # a typed prerequisite refusal is an acceptable repair
        self.assertTrue(valid("run.record.v1.json#/$defs/start_key", key), key)


class RunLogRefusal(unittest.TestCase):
    # s1-methods-tables astra F1, sol F2
    def test_unknown_run_log_refusal_fits_the_refusal_shape(self):
        cursor = {"epoch": 1, "session_id": "run:r-1", "seq": 0}
        self.assertTrue(valid("common.v1.json#/$defs/cursor", cursor))
        answer = rules.cursor_answer(cursor, None, 1)
        self.assertEqual(answer["code"], "session_unknown")
        self.assertTrue(valid("methods.v1.json#/$defs/refusal", answer), answer)


class TranscriptTime(unittest.TestCase):
    # s1-events-transcript astra F2, sol F3
    def test_lines_of_a_later_page_cannot_run_backwards(self):
        v = own("backward-time-on-later-page.json")
        lines = v["page"]["lines"]
        self.assertGreater(lines[0]["time_rel_ms"], lines[1]["time_rel_ms"])
        self.assertTrue(rules.transcript_page_problems(v["params"], v["raw"], v["page"]))


class TrustedText(unittest.TestCase):
    # s1-rules-2 astra F1
    def test_trusted_non_ascii_text_is_compared(self):
        event = {"seq": 1, "session_id": "ses-0001", "run_id": "run-test", "turn_id": "turn-0001",
                 "time": "2026-11-13T07:35:03.120Z", "type": "turn.started", "source": "core", "trust": "trusted",
                 "payload": {"delivery_id": "dlv-0001", "index": 0, "text": "café"}}
        line = {"seq": 1, "time_rel_ms": 0, "event_type": "turn.started", "source": "core", "trust": "trusted",
                "state": None, "text": "Different input entirely", "speech": None, "focus": None}
        self.assertEqual(rules.transcript_problems([event], [line]), ["1:text"])


class DeliveringItem(unittest.TestCase):
    # s1-rules-2 astra F3, s1-runs-sessions astra F2 / sol F2
    def test_restored_delivering_item_never_raises_a_host_error(self):
        item, binding = fixture("queue.item.v1/h03-*"), fixture("queue.item.v1/h07-*")
        self.assertTrue(valid("queue.item.v1.json", item))
        accept = {"delivery_id": binding["delivery_id"], "session_id": binding["session_id"],
                  "run_id": binding["run_id"], "turn_id": binding["target_turn_id"], "how": binding["how"]}
        for happening, facts in (("written", {"delivery_id": binding["delivery_id"]}), ("accepted", accept),
                                 ("turn_ended", {"turn_id": binding["target_turn_id"]})):
            with self.subTest(happening=happening):
                model = rules.DeliveryModel(item)
                try:
                    model.step(happening, facts)
                except rules.Refused:
                    pass



class FirstUse(unittest.TestCase):
    # s1-rules-2 astra F4
    def test_concurrent_first_readers_see_the_whole_schema_set(self):
        approval = fixture("approval.v1/h01-*")
        rules._READ.clear()
        rules._READ_LOCATIONS.clear()
        paused, release = threading.Event(), threading.Event()
        original = Path.read_text
        first, second = [], []

        def held(path, *args, **kwargs):
            if path.parent == rules.PUBLISHED and path.name == "common.v1.json":
                paused.set()
                release.wait(5)
            return original(path, *args, **kwargs)

        def run(into):
            try:
                into.append(rules.fits("approval.v1.json", approval))
            except Exception as err:  # noqa: BLE001 - recorded, asserted below
                into.append(type(err).__name__)

        with patch.object(Path, "read_text", held):
            a = threading.Thread(target=run, args=(first,), daemon=True)
            a.start()
            try:
                self.assertTrue(paused.wait(5))
                b = threading.Thread(target=run, args=(second,), daemon=True)
                b.start()
                b.join(0.2)
            finally:
                release.set()
                a.join(5)
                b.join(5)
        self.assertEqual((first, second), ([True], [True]))


class TaskIds(unittest.TestCase):
    # s1-mcp-task sol F1
    def test_a_run_and_a_turn_never_share_a_task_id(self):
        pair = own("task-id-collision.json")
        r, t = pair["run"], pair["turn"]
        a = rules.task_of_run(r["view"], r["events"], r["pending_approval_ids"])
        b = rules.task_of_turn(t["session"], t["turn_id"], t["events"], t["pending_approval_ids"])
        self.assertNotEqual(a["task_id"], b["task_id"])


class StopClaim(unittest.TestCase):
    # s1-runs-sessions astra F5 / sol F3
    def test_stop_claim_cannot_describe_another_proof(self):
        for pattern, claim in (("stop.result.v1/h03-*", "cgroup_removed_whole_run_unverified"),
                               ("stop.result.v1/h01-*", "cgroup_empty_whole_run_unverified")):
            with self.subTest(claim=claim):
                answer = fixture(pattern)
                self.assertTrue(valid("stop.result.v1.json", answer))
                answer["claim"] = claim
                self.assertFalse(valid("stop.result.v1.json", answer))


@unittest.skipUnless(HAVE_CLIENT, "needs the wire client's validator (jsonschema)")
class WireClient(unittest.TestCase):
    # s1-rules-3 astra F7, s1-approvals astra F2
    def test_approval_plugin_id_refuses_what_the_plugin_api_refuses(self):
        plugin_ok = PLUGIN.validator("api.v1.json#/$defs/plugin_id").is_valid("host")
        approval_ok = CONTROL.validator("approval.v1.json#/$defs/plugin_effect/properties/plugin_id").is_valid("host")
        methods_ok = CONTROL.validator("methods.v1.json#/$defs/plugin_id").is_valid("host")
        self.assertFalse(plugin_ok)
        self.assertEqual(approval_ok, plugin_ok)
        self.assertEqual(methods_ok, plugin_ok)

    # s1-wire-client astra F1 / sol F1
    def test_opaque_plugin_data_is_not_judged_as_an_approval_or_an_event(self):
        # A valid plugin document whose plugin-owned data merely has the keys of a control record. The CLI
        # (wire_client.py:700-705) and the suite (tests.py:495-516, 549-561) walk every good fixture with
        # approvals_in / events_in and judge what they find; valid plugin data must never be reported.
        registry = own("approval-shaped-data-list.json")
        tool = own("event-shaped-data.json")
        self.assertEqual(client.leaf_errors(PLUGIN.validator("api.v1.json#/$defs/registry"), registry), [])
        self.assertEqual(client.leaf_errors(PLUGIN.validator("api.v1.json#/$defs/tool"), tool), [])
        judged = []
        for record in client.approvals_in(registry):
            try:
                judged += rules.approval_problems(record)
            except Exception as err:  # noqa: BLE001 - an untyped crash is the defect too
                judged.append(type(err).__name__)
        self.assertEqual(judged, [])
        self.assertEqual(list(client.events_in(tool)), [])

    # s1-wire-client astra F3
    def test_lifecycle_answers_are_compared_exactly(self):
        folder = ROOT / "schemas/control/v1"
        entries = [e for e in client.load_vectors(folder) if e["rule"] == "approval.lifecycle"]
        mutated = None
        for entry in entries:
            vector = client.read_json(folder / "vectors" / entry["file"])
            if client.judge_vector(CONTROL, "approval.lifecycle", vector) is not None:
                continue  # only a vector that agrees with its rule before the change is a witness
            for step in vector["steps"]:
                rec = step.get("answer", {}).get("approval") if isinstance(step.get("answer"), dict) else None
                if isinstance(rec, dict) and rec.get("revision") == 1:
                    rec["revision"] = True
                    mutated = vector
                    break
            if mutated:
                break
        self.assertIsNotNone(mutated, "no lifecycle vector answers with revision 1")
        self.assertIsNotNone(client.judge_vector(CONTROL, "approval.lifecycle", mutated))


if __name__ == "__main__":
    unittest.main(verbosity=2)
