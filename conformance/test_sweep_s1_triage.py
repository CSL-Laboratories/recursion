"""Red tests for the S1 findings that no open draft answers (triage control-schemas, Nov 16, 2026).

Each test states the behaviour the published contract asks for and is RED on master ff3f8071.
Run from the repository root:  python -m unittest discover -s conformance -p 'test_sweep_s1_triage.py'
Tests that need the wire client's validator (jsonschema) are skipped where it is not installed.
"""
import importlib.util
import json
import sys
import unittest
from pathlib import Path

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



if __name__ == "__main__":
    unittest.main(verbosity=2)
